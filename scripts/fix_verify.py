#!/usr/bin/env python3
"""Hold a harness-ledger resolution to the fix it names: reopen it when that never lands.

A group is retired by a `triage-resolved` event the session writes once the fix is in
its intent -- before any PR exists, so the note names a branch. Nothing checked that
the branch ever became a merged PR. A fix left in a tree the pass could not ship, or a
PR closed unmerged, retired its group for good: the one silent way off the backlog
this ledger was built to refuse.

So each pass reads the resolutions of the last `WINDOW` that name a PR or a branch and
asks GitHub what became of it. Merged: settled, and cached so it is never asked again --
and the group's rows filed while that fix was in flight are retired against it, since
they were the defect waiting on the merge, not a recurrence (`Outcome.covered`).
Closed unmerged, or no PR at all from that branch after `UNLANDED_AFTER`: reopened
(`harness_triage.reopen`), with why -- unless a PR merged since names the group, which
is the fix gone out under another name (`named_by`). Still open: left alone -- an open PR is in flight,
and `fix_stall` owns what sits too long. A resolution naming nothing (`pr=-`) is a
not-a-defect verdict, which has no fix to land.

Tested in `tests/test_fix_verify.py`.
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_triage as triage

# What a resolution in flight *is* lives in `harness_triage`, below this module, because
# the triage CLI shows those groups as pending too and importing this module from there
# would be a cycle. Re-exported so every caller keeps one place to reach it by.
WINDOW = triage.VERIFY_WINDOW
CACHE_NAME = triage.VERIFIED_CACHE_NAME
Resolution = triage.Resolution
target = triage.target
recent = triage.recent
in_flight = triage.in_flight
_within = triage.within
_load = triage.load_settled

UNLANDED_AFTER = _dt.timedelta(days=2)

LANDED = "MERGED"
CLOSED = "CLOSED"
OPEN = "OPEN"


@dataclass(frozen=True)
class Pr:
    """One PR a resolution's `pr=` matched: its state, and when and where it merged."""

    state: str
    merged_at: str = ""
    url: str = ""


PR_FIELDS = "state,mergedAt,url"

# `(where, what)` -> every PR it matches: `where` a project name or "" for every
# project, `what` a number or a head branch. The IO half, injected.
Lookup = Callable[[str, str], list[Pr]]

# A group id -> every merged PR, in any project, whose text names it. Asked only of a
# resolution about to be reopened (`named_by`).
Mentions = Callable[[str], list[Pr]]


@dataclass
class Outcome:
    """What one `verify` found: resolutions to reopen, and rows its merges retire.

    `covered` is `(row id, note, pr)` for each open row of a settled group that was
    filed while that group's fix was in flight -- after the resolution, before the
    merge. Such a row is the defect still on the default branch while the fix waited,
    which `pending_groups` held back for exactly that reason; left open past the merge
    it stopped being pending and read as `RECURRED ... that fix did not hold`, and the
    pass sent a session to re-prove a fix that had held (7a94f5bc).
    """

    reopen: list[tuple[str, str]] = field(default_factory=list)
    covered: list[tuple[str, str, str]] = field(default_factory=list)
    # `(resolution, the merged PR that holds its fix)` for each whose `pr=` never landed
    # but whose group a PR merged since names -- see `named_by`.
    found: list[tuple[Resolution, Pr]] = field(default_factory=list)


def _moment(stamp: str) -> _dt.datetime | None:
    """A ledger stamp or a `gh` `mergedAt` as an aware time; None when unreadable."""
    try:
        when = _dt.datetime.fromisoformat(stamp.strip())
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=_dt.UTC)


def relevant(resolution: Resolution, prs: list[Pr]) -> list[Pr]:
    """The PRs that can hold the fix `resolution` names.

    A branch name is not unique over time: the pass cuts `agent/fix-harness-ledger-0927`
    again the day after its first PR merged, so a PR from that name that merged
    *before* the resolution was written predates the fix and says nothing about it --
    counting it settled a fix the instant it was written. A number names one PR, which
    may legitimately have merged first ("already fixed by #430").
    """
    if target(resolution.pr)[0]:
        return prs
    written = _moment(resolution.stamp)
    return [
        pr
        for pr in prs
        if pr.state != LANDED
        or written is None
        or (merged := _moment(pr.merged_at)) is None
        or merged >= written
    ]


def judge(resolution: Resolution, prs: list[Pr], now: _dt.datetime) -> str:
    """Why this resolution should be reopened, "" when it stands; `LANDED` when settled."""
    states = {pr.state for pr in relevant(resolution, prs)}
    if LANDED in states:
        return LANDED
    if OPEN in states:
        return ""
    if CLOSED in states:
        return f"the PR it names ({resolution.pr}) was closed without merging"
    if _within(resolution.stamp, now, UNLANDED_AFTER):
        return ""
    return (
        f"no PR from {resolution.pr} opened or merged after this resolution, "
        f"{UNLANDED_AFTER.days} days on (if an earlier PR from it is the fix, "
        "resolve again naming that PR's number)"
    )


def landed(resolution: Resolution, prs: list[Pr]) -> Pr | None:
    """The first relevant merge: the moment the fix reached the default branch."""
    merged = [
        (when, pr)
        for pr in relevant(resolution, prs)
        if pr.state == LANDED and (when := _moment(pr.merged_at)) is not None
    ]
    return min(merged, key=lambda pair: pair[0])[1] if merged else None


