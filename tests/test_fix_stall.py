"""`scripts/fix_stall.py`: a hold, a cap or a skip that has lasted a day is filed."""

from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fix_stall

NOW = _dt.datetime(2026, 9, 26, 12, 0, tzinfo=_dt.UTC)


def line(hours_ago: float, waiting: dict | None = None, skipped: dict | None = None) -> dict:
    when = (NOW - _dt.timedelta(hours=hours_ago)).isoformat(timespec="seconds")
    return {"when": when, "waiting": waiting or {}, "skipped": skipped or {}}


HELD = {"carameli #8": "held until the harness is clean: devkit's gate is red"}


def test_a_wait_is_dated_from_the_start_of_its_unbroken_run():
    history = [line(50, HELD), line(40), line(30, HELD), line(20, HELD), line(0, HELD)]
    assert fix_stall.streaks(history, "waiting") == {
        "carameli #8": (history[2]["when"], HELD["carameli #8"])
    }


def test_a_wait_past_a_day_is_filed_with_its_reason():
    history = [line(30, HELD), line(0, HELD)]
    [found] = fix_stall.stalled(history, NOW)
    assert (found.kind, found.project) == ("stalled", "carameli")
    assert found.detail.startswith(f"waiting since {history[0]['when'][:16]}: carameli #8 -- held")


def test_a_wait_under_a_day_is_not():
    assert fix_stall.stalled([line(20, HELD), line(0, HELD)], NOW) == []


def test_a_long_skip_is_a_release_or_a_sweep_that_is_not_happening():
    skip = {"devkit #410": "red by construction: a release PR fails the newest-tag test"}
    [found] = fix_stall.stalled([line(48, skipped=skip), line(0, skipped=skip)], NOW)
    assert found.detail.startswith("skipped since") and "devkit #410" in found.detail


def test_what_another_part_of_the_loop_already_tracks_is_not_filed_twice():
    tracked = {
        "a #1": "escalated: the devkit session has it on the harness ledger",
        "b #2": "backing off: retried at max effort after 2026-09-27 12:00",
        "c #3": "held until the devkit session in C:/t finishes",
        # A full Dependabot cap frees itself; filed, it would send the devkit session it saves.
        "d dependabot": f"{fix_stall.fix_budget.DAILY_CAPPED}: 2 of 2 sessions sent in the last 24h",
    }
    assert fix_stall.stalled([line(72, tracked), line(0, tracked)], NOW) == []


def test_history_reads_skip_junk_and_a_missing_file_is_empty(tmp_path):
    path = tmp_path / "h.jsonl"
    assert fix_stall.read_history(path) == []
    path.write_text(json.dumps(line(1, HELD)) + "\nnot json\n[1]\n", encoding="utf-8")
    assert len(fix_stall.read_history(path)) == 1
    assert fix_stall.streaks([{"when": "x"}], "waiting") == {}, "an older line with no names"
