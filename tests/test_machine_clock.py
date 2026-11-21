"""`scripts/machine_clock.py`: when the machine last booted or woke."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from support import load_script

clock = load_script("scripts/machine_clock.py")


def _can_answer() -> bool:
    # What the machine can answer from, not what it calls itself: the POSIX rehearsal is
    # a Windows host reporting `linux`, with no `/proc/uptime` to read.
    return sys.platform == "win32" or Path("/proc/uptime").is_file()


def _came_up_in_the_past(now: float) -> None:
    # `awake_since` reads the uptime itself, so only a read taken after it bounds it.
    since, up = clock.awake_since(now), clock.uptime()
    assert up is not None and up > 0
    assert since is not None and now - up <= since <= now


def test_the_real_machine_came_up_in_the_past():
    if _can_answer():
        _came_up_in_the_past(time.time())
    assert clock.last_wake() >= 0.0


def test_a_tick_between_two_reads_of_the_uptime_is_not_a_boot_in_the_future(monkeypatch):
    """`/proc/uptime` ticks every 10 ms, and `awake_since` reads it again after the test
    did: a CI runner crossed a tick between the two and put the boot 0.01 s early."""
    ticks = iter([176.79, 176.80, 176.81])
    monkeypatch.setattr(clock, "uptime", lambda: next(ticks))
    monkeypatch.setattr(clock, "last_wake", lambda: 0.0)
    _came_up_in_the_past(1_000_000.0)


def test_a_wake_after_the_boot_is_when_the_machine_came_up(monkeypatch):
    """Fast Startup's "shutdown" is a hibernation: the tick runs on from a boot days
    old, and only the last resume says the machine was off overnight."""
    monkeypatch.setattr(clock, "uptime", lambda: 3 * 86_400.0)
    monkeypatch.setattr(clock, "last_wake", lambda: 3 * 86_400.0 - 120)
    assert clock.awake_since(1_000_000.0) == 1_000_000.0 - 120


def test_a_machine_that_never_slept_came_up_at_its_boot(monkeypatch):
    monkeypatch.setattr(clock, "uptime", lambda: 600.0)
    monkeypatch.setattr(clock, "last_wake", lambda: 0.0)
    assert clock.awake_since(1_000_000.0) == 1_000_000.0 - 600


@pytest.mark.parametrize("wake", [-5.0, 10_000.0])
def test_a_wake_off_the_tick_is_clamped_to_it(monkeypatch, wake):
    monkeypatch.setattr(clock, "uptime", lambda: 600.0)
    monkeypatch.setattr(clock, "last_wake", lambda: wake)
    assert 1_000_000.0 - 600 <= clock.awake_since(1_000_000.0) <= 1_000_000.0


def test_a_machine_that_cannot_say_says_nothing(monkeypatch):
    monkeypatch.setattr(clock, "uptime", lambda: None)
    assert clock.awake_since(1_000_000.0) is None


def test_no_proc_uptime_off_windows_is_unknown(monkeypatch):
    monkeypatch.setattr(clock.sys, "platform", "linux")
    monkeypatch.setattr(clock, "Path", lambda _p: Path("/nonexistent/uptime"))
    assert clock.uptime() is None
    assert clock.last_wake() == 0.0
