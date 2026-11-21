#!/usr/bin/env python3
"""When this machine last came up -- booted or woke -- for a job that runs on its
scheduler's catch-up fire.

Every devkit job registers with `StartWhenAvailable` (`devkit_schtasks`), so the fires a
machine slept through or was off for all land in the first minutes after it comes back,
together, in no order, alongside Docker Desktop still starting. A job that reads that
moment as steady state files a defect that is only a race: on 2026-10-07 the collectors'
logon fire found Docker not yet answering (aea4ccfa), and the daily workspace pass read a
reconcile log fourteen hours old sixteen seconds before reconcile's own catch-up fire
wrote it (87bae129). `awake_since` is the one fact both needed.

Windows answers it exactly: the tick count runs from boot through sleep and hibernation
(Fast Startup's "shutdown" included), and `CallNtPowerInformation(LastWakeTime)` places
the last resume on the same clock. Linux's `/proc/uptime` gives the boot alone. Anywhere
else, None, and a caller judges as it did before it knew.
"""

from __future__ import annotations

import sys
import time as _time
from pathlib import Path

# `POWER_INFORMATION_LEVEL.LastWakeTime`: the interrupt time, in 100 ns units since
# boot, of the last resume -- 0 when the machine has not slept since it booted.
LAST_WAKE_TIME = 14
_TICKS_PER_SECOND = 10_000_000


def uptime() -> float | None:
    """Seconds since boot, sleep included, or None where the platform cannot say."""
    if sys.platform == "win32":
        import ctypes

        kernel = ctypes.windll.kernel32
        kernel.GetTickCount64.restype = ctypes.c_ulonglong
        return float(kernel.GetTickCount64()) / 1000
    try:
        return float(Path("/proc/uptime").read_text(encoding="ascii").split()[0])
    except (OSError, ValueError, IndexError):
        return None


def last_wake() -> float:
    """Seconds after boot of the last resume from sleep or hibernation; 0 for none, and
    0 wherever it cannot be read -- the boot is then the latest thing known."""
    if sys.platform != "win32":
        return 0.0
    import ctypes

    value = ctypes.c_ulonglong(0)
    try:
        status = ctypes.windll.powrprof.CallNtPowerInformation(
            LAST_WAKE_TIME, None, 0, ctypes.byref(value), ctypes.sizeof(value)
        )
    except OSError:
        return 0.0
    return value.value / _TICKS_PER_SECOND if status == 0 else 0.0


def awake_since(now: float | None = None) -> float | None:
    """The POSIX time this machine last booted or woke, whichever is later; None where
    the boot cannot be read."""
    now = _time.time() if now is None else now
    up = uptime()
    if up is None:
        return None
    return now - up + min(max(last_wake(), 0.0), up)
