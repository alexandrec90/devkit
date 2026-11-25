"""`tests/machine_clock_plugin.py`: no test waits on how long its machine has been up.

The regression it holds: `test_collectors.py::test_a_handoff_waiting_while_docker_is_
silent_is_left_for_the_next_pass` slept 549 seconds on every PR gate, because a fresh CI
runner had been up for less than `collectors.ENGINE_STARTUP` and the real clock said so.
"""

from __future__ import annotations

import machine_clock
from support import load_script

collectors = load_script("scripts/collectors.py")
ws = load_script("scripts/workspace-status.py")


class Silent:
    """A Docker engine that never answers, and counts how often it was asked."""

    def __init__(self) -> None:
        self.asked = 0

    def ps(self):
        self.asked += 1


def test_no_test_is_told_when_its_machine_came_up():
    assert machine_clock.awake_since() is None


def test_the_modules_that_wait_on_the_boot_read_the_same_answer():
    """The patch is on the module object every caller shares, not on a private copy."""
    assert collectors.machine_clock is machine_clock
    assert ws.machine_clock is machine_clock


def test_a_silent_engine_is_asked_once_and_not_waited_on():
    """What `collectors.act` hands `wait_for_engine` under the plugin: one look, no sleep."""
    since = machine_clock.awake_since()
    engine = Silent()

    def no_sleep(seconds):
        raise AssertionError(f"a test slept {seconds}s waiting for a fresh boot")

    assert collectors.wait_for_engine(engine, since, sleep=no_sleep) is None
    assert engine.asked == 1


def test_a_freshly_booted_machine_would_have_waited():
    """The other half, so the test above is not vacuous: given a real fresh boot, the
    same call does sleep -- which is the wait the plugin keeps out of the suite."""
    slept: list[float] = []
    now = [0.0]

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    engine = Silent()
    collectors.wait_for_engine(engine, 30.0, clock=lambda: now[0], sleep=sleep)
    assert sum(slept) == collectors.ENGINE_STARTUP - 30.0
