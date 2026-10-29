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

The ingestion collectors ride along as rows of their own. A container collector is no
scheduled task, so the scheduler cannot report it, and `collectors.row` is its
`schedule_health` (`collector_states`). A scheduled one *is* a task -- named after its
project, not `devkit-` -- so it is asked about in the same scheduler query and judged by
the same `problems`, then shown under the collectors' label (`collector_task_states`).

The fix pass rides along too, read off its history's newest line (`fix_pass_states`): a
red row per step the ledger awaits a person for -- the one thing no fixer can take, which
otherwise reached the person only through the morning toast -- and one row on the fixers
themselves. The pass judges both and writes them down; this only draws them.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collectors
import schedule_health

REPO_ROOT = Path(__file__).resolve().parents[1]

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
    # A row that is not a scheduled task -- a collector -- names its record itself.
    log: str = ""

    @property
    def artifact(self) -> str:
        """The job's own record, or "" for a job that names none."""
        return self.log or schedule_health.ARTIFACTS.get(self.name, "")


def named_in(line: str, known: frozenset[str] = frozenset()) -> str:
    """The job a problem line is about.

    `schedule_health` writes every line as `<task name>: <what is wrong>`, and the task
    names all carry the `devkit-` prefix or are one of `known` -- the collector tasks,
    which do not contain `: ` -- so the split is unambiguous even though the remainder of
    the line contains colons of its own (a timestamp, for one).
    """
    name, sep, _rest = line.partition(": ")
    return name if sep and (name.startswith(schedule_health.PREFIX) or name in known) else ""


def severity(line: str) -> str:
    """How loud one problem line is."""
    lowered = line.lower()
    return FAIL if any(marker in lowered for marker in FAIL_MARKERS) else WARN


def states(
    jobs: list, lines: list[str], deliberate: frozenset[str] = frozenset()
) -> list[JobState]:
    """One `JobState` per registered job, worst-first then alphabetical.

    Every job appears, not only the unhealthy ones. The tray's job is to make the whole
    set visible -- "nothing is ever totally invisible" is the point of it -- and a menu
    that listed only failures would be indistinguishable from a menu that had lost track
    of a job entirely.

    A job stood down through `harness-switch.py --off jobs` is `OK` -- it is doing what
    someone asked of it -- but it is **not** left to read as a job that is running. That
    would trade one wrong report for its mirror image: the menu's whole claim is that a
    stopped job cannot hide in it, and a deliberately stopped one rendered identically to
    a working one is exactly that hiding place. It gets the state of a healthy job and a
    detail saying why it is quiet, so the menu answers "is anything broken" and "is
    anything off" without conflating them.
    """
    worst: dict[str, tuple[str, str]] = {}
    known = frozenset(job.name for job in jobs)
    for line in lines:
        name = named_in(line, known)
        if not name:
            continue
        level = severity(line)
        if name not in worst or RANK[level] > RANK[worst[name][0]]:
            worst[name] = (level, line.partition(": ")[2])
    found = [
        JobState(
            job.name,
            *worst.get(
                job.name,
                (OK, "off on purpose (harness-switch)" if job.name in deliberate else ""),
            ),
        )
        for job in jobs
    ]
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
    """Ask the scheduler and turn its answer into what the tray draws.

    `stood_down` is read here for the same reason `report` reads it: `problems` is pure
    and takes intent as an argument, so every entry point owes it that argument or it
    silently gets the strict answer. The tray is the entry point where the omission
    would be loudest -- a job an operator deliberately switched off would sit in the
    menu under a red icon indefinitely, which is the one thing an always-visible
    indicator must never do if its red is to keep meaning anything.
    """
    tasks = collectors.scheduled_tasks()
    jobs = schedule_health.query(also=frozenset(tasks))
    deliberate = schedule_health.stood_down()
    lines = schedule_health.problems(
        jobs, now, deliberate, artifacts={**schedule_health.ARTIFACTS, **tasks}
    )
    found = collector_task_states(states(jobs, lines, deliberate), tasks)
    found += collector_states() + fix_pass_states(read_history(REPO_ROOT / FIX_HISTORY))
    return sorted(found, key=lambda item: (-RANK[item.state], item.name))


