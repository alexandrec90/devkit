"""`scripts/ship_step.py`: what the fix pass and the ship task share.

The loop is driven at length through `fix-pass.ship_intents` in `tests/test_fix_pass.py`,
the tasks' choice of intents in `tests/test_ship_pass.py`. What is left for here is what
only this module decides: the shared lock's age, and that a direct intent needs no toolchain.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from support import load_script

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fix_cycle
import ship_intent
import ship_step


@pytest.fixture(autouse=True)
def no_session_busy(monkeypatch):
    monkeypatch.setattr(ship_step.fix_loop, "fixers_working", frozenset)


def test_the_shared_lock_outlasts_its_longest_holder():
    """A pass's ship step ends by its send deadline; stale any sooner, the ship task could
    break a lock a live pass holds."""
    fix_pass = load_script("scripts/fix-pass.py")
    assert ship_step.SHIP_LOCK_STALE > fix_pass.WINDOW_SECONDS - fix_pass.SEND_RESERVE_SECONDS
    assert ship_step.SHIP_LOCK_NAME == "ship-pass.lock" != fix_pass.RUN_LOCK_NAME


def _ship(monkeypatch, intent):
    provisioned, shipped = [], []
    monkeypatch.setattr(
        ship_step, "provision_for_ship", lambda tree: provisioned.append(tree) or ""
    )
    monkeypatch.setattr(ship_step.push_gate, "interpreter", lambda tree: "py")
    monkeypatch.setattr(
        ship_step.ship_intent,
        "ship_one",
        lambda i, p, b: shipped.append(i.branch) or ship_intent.Outcome(i, "shipped", "u"),
    )
    lines, _, _ = ship_step.ship_each([intent], Path("r"), fix_cycle.DISPATCH, None, 60.0)
    return provisioned, shipped, lines


def test_a_direct_intent_ships_with_no_toolchain_provisioned(monkeypatch, tmp_path):
    """It runs no framework, so a tree nobody provisioned has nothing to wait for."""
    one = ship_intent.Intent("roguelike", tmp_path, "agent/d", "S", "B", direct=True)
    provisioned, shipped, _ = _ship(monkeypatch, one)
    assert shipped == ["agent/d"] and provisioned == []


def test_a_gated_intent_is_provisioned_as_it_always_was(monkeypatch, tmp_path):
    one = ship_intent.Intent("carameli", tmp_path, "agent/g", "S", "B")
    provisioned, shipped, _ = _ship(monkeypatch, one)
    assert shipped == ["agent/g"] and provisioned == [tmp_path]


def test_a_blocked_direct_intent_is_held_back_and_never_reads_as_refused(monkeypatch, tmp_path):
    one = ship_intent.Intent("roguelike", tmp_path, "main", "S", "B", "the default", direct=True)
    monkeypatch.setattr(
        ship_step.ship_intent, "stored_refusal", lambda i: pytest.fail("not reached")
    )
    lines, refused = ship_step.left_to_ship_task([one], fix_cycle.DISPATCH)
    assert lines == ["roguelike main -- NOT shipped: the default"] and not refused


def test_nothing_to_leave_asks_no_one_whether_a_session_is_busy(monkeypatch):
    """The pass asks `claude agents` once already; a machine with no direct project should
    not pay for a second."""
    monkeypatch.setattr(
        ship_step.fix_loop, "fixers_working", lambda: pytest.fail("no intents, no question")
    )
    assert ship_step.left_to_ship_task([], fix_cycle.DISPATCH) == ([], [])


def test_a_direct_intent_a_session_is_still_in_stays_held(monkeypatch, tmp_path):
    one = ship_intent.Intent("roguelike", tmp_path, "agent/d", "S", "B", direct=True)
    monkeypatch.setattr(ship_step.fix_loop, "why_held", lambda busy, tree, now: "still working")
    lines, refused = ship_step.left_to_ship_task([one], fix_cycle.DISPATCH)
    assert lines == ["roguelike agent/d -- held: still working"] and not refused
