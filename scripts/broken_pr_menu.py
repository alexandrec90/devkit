#!/usr/bin/env python3
"""Which open PRs are broken: the scan behind `scripts/fix-prs.py`.

Cut out of `scripts/fix-prs.py`, whose `file_lines` had been recorded **five** times
(649, 660, 639, 769, 809 against a limit of 500), every record naming this same seam and
every one deferring it: the scan-and-menu half against the launch half.
`.claude/rules/engineering.md` makes a third consecutive raise a defect report, and
nothing had scheduled the split in eleven days.

**The menu half is gone, and the name outlived it on purpose.** This module drew the
rows of a two-stage quick-pick -- tick the checkouts, tick the PRs -- until the ticking
turned out to be the expensive part: eight consumers red on one devkit release were
eight ticked rows and eight sessions rediscovering one cause. `scripts/fix_plan.py` now
decides what gets a session, from the gate's own evidence, and the task asks only which
agent. What stays here is the scan itself -- what counts as broken, and every open PR of
every checkout at once -- and reading one PR live. The file keeps its name because
`tests/test_broken_pr_menu.py`, the untested-symbols baseline and the structure ratchet
all key on it, and a rename buys nothing a reader needs.

Stdlib plus this repo's own modules. Tested in `tests/test_broken_pr_menu.py`, with the
CLI that drives it in `tests/test_fix_prs.py`.
"""

from __future__ import annotations

import concurrent.futures as futures
import json
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devkit_project
import pr_mergeability as mergeability
import sweep

REPO_ROOT = Path(__file__).resolve().parents[1]

# How many open PRs to ask about per checkout. Well past what any of these repos carries
# at once; the cap is here so a runaway bot cannot turn one scan into a thousand rows.
PR_LIMIT = 50

# Ask for both conflict signals: `mergeable: CONFLICTING` and `mergeStateStatus: DIRTY`.
# `statusCheckRollup` is the gate half; `isDraft` is what a draft is excluded by.
# `baseRefName` and `headRefOid` are what the plan needs without a second `pr view`: the
# branch a fix merges into, and the commit the gate's evidence has to be read at.
PR_LIST_FIELDS = (
    "number,title,headRefName,baseRefName,headRefOid,updatedAt,url,isDraft,mergeable,"
    "mergeStateStatus,statusCheckRollup"
)

# The one state worth a tree; CLOSED and MERGED both delete the head branch. Rebound from
# `pr_mergeability` rather than spelled again: both modules turn on it, and GitHub's
# vocabulary -- including the three answers it gives about merging -- has one owner there.
OPEN = mergeability.OPEN

