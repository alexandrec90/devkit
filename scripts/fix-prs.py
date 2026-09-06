#!/usr/bin/env python3
"""Send an agent at the PRs that are already red, one box per PR.

A PR goes red two ways and both of them wait for a person: `origin/<default>` moved
under it (`mergeable: CONFLICTING`), or its gate failed. Neither is work anybody wants
to do by hand, and neither is work the scheduled tier will ever do -- `worktree.py
reconcile` merges only what is *green* and carries the merge label, so a red PR is
precisely the state it steps over every quarter hour, forever.

**The unit of work is one PR in one box on that PR's own head branch.** Not a new branch:
the fix belongs on the branch under review, and `worktree.py resume <project> --branch
<head>` is the verb that puts a box back on an existing branch with the upstream set so
a bare push lands where the PR is looking. That is also this repo's answer to "is there
a CLI flag that attaches an agent to a PR branch": Claude Code's `--from-pr` *resumes a
session linked to a PR*, which needs that session to still exist on this machine, and
boxes are reaped after `worktree.DEFAULT_MAX_AGE_DAYS`. Resuming the box is the spelling
that works on a PR nobody has touched this week, and it is the one that comes with a
port lease and a `COMPOSE_PROJECT_NAME`.

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
import devkit_project
import sweep
import task_input
import worktree

# `agent-box.py` is hyphenated, so it cannot be a plain import. Loaded by path for the
# one thing worth sharing rather than copying: how a tab's command line is built and
# which window it lands in. `worktree` above is imported normally on purpose -- see the
# note on the same pair of inserts in `agent-box.py`.
from _loader import load_by_path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKTREE = REPO_ROOT / "scripts" / "worktree.py"

agent_box = load_by_path("agent_box", REPO_ROOT / "scripts" / "agent-box.py")

# `<project>:<number>`, one token because a VS Code input resolves to one string. A
# checkout name cannot contain a colon (it is a directory name and a
# `COMPOSE_PROJECT_NAME`), so the first one always separates the halves.
PICK_SEP = ":"

# What joins several ticked rows into that one string. A space, matching `previewRow`
# and chosen on the same terms: neither half can contain one.
PICK_LIST_SEP = " "

# The row a scan that found nothing draws. `shellCommand.execute` has `defaultOptions`
# for this, and the row is written here instead so the *reason* the list is empty is one
# of this module's outputs and testable with the rest: an empty quick-pick says nothing
# about whether the scan ran. `main` recognises the sentinel and spawns nothing.
NOTHING = "none"

# What separates the four fields of a row. `shellCommand.execute` splits each line into
# `value|label|description|detail` and returns the value alone; the other three are the
# two lines the quick-pick draws. A PR title is the one field a person wrote, so `cell`
# takes the separator back out rather than trusting GitHub not to carry one.
FIELD_SEP = "|"

# How many checkouts `scan` asks about at once. Well above the registry's size, so the
# pool is bounded by the number of checkouts in practice; the ceiling is here so a
# workspace that grows to thirty repos does not open thirty `gh` processes at once.
SCAN_WORKERS = 8

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
OPEN = "OPEN"  # the one state worth a box; CLOSED and MERGED both delete the head branch

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


def cell(text: object) -> str:
    """One field of a row: a single line, with no `FIELD_SEP` left in it."""
    return " ".join(str(text).replace(FIELD_SEP, "/").split())


def menu_row(project: str, pr: dict, now: _dt.datetime | None = None) -> str:
    """One quick-pick line for a broken PR.

    The checkout is in the description rather than the label because the list is flat --
    `shellCommand.execute` resolves one input per command, and a "which checkout, then
    which of its PRs" pair would be two, which VS Code gives no sight of each other. One
    scan across every checkout was always the question this task asked; the two-stage
    picker was how a *file* keyed its rows, not what a reader wanted.
    """
    number = pr.get("number", "?")
    return FIELD_SEP.join(
        (
            cell(pick_value(project, pr.get("number", ""))),
            cell(f"#{number} {pr.get('headRefName', '')}"),
            cell(f"{project} -- {broken_reason(pr)} -- {age(str(pr.get('updatedAt', '')), now)}"),
            cell(pr.get("title", "")),
        )
    )


def placeholder_row() -> str:
    """The row a scan that found nothing draws. See `NOTHING`."""
    return FIELD_SEP.join(
        (
            NOTHING,
            "nothing broken",
            "every open PR on this machine is green, or a draft",
            "picking this runs nothing",
        )
    )


def rows(found: dict[str, list[dict]], now: _dt.datetime | None = None) -> list[str]:
    """Every broken PR on the machine as a quick-pick line, most recently touched first.

    Newest first rather than grouped by checkout: the reader is choosing which red PR to
    send a session at, and "which repo" is a field on the row rather than the question.
    """
    listed = [(project, pr) for project, prs in found.items() for pr in prs]
    listed.sort(key=lambda pair: str(pair[1].get("updatedAt", "")), reverse=True)
    return [menu_row(project, pr, now) for project, pr in listed] or [placeholder_row()]


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
    # Ahead of the split, and a bare word rather than the `<project>:none` this used to
    # be: the pick reaches the script as `--picks <value>`, and argparse reads any value
    # starting with `-` as an option, so a sentinel needs no leading punctuation either.
    if str(token) == NOTHING:
        return None
    project, _, tail = str(token).partition(PICK_SEP)
    if not project or not tail:
        raise FixError(f"cannot read the pick {token!r}; expected <project>{PICK_SEP}<number>")
    if tail == NOTHING:
        return None
    if not tail.isdigit():
        raise FixError(f"{token!r} does not name a PR number")
    return Pick(project, int(tail))


def pr_view(project_dir: Path, number: int) -> dict:
    """The PR as it is *now*, not as the menu last saw it. Empty on any failure.

    The menu is up to a quarter of an hour old, which is long enough for the gate to have
    gone green or for a rebase to have cleared the conflict. What the agent is told has
    to be current, so this is read at launch time -- and it is also the check that stops
    a box being cut for a PR that no longer needs one.
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
    """One line, with no `;` in it -- the two things a `wt` command line cannot carry.

    `wt.exe` parses its own command line and splits on an unescaped semicolon into a
    second sub-command, so a prompt containing one would open a tab running half a
    sentence and then try to run the other half as a `wt` verb. Newlines end the command
    outright. Both are replaced rather than escaped because the prompt is prose this
    module writes: there is no case where the exact punctuation matters more than the
    tab opening.
    """
    return " ".join(str(text).replace(";", ",").split())


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
        f"This box is checked out on the PR head branch {head} with its upstream set, "
        f"so a bare git push lands on the PR. "
        f"Merge origin/{base} in, fix what the gate is failing on, run the targeted "
        f"tests and the linter, push, and then merge the PR once the gate is green. "
        f"If it cannot be made green, stop and say what is in the way."
    )


