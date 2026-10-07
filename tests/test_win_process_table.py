"""`win_process_table.py`: the kernel's process list, walked and declared correctly.

The calls themselves need Windows, so what can go wrong without a sound is checked here
on any platform: the layout the walk reads (a wrong offset reads a neighbour's field as
a pid, and nothing fails), the chain of entries, and that every Win32 call carries a
declared signature (an undeclared one truncates a 64-bit handle and fails as if refused).
`tests/test_reap_machine.py` reads the real table on a Windows desk.
"""

from __future__ import annotations

import ast
import ctypes

import pytest
from support import REPO_ROOT, load_script

win_process_table = load_script("scripts/win_process_table.py")

SPI = win_process_table.SYSTEM_PROCESS_INFORMATION
UNICODE = win_process_table.UNICODE_STRING
Row = win_process_table.Row

SIXTY_FOUR_BIT = ctypes.sizeof(ctypes.c_void_p) == 8


def win32_calls() -> set[str]:
    """Every `kernel32`/`ntdll` function the module calls, read off its source."""
    source = REPO_ROOT / "scripts" / "win_process_table.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    return {
        f"{node.func.value.id}.{node.func.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in {"kernel32", "ntdll"}
    }


def test_every_win32_call_is_declared():
    undeclared = sorted(win32_calls() - set(win_process_table.SIGNATURES))
    assert not undeclared, f"{undeclared} are called with no entry in SIGNATURES"


def test_no_signature_is_declared_for_a_call_that_is_gone():
    assert set(win_process_table.SIGNATURES) <= win32_calls()


def test_the_entry_layout_is_the_kernels():
    """The offsets the x64 kernel writes, from its published `SYSTEM_PROCESS_INFORMATION`.

    Every interpreter devkit runs under is 64-bit, CI's included, and the fixed-width
    members are what make the layout the same off Windows."""
    assert SIXTY_FOUR_BIT
    assert (ctypes.sizeof(win_process_table.UNICODE_STRING), UNICODE.Buffer.offset) == (16, 8)
    offsets = {
        "CreateTime": 0x20,
        "ImageName": 0x38,
        "UniqueProcessId": 0x50,
        "InheritedFromUniqueProcessId": 0x58,
        "WorkingSetSize": 0x90,
        "PrivatePageCount": 0xC8,
    }
    assert {name: getattr(SPI, name).offset for name in offsets} == offsets


def write_entries(entries: list[tuple[int, int, str, int]]) -> ctypes.Array:
    """A buffer laid out as the kernel writes one: each entry, room for its threads, its
    image name after them, and `NextEntryOffset` chaining them with 0 on the last."""
    stride = ctypes.sizeof(SPI) + 0x80 + 64 * 2
    buffer = ctypes.create_string_buffer(stride * len(entries))
    base = ctypes.addressof(buffer)
    for index, (pid, ppid, image, private) in enumerate(entries):
        at = index * stride
        entry = SPI.from_buffer(buffer, at)
        entry.NextEntryOffset = stride if index < len(entries) - 1 else 0
        entry.UniqueProcessId = pid
        entry.InheritedFromUniqueProcessId = ppid
        entry.PrivatePageCount = private
        entry.WorkingSetSize = private * 2
        entry.CreateTime = win_process_table.EPOCH_AS_FILETIME + 30_000_000 * (index + 1)
        if image:
            name_at = at + ctypes.sizeof(SPI) + 0x80
            encoded = image.encode("utf-16-le")
            ctypes.memmove(base + name_at, encoded, len(encoded))
            entry.ImageName.Length = len(encoded)
            entry.ImageName.MaximumLength = len(encoded) + 2
            entry.ImageName.Buffer = base + name_at
    return buffer


def test_the_walk_follows_the_chain_and_reads_names_where_they_point():
    buffer = write_entries([(0, 0, "", 0), (4, 0, "System", 1), (7120, 4, "claude.exe", 9)])
    assert win_process_table.parse_processes(buffer) == [
        Row(0, 0, "System Idle Process", created=3.0),
        Row(4, 0, "System", private=1, rss=2, created=6.0),
        Row(7120, 4, "claude.exe", private=9, rss=18, created=9.0),
    ]


def test_a_buffer_too_short_for_one_entry_is_no_rows():
    assert win_process_table.parse_processes(ctypes.create_string_buffer(16)) == []


def test_a_filetime_reads_as_epoch_seconds_and_a_boot_time_zero_as_unknown():
    assert win_process_table.filetime_epoch(win_process_table.EPOCH_AS_FILETIME + 15) == 1.5e-6
    assert win_process_table.filetime_epoch(0) == 0.0


def test_command_lines_are_asked_per_row_and_the_rest_kept():
    rows = [Row(4, 0, "System", private=1), Row(7, 4, "node.exe", rss=2, created=3.0)]
    lines = {7: "node vite.js"}
    assert win_process_table.read(lambda: rows, lambda pid: lines.get(pid, "")) == (
        [
            Row(4, 0, "System", "", private=1),
            Row(7, 4, "node.exe", "node vite.js", rss=2, created=3.0),
        ],
        "",
    )


