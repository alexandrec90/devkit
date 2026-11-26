"""`scripts/red_seen.py`: a grace runs from the first pass that saw the red."""

from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fix_ledger
import red_seen

NOW = _dt.datetime(2026, 11, 6, 12, 0, tzinfo=_dt.UTC)
LATER = NOW + _dt.timedelta(hours=5)


def path(root: Path) -> Path:
    return root / red_seen.STATE_DIR / "roguelike.json"


def test_the_first_sighting_is_now_and_every_later_one_remembers_it(tmp_path):
    assert red_seen.first_seen(tmp_path, "roguelike", "revert", "abc", NOW) == NOW
    assert red_seen.first_seen(tmp_path, "roguelike", "revert", "abc", LATER) == NOW


def test_a_new_red_starts_its_own_clock(tmp_path):
    red_seen.first_seen(tmp_path, "roguelike", "revert", "abc", NOW)
    assert red_seen.first_seen(tmp_path, "roguelike", "revert", "def", LATER) == LATER


def test_two_clocks_of_one_project_do_not_touch(tmp_path):
    red_seen.first_seen(tmp_path, "roguelike", "revert", "abc", NOW)
    assert red_seen.first_seen(tmp_path, "roguelike", "tidy", "x", LATER) == LATER
    assert red_seen.first_seen(tmp_path, "roguelike", "revert", "abc", LATER) == NOW


def test_a_read_only_pass_starts_no_clock(tmp_path):
    assert red_seen.first_seen(tmp_path, "roguelike", "revert", "abc", NOW, write=False) == NOW
    assert not path(tmp_path).exists()
    assert red_seen.first_seen(tmp_path, "roguelike", "revert", "abc", LATER, write=False) == LATER


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        '{"revert": "abc"}',
        '{"revert": {"key": "abc"}}',
        '{"revert": {"key": "abc", "seen": "yesterday"}}',
        '{"revert": {"key": "abc", "seen": "2026-11-06T09:00:00"}}',
    ],
)
def test_unreadable_state_is_first_seen_now(tmp_path, text):
    path(tmp_path).parent.mkdir(parents=True)
    path(tmp_path).write_text(text, encoding="utf-8")
    assert red_seen.first_seen(tmp_path, "roguelike", "revert", "abc", LATER) == LATER


def test_a_state_file_that_cannot_be_written_still_answers(tmp_path):
    (tmp_path / "logs").write_text("a file where the directory goes", encoding="utf-8")
    assert red_seen.first_seen(tmp_path, "roguelike", "revert", "abc", NOW) == NOW
    assert red_seen.lapsed(tmp_path, "revert", LATER, _dt.timedelta(0)) == []


def test_lapsed_names_each_clock_of_its_kind_that_ran_long_enough(tmp_path):
    red_seen.first_seen(tmp_path, "roguelike", "revert", "abc", NOW)
    red_seen.first_seen(tmp_path, "carameli", "revert", "def", LATER)
    red_seen.first_seen(tmp_path, "devkit", "tidy", "x", NOW)
    hour = _dt.timedelta(hours=1)
    assert red_seen.lapsed(tmp_path, "revert", LATER, hour) == [("roguelike", "abc")]
    assert red_seen.lapsed(tmp_path, "revert", LATER + hour, hour) == [
        ("carameli", "def"),
        ("roguelike", "abc"),
    ]
    assert red_seen.lapsed(tmp_path, "tidy", NOW, hour) == []
    assert red_seen.lapsed(tmp_path / "nowhere", "revert", LATER, hour) == []


def test_unreadable_state_has_lapsed_nothing(tmp_path):
    path(tmp_path).parent.mkdir(parents=True)
    path(tmp_path).write_text('{"revert": {"key": "abc", "seen": "yesterday"}}', encoding="utf-8")
    assert red_seen.lapsed(tmp_path, "revert", LATER, _dt.timedelta(0)) == []


def test_the_revert_ceiling_is_the_window_a_sent_fixer_counts_as_working():
    """Held past it, a fixer is one the pass would send again anyway."""
    assert red_seen.REVERT_GRACE == _dt.timedelta(minutes=20)
    assert red_seen.REVERT_CEILING == fix_ledger.BRANCH_SETTLE
    assert red_seen.REVERT_GRACE < red_seen.REVERT_CEILING