# Rollup conclusions that mean a check has failed rather than passed, is running, or was
# never required. `SKIPPED` and `NEUTRAL` are absent because both are how a correctly
# configured workflow reports "not applicable here".
FAILED_CONCLUSIONS = frozenset(
    {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE"}
)
# The same, for the legacy status-context shape `statusCheckRollup` still mixes in.
FAILED_STATES = frozenset({"FAILURE", "ERROR"})

# The same question asked of one PR at launch time, plus the base branch, which is what
# the agent has to merge in when the answer is a conflict, and `state`/`isDraft`, which
# the scan gets free from `--state open` and this half has to ask for: a closed PR keeps
# its last FAILURE in the rollup, so without them it still reads as broken and the run
# dies in `resume` on the head branch GitHub deleted when it closed.
PR_VIEW_FIELDS = (
    "number,title,headRefName,baseRefName,headRefOid,url,state,isDraft,mergeable,"
    "mergeStateStatus,statusCheckRollup"
)

# `<project>:<number>`, the one token `--picks` takes by hand. A checkout name cannot
# contain a colon (it is a directory name and a `COMPOSE_PROJECT_NAME`), so the first one
# always separates the halves.
PICK_SEP = ":"

# What joins several picks into one argument. A space: neither half can contain one.
PICK_LIST_SEP = " "

# How many checkouts `scan` asks about at once. Well above the registry's size, so the
# pool is bounded by the number of checkouts in practice; the ceiling is here so a
# workspace that grows to thirty repos does not open thirty `gh` processes at once.
SCAN_WORKERS = 8


class FixError(ValueError):
    """The request names a pick, a checkout or a PR this tool will not act on."""


# --- what counts as broken --------------------------------------------------------


def failing_checks(rollup: object) -> int:
    """How many entries of a `statusCheckRollup` have failed.

    Total over the shapes GitHub actually returns: a check run carries `conclusion` and
    a legacy status context carries `state`, and one rollup can hold both. Anything that
    is neither -- a null, a string, a shape a future API adds -- counts as zero rather
    than raising, because this decides whether a row appears in a dropdown and a menu
    that cannot be built is worse than a row that is merely wrong.
    """
    if not isinstance(rollup, list):
        return 0
    failed = 0
    for node in rollup:
        if not isinstance(node, dict):
            continue
        if str(node.get("conclusion") or "").upper() in FAILED_CONCLUSIONS:
            failed += 1
        elif str(node.get("state") or "").upper() in FAILED_STATES:
            failed += 1
    return failed


def broken_reason(pr: dict) -> str:
    """Why this PR is stuck, in the words the dropdown and the agent's prompt both use.

    Empty means "not broken", which is what every caller branches on -- so a draft is
    empty here rather than filtered somewhere else. A draft is not asking to be merged,
    and a repo that opens drafts as a matter of course (dependabot, an autofix sweep
    mid-gate) would otherwise fill this menu with rows nobody wants an agent sent at.
    """
    if not isinstance(pr, dict) or pr.get("isDraft"):
        return ""
    reasons = []
    if mergeability.conflicted(pr):
        reasons.append("merge conflict")
    failed = failing_checks(pr.get("statusCheckRollup"))
    if failed:
        reasons.append(f"{failed} check{'s' if failed != 1 else ''} failing")
    return " + ".join(reasons)


def settle_mergeability(project_dir: Path, entries: list[dict]) -> None:
    """`pr_mergeability.settle`, with this checkout's `gh pr view` as the ask.

    Named rather than spelled as a lambda at each call site, because both paths need the
    same binding: the scan re-asks about a page of rows, and the launch path re-asks
    about the single PR a person just ticked.
    """
    mergeability.settle(lambda number: pr_view(project_dir, number), entries)


def broken_prs(project_dir: Path, limit: int = PR_LIMIT) -> list[dict]:
    """The open PRs of one checkout that are broken, newest first. Empty on any failure.

    Empty rather than raising, on `preview-task.open_prs`'s terms: an offline or
    unauthenticated machine has to lose the rows and keep the menu. The failure this
    protects against is not hypothetical -- the scan runs from a scheduled reconcile
    pass, where a `gh` that cannot reach GitHub is an ordinary Tuesday.
    """
    try:
        result = sweep.gh_for(project_dir)(
            "pr", "list", "--state", "open", "--limit", str(limit), "--json", PR_LIST_FIELDS
        )
    except OSError:
        return []
    if result.returncode != 0:
        return []
    try:
        entries = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(entries, list):
        return []
    entries = [entry for entry in entries if isinstance(entry, dict)]
    settle_mergeability(project_dir, entries)
    return [entry for entry in entries if entry.get("state", OPEN) == OPEN and broken_reason(entry)]


def scan(workspace: Path, projects: list[str] | None = None) -> dict[str, list[dict]]:
    """Every checkout in the registry, and the broken PRs it has.

    Concurrent because a person is watching: this runs at the click rather than on a
    scheduled pass, and six serial `gh pr list` calls are five seconds of nothing. The
    calls share nothing and `broken_prs` is total, so a pool of them cannot fail
    differently from the loop it replaced -- only sooner.
    """
    text = workspace.read_text(encoding="utf-8")
    names = devkit_project.known_projects(text) if projects is None else projects
    root = workspace.parent
    if not names:
        return {}
    with futures.ThreadPoolExecutor(max_workers=min(SCAN_WORKERS, len(names))) as pool:
        found = pool.map(lambda name: broken_prs(root / name), names)
        return dict(zip(names, found, strict=True))


# --- reading a pick ---------------------------------------------------------------


@dataclass(frozen=True)
class Pick:
    """One ticked row, as the two halves of its token."""

    project: str
    number: int


def split_picks(text: str) -> list[str]:
    """The picked tokens, in the order given. Duplicates dropped."""
    return list(dict.fromkeys(token for token in str(text).split(PICK_LIST_SEP) if token))


def parse_pick(token: str) -> Pick:
    """`carameli:412` -> `Pick("carameli", 412)`.

    Raises for anything else, rather than skipping it: a malformed pick is a typo in a
    hand-written argument, and running the rest of a batch while silently dropping one
    is how a user ends up believing a PR was looked at.
    """
    project, _, tail = str(token).partition(PICK_SEP)
    if not project or not tail:
        raise FixError(f"cannot read the pick {token!r}; expected <project>{PICK_SEP}<number>")
    if not tail.isdigit():
        raise FixError(f"{token!r} does not name a PR number")
    return Pick(project, int(tail))


def pr_view(project_dir: Path, number: int) -> dict:
    """The PR as it is *now*, not as the menu last saw it. Empty on any failure.

    The picker scans live, but a gate can go green or a rebase clear the conflict while
    the person chooses. Read again at launch to avoid cutting an unnecessary worktree.
    The scan also uses this once for each PR whose mergeability is still unknown.
    """
    try:
        result = sweep.gh_for(project_dir)("pr", "view", str(number), "--json", PR_VIEW_FIELDS)
    except OSError:
        return {}
    if result.returncode != 0:
        return {}
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}
