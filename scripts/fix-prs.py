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

**The menu is a scan, not live state.** `rioj7.command-variable` can read a file and
cannot run a command, so the dropdown's options are whatever the last `--refresh` wrote
-- and the writer that matters is not a task run but `worktree.reconcile`, which calls
`refresh_menu` on its fifteen-minute pass exactly as it does for `preview-task.py`. The
rows therefore track GitHub without anyone asking, and every row carries the scan's
timestamp so a stale one says so. What a *click* then acts on is read fresh from `gh pr
view`: the menu is up to a quarter hour old, and the prompt an agent is handed must
describe the PR as it is now.

Every function that decides something is pure and tested in `tests/test_fix_prs.py`;
the ones that spawn take a runner.
"""

from __future__ import annotations

import argparse
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

# The file the dropdown reads. Under `logs/` for `preview-task.MENU_CACHE`'s reason: it
# is machine state with the lifetime of a reconcile pass, gitignored, and worth nothing
# to a fresh clone.
MENU_CACHE = REPO_ROOT / "logs" / "broken-prs.json"

# `<project>:<number>`, one token because a VS Code input resolves to one string. A
# checkout name cannot contain a colon (it is a directory name and a
# `COMPOSE_PROJECT_NAME`), so the first one always separates the halves.
PICK_SEP = ":"

# What joins several ticked rows into that one string. A space, matching `previewRow`
# and chosen on the same terms: neither half can contain one.
PICK_LIST_SEP = " "

# The row a healthy checkout still draws. The extension builds its list by evaluating
# one expression per field against rising indices until one *throws*, so a checkout with
# an empty `rows` array would end the list at the first project that had nothing wrong
# -- hiding every checkout after it. A placeholder row keeps the array non-empty, and
# `main` recognises the sentinel and reports rather than spawning anything.
NOTHING = "none"

# How many open PRs to ask about per checkout. Well past what any of these repos carries
# at once; the cap is here so a runaway bot cannot turn one dropdown into a thousand.
PR_LIMIT = 50

# `gh pr list` fields. `mergeable` is the conflict half and `statusCheckRollup` the gate
# half; `isDraft` is what a draft is excluded by.
PR_LIST_FIELDS = "number,title,headRefName,updatedAt,url,isDraft,mergeable,statusCheckRollup"

# ...and the same question asked of one PR at launch time, plus the base branch, which is
# what the agent has to merge in when the answer is a conflict, and `state`, which the
# list half gets for free from `--state open` and this half has to ask for.
PR_VIEW_FIELDS = (
    "number,title,headRefName,baseRefName,url,state,isDraft,mergeable,statusCheckRollup"
)

# The one `state` a PR can be in and still be worth a box. GitHub's other two are `CLOSED`
# and `MERGED`, and both delete the head branch on the way out, so a resume aimed at one
# fails several steps later with `origin has no branch ... -- nothing to resume`: a
# message about worktrees for what is really a stale menu row.
OPEN = "OPEN"

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


# --- the dropdown's options file --------------------------------------------------


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


def menu_row(project: str, pr: dict, now: _dt.datetime | None = None) -> dict[str, str]:
    """One dropdown entry. Every field a string -- see `menu_payload` for why."""
    return {
        "value": pick_value(project, pr.get("number", "")),
        "label": f"#{pr.get('number', '?')} {pr.get('headRefName', '')}".strip(),
        "description": f"{broken_reason(pr)} -- {age(str(pr.get('updatedAt', '')), now)}",
        "detail": str(pr.get("title", "")),
    }


def placeholder_row(project: str) -> dict[str, str]:
    """The row a checkout with nothing broken still draws. See `NOTHING`."""
    return {
        "value": pick_value(project, NOTHING),
        "label": "nothing broken",
        "description": "every open PR here is either green or a draft",
        "detail": "picking this runs nothing",
    }


def project_note(rows: list[dict], as_of: str) -> str:
    """The first dropdown's second column: what picking this checkout will offer."""
    if not rows:
        return f"nothing broken -- as of {as_of}"
    return f"{len(rows)} broken -- as of {as_of}"


