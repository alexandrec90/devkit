"""Tests for scripts/reconcile_journal.py.

1f44005c: the reconcile log kept only the latest pass and stamped only its end, so how
long the overlapping 00:30 pass ran, and in which step, could not be read anywhere.
"""

from __future__ import annotations

import datetime as dt
import json

from support import load_script

journal_mod = load_script("scripts/reconcile_journal.py")

START = dt.datetime(2026, 11, 7, 5, 30, tzinfo=dt.UTC)


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def history(root):
    path = root / journal_mod.HISTORY
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_a_finished_pass_is_kept_with_its_start_duration_and_steps(tmp_path):
    clock = Clock()
    journal = journal_mod.begin(tmp_path, START, clock)
    clock.now += 12
    journal.step("inspect demo--x-0806")
    clock.now += 900
    journal.step("sync checkouts")
    clock.now += 30
    journal_mod.finish(journal, 0, START + dt.timedelta(seconds=942))
    (row,) = history(tmp_path)
    assert row["started"] == "2026-11-07T05:30:00+00:00"
    assert row["took"] == 942 and row["exit"] == 0
    assert row["steps"] == [["start", 0], ["inspect demo--x-0806", 12], ["sync checkouts", 912]]
    assert not (tmp_path / journal_mod.RUNNING).exists(), "a finished pass leaves no marker"


def test_the_marker_names_the_step_a_pass_is_in(tmp_path):
    clock = Clock()
    journal = journal_mod.begin(tmp_path, START, clock)
    clock.now += 5
    journal.step("reap demo--x-0806")
    marker = journal_mod.read_marker(tmp_path)
    assert marker["step"] == "reap demo--x-0806"
    assert marker["started"] == "2026-11-07T05:30:00+00:00"


def test_a_pass_that_never_finished_is_filed_by_the_next(tmp_path):
    """Killed by the scheduler, or still going: either way it wrote no log, and the
    marker it left is the only account of where it was."""
    dead = journal_mod.begin(tmp_path, START, Clock())
    dead.step("sync checkouts")
    later = START + dt.timedelta(minutes=15)
    journal_mod.begin(tmp_path, later, Clock())
    (row,) = history(tmp_path)
    assert row["unfinished"] is True
    assert row["step"] == "sync checkouts" and row["seen"] == later.isoformat()


def test_an_earlier_pass_finishing_late_leaves_the_newer_marker(tmp_path):
    """The overlap: the 00:15 pass ends after the 00:30 one began, and must not erase
    the marker that says where the 00:30 one is."""
    early = journal_mod.begin(tmp_path, START, Clock())
    late = journal_mod.begin(tmp_path, START + dt.timedelta(minutes=15), Clock())
    journal_mod.finish(early, 0)
    assert journal_mod.read_marker(tmp_path)["started"] == late.marker()["started"]


def test_history_keeps_only_the_newest_lines(tmp_path):
    for n in range(5):
        journal_mod.append_history(tmp_path, {"n": n}, keep=3)
    assert [row["n"] for row in history(tmp_path)] == [2, 3, 4]


def test_the_header_says_when_the_pass_started_and_how_long_it_took(tmp_path):
    clock = Clock()
    journal = journal_mod.begin(tmp_path, START, clock)
    clock.now += 61
    head = journal.header(1, START + dt.timedelta(seconds=61))
    assert "exit=1" in head and "started=2026-11-07T05:30:00+00:00" in head
    assert "took=61s" in head and "start@0s" in head


def test_the_header_of_a_journal_with_no_steps_is_one_line(tmp_path):
    journal = journal_mod.Journal(tmp_path, START, Clock())
    assert "\n" not in journal.header(0, START)
    assert journal.entry(0, START)["steps"] == []


def test_a_marker_that_is_not_json_reads_as_none(tmp_path):
    path = tmp_path / journal_mod.RUNNING
    path.parent.mkdir(parents=True)
    path.write_text("{half", encoding="utf-8")
    assert journal_mod.read_marker(tmp_path) is None
    path.write_text("[1]", encoding="utf-8")
    assert journal_mod.read_marker(tmp_path) is None
    assert journal_mod.read_marker(tmp_path / "none") is None


def test_step_is_inert_outside_a_journaled_block(tmp_path):
    journal_mod.step("inspect demo--x-0806")
    with journal_mod.journaled(tmp_path, enabled=False) as journal:
        journal_mod.step("inspect demo--x-0806")
    assert journal is None
    assert not (tmp_path / "logs").exists(), "a dry run journals nothing"


def test_step_reaches_the_journal_of_the_enclosing_block(tmp_path):
    with journal_mod.journaled(tmp_path) as journal:
        journal_mod.step("sync checkouts")
    assert journal.steps[-1][0] == "sync checkouts"
    journal_mod.step("after")
    assert journal.steps[-1][0] == "sync checkouts", "the block's end unbinds it"


def test_an_unwritable_logs_dir_never_raises(tmp_path):
    (tmp_path / "logs").write_text("not a directory", encoding="utf-8")
    journal = journal_mod.begin(tmp_path, START, Clock())
    journal.step("inspect")
    journal_mod.finish(journal, 0)
