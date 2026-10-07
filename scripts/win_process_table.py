#!/usr/bin/env python3
"""The Windows process table, read in this process: no WMI and no PowerShell child.

`reap_machine.read_table` asked `Get-CimInstance Win32_Process` through PowerShell. On an
idle desk that answers in half a second; on 2026-10-07, with fixer sessions, a 29-minute
scrape and Docker sharing 16 GB, it did not answer within 60 s four passes in a row
(ba883e41), and with the bound raised to 240 s (#567) not within 240 s either
(6114e177). A wait that a stall outlasts at any size is not a ceiling to raise a third
time. The time goes to starting a PowerShell under memory pressure and to the WMI
provider host every other CIM client on the machine queues on, and listing processes
needs neither: this reads the same table in tens of milliseconds.

`NtQuerySystemInformation(SystemProcessInformation)` is where WMI gets the table from,
and it answers for every process without opening any: pid, parent, image, private bytes,
working set and start time, for the services and `vmmem` an unelevated caller may not
open as much as for its own. Only the command line needs a handle, and it asks for the
least access Windows has, `PROCESS_QUERY_LIMITED_INFORMATION`. A process that refuses
even that keeps its row with an empty command line, as the WMI rows had
`CommandLine: null`: it is still a parent, and an ancestry that dropped it would end
early and read as orphaned.

Every Win32 call is declared in `SIGNATURES` before it is made. A `ctypes` function left
undeclared returns a C `int`, which truncates a 64-bit handle into one that names
nothing; the call then fails as if access were denied, and nothing says why.
`tests/test_win_process_table.py` reads the calls off this source and walks a buffer laid
out as the kernel writes one, so both checks run on the POSIX CI that has no `ntdll`.
"""

from __future__ import annotations

import ctypes
import sys
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass
from functools import cache
from typing import Any

SYSTEM_PROCESS_INFORMATION_CLASS = 5
PROCESS_COMMAND_LINE_INFORMATION = 60  # Windows 8.1 and later
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STATUS_INFO_LENGTH_MISMATCH = -0x3FFFFFFC  # 0xC0000004, as the signed NTSTATUS ctypes returns

# The table is a few hundred KB; it is re-asked with a larger buffer while processes start
# faster than it can be copied, and a machine where that never settles is a failed read.
FIRST_BUFFER = 1 << 20
BUFFER_ATTEMPTS = 6

# FILETIME counts 100 ns ticks from 1601-01-01; this many of them reach 1970-01-01.
EPOCH_AS_FILETIME = 116_444_736_000_000_000

# The kernel names no image for pid 0; WMI called it this, and so do the rows here.
IDLE_IMAGE = "System Idle Process"


# Fixed-width members only: `wintypes.ULONG` is `c_ulong`, eight bytes on 64-bit POSIX, and
# these layouts must be the kernel's on the CI that tests them as well as on Windows.
class UNICODE_STRING(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_uint16),
        ("MaximumLength", ctypes.c_uint16),
        ("Buffer", ctypes.c_void_p),
    ]


class SYSTEM_PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("NextEntryOffset", ctypes.c_uint32),
        ("NumberOfThreads", ctypes.c_uint32),
        ("WorkingSetPrivateSize", ctypes.c_int64),
        ("HardFaultCount", ctypes.c_uint32),
        ("NumberOfThreadsHighWatermark", ctypes.c_uint32),
        ("CycleTime", ctypes.c_uint64),
        ("CreateTime", ctypes.c_int64),
        ("UserTime", ctypes.c_int64),
        ("KernelTime", ctypes.c_int64),
        ("ImageName", UNICODE_STRING),
        ("BasePriority", ctypes.c_int32),
        ("UniqueProcessId", ctypes.c_void_p),
        ("InheritedFromUniqueProcessId", ctypes.c_void_p),
        ("HandleCount", ctypes.c_uint32),
        ("SessionId", ctypes.c_uint32),
        ("UniqueProcessKey", ctypes.c_size_t),
        ("PeakVirtualSize", ctypes.c_size_t),
        ("VirtualSize", ctypes.c_size_t),
        ("PageFaultCount", ctypes.c_uint32),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivatePageCount", ctypes.c_size_t),
    ]


# `<dll>.<function>` -> (restype, argtypes), for every call this module makes.
SIGNATURES: dict[str, tuple[Any, list[Any]]] = {
    "ntdll.NtQuerySystemInformation": (
        wintypes.LONG,
        [ctypes.c_int, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)],
    ),
    "ntdll.NtQueryInformationProcess": (
        wintypes.LONG,
        [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.ULONG,
            ctypes.POINTER(wintypes.ULONG),
        ],
    ),
    "kernel32.OpenProcess": (wintypes.HANDLE, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]),
    "kernel32.CloseHandle": (wintypes.BOOL, [wintypes.HANDLE]),
}


@dataclass(frozen=True)
class Row:
    """One process, with the fields `reap_machine.Process` keeps."""

    pid: int
    ppid: int
    image: str
    cmdline: str = ""
    private: int = 0  # bytes this process alone has committed
    rss: int = 0  # working set, bytes
    created: float = 0.0  # epoch seconds; 0.0 when unknown


