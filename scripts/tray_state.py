#!/usr/bin/env python3
"""What the tray indicator shows: one verdict per scheduled job, and one overall.

Split from `tray.py` because everything here is a decision and everything there is
`ctypes`. A tray icon whose colour logic lives inside a Windows message loop is a thing
nobody can test, and the colour is the entire product.

**The judgement is not made here.** `schedule_health.problems` already decides what
counts as a problem, and it holds a lot of hard-won detail -- which exit codes are
statuses rather than failures, that one missed daily run on a sleeping laptop is the
system working, that a job gets at most one line. Re-deriving any of that would create a
second opinion that disagrees with the session-start line for the same machine, which is
worse than no indicator. So this consumes those lines and adds only the one thing they
do not carry: how loud each is.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import schedule_health

OK = "ok"
WARN = "warn"
FAIL = "fail"

# Rank for `overall`: the worst state present wins, so one red job colours the icon red
# however many green ones surround it. An indicator that averaged would be worse than
# none -- it would go green while something was broken.
RANK = {OK: 0, WARN: 1, FAIL: 2}

# Substrings that make a problem line **red** rather than amber. Everything
# `schedule_health` reports is worth knowing; these are the ones where something is
# already broken rather than merely late or unstarted.
#
# Matched on the sentence `schedule_health` writes, so this list and that module's
# wording are coupled: `test_tray_state.py` asserts each marker still appears in a line
# that module can actually produce, which is what stops a reworded message silently
# turning every red into amber.
FAIL_MARKERS = ("last run failed", "disabled")

# Shell_NotifyIcon's tooltip field is 128 wide **including the terminator**, and it
# truncates without saying so.
TOOLTIP_LIMIT = 127

COLOURS = {
    # Deliberately not pure red/green. These are picked to stay distinguishable against
    # both a light and a dark taskbar, and to differ in brightness as well as hue, which
    # is the half that survives the most common colour blindness.
    OK: (0x2E, 0x7D, 0x32),
    WARN: (0xE6, 0x8A, 0x00),
    FAIL: (0xC6, 0x28, 0x28),
}


@dataclass(frozen=True)
class JobState:
    """One scheduled job, as the tray presents it."""

    name: str
    state: str
    detail: str = ""

    @property
    def artifact(self) -> str:
        """The job's own record, or "" for a job that names none."""
        return schedule_health.ARTIFACTS.get(self.name, "")


def named_in(line: str) -> str:
    """The job a problem line is about.

    `schedule_health` writes every line as `<task name>: <what is wrong>`, and the task
    names all carry the `devkit-` prefix, so the split is unambiguous even though the
    remainder of the line contains colons of its own (a timestamp, for one).
    """
    name, sep, _rest = line.partition(": ")
    return name if sep and name.startswith(schedule_health.PREFIX) else ""


def severity(line: str) -> str:
    """How loud one problem line is."""
    lowered = line.lower()
    return FAIL if any(marker in lowered for marker in FAIL_MARKERS) else WARN


def states(jobs: list, lines: list[str]) -> list[JobState]:
    """One `JobState` per registered job, worst-first then alphabetical.

    Every job appears, not only the unhealthy ones. The tray's job is to make the whole
    set visible -- "nothing is ever totally invisible" is the point of it -- and a menu
    that listed only failures would be indistinguishable from a menu that had lost track
    of a job entirely.
    """
    worst: dict[str, tuple[str, str]] = {}
    for line in lines:
        name = named_in(line)
        if not name:
            continue
        level = severity(line)
        if name not in worst or RANK[level] > RANK[worst[name][0]]:
            worst[name] = (level, line.partition(": ")[2])
    found = [JobState(job.name, *worst.get(job.name, (OK, ""))) for job in jobs]
    return sorted(found, key=lambda item: (-RANK[item.state], item.name))


def overall(found: list[JobState]) -> str:
    """The icon's colour. Worst wins; an empty machine is not green.

    Nothing registered is `WARN`, not `OK`. A tray that sat green on a machine where the
    installers had never been run would be reporting "all healthy" about a set of zero
    jobs, which is the most misleading thing it could say.
    """
    if not found:
        return WARN
    return max((item.state for item in found), key=lambda level: RANK[level])


def tooltip(found: list[JobState], limit: int = TOOLTIP_LIMIT) -> str:
    """The hover text: a count, plus the first thing that is wrong.

    Truncated here rather than by Windows, which cuts mid-word and gives no sign it did.
    """
    if not found:
        return "devkit: no scheduled jobs registered"
    bad = [item for item in found if item.state != OK]
    if not bad:
        return f"devkit: {len(found)} scheduled jobs, all healthy"
    head = f"devkit: {len(bad)} of {len(found)} need attention -- {bad[0].name}"
    return head if len(head) <= limit else head[: limit - 1] + "…"


def menu_label(item: JobState) -> str:
    """One line in the right-click menu.

    The mark is a character rather than a colour because a menu item cannot be coloured
    without owner-drawing the whole menu, which is a large amount of `ctypes` for an
    indicator that has already made its point with the icon.
    """
    mark = {OK: "OK  ", WARN: "!   ", FAIL: "X   "}[item.state]
    detail = f" -- {item.detail}" if item.detail else ""
    return f"{mark}{item.name}{detail}"


def refresh(now=None) -> list[JobState]:
    """Ask the scheduler and turn its answer into what the tray draws."""
    jobs = schedule_health.query()
    return states(jobs, schedule_health.problems(jobs, now))
