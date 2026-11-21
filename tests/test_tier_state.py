"""`scripts/tier_state.py`: what the medium tier remembers about a project with no verdict."""

from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import tier_state

NOW = _dt.datetime(2026, 11, 6, 14, 0)
HOUR = _dt.timedelta(hours=1)


def _failed(times: int, own: bool = False) -> tier_state.State:
    state = tier_state.State()
    for _ in range(times):
        state = tier_state.could_not_judge(state, NOW, "no network", own)
    return state


@pytest.mark.parametrize(
    ("times", "hours"), [(1, 1), (2, 2), (3, 4), (4, 8), (5, 16), (6, 24), (7, 24), (30, 24)]
)
def test_the_backoff_doubles_from_an_hour_and_caps_at_a_day(times, hours):
    state = _failed(times)
    assert state.failures == times
    assert state.next_try == (NOW + hours * HOUR).isoformat(timespec="seconds")


def test_a_project_is_due_once_its_backoff_has_run():
    state = _failed(1)
    assert tier_state.due(tier_state.State(), NOW)
    assert not tier_state.due(state, NOW)
    assert tier_state.due(state, NOW + HOUR)


def test_a_next_try_that_cannot_be_read_is_due():
    assert tier_state.due(tier_state.State(next_try="whenever"), NOW)


def test_the_first_failure_dates_the_wait_and_a_later_one_keeps_it():
    first = tier_state.could_not_judge(tier_state.State(), NOW, "a", False)
    later = tier_state.could_not_judge(first, NOW + 5 * HOUR, "b", False)
    assert later.since == first.since
    assert later.reason == "b"


def test_filing_is_owed_once_it_has_lasted_a_day():
    young = tier_state.could_not_judge(tier_state.State(), NOW, "a", False)
    assert not tier_state.owes_filing(young, NOW + 23 * HOUR)
    assert tier_state.owes_filing(young, NOW + tier_state.FILE_AFTER)


def test_filing_is_owed_once_and_never_for_the_project_s_own_defect():
    young = tier_state.could_not_judge(tier_state.State(), NOW, "a", False)
    later = NOW + tier_state.FILE_AFTER
    assert not tier_state.owes_filing(tier_state.filed(young), later)
    own = tier_state.could_not_judge(tier_state.State(), NOW, "a", True)
    assert not tier_state.owes_filing(own, later)


def test_memory_skips_are_counted_and_dated_from_the_first():
    first = tier_state.skipped_for_memory(tier_state.State(), NOW)
    second = tier_state.skipped_for_memory(first, NOW + HOUR)
    assert (second.memory_skips, second.memory_since) == (2, first.memory_since)


def test_a_day_short_of_memory_is_said_and_less_is_not():
    state = tier_state.skipped_for_memory(tier_state.State(), NOW)
    assert tier_state.notes("a", state, NOW + 23 * HOUR) == []
    day = NOW + tier_state.STARVED_AFTER
    assert tier_state.notes("a", state, day) == [
        "a: unjudged for 24 hours: short of memory, 1 skip(s)"
    ]


def test_could_not_judge_is_said_and_says_whose_it_is():
    assert tier_state.notes("a", _failed(1), NOW) == [
        f"a: could not judge since {NOW.isoformat(timespec='seconds')}, next try "
        f"{(NOW + HOUR).isoformat(timespec='seconds')} -- no network"
    ]
    assert "the project's own" in tier_state.notes("a", _failed(1, own=True), NOW)[0]


def test_a_plain_state_says_nothing():
    assert tier_state.notes("a", tier_state.State(), NOW) == []


def test_it_survives_a_round_trip_and_clearing(tmp_path):
    state = _failed(2)
    tier_state.write("a", state, tmp_path)
    assert tier_state.read("a", tmp_path) == state
    tier_state.clear("a", tmp_path)
    assert tier_state.read("a", tmp_path) == tier_state.State()
    tier_state.clear("a", tmp_path)


@pytest.mark.parametrize("body", ["not json", "[]", '{"failures": "many"}'])
def test_an_unreadable_record_is_no_record(tmp_path, body):
    path = tier_state.path_for("a", tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(body, encoding="utf-8")
    assert tier_state.read("a", tmp_path) == tier_state.State()


def test_a_key_from_a_newer_runner_is_dropped(tmp_path):
    path = tier_state.path_for("a", tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text('{"failures": 2, "unknown": 1}', encoding="utf-8")
    assert tier_state.read("a", tmp_path).failures == 2
