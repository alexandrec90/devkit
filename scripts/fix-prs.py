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

**New worktrees go under `.claude/worktrees/`.** `agent-worktree.py` lists and removes
them. Existing Claude and Codex worktrees are reused, as are live devkit boxes whose
project, branch and path match the PR's checkout and head. Upgrade PRs already have
such boxes; refusing them prevents the task from fixing those PRs. Reuse leaves the
box's lease and lifecycle with `worktree.py`. This task creates no boxes or port leases.
`scripts/agent_worktrees.py` owns `holder`, `tree_name` and `add_steps`; what is here is
the PR half.

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

**Whether a PR still merges is `scripts/pr_mergeability.py`'s question**, and it is a
question with three answers rather than two: GitHub computes mergeability on demand and
invalidates it whenever the base branch moves, so a scan run in the minute after a merge
asks about a whole checkout it has not re-judged. That module owns what to do about the
third answer, because reading it as "merges fine" is a row that silently is not here.

**What counts as broken, and the rows the picker draws, are `scripts/broken_pr_menu.py`.**
That half was cut out when this file's `file_lines` was recorded a fifth time against
the seam its own section headers drew. The entrypoint stayed here on purpose:
`devkit_project.ACTIONS` names `scripts/fix-prs.py` for the live `--rows` picker, and a
dropdown's command line is spelled by hand in the workspace task block, so the library
came out from under the CLI rather than the CLI moving to it.

Every function that decides something is pure and tested in `tests/test_fix_prs.py`
(with the menu tier's own in `tests/test_broken_pr_menu.py`); the ones that spawn take
a runner.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
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

# The scan-and-menu half, cut into its own module when this file's `file_lines` was
# recorded a fifth time against the same never-cut seam. The **entrypoint stayed
# here**: `devkit_project.ACTIONS` names `scripts/fix-prs.py` for the live `--rows`
# picker, and a dropdown's command line is spelled by hand in the workspace task
# block, so cutting the library out instead leaves that path and every flag on it
# exactly where they were.
#
# Qualified rather than imported name by name, and that is the load-bearing part.
# `menu.picked_rows` calls `menu.scan` through its own module global, so a `from`
# import would leave two bindings for one function and a caller patching the wrong
# one -- which is exactly what the test suite did on the first cut, silently taking
# the real `gh` path while asserting against a stub.
import broken_pr_menu as menu

# `agent-box.py` is hyphenated, so it cannot be a plain import. Loaded by path for the
# one thing worth sharing rather than copying: how a tab's command line is built and
# which window it lands in. `worktree` above is imported normally on purpose -- see the
# note on the same pair of inserts in `agent-box.py`.
from _loader import load_by_path

REPO_ROOT = Path(__file__).resolve().parents[1]

agent_box = load_by_path("agent_box", REPO_ROOT / "scripts" / "agent-box.py")

# The agent modes the picker offers. The value is what reaches `--agent`; the mapping is
# to how the session is opened, which is the whole of the difference between them. These
# stay here rather than moving with the menu: they describe launching, which is this
# half's whole subject, and the menu never reads them.
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
    """Return a reusable tree, an unheld branch `(None, "")`, or `(None, refusal)`.

    Git permits only one worktree per branch. Reuse agent worktrees and matching live
    boxes; name the directory for other holders rather than attempting another cut.
    """
    listed = sweep.git_for(project_dir)("worktree", "list", "--porcelain")
    if listed.returncode != 0:
        return None, f"git could not list the worktrees of {project_dir}"
    held, nested = aw.holder(project_dir, listed.stdout, branch)
    if not held:
        return None, ""
    if not nested:
        # Upgrade PRs already have a box on their head branch. Reuse it just as
        # agent-box attach does, without creating a tree or changing its lease.
        root = project_dir.parent
        for box in worktree.live_boxes(root).values():
            if (
                box.project == project_dir.name
                and box.branch == branch
                and Path(held).resolve() == worktree.box_path(root, box.name).resolve()
            ):
                return Path(held), ""
        return None, (
            f"{branch} is already checked out at {held}, which is not in "
            f"{aw.TIER_SUMMARY} or a matching live devkit box -- finish the PR from there"
        )
    return Path(held), ""


def cut_tree(project_dir: Path, branch: str, runner=subprocess.run) -> Path | None:
    """Cut a default-tier worktree on the PR's own head branch. None when git refused.

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
    root = aw.default_root(project_dir)
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
    pick: menu.Pick,
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
        raise menu.FixError(f"unknown checkout {pick.project!r} in {root}")

    pr = menu.pr_view(project_dir, pick.number)
    if not pr:
        print(f"{pick.project} #{pick.number}: gh could not read this PR -- skipped")
        return EXIT_FAILED
    state = str(pr.get("state") or menu.OPEN).upper()
    if state != menu.OPEN:
        print(
            f"{pick.project} #{pick.number}: {state.lower()} since the menu.scan -- nothing to do"
        )
        return EXIT_OK
    # The same re-ask the menu.scan does, for the same reason and one the launch path feels
    # more sharply: between the click and here, anything merging to the base branch puts
    # this PR's verdict back to `UNKNOWN`, and an unresolved verdict read straight off
    # this view says "nothing wrong with it now" -- a ticked row that opens nothing,
    # reports success, and leaves the PR exactly as red as it was.
    menu.settle_mergeability(project_dir, [pr])
    reason = menu.broken_reason(pr)
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


def run(picks: list[menu.Pick], workspace: Path, mode: str, runner=subprocess.run) -> int:
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
                f"  #{pr.get('number')} {pr.get('headRefName', '')} -- {menu.broken_reason(pr)}"
            )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--picks",
        default="",
        help=f"ticked rows, `<project>{menu.PICK_SEP}<number>` joined by a space",
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
        help="print the CHECKOUT picker's rows and stop, recording the menu.scan they came from",
    )
    parser.add_argument(
        "--checkouts",
        default="",
        help=(
            f"ticked checkouts, `<project>{picker_scan.SEP}<menu.scan token>` joined by "
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
            found = menu.scan(workspace)
            token = picker_scan.write(menu.SCAN_NAME, menu.scan_entries(found))
            picker_rows.emit(menu.project_rows(found, token))
            return EXIT_OK
        if args.rows:
            picker_rows.emit(menu.picked_rows(workspace, args.checkouts))
            return EXIT_OK
        if args.list:
            print(render_scan(menu.scan(workspace)))
            return EXIT_OK

        tokens = menu.split_picks(args.picks)
        if not tokens:
            print("fix-prs: nothing ticked -- nothing to do")
            return EXIT_OK
        picks = [pick for pick in (menu.parse_pick(token) for token in tokens) if pick is not None]
        if not picks:
            print("fix-prs: only the `nothing broken` row was ticked -- nothing to do")
            return EXIT_OK
        strayed = menu.strayed_picks(picks, args.checkouts)
        if strayed:
            print(f"fix-prs: {menu.stray_report(strayed)}", file=sys.stderr)
            return EXIT_USAGE
        return run(picks, workspace, args.agent)
    except (menu.FixError, worktree.WorktreeError, devkit_project.ProjectError) as exc:
        print(f"fix-prs: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
