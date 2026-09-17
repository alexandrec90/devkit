#!/usr/bin/env python3
"""Which open PRs are broken, and the rows the picker draws from them.

Cut out of `scripts/fix-prs.py`, whose `file_lines` had been recorded **five** times
(649, 660, 639, 769, 809 against a limit of 500), every record naming this same seam and
every one deferring it: the scan-and-menu half against the launch half.
`.claude/rules/engineering.md` makes a third consecutive raise a defect report, and
nothing had scheduled the split in eleven days.

**The entrypoint deliberately did not move.** Each earlier record read the seam the other
way round -- the menu becoming the new module -- and balked, because
`devkit_project.ACTIONS` names `scripts/fix-prs.py` for the live `--rows` picker and
moving it would change a command line the workspace task block spells by hand. Cutting
the *library* out instead leaves that path, that CLI and every one of its flags exactly
where they were, so the split needs no reversion check against the dropdown at all. That
is why this file is named for what it holds rather than for the task it serves.

The token format lives here whole: `pick_value` writes it and `parse_pick` reads it, and
a picker whose two ends can disagree about its own encoding is the bug that separating
them would invite. What stays in `fix-prs.py` is everything that acts -- reading one PR,
writing the agent's prompt, cutting the tree, opening the session.

Stdlib plus this repo's own modules. Tested in `tests/test_broken_pr_menu.py`, with the
end-to-end picker behaviour still in `tests/test_fix_prs.py`.
"""

from __future__ import annotations

import concurrent.futures as futures
import datetime as _dt
import json
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devkit_project
import picker_rows
import picker_scan
import pr_mergeability as mergeability
import sweep

REPO_ROOT = Path(__file__).resolve().parents[1]

# What `picker_scan` files this task's stage-one write under. One name per picker, so
# two tasks scanning at once cannot read each other's rows.
SCAN_NAME = "fix-prs"

# How many open PRs to ask about per checkout. Well past what any of these repos carries
# at once; the cap is here so a runaway bot cannot turn one dropdown into a thousand.
PR_LIMIT = 50

# Ask for both conflict signals: `mergeable: CONFLICTING` and `mergeStateStatus: DIRTY`.
# `statusCheckRollup` is the gate half; `isDraft` is what a draft is excluded by.
PR_LIST_FIELDS = (
    "number,title,headRefName,updatedAt,url,isDraft,mergeable,mergeStateStatus,statusCheckRollup"
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

# `<project>:<number>`, one token because a VS Code input resolves to one string. A
# checkout name cannot contain a colon (it is a directory name and a
# `COMPOSE_PROJECT_NAME`), so the first one always separates the halves.
# ...and the same question asked of one PR at launch time, plus the base branch, which is
# what the agent has to merge in when the answer is a conflict, and `state`/`isDraft`,
# which the scan gets free from `--state open` and this half has to ask for: a closed PR
# keeps its last FAILURE in the rollup, so without them it still reads as broken and the
# run dies in `resume` on the head branch GitHub deleted when it closed.
PR_VIEW_FIELDS = "number,title,headRefName,baseRefName,url,state,isDraft,mergeable,mergeStateStatus,statusCheckRollup"

PICK_SEP = ":"

# What joins several ticked rows into that one string. A space, matching `previewRow`
# and chosen on the same terms: neither half can contain one.
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


# --- the rows the picker draws ----------------------------------------------------


def age(stamp: str, now: _dt.datetime | None = None) -> str:
    """`2026-09-04T10:11:12Z` -> `3h ago`. `?` when the stamp cannot be read.

    Coarse on purpose: the reader is deciding which of four red PRs to look at, and
    minutes past the first hour are not part of that decision.
    """
    try:
        moment = _dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return "?"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_dt.UTC)
    delta = (now or _dt.datetime.now(_dt.UTC)) - moment
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return "just now"
    if hours < 24:
        return f"{int(hours)}h ago"
    return f"{int(hours // 24)}d ago"


def pick_value(project: str, number: object) -> str:
    """The one token a ticked row resolves to."""
    return f"{project}{PICK_SEP}{number}"


def menu_row(project: str, pr: dict, now: _dt.datetime | None = None) -> str:
    """One quick-pick line for a broken PR.

    The checkout is in the description rather than the label because the list is flat --
    `shellCommand.execute` resolves one input per command, and a "which checkout, then
    which of its PRs" pair would be two, which VS Code gives no sight of each other. One
    scan across every checkout was always the question this task asked; the two-stage
    picker was how a *file* keyed its rows, not what a reader wanted.
    """
    number = pr.get("number", "?")
    return picker_rows.row(
        pick_value(project, pr.get("number", "")),
        f"#{number} {pr.get('headRefName', '')}",
        f"{project} -- {broken_reason(pr)} -- {age(str(pr.get('updatedAt', '')), now)}",
        pr.get("title", ""),
    )


def picked_rows(workspace: Path, checkouts: str, now: _dt.datetime | None = None) -> list[str]:
    """Stage two: the rows for the ticked checkouts, from stage one's scan if it is theirs.

    The token decides, and a miss is answered by scanning rather than by serving
    anything older -- see `picker_scan`. The rescan covers only what was ticked, so the
    fallback costs less than the scan stage one already did.

    An empty `checkouts` means the chain did not run at all, which is a person calling
    `--rows` by hand: that answers with the whole machine, the way it did before there
    was a first stage.
    """
    projects, token = picker_scan.parse_projects(checkouts)
    if not projects:
        return rows(scan(workspace), now)
    cached = picker_scan.read(SCAN_NAME, token)
    if cached is not None:
        return picker_scan.select(cached, projects) or [placeholder_row()]
    return rows(scan(workspace, projects), now)


