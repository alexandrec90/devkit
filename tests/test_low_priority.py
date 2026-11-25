"""`scripts/low_priority.py`: the local test tiers run behind the person's own work."""

from __future__ import annotations

import subprocess
import sys

from support import REPO_ROOT, load_script

low_priority = load_script("scripts/low_priority.py")

# In a child, so the suite's own priority is left alone; and the grandchild is the point:
# what a runner starts is what takes the CPU.
WINDOWS_CHECK = """
import ctypes, subprocess, sys
sys.path.insert(0, {scripts!r})
import low_priority
ask = "import ctypes; k = ctypes.windll.kernel32; " \
      "k.GetCurrentProcess.restype = ctypes.c_void_p; " \
      "k.GetPriorityClass.argtypes = (ctypes.c_void_p,); " \
      "print(k.GetPriorityClass(k.GetCurrentProcess()))"
assert low_priority.lower()
print(subprocess.run([sys.executable, "-c", ask], capture_output=True, text=True).stdout)
"""
POSIX_CHECK = """
import os, sys
sys.path.insert(0, {scripts!r})
import low_priority
before = os.nice(0)
assert low_priority.lower()
print(os.nice(0) - before)
"""


def test_a_runner_and_what_it_starts_are_below_normal():
    windows = sys.platform == "win32"
    script = WINDOWS_CHECK if windows else POSIX_CHECK
    source = script.format(scripts=str(REPO_ROOT / "scripts"))
    done = subprocess.run(
        [sys.executable, "-c", source], capture_output=True, text=True, check=False
    )
    wanted = low_priority.BELOW_NORMAL_PRIORITY_CLASS if windows else low_priority.NICE
    assert done.stdout.strip() == str(wanted), done.stderr


def test_a_machine_that_refuses_is_an_answer(monkeypatch):
    def refuse(_by):
        raise OSError("no")

    monkeypatch.setattr(low_priority.sys, "platform", "linux")
    monkeypatch.setattr(low_priority.os, "nice", refuse, raising=False)
    assert low_priority.lower() is False