Snapshot = Callable[[], list[Row]]
CommandLine = Callable[[int], str]


def filetime_epoch(ticks: int) -> float:
    """A FILETIME in epoch seconds; `0.0` for one at or before 1970, as a system process's is."""
    return 0.0 if ticks <= EPOCH_AS_FILETIME else (ticks - EPOCH_AS_FILETIME) / 10_000_000


def unicode_text(string: UNICODE_STRING) -> str:
    """The text a `UNICODE_STRING` points at; "" for one with no buffer.

    Its `Length` is in bytes of UTF-16. `ctypes.wstring_at` reads `wchar_t`, which is
    UTF-32 off Windows, so the bytes are decoded here and the walk reads the same on CI.
    """
    if not string.Buffer:
        return ""
    return ctypes.string_at(string.Buffer, string.Length).decode("utf-16-le", "replace")


def parse_processes(buffer: Any) -> list[Row]:
    """Every entry of a `SystemProcessInformation` answer, command lines left empty.

    The entries are chained by `NextEntryOffset`, each followed by its threads, and the
    last one's offset is 0. An image name's `Buffer` is an address inside the same
    buffer, so the name is read where it points.
    """
    rows = []
    offset = 0
    while offset + ctypes.sizeof(SYSTEM_PROCESS_INFORMATION) <= len(buffer):
        entry = SYSTEM_PROCESS_INFORMATION.from_buffer(buffer, offset)
        image = unicode_text(entry.ImageName)
        rows.append(
            Row(
                entry.UniqueProcessId or 0,
                entry.InheritedFromUniqueProcessId or 0,
                image or IDLE_IMAGE,
                private=entry.PrivatePageCount,
                rss=entry.WorkingSetSize,
                created=filetime_epoch(entry.CreateTime),
            )
        )
        if not entry.NextEntryOffset:
            break
        offset += entry.NextEntryOffset
    return rows


def read(
    snapshot: Snapshot | None = None, command_line: CommandLine | None = None
) -> tuple[list[Row] | None, str]:
    """`(every process, "")`, or `(None, why the table could not be taken)`.

    A command line that cannot be read costs only that row's command line; only the
    snapshot itself failing is a table that was not read.
    """
    if snapshot is None and sys.platform != "win32":
        return None, "the native table is only implemented on Windows"
    try:
        rows = (snapshot or system_processes)()
    except OSError as error:
        return None, f"the snapshot failed: {error}"
    ask = command_line or process_command_line
    return [
        Row(row.pid, row.ppid, row.image, ask(row.pid), row.private, row.rss, row.created)
        for row in rows
    ], ""


@cache
def _dlls() -> tuple[Any, Any]:
    """`kernel32` and `ntdll`, with every signature in `SIGNATURES` declared."""
    if sys.platform != "win32":  # also what tells mypy `WinDLL` exists below
        raise OSError("no Win32 API on this platform")
    dlls = {name: ctypes.WinDLL(name, use_last_error=True) for name in ("kernel32", "ntdll")}
    for qualified, (restype, argtypes) in SIGNATURES.items():
        dll, _, name = qualified.partition(".")
        function = getattr(dlls[dll], name)
        function.restype, function.argtypes = restype, argtypes
    return dlls["kernel32"], dlls["ntdll"]


def system_processes() -> list[Row]:
    """The kernel's process list, re-asked with a larger buffer while it outgrows one."""
    _, ntdll = _dlls()
    size = FIRST_BUFFER
    for _ in range(BUFFER_ATTEMPTS):
        buffer = ctypes.create_string_buffer(size)
        needed = wintypes.ULONG(0)
        status = ntdll.NtQuerySystemInformation(
            SYSTEM_PROCESS_INFORMATION_CLASS, buffer, size, ctypes.byref(needed)
        )
        if status == STATUS_INFO_LENGTH_MISMATCH:
            size = max(size * 2, needed.value + FIRST_BUFFER // 4)
            continue
        if status < 0:  # NT_SUCCESS is a non-negative NTSTATUS
            raise OSError(f"NtQuerySystemInformation returned {status & 0xFFFFFFFF:#010x}")
        return parse_processes(buffer)
    raise OSError(f"the process list outgrew a {size // 1024} KB buffer {BUFFER_ATTEMPTS} times")


def process_command_line(pid: int) -> str:
    """`pid`'s command line: one call for its size, one to read it; "" when refused."""
    try:
        kernel32, ntdll = _dlls()
    except OSError:
        return ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.ULONG(0)
        ntdll.NtQueryInformationProcess(
            handle, PROCESS_COMMAND_LINE_INFORMATION, None, 0, ctypes.byref(size)
        )
        if not size.value:
            return ""
        buffer = ctypes.create_string_buffer(size.value)
        status = ntdll.NtQueryInformationProcess(
            handle, PROCESS_COMMAND_LINE_INFORMATION, buffer, size.value, ctypes.byref(size)
        )
        if status < 0:
            return ""
        return unicode_text(UNICODE_STRING.from_buffer(buffer))
    finally:
        kernel32.CloseHandle(handle)


if __name__ == "__main__":
    table, why = read()
    print(why or f"{len(table or [])} processes")
