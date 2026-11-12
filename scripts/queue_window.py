#!/usr/bin/env python3
"""A clock window an after-merge task waits for: `when = "outside:09:30-16:00 UTC-05:00 mon-fri"`.

A task that must not run during some hours -- a restart of a trading gateway while the
market is open -- used to have nothing to wait on but `merged`, so a session sent inside
the hours could only queue itself again, costing a session and a PR per attempt. A
window lets the file wait in the queue instead:

    inside:<HH:MM>-<HH:MM> <zone> [<days>]    true while the clock is in the window
    outside:<HH:MM>-<HH:MM> <zone> [<days>]   true at every other moment

`<zone>` is `UTC` or a fixed offset, `UTC+05:30`, `UTC-05:00`. **Never a zone name**:
`zoneinfo` reads the operating system's database, and Windows has none, so a name that
parses on one PC would be a malformed task on the next. A window that moves with
daylight saving is written wide enough to cover both offsets. `<days>` are `mon`..`sun`,
comma-separated, with `a-b` ranges that may wrap (`fri-mon`); omitted, every day. The
window starts at its first minute and ends before its last, which may be `24:00`, and
it never spans midnight: one that would is two `inside` days or an `outside` of the rest.

Pure: the clock arrives as an argument. Tested in `tests/test_queue_window.py`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

INSIDE = "inside"
OUTSIDE = "outside"
DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
EVERY_DAY = frozenset(range(len(DAY_NAMES)))
DAY_MINUTES = 24 * 60
# The widest offset any zone uses, in hours (Kiribati's UTC+14).
MAX_OFFSET_HOURS = 14

SHAPE = re.compile(
    r"(?P<side>inside|outside):(?P<start>\d\d:\d\d)-(?P<end>\d\d:\d\d) (?P<zone>\S+)"
    r"(?: (?P<days>\S+))?$"
)
ZONE = re.compile(r"UTC(?:(?P<sign>[+-])(?P<hours>\d\d):(?P<minutes>\d\d))?$")
TIME = re.compile(r"(?P<hours>\d\d):(?P<minutes>\d\d)$")


@dataclass(frozen=True)
class Window:
    """`inside` or not, minutes from the start of the day, minutes east of UTC, and the
    weekdays (`datetime.weekday` numbers) the window falls on."""

    inside: bool
    start: int
    end: int
    offset: int = 0
    days: frozenset[int] = EVERY_DAY


def is_window(when: str) -> bool:
    """`when` is spelled as a window, well or badly: `parse` says which."""
    return when.startswith((f"{INSIDE}:", f"{OUTSIDE}:"))


def parse(when: str) -> Window:
    """The window `when` names. ValueError, saying what to write instead, for one it does
    not: its words reach a session that has only the file to go on."""
    found = SHAPE.match(when)
    if found is None:
        raise ValueError(
            f"when = {when!r} is not a window: write inside:<HH:MM>-<HH:MM> <zone> [<days>], "
            "or outside:, as in outside:09:30-16:00 UTC-05:00 mon-fri"
        )
    start, end = _minutes(found["start"]), _minutes(found["end"])
    if start >= end:
        raise ValueError(
            f"{found['start']}-{found['end']} does not start before it ends: a window never "
            "spans midnight, so write the other side of it, or two windows' worth of days"
        )
    return Window(found["side"] == INSIDE, start, end, _offset(found["zone"]), _days(found["days"]))


def holds(window: Window, now: float) -> bool:
    """`window` is true at `now`, seconds since the epoch."""
    local = datetime.fromtimestamp(now, UTC) + timedelta(minutes=window.offset)
    minute = local.hour * 60 + local.minute
    within = local.weekday() in window.days and window.start <= minute < window.end
    return within == window.inside


def local_time(window: Window, now: float) -> str:
    """`now` as the window's own clock reads it, for a wait's reason."""
    zone = timezone(timedelta(minutes=window.offset))
    return datetime.fromtimestamp(now, zone).strftime("%a %H:%M")


def _minutes(text: str) -> int:
    found = TIME.match(text)
    hours, minutes = (int(found["hours"]), int(found["minutes"])) if found else (99, 99)
    total = hours * 60 + minutes
    if minutes > 59 or total > DAY_MINUTES:
        raise ValueError(f"{text} is not a time of day between 00:00 and 24:00")
    return total


def _offset(zone: str) -> int:
    found = ZONE.match(zone)
    if found is None:
        raise ValueError(
            f"zone {zone!r} is not UTC or an offset such as UTC-05:00: the queue reads no "
            "time-zone database, which Windows lacks, so name the offset, and cover both "
            "offsets of a zone with daylight saving by widening the window"
        )
    if found["sign"] is None:
        return 0
    hours, minutes = int(found["hours"]), int(found["minutes"])
    if hours > MAX_OFFSET_HOURS or minutes > 59:
        raise ValueError(f"zone {zone!r} is past any offset a clock uses")
    total = hours * 60 + minutes
    return -total if found["sign"] == "-" else total


def _days(text: str | None) -> frozenset[int]:
    if text is None:
        return EVERY_DAY
    days: set[int] = set()
    for part in text.split(","):
        first, dash, last = part.partition("-")
        start = _day(first)
        end = _day(last) if dash else start
        days.update((start + step) % len(DAY_NAMES) for step in range((end - start) % 7 + 1))
    return frozenset(days)


def _day(name: str) -> int:
    if name not in DAY_NAMES:
        raise ValueError(f"{name!r} is not a day: write {', '.join(DAY_NAMES)}, or a-b ranges")
    return DAY_NAMES.index(name)
