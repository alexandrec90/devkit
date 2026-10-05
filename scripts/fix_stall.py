#!/usr/bin/env python3
"""Nothing the fix pass holds, caps or skips may sit there: past a day, it is a finding.

Every wait the pass makes is reasonable on the pass that makes it -- a PR held behind a
red harness, a release PR red by construction until its tag exists, an adoption the
upgrade sweep is about to close. Each is also a way for something to sit for a week
with a sensible sentence beside it, because the record is rewritten every half hour and
nobody reads the one before. `fix-pass.history.jsonl` keeps the waits by name
(`fix_cycle.history_line`), so how long each has lasted is a question with an answer.

A wait that has lasted `STALL_AFTER` is filed: a harness fix that is not landing, a
release pipeline that is not releasing, a sweep that is not closing. What the pass is
already tracking elsewhere -- an escalation the devkit session has, a back-off, a
session in flight -- is not filed twice.

Tested in `tests/test_fix_stall.py`.
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fix_budget
import fix_findings

STALL_AFTER = _dt.timedelta(hours=24)
FIELDS = ("waiting", "skipped")
# Waits some other part of the loop already owns, by how the reason starts. A full
# Dependabot cap frees itself within the day, and filing it as a stall would send the
# devkit session the cap exists to save.
TRACKED = (
    "escalated",
    "backing off",
    "already dispatched",
    "held until the devkit session",
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


def stalled(
    history: list[dict], now: _dt.datetime, after: _dt.timedelta = STALL_AFTER
) -> list[fix_findings.Finding]:
    """A finding for each wait older than `after` that nothing else is tracking."""
    found = []
    for field in FIELDS:
        for name, (since, why) in sorted(streaks(history, field).items()):
            if tracked(why) or not _older(since, now, after):
                continue
            project = name.split(" ", 1)[0]
            detail = f"{field} since {since[:16]}: {name} -- {why}"
            found.append(fix_findings.Finding("stalled", project, detail))
    return found


def tracked(why: object) -> bool:
    """Whether a wait's reason is one some other part of the loop already owns."""
    return str(why).lower().startswith(TRACKED)


def _older(since: str, now: _dt.datetime, after: _dt.timedelta) -> bool:
    try:
        began = _dt.datetime.fromisoformat(since)
    except ValueError:
        return False
    if began.tzinfo is None:
        began = began.replace(tzinfo=_dt.UTC)
    return now - began >= after