# --- opening the session ----------------------------------------------------------


def existing_box(boxes: dict, project: str, branch: str):
    """The live box already on `branch`, or None. Reuse before resume.

    `worktree.resume_plan` refuses a branch that is already checked out, and rightly --
    two worktrees on one branch is a state git will not hold. But this task's ordinary
    second click is on a PR whose box is still open from the first, so the refusal would
    read as a failure when what it describes is the box being *ready*.
    """
    return next(
        (box for box in boxes.values() if box.project == project and box.branch == branch), None
    )


def resume_box(project: str, branch: str, workspace: Path, runner=subprocess.run) -> Path | None:
    """Put a box back on `branch` and return its path. None when it could not be cut."""
    argv = [
        sys.executable,
        str(WORKTREE),
        "resume",
        project,
        "--branch",
        branch,
        "--yes",
        "--json",
        "--workspace",
        str(workspace),
    ]
    done = runner(argv, capture_output=True, text=True, check=False)
    sys.stderr.write(done.stderr or "")
    if done.returncode != 0:
        return None
    try:
        plan = json.loads(done.stdout or "{}")
    except ValueError:
        return None
    for note in plan.get("notes", []):
        print(f"  {note}")
    path = plan.get("path")
    return Path(path) if path else None


def background_argv(cli: str, prompt: str) -> list[str]:
    """`claude --bg <prompt>`, as an argv rather than a command line.

    No shell here, so no quoting: the prompt is one argument. That is the one thing the
    background mode has strictly better than the tab, and it is why `tab_safe` is applied
    to the prompt anyway -- the two modes must hand the agent the same words, or a report
    about one says nothing about the other.
    """
    return [cli, "--bg", prompt]


def launch_background(
    cli: str, box: Path, prompt: str, hooks_off: bool, runner=subprocess.run
) -> int:
    """Start a detached session and print the id that reads it back."""
    exe = shutil.which(cli)
    if not exe:
        print(f"fix-prs: {cli} is not on PATH; run this yourself:\n  cd {box}\n  {cli} --bg ...")
        return EXIT_FAILED
    env = dict(os.environ)
    if hooks_off:
        env[agent_box.harness_switch.HOOKS_OFF_ENV] = agent_box.harness_switch.HOOKS_OFF_VALUE
    done = runner(
        background_argv(exe, prompt), cwd=str(box), capture_output=True, text=True, env=env
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
    """One PR, end to end: read it, get a box on its branch, open the agent in it.

    Returns non-zero for anything that stopped this PR getting an agent. A PR that went
    green, or that left the open set entirely, is `EXIT_OK` and no box: the menu was
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
    held = existing_box(worktree.live_boxes(root), pick.project, branch)
    box = Path(held.path) if held is not None else resume_box(pick.project, branch, workspace)
    if box is None:
        print(f"  no box for {branch}; nothing opened", file=sys.stderr)
        return EXIT_FAILED
    print(f"  box {box}")

    cli, how = AGENT_MODES[mode]
    prompt = seed_prompt(pick.project, pr, reason)
    if how == BACKGROUND:
        return launch_background(cli, box, prompt, agent_box.harness_switch.hooks_are_off(), runner)
    return agent_box.open_agent(
        cli, box, branch, runner, prompt=prompt, title=f"{pick.project} #{pick.number}"
    )


def run(picks: list[Pick], workspace: Path, mode: str, runner=subprocess.run) -> int:
    """Every ticked PR in turn. The worst exit code, so one failure is still reported.

    In turn rather than at once, and that is the cost this task states in its `detail`:
    each PR wants a box, and a box wants a port slot out of a fixed ceiling and a cold
    toolchain install. Three at once is three provisioning runs competing for the same
    disk.
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
        if args.rows:
            # The picker's stdout, so nothing else may be written to it: a status line
            # here is an extra option in the quick-pick.
            for row in rows(scan(workspace)):
                print(row)
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
        return run(picks, workspace, args.agent)
    except (FixError, worktree.WorktreeError, devkit_project.ProjectError) as exc:
        print(f"fix-prs: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
