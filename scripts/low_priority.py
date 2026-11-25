#!/usr/bin/env python3
"""Run this process, and everything it starts, behind whatever the person is doing.

The local test tiers run on the PC somebody works at, and a suite at normal priority
takes the CPU from the editor in front of them. Below-normal is inherited by every child
(Windows documents it for the below-normal and idle classes), so lowering the runner
lowers the pytest, the browser and the compiler under it, and an otherwise idle machine
runs them at full speed.

Stdlib only. Tested in `tests/test_low_priority.py`.
"""

from __future__ import annotations

import ctypes
import os
import sys

BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
NICE = 5


def lower() -> bool:
    """Whether this process now runs below normal. A refusal costs nothing but the
    courtesy, so it is an answer rather than an error."""
    try:
        if sys.platform != "win32":
            os.nice(NICE)
            return True
        kernel32 = ctypes.windll.kernel32
        # A pseudo-handle is pointer-sized: left to the default it is cut to 32 bits.
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.SetPriorityClass.argtypes = (ctypes.c_void_p, ctypes.c_uint)
        own = kernel32.GetCurrentProcess()
        return bool(kernel32.SetPriorityClass(own, BELOW_NORMAL_PRIORITY_CLASS))
    except (AttributeError, OSError):
        return False
