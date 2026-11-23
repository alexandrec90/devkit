#!/usr/bin/env python3
"""Nothing the fix pass holds, caps or skips may sit there: past a day, it is a finding.

Every wait the pass makes is reasonable on the pass that makes it -- a red local run held
for the next scheduled one, a release PR red by construction until its tag exists, an
adoption the upgrade sweep is about to close. Each is also a way for something to sit for a week
with a sensible sentence beside it, because the record is rewritten every half hour and
nobody reads the one before. `fix-pass.history.jsonl` keeps the waits by name
(`fix_cycle.history_line`), so how long each has lasted is a question with an answer.

A wait that has lasted `STALL_AFTER` (`stall_after`: two nights for the nightly tier) is filed: a local tier that is not re-running, a
release pipeline that is not releasing, a sweep that is not closing. What the pass is
already tracking elsewhere -- an escalation the devkit session has, a back-off, a
session in flight, a hold behind the red harness or an open adoption -- is not filed
twice.

Tested in `tests/test_fix_stall.py`.
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fix_budget
import fix_cycle
import fix_findings
import fix_plan

STALL_AFTER = _dt.timedelta(hours=24)
# A hold for the nightly local run waits on a job that fires once a day and then waits for
# the PC to go idle (`e2e-pass.py`), so at a day it has had one chance, and that one may
# still be waiting. tvshell's was held at 09:00, and filed at 09:00 the next day with
# that day's run started and not yet idle (09f80e67). Two nights with no re-test is a tier
# that is not re-running.
NIGHTLY_STALL_AFTER = _dt.timedelta(hours=48)
KIND = "stalled"
FIELDS = ("waiting", "skipped")
# Waits some other part of the loop already owns, by how the reason starts. A full
# Dependabot cap frees itself within the day, and filing it as a stall would send the
# devkit session the cap exists to save.
TRACKED = (
    "escalated",
    "backing off",
    "already dispatched",
    "held until the devkit session",
    # A PR held behind the red harness or behind its project's open adoption waits on
    # something already a finding: the ledger groups or the devkit gate that make the
    # harness red, the adoption PR with its own decision. Filed as a stall, the hold went
    # onto the harness ledger and kept the harness red, so it held itself, and its
    # adoption with it: carameli #526 and #530, 2026-11-21 (39517cbb, 4e79a5bd).
    fix_cycle.HELD_FOR_HARNESS,
    fix_cycle.HELD_FOR_ADOPTION,
    # Another PC's claim, which frees itself at its TTL (`fix_claims.CLAIMED_BY`). The one
    # hold a claim makes: one that could not be made sends anyway.
    "claimed by",
    fix_budget.DAILY_CAPPED,
)


def read_history(path: Path) -> list[dict]:
    rows = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def streaks(history: list[dict], field: str) -> dict[str, tuple[str, str]]:
    """`name -> (since, latest why)` for every name in the newest pass's `field`, where
    `since` is the first pass of the unbroken run of passes that carried it.

    A pass that carried it for a `TRACKED` reason breaks the run: the loop owned the
    wait then, so a day of Dependabot cap ending in half an hour behind a red harness
    is half an hour of harness hold, not a day of it."""
    if not history:
        return {}
    newest = history[-1].get(field)
    if not isinstance(newest, dict):
        return {}
    found: dict[str, tuple[str, str]] = {}
    for name, why in newest.items():
        since = str(history[-1].get("when", ""))
        for row in reversed(history[:-1]):
            carried = row.get(field)
            if not isinstance(carried, dict) or name not in carried or tracked(carried[name]):
                break
            since = str(row.get("when", since))
        found[name] = (since, str(why))
    return found


def stall_after(name: str) -> _dt.timedelta:
    """How long the wait `name` may last before it is a stall: `NIGHTLY_STALL_AFTER` for
    one on the nightly local tier, `STALL_AFTER` for every other."""
    nightly = name.endswith(f"({fix_plan.LOCAL_E2E_NAME})")
    return NIGHTLY_STALL_AFTER if nightly else STALL_AFTER


def stalled(
    history: list[dict], now: _dt.datetime, after: _dt.timedelta | None = None
) -> list[fix_findings.Finding]:
    """A finding for each wait nothing else is tracking that is older than `after`, or
    than its own `stall_after` when none is given."""
    found = []
    for field in FIELDS:
        for name, (since, why) in sorted(streaks(history, field).items()):
            if tracked(why) or not _older(since, now, after or stall_after(name)):
                continue
            project = name.split(" ", 1)[0]
            detail = f"{field} since {since[:16]}: {name} -- {why}"
            found.append(fix_findings.Finding(KIND, project, detail))
    return found


def waited_name(detail: str) -> str:
    """The wait a `stalled` finding's ledger detail names (`stalled: <field> since
    <when>: <name> -- <why>`), or "" for a detail of another shape."""
    kind, sep, rest = detail.partition(": ")
    if kind != KIND or not sep:
        return ""
    head, sep, rest = rest.partition(": ")
    if not sep or not head.startswith(tuple(f"{field} since " for field in FIELDS)):
        return ""
    name, sep, _ = rest.partition(" -- ")
    return name if sep else ""


def ended(history: list[dict], items: list) -> list:
    """Each open `stalled` item in `items` whose wait the newest pass no longer made.

    Nothing else retires one, and the wait it names is gone once its cause is fixed: six
    local-e2e stalls a pass running the code before #787 filed sat open after the next
    pass stopped making them, each one more group sent to a devkit session. An empty
    history proves nothing, so it retires nothing."""
    if not history:
        return []
    carried: set[str] = set()
    for field in FIELDS:
        names = history[-1].get(field)
        if isinstance(names, dict):
            carried.update(names)
    gone = []
    for item in items:
        name = waited_name(item.fields.get("detail", ""))
        if name and name not in carried:
            gone.append(item)
    return gone


def tracked(why: object) -> bool:
    """Whether a wait's reason is one some other part of the loop already owns.

    A wait that names a person's step (`harness_triage.PERSON_STEP`) is the person's, and
    the finding that names it awaits them already (`fix_red.billing`'s, for a gate refused
    over billing). Filed as a stall too, it is a second red tray row per PR for one step:
    productivity-tracker and social-scraper's billing skips made six of one, 2026-11-01."""
    text = str(why)
    return text.lower().startswith(TRACKED) or fix_findings.triage.PERSON_STEP in text


def _older(since: str, now: _dt.datetime, after: _dt.timedelta) -> bool:
    try:
        began = _dt.datetime.fromisoformat(since)
    except ValueError:
        return False
    if began.tzinfo is None:
        began = began.replace(tzinfo=_dt.UTC)
    return now - began >= after
