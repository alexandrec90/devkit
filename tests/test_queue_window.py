"""`scripts/queue_window.py`: the clock window an after-merge task can wait for."""

from __future__ import annotations

import sys
from datetime import UTC, datetime

import pytest
from support import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import queue_window as qw


def at(text: str) -> float:
    """`text`, `YYYY-MM-DD HH:MM` in UTC, as seconds since the epoch."""
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=UTC).timestamp()


# 2027-01-15 is a Friday.
FRIDAY = "2027-01-15"
MARKET = "09:30-16:00 UTC-05:00 mon-fri"


def test_a_full_window_parses_into_its_parts():
    window = qw.parse(f"outside:{MARKET}")
    assert window == qw.Window(False, 9 * 60 + 30, 16 * 60, -5 * 60, frozenset(range(5)))


def test_zone_and_days_default_to_utc_and_every_day():
    assert qw.parse("inside:00:00-24:00 UTC") == qw.Window(True, 0, qw.DAY_MINUTES)
    assert qw.parse("inside:01:00-02:00 UTC+05:30").offset == 5 * 60 + 30


@pytest.mark.parametrize(
    ("days", "numbers"),
    [
        ("mon-fri", {0, 1, 2, 3, 4}),
        ("sat,sun", {5, 6}),
        ("fri-mon", {4, 5, 6, 0}),
        ("wed", {2}),
        ("mon,wed-thu", {0, 2, 3}),
    ],
)
def test_days_are_names_lists_and_ranges_that_may_wrap(days, numbers):
    assert qw.parse(f"inside:01:00-02:00 UTC {days}").days == frozenset(numbers)


def test_is_window_spots_either_side_however_badly_spelled():
    assert qw.is_window("outside:anything")
    assert qw.is_window("inside:09:00-10:00 UTC")
    assert not qw.is_window("merged")
    assert not qw.is_window("task:outside")


@pytest.mark.parametrize(
    ("when", "said"),
    [
        ("outside:soon", "not a window"),
        ("outside:9:30-16:00 UTC", "not a window"),
        ("outside:09:30-16:00", "not a window"),
        ("outside:09:30-16:00 America/New_York mon-fri", "time-zone database"),
        ("outside:09:30-16:00 EST", "UTC-05:00"),
        ("outside:09:30-16:00 UTC+15:00", "past any offset"),
        ("outside:09:30-16:00 UTC+05:60", "past any offset"),
        ("inside:16:00-09:30 UTC", "spans midnight"),
        ("inside:10:00-10:00 UTC", "spans midnight"),
        ("inside:09:60-10:00 UTC", "time of day"),
        ("inside:09:00-24:01 UTC", "time of day"),
        ("inside:09:00-10:00 UTC funday", "not a day"),
        ("inside:09:00-10:00 UTC mon-", "not a day"),
    ],
)
def test_a_bad_window_says_what_to_write_instead(when, said):
    with pytest.raises(ValueError, match=said):
        qw.parse(when)


@pytest.mark.parametrize(
    ("utc", "trading"),
    [
        (f"{FRIDAY} 14:29", False),  # 09:29 in New York: not open yet
        (f"{FRIDAY} 14:30", True),  # 09:30: the window's first minute is in it
        (f"{FRIDAY} 20:59", True),
        (f"{FRIDAY} 21:00", False),  # 16:00: its last is not
        ("2027-01-16 15:00", False),  # Saturday
        ("2027-01-18 03:00", False),  # Sunday 22:00 there, though Monday in UTC
    ],
)
def test_inside_and_outside_are_judged_on_the_windows_own_clock(utc, trading):
    now = at(utc)
    assert qw.holds(qw.parse(f"inside:{MARKET}"), now) is trading
    assert qw.holds(qw.parse(f"outside:{MARKET}"), now) is not trading


def test_local_time_is_the_windows_clock_for_a_wait_reason():
    assert qw.local_time(qw.parse(f"outside:{MARKET}"), at(f"{FRIDAY} 15:00")) == "Fri 10:00"
    assert qw.local_time(qw.parse("inside:01:00-02:00 UTC"), at(f"{FRIDAY} 15:00")) == "Fri 15:00"