# The fix pass's own history and record (`fix-pass.HISTORY`, `fix-pass.ARTIFACT`), in the
# checkout both it and the tray are installed from. The history's newest line is the
# pass's verdict; the tray re-judges none of it, for the reason this module's docstring
# gives about `schedule_health`.
FIX_HISTORY = Path("logs") / "fix-pass.history.jsonl"
FIX_RECORD = Path("logs") / "fix-pass.log"
PERSON_ROW = "your step"
FIXERS_ROW = "fixers"
# Passes in a row with a fixer owed and none sent, at work or waiting with a reason,
# before the row turns amber: three hours at the half-hourly schedule. A lapsed login, a
# launcher that cannot start, a plan that drops the backlog -- each looks like this, and
# none of them is a failed job, so no other row goes amber for it.
STUCK_PASSES = 6
# `fix_cycle.DISPATCH`, spelled here so the tray imports none of the pass; a test holds
# the two equal.
DISPATCH = "dispatch"


def read_history(path: Path) -> list[dict]:
    """The pass's history lines, oldest first; empty when it has none or cannot be read."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def fix_pass_states(history: list[dict]) -> list[JobState]:
    """A red row per step the ledger awaits a person for, then one row on the fixers.

    None at all on a machine with no history: the pass's scheduled job is a row of its
    own, and a pass that never ran is that row's to report.
    """
    if not history:
        return []
    record = FIX_RECORD.as_posix()
    steps = history[-1].get("person") or []
    person = [JobState(PERSON_ROW, FAIL, str(step), record) for step in steps]
    return [*person, fixers_state(history)]


def _clock(when: object) -> str:
    """`HH:MMZ` of an ISO stamp, or the stamp itself when it is not one."""
    text = str(when)
    return f"{text[11:16]}Z" if len(text) >= 16 and text[10] == "T" else text


def _stuck(row: dict) -> bool:
    """Whether one pass owed a fixer and had none sent, at work or waiting."""
    owed = row.get("owed")
    if not isinstance(owed, int) or "working" not in row:
        return False  # written before the pass recorded either: nothing to judge by
    return owed > 0 and not (row.get("sent") or row.get("working") or row.get("waiting"))


def fixers_state(history: list[dict]) -> JobState:
    """What the fixers are doing, by the newest pass; amber when they cannot be sent."""
    last = history[-1]
    working, waiting = last.get("working") or [], last.get("waiting") or {}
    parts = [f"{len(working)} at work"] if "working" in last else []
    parts += [f"{len(waiting)} waiting"] if waiting else []
    parts += [f"{last['owed']} owed"] if last.get("owed") else []
    sends = [row for row in history if row.get("sent")]
    parts.append(f"last sent {_clock(sends[-1]['when'])}" if sends else "none sent")
    detail = ", ".join(parts)
    record = FIX_RECORD.as_posix()
    harness = last.get("harness", "clean")
    if harness != "clean":
        reasons = "; ".join(harness) if isinstance(harness, list) else str(harness)
        return JobState(FIXERS_ROW, WARN, f"harness RED, sends held -- {reasons}; {detail}", record)
    recent = history[-STUCK_PASSES:]
    if len(recent) == STUCK_PASSES and all(_stuck(row) for row in recent):
        stuck = f"owed a fixer and none sent for {STUCK_PASSES} passes"
        return JobState(FIXERS_ROW, WARN, f"{stuck}; {detail}", record)
    mode = last.get("mode", "")
    if mode != DISPATCH:
        detail = f"mode={mode}, sends nothing; {detail}"
    return JobState(FIXERS_ROW, OK, detail, record)


def collector_task_states(found: list[JobState], tasks: dict[str, str]) -> list[JobState]:
    """`found` with each scheduled collector's task relabelled as a collector row, and a
    red row for one this machine runs whose task the scheduler does not have.

    That second half is the point of asking by name: a task that was never registered,
    or was deleted by hand, is absent from the query, and a row that simply vanished
    would read as nothing to report -- the silence `schedule_health` exists to break.
    """
    seen = {item.name for item in found}
    relabelled = [
        JobState(collectors.ROW_PREFIX + item.name, item.state, item.detail, tasks[item.name])
        if item.name in tasks
        else item
        for item in found
    ]
    missing = [
        JobState(
            collectors.ROW_PREFIX + name,
            FAIL,
            "not scheduled on this machine -- `collectors.py maintain` registers it",
            collectors.ARTIFACT.as_posix(),
        )
        for name in sorted(tasks)
        if name not in seen
    ]
    return relabelled + missing


def collector_states() -> list[JobState]:
    """One row per ingestion collector assigned to this machine; none on any other.

    The judgement is `collectors.row`'s, for this module's reason about
    `schedule_health`: `collectors.py status` and the tray must not disagree.
    """
    log = collectors.ARTIFACT.as_posix()
    return [JobState(name, level, detail, log) for name, level, detail in collectors.tray_rows()]