def menu_payload(
    found: dict[str, list[dict]], now: _dt.datetime | None = None
) -> dict[str, object]:
    """The options file: the checkouts, and each one's rows keyed by name.

    The shape is `preview-task.menu_payload`'s, and load-bearing for its reasons: the
    extension appends options until an expression *throws*, `undefined` does not throw,
    and a bare list index merely returns it. So the rows are an array under a key per
    checkout -- `rows[project][i].value` raises past the end -- and every row carries all
    four fields as strings.

    Checkouts with something broken sort first, and within that by count. A dropdown
    whose top entry is the one with four red PRs is the dropdown answering the question
    it was opened to answer; alphabetical order puts `carameli` first every time
    regardless of whether anything is wrong there.
    """
    stamp = now or _dt.datetime.now(_dt.UTC)
    as_of = stamp.astimezone().strftime("%Y-%m-%d %H:%M")
    entries, rows = [], {}
    for project in sorted(found, key=lambda name: (-len(found[name]), name)):
        listed = sorted(found[project], key=lambda pr: str(pr.get("updatedAt", "")), reverse=True)
        rows[project] = [menu_row(project, pr, stamp) for pr in listed] or [
            placeholder_row(project)
        ]
        entries.append(
            {"name": project, "label": project, "description": project_note(listed, as_of)}
        )
    return {"generated": stamp.isoformat(), "asOf": as_of, "projects": entries, "rows": rows}


def write_menu(payload: dict, path: Path | None = None) -> Path | None:
    """Save the options, atomically. The path on success, None on any failure.

    Never raises, for `preview-task.write_menu`'s reason: this runs as a rider on
    somebody else's pass, and the cost of a swallowed error is one stale dropdown that
    the next pass rewrites within the quarter hour.

    The destination defaults at CALL time so a test can point `MENU_CACHE` somewhere
    disposable and the caller in `main` follows it there.
    """
    path = path or MENU_CACHE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        scratch = path.with_suffix(".json.tmp")
        scratch.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        scratch.replace(path)
    except OSError:
        return None
    return path


def scan(workspace: Path, projects: list[str] | None = None) -> dict[str, list[dict]]:
    """Every checkout in the registry, and the broken PRs it has.

    Every registered checkout is listed even when it contributes no row, because the
    reader's question is "where is something red", and a checkout that silently drops out
    of the list when it is healthy is indistinguishable from one the scan could not
    reach.
    """
    text = workspace.read_text(encoding="utf-8")
    names = devkit_project.known_projects(text) if projects is None else projects
    root = workspace.parent
    return {name: broken_prs(root / name) for name in names}


def refresh_menu(workspace: Path, path: Path | None = None) -> Path | None:
    """Rebuild the options file. The path on success, None on any failure.

    Total, like `write_menu` and for the stronger reason: `worktree.reconcile` calls this
    at the end of every pass, and a menu that could not be built must never fail a
    reconcile that reaped boxes correctly. `broken_prs` and `write_menu` are already the
    forgiving kind, so what is left here is the workspace file itself: `OSError` for one
    that cannot be read, and `ValueError` for one that cannot be parsed as a registry --
    `json.JSONDecodeError` and `devkit_project.ProjectError` are both that. Named rather
    than caught as `Exception`, so a bug in the shapes above still surfaces as a
    traceback instead of an empty dropdown nobody can account for.
    """
    try:
        return write_menu(menu_payload(scan(workspace)), path)
    except (OSError, ValueError):
        return None


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

    Returns non-zero for anything that stopped this PR getting an agent. The three ways
    the menu can be stale -- the PR went green, someone closed it, someone merged it --
    are all `EXIT_OK` and no box: the work is done or abandoned, and reporting that as a
    failure would put a red icon on good news. `state` is the half that is easy to leave
    out, because the scan asks `gh` for open PRs only and a *view* answers for any of
    them; without it a closed PR reads as broken, and the run dies in `resume` on the
    head branch GitHub deleted when it closed.
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
    parser.add_argument("--refresh", action="store_true", help="rewrite the menu file and stop")
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
        if args.refresh:
            written = refresh_menu(workspace)
            print(f"fix-prs: wrote {written}" if written else "fix-prs: the menu was not written")
            return EXIT_OK if written else EXIT_FAILED
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
