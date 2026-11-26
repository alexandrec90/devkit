#!/usr/bin/env python3
"""How much memory the machine has free for another session.

The fix pass has no cap on how many sessions it sends, on purpose -- but each is a real
process, and the fourth supervised round sent enough (nine fixers plus their MCP
servers, ~700 MB apiece) that Claude Code killed the supervisor for low memory. Memory
is the one limit the machine sets whatever the pass decides, so the pass asks it.

Standard library only: `ctypes` on Windows, `/proc/meminfo` on Linux. `None` anywhere
else, or when the probe fails -- which the caller reads as "no guard", since holding
every session on a probe that broke would stop the pass for a reason nothing files.

Tested in `tests/test_host_memory.py`.
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

MEMINFO = Path("/proc/meminfo")

# A background fixer measured ~450 MB (the session and its pty host) once it loads no
# MCP server.
SESSION_MB = 500
# What anything a job starts must leave free: the person at the machine's room for their
# own work.
FLOOR_MB = 2048
# What each fixer must leave free, however many already run. They are in the probe
# already, and each one this pass sends is charged against it, which is what stops nine
# at once; a floor raised whenever one is running counted them twice. Held to `FLOOR_MB`
# that way, 35 of 43 passes on 2026-11-23/24 held a session, 33 of them with over 1 GB
# free, while d2-trader #32 sat conflicted on 2026-11-25. Higher than the 512 that let
# one alone in: the probe now includes commit, whose exhaustion fails an allocation in
# the person's own program rather than paging it.
FIX_FLOOR_MB = 1024


class _MemoryStatus(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def windows_room(avail_phys: int, avail_commit: int) -> int:
    """The MB a new process can take on Windows: the smaller of the two limits, in bytes.

    Available physical memory (standby included) is what it can get without paging;
    available commit (`ullAvailPageFile`, the commit limit less the charge) is what it
    can get at all, past which an allocation fails. 2026-11-25: 1.0 GB physical and
    1.4 GB commit free, with a game running; either limit alone can be the lower.
    """
    return int(min(avail_phys, avail_commit) // (1024 * 1024))


def _windows_mb() -> int | None:
    if sys.platform != "win32":  # also what tells mypy `windll` exists below
        return None
    status = _MemoryStatus()
    status.dwLength = ctypes.sizeof(_MemoryStatus)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return windows_room(status.ullAvailPhys, status.ullAvailPageFile)


def meminfo_mb(text: str) -> int | None:
    """`MemAvailable` from a `/proc/meminfo` body, in MB."""
    for line in text.splitlines():
        name, _, value = line.partition(":")
        if name == "MemAvailable" and value.split():
            return int(value.split()[0]) // 1024
    return None


def available_mb() -> int | None:
    """Memory available to a new process, in MB (`windows_room` on Windows,
    `MemAvailable` elsewhere); `None` when it cannot be read."""
    try:
        if sys.platform == "win32":
            return _windows_mb()
        return meminfo_mb(MEMINFO.read_text(encoding="utf-8"))
    except (OSError, ValueError, AttributeError):
        return None


def room_for_session(free_mb: int | None, floor: int = FLOOR_MB) -> bool:
    """`free_mb` holds one more session above `floor`; True for a probe that failed,
    for the reason the module docstring gives."""
    return free_mb is None or free_mb - SESSION_MB >= floor


if __name__ == "__main__":
    print(available_mb())