def named_by(resolution: Resolution, prs: list[Pr]) -> Pr | None:
    """The first PR merged since `resolution` was made out of `prs`, those naming its group.

    A resolution's `pr=` can name the wrong branch while the fix merges anyway: 950c4a96
    named `agent/fix-harness-ledger-0927-3`, whose PR had merged that morning, while its
    fix went out from `-0927-19` as #440 -- whose body names the group, as a sweep's
    does. Reopened on the branch alone, the group sent a fixer to find #440 by hand. A PR
    merged before the resolution was written predates the fix, so it is never the one.
    """
    written = _moment(resolution.stamp)
    if written is None:
        return None
    merged = [
        (when, pr)
        for pr in prs
        if pr.state == LANDED and (when := _moment(pr.merged_at)) is not None and when >= written
    ]
    return min(merged, key=lambda pair: pair[0])[1] if merged else None


def covered(
    items: list[triage.Item], resolution: Resolution, merge: Pr
) -> list[tuple[str, str, str]]:
    """`(row id, note, pr)` for each open row of `resolution`'s group filed after the
    resolution whose run began before `merge` -- see `Outcome.covered`. A row whose run
    began after the merge is a real recurrence and stays open; so does every row when
    either time is unreadable. A row with no readable `started=` (`ran_from`) began
    when it was filed, as far as anything can tell."""
    head = {item.id: item for item in items}.get(resolution.ref)
    start, end = _moment(resolution.stamp), _moment(merge.merged_at)
    if head is None or start is None or end is None:
        return []
    note = (
        f"filed while the fix for [{resolution.ref}] ({resolution.pr}) was in flight; "
        f"it merged at {merge.merged_at}, after this row's run began"
    )
    found = []
    for item in triage.open_items(items):
        when, began = _moment(item.stamp), ran_from(item)
        if item.signature != head.signature or when is None or began is None:
            continue
        if start < when and began < end:
            found.append((item.id, note, merge.url or resolution.pr))
    return found


def ran_from(item: triage.Item) -> _dt.datetime | None:
    """When the run a row reports began: its `started=` (`log-wrap.record_failure`), else
    its stamp. A scheduled job ran the code on disk when it started, so a run spanning a
    merge failed on the code before it -- 1fad5675, a scrape begun four minutes before
    social-scraper #68 merged and filed nine minutes after."""
    return _moment(item.fields.get("started", "")) or _moment(item.stamp)


def verify(
    items: list[triage.Item],
    lookup: Lookup,
    cache_path: Path,
    now: _dt.datetime,
    mentions: Mentions | None = None,
) -> Outcome:
    """The resolutions to reopen and the rows a merge retires; the settled are cached.

    A covered row is cached as settled too: the fix its resolution names merged by
    construction, so it is never in flight and never asked about. Before a resolution
    is reopened, `mentions` is asked for a merged PR naming its group (`named_by`); one
    found settles it instead, and is in `Outcome.found` for the ledger to name.
    """
    settled = _load(cache_path)
    outcome = Outcome()
    for resolution in recent(items, now):
        if resolution.ref in settled:
            continue
        project, what = target(resolution.pr)
        prs = lookup(project, what)
        verdict = judge(resolution, prs, now)
        if verdict not in ("", LANDED) and mentions is not None:
            if fix := named_by(resolution, mentions(resolution.ref)):
                outcome.found.append((resolution, fix))
                prs, verdict = [fix], LANDED
        if verdict == LANDED:
            settled.add(resolution.ref)
            if merge := landed(resolution, prs):
                outcome.covered += covered(items, resolution, merge)
        elif verdict:
            outcome.reopen.append((resolution.ref, verdict))
    settled.update(row for row, _, _ in outcome.covered)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(sorted(settled)) + "\n", encoding="utf-8")
    return outcome


def gh_lookup(root: Path, projects: list[str], gh_for) -> Lookup:
    """The real `Lookup`: `gh pr view` for a number, `gh pr list --head` for a branch.

    A `gh` that cannot answer answers nothing, which `judge` reads as "no PR yet" --
    so an outage can at worst reopen a group after `UNLANDED_AFTER`, never retire one.
    """

    def prs(project: str, what: str) -> list[Pr]:
        where = [project] if project else projects
        found: list[Pr] = []
        for name in where:
            if not (root / name).is_dir():
                continue
            gh = gh_for(root / name)
            if what.isdigit():
                answer: object = [_json(gh("pr", "view", what, "--json", PR_FIELDS))]
            else:
                answer = _json(
                    gh("pr", "list", "--head", what, "--state", "all", "--json", PR_FIELDS)
                )
            found += _rows(answer)
        return found

    return prs


def gh_mentions(root: Path, projects: list[str], gh_for) -> Mentions:
    """The real `Mentions`: GitHub's search for the id over every project's merged PRs.

    The same outage rule as `gh_lookup`: no answer finds nothing, and the reopen stands.
    """

    def prs(ref: str) -> list[Pr]:
        found: list[Pr] = []
        for name in projects:
            if (root / name).is_dir():
                gh = gh_for(root / name)
                found += _rows(
                    _json(
                        gh("pr", "list", "--state", "merged", "--search", ref, "--json", PR_FIELDS)
                    )
                )
        return found

    return prs


def _rows(answer: object) -> list[Pr]:
    rows = answer if isinstance(answer, list) else []
    return [
        Pr(
            str(row.get("state", "")).upper(),
            str(row.get("mergedAt") or ""),
            str(row.get("url") or ""),
        )
        for row in rows
        if isinstance(row, dict)
    ]


def _json(done: object) -> object:
    if getattr(done, "returncode", 1) != 0:
        return []
    try:
        return json.loads(getattr(done, "stdout", "") or "[]")
    except ValueError:
        return []
