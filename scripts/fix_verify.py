#!/usr/bin/env python3
"""Hold a harness-ledger resolution to the fix it names: reopen it when that never lands.

A group is retired by a `triage-resolved` event the session writes once the fix is in
its intent -- before any PR exists, so the note names a branch. Nothing checked that
the branch ever became a merged PR. A fix left in a tree the pass could not ship, or a
PR closed unmerged, retired its group for good: the one silent way off the backlog
this ledger was built to refuse.

So each pass reads the resolutions of the last `WINDOW` that name a PR or a branch and
asks GitHub what became of it. Merged: settled, and cached so it is never asked again.
Closed unmerged, or no PR at all from that branch after `UNLANDED_AFTER`: reopened
(`harness_triage.reopen`), with why. Still open: left alone -- an open PR is in flight,
and `fix_stall` owns what sits too long. A resolution naming nothing (`pr=-`) is a
not-a-defect verdict, which has no fix to land.

Tested in `tests/test_fix_verify.py`.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_triage as triage

WINDOW = _dt.timedelta(days=14)
UNLANDED_AFTER = _dt.timedelta(days=2)
CACHE_NAME = "triage-verified.json"

LANDED = "MERGED"
CLOSED = "CLOSED"
OPEN = "OPEN"

PR_URL = re.compile(r"github\.com/[^/\s]+/(?P<repo>[^/\s]+)/pull/(?P<number>\d+)")
PR_NUMBER = re.compile(r"^#?(?P<number>\d+)$")
BRANCH = re.compile(r"^[\w.-]+(?:/[\w.-]+)+$")

# `(where, what)` -> the states of every PR it matches: `where` a project name or "" for
# every project, `what` a number or a head branch. The IO half, injected.
Lookup = Callable[[str, str], list[str]]


@dataclass(frozen=True)
class Resolution:
    ref: str
    stamp: str
    pr: str
    note: str


def target(pr: str) -> tuple[str, str]:
    """`(project, number-or-branch)` a resolution's `pr=` names; `("", "")` for none.

    A bare number is a devkit PR, the ledger being devkit's. A URL names its repo. A
    value shaped like a branch (`agent/fix-x-0919`) is looked up in every project.
    """
    text = pr.strip().rstrip(".,;")
    if found := PR_URL.search(text):
        return found["repo"], found["number"]
    if found := PR_NUMBER.match(text):
        return "devkit", found["number"]
    if BRANCH.match(text):
        return "", text
    return "", ""


def recent(
    items: list[triage.Item], now: _dt.datetime, window: _dt.timedelta = WINDOW
) -> list[Resolution]:
    """Resolutions that still stand, from inside `window`, that name something to check."""
    standing = triage.verdicts(items)
    found = []
    for item in items:
        ref = item.fields.get("ref", "")
        if item.event != triage.RESOLVED_EVENT or standing.get(ref) != (item.event, item.stamp):
            continue
        if not target(item.fields.get("pr", ""))[1] or not _within(item.stamp, now, window):
            continue
        found.append(
            Resolution(ref, item.stamp, item.fields.get("pr", ""), item.fields.get("note", ""))
        )
    return found


def _within(stamp: str, now: _dt.datetime, window: _dt.timedelta) -> bool:
    try:
        return now - _dt.datetime.fromisoformat(stamp) <= window
    except ValueError:
        return False


def judge(resolution: Resolution, states: list[str], now: _dt.datetime) -> str:
    """Why this resolution should be reopened, "" when it stands; `LANDED` when settled."""
    if LANDED in states:
        return LANDED
    if OPEN in states:
        return ""
    if CLOSED in states:
        return f"the PR it names ({resolution.pr}) was closed without merging"
    if _within(resolution.stamp, now, UNLANDED_AFTER):
        return ""
    return f"no PR was ever opened from {resolution.pr}, {UNLANDED_AFTER.days} days on"


def verify(
    items: list[triage.Item],
    lookup: Lookup,
    cache_path: Path,
    now: _dt.datetime,
) -> list[tuple[str, str]]:
    """`(ref, why)` for every resolution to reopen; the settled ones are cached."""
    settled = _load(cache_path)
    reopen: list[tuple[str, str]] = []
    for resolution in recent(items, now):
        if resolution.ref in settled:
            continue
        project, what = target(resolution.pr)
        verdict = judge(resolution, lookup(project, what), now)
        if verdict == LANDED:
            settled.add(resolution.ref)
        elif verdict:
            reopen.append((resolution.ref, verdict))
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(sorted(settled)) + "\n", encoding="utf-8")
    return reopen


def _load(path: Path) -> set[str]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {str(ref) for ref in loaded} if isinstance(loaded, list) else set()


def gh_lookup(root: Path, projects: list[str], gh_for) -> Lookup:
    """The real `Lookup`: `gh pr view` for a number, `gh pr list --head` for a branch.

    A `gh` that cannot answer answers nothing, which `judge` reads as "no PR yet" --
    so an outage can at worst reopen a group after `UNLANDED_AFTER`, never retire one.
    """

    def states(project: str, what: str) -> list[str]:
        where = [project] if project else projects
        found: list[str] = []
        for name in where:
            if not (root / name).is_dir():
                continue
            gh = gh_for(root / name)
            if what.isdigit():
                answer: object = [_json(gh("pr", "view", what, "--json", "state"))]
            else:
                answer = _json(
                    gh("pr", "list", "--head", what, "--state", "all", "--json", "state")
                )
            rows = answer if isinstance(answer, list) else []
            found += [str(row.get("state", "")).upper() for row in rows if isinstance(row, dict)]
        return found

    return states


def _json(done: object) -> object:
    if getattr(done, "returncode", 1) != 0:
        return []
    try:
        return json.loads(getattr(done, "stdout", "") or "[]")
    except ValueError:
        return []
