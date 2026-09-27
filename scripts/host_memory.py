#!/usr/bin/env python3
"""How much physical memory the machine has free for another session.

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


def _windows_mb() -> int | None:
    if sys.platform != "win32":  # also what tells mypy `windll` exists below
        return None
    status = _MemoryStatus()
    status.dwLength = ctypes.sizeof(_MemoryStatus)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return int(status.ullAvailPhys // (1024 * 1024))


def meminfo_mb(text: str) -> int | None:
    """`MemAvailable` from a `/proc/meminfo` body, in MB."""
    for line in text.splitlines():
        name, _, value = line.partition(":")
        if name == "MemAvailable" and value.split():
            return int(value.split()[0]) // 1024
    return None


def available_mb() -> int | None:
    """Physical memory available to a new process, in MB; `None` when it cannot be read."""
    try:
        if sys.platform == "win32":
            return _windows_mb()
        return meminfo_mb(MEMINFO.read_text(encoding="utf-8"))
    except (OSError, ValueError, AttributeError):
        return None


if __name__ == "__main__":
    print(available_mb())