def test_a_failed_snapshot_is_no_table_and_says_why():
    def refused():
        raise OSError("NtQuerySystemInformation returned 0xc0000022")

    assert win_process_table.read(refused, lambda pid: "") == (
        None,
        "the snapshot failed: NtQuerySystemInformation returned 0xc0000022",
    )


class FakeNtdll:
    """`NtQuerySystemInformation` and `NtQueryInformationProcess` as the kernel answers:
    `STATUS_INFO_LENGTH_MISMATCH` with the size needed until the buffer is big enough."""

    def __init__(self, table: ctypes.Array, status: int = 0, command_line: str = "") -> None:
        self.table, self.status, self.command_line = table, status, command_line
        self.sizes: list[int] = []

    def NtQuerySystemInformation(self, cls, buffer, size, needed):
        assert cls == win_process_table.SYSTEM_PROCESS_INFORMATION_CLASS
        self.sizes.append(size)
        needed._obj.value = len(self.table)
        if size < len(self.table):
            return win_process_table.STATUS_INFO_LENGTH_MISMATCH
        ctypes.memmove(buffer, self.table, len(self.table))
        return self.status

    def NtQueryInformationProcess(self, handle, cls, buffer, size, needed):
        assert cls == win_process_table.PROCESS_COMMAND_LINE_INFORMATION
        text = self.command_line.encode("utf-16-le")
        needed._obj.value = ctypes.sizeof(UNICODE) + len(text)
        if buffer is None:
            return win_process_table.STATUS_INFO_LENGTH_MISMATCH
        name = UNICODE.from_buffer(buffer)
        at = ctypes.addressof(buffer) + ctypes.sizeof(UNICODE)
        ctypes.memmove(at, text, len(text))
        name.Length, name.Buffer = len(text), at
        return self.status


class FakeKernel32:
    def __init__(self, handle: int) -> None:
        self.handle = handle
        self.closed: list[int] = []

    def OpenProcess(self, access, inherit, pid):
        assert access == win_process_table.PROCESS_QUERY_LIMITED_INFORMATION
        return self.handle

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return 1


def with_dlls(monkeypatch, kernel32, ntdll) -> None:
    monkeypatch.setattr(win_process_table, "_dlls", lambda: (kernel32, ntdll))


def test_the_process_list_is_re_asked_with_the_size_it_needs(monkeypatch):
    table = write_entries([(4, 0, "System", 1)])
    monkeypatch.setattr(win_process_table, "FIRST_BUFFER", 64)
    ntdll = FakeNtdll(table)
    with_dlls(monkeypatch, FakeKernel32(0), ntdll)
    assert win_process_table.system_processes() == [
        Row(4, 0, "System", private=1, rss=2, created=3.0)
    ]
    assert ntdll.sizes[0] == 64 < len(table) <= ntdll.sizes[1]


def test_a_refused_process_list_says_what_the_kernel_returned(monkeypatch):
    with_dlls(monkeypatch, FakeKernel32(0), FakeNtdll(write_entries([(4, 0, "", 0)]), -0x3FFFFFDE))
    with pytest.raises(OSError, match="returned 0xc0000022"):
        win_process_table.system_processes()


def test_a_list_that_never_fits_is_a_failed_read_not_a_loop(monkeypatch):
    class Growing(FakeNtdll):
        def NtQuerySystemInformation(self, cls, buffer, size, needed):
            self.sizes.append(size)
            needed._obj.value = size * 4
            return win_process_table.STATUS_INFO_LENGTH_MISMATCH

    ntdll = Growing(write_entries([(4, 0, "", 0)]))
    with_dlls(monkeypatch, FakeKernel32(0), ntdll)
    with pytest.raises(OSError, match="outgrew"):
        win_process_table.system_processes()
    assert len(ntdll.sizes) == win_process_table.BUFFER_ATTEMPTS


def test_a_command_line_is_read_and_its_handle_closed(monkeypatch):
    kernel32 = FakeKernel32(77)
    with_dlls(monkeypatch, kernel32, FakeNtdll(write_entries([]), command_line="node vite.js"))
    assert win_process_table.process_command_line(7) == "node vite.js"
    assert kernel32.closed == [77]


def test_a_process_that_refuses_a_handle_has_no_command_line(monkeypatch):
    kernel32 = FakeKernel32(0)
    with_dlls(monkeypatch, kernel32, FakeNtdll(write_entries([]), command_line="x"))
    assert win_process_table.process_command_line(4) == ""
    assert kernel32.closed == []
    kernel32 = FakeKernel32(77)
    with_dlls(monkeypatch, kernel32, FakeNtdll(write_entries([]), -0x3FFFFFDE, "x"))
    assert win_process_table.process_command_line(4) == ""
    assert kernel32.closed == [77]


def test_off_windows_the_default_read_declines_rather_than_raising(monkeypatch):
    monkeypatch.setattr(win_process_table.sys, "platform", "linux")
    assert win_process_table.read() == (None, "the native table is only implemented on Windows")
