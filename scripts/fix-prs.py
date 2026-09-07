#!/usr/bin/env python3
"""Send an agent at the PRs that are already red, one worktree per PR.

A PR goes red two ways and both of them wait for a person: `origin/<default>` moved
under it (`mergeable: CONFLICTING`), or its gate failed. Neither is work anybody wants
to do by hand, and neither is work the scheduled tier will ever do -- `worktree.py
reconcile` merges only what is *green* and carries the merge label, so a red PR is
precisely the state it steps over every quarter hour, forever.

**The unit of work is one PR in one worktree on that PR's own head branch.** Not a new
branch: the fix belongs on the branch under review, so the worktree is cut on the head
branch with `origin/<head>` as its upstream and a bare push lands where the PR is
looking. That is also this repo's answer to "is there a CLI flag that attaches an agent
to a PR branch": Claude Code's `--from-pr` *resumes a session linked to a PR*, which
needs that session to still exist on this machine. Cutting the worktree is the spelling
that works on a PR nobody has touched this week.

**The worktree is a `.claude/worktrees/` one, not a box, and that is the whole of where
this tool puts things.** Every worktree on this machine lives under a checkout's
`.claude/worktrees/`, which is where `claude --worktree` cuts, where a remote session
spawns, and what `agent-worktree.py` lists and removes -- so a PR fixed from here is
visible to the same two dropdowns as everything else, and reachable by the same delete
row. `scripts/agent_worktrees.py` owns the three decisions that takes (`holder`,
`tree_name`, `add_steps`); what is here is the PR half. The box tier -- `worktree.py`,
a port lease, a `COMPOSE_PROJECT_NAME`, a provisioned toolchain and a reaper -- is still
`agent-box.py spawn`'s, for a session that runs a compose stack, and this task no longer
cuts one.

**Three agent modes, and the third one is an asymmetry rather than an omission.**
`claude` and `codex` each open a Windows Terminal tab, the same one `agent-box.py`
opens; `claude-bg` is `claude --bg`, which returns an id immediately and is read back
with `claude attach` / `claude logs`. There is no `codex-bg` row because Codex has no
background session: `codex exec` is non-interactive but streams to the terminal it was
started in and hands back nothing to attach to. Offering a row per agent per mode would
have made that difference silent; three rows makes it visible in the dropdown.

**The menu is live, and that is a change of writer rather than of shape.** It used to be
a JSON file rebuilt every fifteen minutes by `worktree.reconcile`, because
`rioj7.command-variable` reads a file and cannot run a command -- so the rows were stale
by construction, and stale in the one direction that costs: a PR closed since the scan
still drew a row, and clicking it sent `resume` at a head branch GitHub had deleted.
`--rows` is that scan with no file under it, run by `shellCommand.execute` at the moment
the picker opens. `run_one` still re-reads the PR it was handed, because a scan of six
checkouts is seconds of quick-pick and a person then reads the list.

Every function that decides something is pure and tested in `tests/test_fix_prs.py`;
the ones that spawn take a runner.
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import datetime as _dt
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "precommit"))
import agent_worktrees as aw
import devkit_project
import picker_rows
import picker_scan
import sweep
import task_input
import worktree

# `agent-box.py` is hyphenated, so it cannot be a plain import. Loaded by path for the
# one thing worth sharing rather than copying: how a tab's command line is built and
# which window it lands in. `worktree` above is imported normally on purpose -- see the
# note on the same pair of inserts in `agent-box.py`.
from _loader import load_by_path

REPO_ROOT = Path(__file__).resolve().parents[1]

agent_box = load_by_path("agent_box", REPO_ROOT / "scripts" / "agent-box.py")

# `<project>:<number>`, one token because a VS Code input resolves to one string. A
# checkout name cannot contain a colon (it is a directory name and a
# `COMPOSE_PROJECT_NAME`), so the first one always separates the halves.
PICK_SEP = ":"

# What joins several ticked rows into that one string. A space, matching `previewRow`
# and chosen on the same terms: neither half can contain one.
PICK_LIST_SEP = " "

# How many checkouts `scan` asks about at once. Well above the registry's size, so the
# pool is bounded by the number of checkouts in practice; the ceiling is here so a
# workspace that grows to thirty repos does not open thirty `gh` processes at once.
SCAN_WORKERS = 8

# What `picker_scan` files this task's stage-one write under. One name per picker, so
# two tasks scanning at once cannot read each other's rows.
SCAN_NAME = "fix-prs"

# How many open PRs to ask about per checkout. Well past what any of these repos carries
# at once; the cap is here so a runaway bot cannot turn one dropdown into a thousand.
PR_LIMIT = 50

# `gh pr list` fields. `mergeable` is the conflict half and `statusCheckRollup` the gate
# half; `isDraft` is what a draft is excluded by.
PR_LIST_FIELDS = "number,title,headRefName,updatedAt,url,isDraft,mergeable,statusCheckRollup"

# ...and the same question asked of one PR at launch time, plus the base branch, which is
# what the agent has to merge in when the answer is a conflict, and `state`/`isDraft`,
# which the scan gets free from `--state open` and this half has to ask for: a closed PR
# keeps its last FAILURE in the rollup, so without them it still reads as broken and the
# run dies in `resume` on the head branch GitHub deleted when it closed.
PR_VIEW_FIELDS = (
    "number,title,headRefName,baseRefName,url,state,isDraft,mergeable,statusCheckRollup"
)
OPEN = "OPEN"  # the one state worth a tree; CLOSED and MERGED both delete the head branch

# How GitHub says the branch no longer merges cleanly. `UNKNOWN` is its answer while the
# mergeability job is still running, and is deliberately NOT treated as a conflict: a PR
# opened seconds ago reports it, and a menu that called those broken would offer every
# fresh PR on the machine.
CONFLICTING = "CONFLICTING"

# Rollup conclusions that mean a check has failed rather than passed, is running, or was
# never required. `SKIPPED` and `NEUTRAL` are absent because both are how a correctly
# configured workflow reports "not applicable here".
FAILED_CONCLUSIONS = frozenset(
    {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE"}
)
# The same, for the legacy status-context shape `statusCheckRollup` still mixes in.
FAILED_STATES = frozenset({"FAILURE", "ERROR"})

# The agent modes the picker offers. The value is what reaches `--agent`; the mapping is
# to how the session is opened, which is the whole of the difference between them.
TAB = "tab"  # a Windows Terminal tab, watched by whoever clicked
BACKGROUND = "bg"  # `claude --bg`, read back with `claude attach` / `claude logs`
AGENT_MODES: dict[str, tuple[str, str]] = {
    "claude": ("claude", TAB),
    "claude-bg": ("claude", BACKGROUND),
    "codex": ("codex", TAB),
}

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2


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
    if str(pr.get("mergeable") or "").upper() == CONFLICTING:
        reasons.append("merge conflict")
    failed = failing_checks(pr.get("statusCheckRollup"))
    if failed:
        reasons.append(f"{failed} check{'s' if failed != 1 else ''} failing")
    return " + ".join(reasons)


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
    return [entry for entry in entries if isinstance(entry, dict) and broken_reason(entry)]


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

    The menu is up to a quarter of an hour old, which is long enough for the gate to have
    gone green or for a rebase to have cleared the conflict. What the agent is told has
    to be current, so this is read at launch time -- and it is also the check that stops
    a worktree being cut for a PR that no longer needs one.
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


# --- what the agent is told -------------------------------------------------------


def tab_safe(text: str) -> str:
    """One line -- what a `wt` command line cannot carry at all.

    A newline ends `wt`'s command outright, and there is no escape for one, so the
    prompt is flattened rather than quoted. Semicolons are *not* touched here:
    `agent_box.wt_argv` escapes them for every string that reaches a tab, which it has
    to do anyway for the kill switch's own `;` that this function can never see, and two
    owners for one hazard is how the prefix went unescaped in the first place.
    """
    return " ".join(str(text).split())


def seed_prompt(project: str, pr: dict, reason: str) -> str:
    """The opening instruction the agent's session starts with.

    It names the PR, what is wrong with it *now*, and the finish line -- because a
    session opened with no prompt starts by rediscovering all three, and this task exists
    to skip exactly that. The merge is stated as a condition rather than an instruction
    (`once the gate is green`) so the agent that cannot get there reports instead of
    forcing: `--admin` is not in anybody's prompt here.
    """
    number = pr.get("number", "?")
    base = pr.get("baseRefName", "the base branch")
    head = pr.get("headRefName", "its head branch")
    return tab_safe(
        f"PR #{number} in {project} is stuck: {reason}. "
        f"This worktree is checked out on the PR head branch {head} with its upstream set, "
        f"so a bare git push lands on the PR. "
        f"Merge origin/{base} in, fix what the gate is failing on, run the targeted "
        f"tests and the linter, push, and then merge the PR once the gate is green. "
        f"If it cannot be made green, stop and say what is in the way."
    )


# --- opening the session ----------------------------------------------------------


def existing_tree(project_dir: Path, branch: str) -> tuple[Path | None, str]:
    """The worktree already on `branch`, or why one cannot be cut. See `aw.holder`.

    Three answers in two fields, because they need three different next moves.
    `(path, "")` is one of this checkout's own `.claude/worktrees/` and is reused as it
    stands: this task's ordinary second click is on a PR whose worktree is still open
    from the first, and two worktrees on one branch is a state git will not hold, so
    cutting again would fail on the very thing that means "ready". `(None, "")` is a
    branch nothing holds, which is the case `cut_tree` exists for. `(None, why)` is a
    branch held somewhere this tool does not own -- the checkout itself, a `.worktrees/`
    box, a worktree cut by hand -- where the honest answer is the sentence naming the
    directory, not a `git worktree add` that fails talking about the branch instead.
    """
    listed = sweep.git_for(project_dir)("worktree", "list", "--porcelain")
    if listed.returncode != 0:
        return None, f"git could not list the worktrees of {project_dir}"
    held, nested = aw.holder(project_dir, listed.stdout, branch)
    if not held:
        return None, ""
    if not nested:
        return None, (
            f"{branch} is already checked out at {held}, which is outside "
            f"{aw.WORKTREES_DIR} -- finish the PR from there, or remove that worktree"
        )
    return Path(held), ""


def cut_tree(project_dir: Path, branch: str, runner=subprocess.run) -> Path | None:
    """Cut `.claude/worktrees/<name>` on the PR's own head branch. None when git refused.

    The fetch first is `agent-worktree.create`'s and for its reason: a checkout that has
    not fetched is however stale it last was, and here that decides the question below
    it -- whether `origin/<branch>` exists at all is what tells a branch this machine has
    never seen from a PR whose head this checkout simply has not heard about yet.
    """
    git = sweep.git_for(project_dir)
    runner(["git", "-C", str(project_dir), "fetch", "--quiet", "origin"], check=False)
    local = git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0
    remote = git("rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}").returncode
    if not local and remote != 0:
        print(f"  origin has no branch {branch} in {project_dir.name}", file=sys.stderr)
        return None
    root = project_dir / aw.WORKTREES_DIR
    taken = [entry.name for entry in root.iterdir()] if root.is_dir() else []
    path = root / aw.tree_name(branch, taken)
    argv = ["git", "-C", str(project_dir), *aw.add_steps(branch, str(path), local)]
    if runner(argv, check=False).returncode != 0:
        return None
    # Nothing is written to make this appear in the delete dropdown, because that menu
    # has no file behind it any more: `agent-worktree.py rows` scans
    # `git worktree list --porcelain` when the picker opens, and `aw.nested` selects
    # exactly the directory cut above. The worktree you just cut is in the list because
    # it exists, not because a writer remembered to say so.
    return path


def background_argv(cli: str, prompt: str) -> list[str]:
    """`claude --bg <prompt>`, as an argv rather than a command line.

    No shell here, so no quoting: the prompt is one argument. That is the one thing the
    background mode has strictly better than the tab, and it is why `tab_safe` is applied
    to the prompt anyway -- the two modes must hand the agent the same words, or a report
    about one says nothing about the other.
    """
    return [cli, "--bg", prompt]


def launch_background(
    cli: str, tree: Path, prompt: str, hooks_off: bool, runner=subprocess.run
) -> int:
    """Start a detached session and print the id that reads it back."""
    exe = shutil.which(cli)
    if not exe:
        print(f"fix-prs: {cli} is not on PATH; run this yourself:\n  cd {tree}\n  {cli} --bg ...")
        return EXIT_FAILED
    env = dict(os.environ)
    if hooks_off:
        env[agent_box.harness_switch.HOOKS_OFF_ENV] = agent_box.harness_switch.HOOKS_OFF_VALUE
    done = runner(
        background_argv(exe, prompt), cwd=str(tree), capture_output=True, text=True, env=env
    )
    sys.stdout.write(done.stdout or "")
    sys.stderr.write(done.stderr or "")
    if done.returncode != 0:
        return EXIT_FAILED
    print("  read it back with `claude agents`, `claude logs <id>`, `claude attach <id>`")
    return EXIT_OK


def run_one(
    pick: Pick,
    workspace: Path,
    mode: str,
    runner=subprocess.run,
) -> int:
    """One PR, end to end: read it, get a worktree on its branch, open the agent in it.

    Returns non-zero for anything that stopped this PR getting an agent. A PR that went
    green, or that left the open set entirely, is `EXIT_OK` and no worktree: the menu was
    stale, the work is done or abandoned, and reporting that as a failure would put a
    red icon on good news.
    """
    root = workspace.parent
    project_dir = root / pick.project
    if not project_dir.is_dir():
        raise FixError(f"unknown checkout {pick.project!r} in {root}")

    pr = pr_view(project_dir, pick.number)
    if not pr:
        print(f"{pick.project} #{pick.number}: gh could not read this PR -- skipped")
        return EXIT_FAILED
    state = str(pr.get("state") or OPEN).upper()
    if state != OPEN:
        print(f"{pick.project} #{pick.number}: {state.lower()} since the scan -- nothing to do")
        return EXIT_OK
    reason = broken_reason(pr)
    if not reason:
        print(f"{pick.project} #{pick.number}: nothing wrong with it now -- nothing to do")
        return EXIT_OK

    branch = str(pr.get("headRefName") or "")
    if not branch:
        print(f"{pick.project} #{pick.number}: gh reported no head branch -- skipped")
        return EXIT_FAILED

    print(f"{pick.project} #{pick.number} ({reason}) on {branch}")
    tree, refused = existing_tree(project_dir, branch)
    if refused:
        print(f"  {refused}", file=sys.stderr)
        return EXIT_FAILED
    tree = tree or cut_tree(project_dir, branch, runner)
    if tree is None:
        print(f"  no worktree for {branch}; nothing opened", file=sys.stderr)
        return EXIT_FAILED
    print(f"  worktree {tree}")

    cli, how = AGENT_MODES[mode]
    prompt = seed_prompt(pick.project, pr, reason)
    if how == BACKGROUND:
        return launch_background(
            cli, tree, prompt, agent_box.harness_switch.hooks_are_off(), runner
        )
    return agent_box.open_agent(
        cli, tree, branch, runner, prompt=prompt, title=f"{pick.project} #{pick.number}"
    )


def run(picks: list[Pick], workspace: Path, mode: str, runner=subprocess.run) -> int:
    """Every ticked PR in turn. The worst exit code, so one failure is still reported.

    In turn rather than at once, and the reason survived the move off the box tier
    intact even though the expensive half of it did not: several ticked PRs are usually
    several PRs of the *same* checkout, `git worktree add` takes that checkout's index
    lock, and a fetch runs before each one. Three at once is three git processes
    queueing on one lock, with the failures arriving interleaved with the tabs.
    """
    worst = EXIT_OK
    for pick in picks:
        worst = max(worst, run_one(pick, workspace, mode, runner))
    return worst


def render_scan(found: dict[str, list[dict]]) -> str:
    """`--list`, for the terminal. The same rows the dropdown would draw."""
    lines = []
    for project in sorted(found, key=lambda name: (-len(found[name]), name)):
        prs = found[project]
        lines.append(f"{project}: {len(prs) or 'nothing'} broken")
        for pr in sorted(prs, key=lambda entry: str(entry.get("updatedAt", "")), reverse=True):
            lines.append(
                f"  #{pr.get('number')} {pr.get('headRefName', '')} -- {broken_reason(pr)}"
            )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--picks",
        default="",
        help=f"ticked rows, `<project>{PICK_SEP}<number>` joined by a space",
    )
    parser.add_argument(
        "--agent",
        default="claude",
        choices=sorted(AGENT_MODES),
        help="which CLI opens, and whether it opens in a tab or in the background",
    )
    parser.add_argument(
        "--rows",
        action="store_true",
        help="print the picker's rows (`value|label|description|detail`) and stop",
    )
    parser.add_argument(
        "--project-rows",
        action="store_true",
        help="print the CHECKOUT picker's rows and stop, recording the scan they came from",
    )
    parser.add_argument(
        "--checkouts",
        default="",
        help=(
            f"ticked checkouts, `<project>{picker_scan.SEP}<scan token>` joined by "
            f"`{picker_scan.LIST_SEP}` -- what the checkout picker returns"
        ),
    )
    parser.add_argument("--list", action="store_true", help="print the broken PRs and stop")
    parser.add_argument("--workspace", type=Path, default=worktree.DEFAULT_WORKSPACE)
    return parser


def main(argv: list[str] | None = None) -> int:
    raw = sys.argv[1:] if argv is None else argv
    # Ahead of `argparse`, per `.claude/rules/vscode-tasks.md`: a dismissed picker that
    # reached the parser would be a usage error, which is a red icon, a toast and a
    # `logs/` artifact for a run the user called off.
    dismissed = task_input.cancelled_inputs(raw)
    if dismissed:
        print(task_input.cancel_report("fix-prs", dismissed))
        return EXIT_OK

    args = build_parser().parse_args(raw)
    workspace = args.workspace.resolve()
    if not workspace.is_file():
        print(f"fix-prs: no workspace file at {workspace}", file=sys.stderr)
        return EXIT_USAGE

    try:
        if args.project_rows:
            found = scan(workspace)
            token = picker_scan.write(SCAN_NAME, scan_entries(found))
            picker_rows.emit(project_rows(found, token))
            return EXIT_OK
        if args.rows:
            picker_rows.emit(picked_rows(workspace, args.checkouts))
            return EXIT_OK
        if args.list:
            print(render_scan(scan(workspace)))
            return EXIT_OK

        tokens = split_picks(args.picks)
        if not tokens:
            print("fix-prs: nothing ticked -- nothing to do")
            return EXIT_OK
        picks = [pick for pick in (parse_pick(token) for token in tokens) if pick is not None]
        if not picks:
            print("fix-prs: only the `nothing broken` row was ticked -- nothing to do")
            return EXIT_OK
        strayed = strayed_picks(picks, args.checkouts)
        if strayed:
            print(f"fix-prs: {stray_report(strayed)}", file=sys.stderr)
            return EXIT_USAGE
        return run(picks, workspace, args.agent)
    except (FixError, worktree.WorktreeError, devkit_project.ProjectError) as exc:
        print(f"fix-prs: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