def strayed_picks(picks: list[Pick], checkouts: str) -> list[str]:
    """Ticked PRs whose checkout was not ticked in the first stage.

    Nothing in the two stages can produce one: stage two draws only the checkouts stage
    one returned. So a stray is evidence the chain itself misfired -- the extension
    resolves `${input:...}` against a value it recorded when that input last ran, so an
    input order that stopped putting the checkout stage first would quietly filter by
    the *previous* click's checkouts. That is the one failure mode of this design that
    could be silent, and this is what makes it loud.

    Empty `checkouts` returns nothing, because a hand-typed `--picks` has no first stage
    to disagree with.
    """
    ticked, _ = picker_scan.parse_projects(checkouts)
    if not ticked:
        return []
    return sorted({pick.project for pick in picks} - set(ticked))


def stray_report(strayed: list[str]) -> str:
    """What to print when a pick names a checkout the first stage did not."""
    return (
        f"ticked {'a PR' if len(strayed) == 1 else 'PRs'} from {', '.join(strayed)}, which the "
        "checkout picker did not return -- the two picker stages disagree, so nothing was run. "
        "See `.claude/rules/vscode-tasks.md` on the order the inputs have to appear in."
    )


def placeholder_row() -> str:
    """The row a scan that found nothing draws. See `picker_rows.nothing_row`."""
    return picker_rows.nothing_row(
        "nothing broken", "every open PR on this machine is green, or a draft"
    )


def listed(found: dict[str, list[dict]]) -> list[tuple[str, dict]]:
    """Every broken PR as `(checkout, pr)`, most recently touched first.

    One ranking, named once, because two callers depend on it being the same one:
    `rows` prints it and `scan_entries` records it for the second stage to filter. A
    stage two that filtered a differently-ranked list would draw the right PRs in the
    wrong order, which is the kind of wrong nobody reports.
    """
    pairs = [(project, pr) for project, prs in found.items() for pr in prs]
    pairs.sort(key=lambda pair: str(pair[1].get("updatedAt", "")), reverse=True)
    return pairs


def rows(found: dict[str, list[dict]], now: _dt.datetime | None = None) -> list[str]:
    """Every broken PR in `found` as a quick-pick line, most recently touched first.

    Newest first rather than grouped by checkout, and that survived the checkout stage
    coming back: whoever ticked three checkouts is choosing which red PR to send a
    session at, not re-sorting them by repo. The checkout stays on every row because
    `found` can hold several.
    """
    return [menu_row(project, pr, now) for project, pr in listed(found)] or [placeholder_row()]


def scan_entries(
    found: dict[str, list[dict]], now: _dt.datetime | None = None
) -> list[tuple[str, str]]:
    """What stage one hands stage two: every row, tagged with its checkout, in rank.

    The rendered rows rather than the PRs, because `menu_row` has already made every
    decision stage two would otherwise make again -- a second stage that re-renders is
    a second place for the format to drift. Built through `listed` so the order is the
    one `rows` prints, which is the order `picker_scan.select` then preserves.
    """
    return [(project, menu_row(project, pr, now)) for project, pr in listed(found)]


def project_rows(found: dict[str, list[dict]], token: str) -> list[str]:
    """Stage one: one row per checkout, saying how much red is in it.

    A checkout with nothing broken is listed rather than dropped, and this is the whole
    argument for stage one costing a full scan instead of just reading the registry.
    "devkit -- nothing broken" is an answer; a menu that silently omits devkit is
    indistinguishable from one that could not reach it, and a reader who ticks a
    checkout to find it empty has paid a click to learn what the scan already knew.
    """
    if not found:
        return [
            picker_rows.nothing_row(
                "no checkouts", "the workspace registry named nothing that could be scanned"
            )
        ]
    listed = []
    for project in sorted(found, key=lambda name: (-len(found[name]), name)):
        count = len(found[project])
        listed.append(
            picker_scan.project_row(
                project,
                token,
                f"{count} broken PR{'' if count == 1 else 's'}" if count else "nothing broken",
                "tick as many checkouts as you want -- the next list covers all of them",
            )
        )
    return listed


def scan(workspace: Path, projects: list[str] | None = None) -> dict[str, list[dict]]:
    """Every checkout in the registry, and the broken PRs it has.

    Concurrent because a person is watching: this now runs when the picker opens rather
    than on a scheduled pass, and six serial `gh pr list` calls are five seconds of empty
    quick-pick. The calls share nothing and `broken_prs` is total, so a pool of them
    cannot fail differently from the loop it replaced -- only sooner.
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
    """The ticked tokens, in the order the extension joined them. Duplicates dropped."""
    return list(dict.fromkeys(token for token in str(text).split(PICK_LIST_SEP) if token))


def parse_pick(token: str) -> Pick | None:
    """`carameli:412` -> `Pick("carameli", 412)`. None for the `nothing broken` row.

    Raises for a token that is neither, rather than skipping it: a malformed pick means
    the menu file and this parser disagree, and running the rest of a batch while
    silently dropping one is how a user ends up believing a PR was looked at.
    """
    # Ahead of the split, because the sentinel is a bare word: `picker_rows.NOTHING`
    # carries no `PICK_SEP` and would otherwise read as a project with no number. The
    # `<project>:none` spelling below is what the cached menu wrote, kept because a
    # remembered pick from before that change must still mean "nothing" rather than
    # raise at a person who clicked the row that said so.
    if str(token) == picker_rows.NOTHING:
        return None
    project, _, tail = str(token).partition(PICK_SEP)
    if not project or not tail:
        raise FixError(f"cannot read the pick {token!r}; expected <project>{PICK_SEP}<number>")
    if tail == picker_rows.NOTHING:
        return None
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
