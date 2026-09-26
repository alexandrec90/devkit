#!/usr/bin/env python3
"""Send an agent at everything that is red, deciding for itself what "everything" means.

The task asks one question -- which agent -- and this script answers the rest. It scans
every checkout for a red PR and every open scheduled-failure issue, reads what each gate
actually said, and plans: a PR gets a session on its own head branch; a vendored test
failing across several consumers gets **one** session in devkit; a release PR (red by
construction) and an adoption PR for a superseded release get none, out loud. The plan
is `scripts/fix_plan.py`, the evidence is `scripts/gate_evidence.py`, and what is here
is the acting half: worktrees, prompts, sessions, and the ledger that stops a second
click sending a second agent at the same failure.

It used to ask which PRs. That was the expensive part, and not in clicks: eight
consumers red on one devkit release were eight ticked rows and eight sessions each
rediscovering one cause. `--picks` survives as a hand-typed override for a terminal.

**The unit of work for a PR is one worktree on that PR's own head branch.** Not a new
branch: the fix belongs on the branch under review, so the worktree is cut on the head
branch with `origin/<head>` as its upstream and a bare push lands where the PR is
looking. (Claude Code's `--from-pr` *resumes a session linked to a PR*, which needs that
session to still exist here; cutting the worktree works on a PR nobody touched this
week.) A failure with no branch of its own -- a nightly, a shared vendored failure --
gets a fresh branch off the default one and a prompt that ends with the ship skill.

**New worktrees go under `.claude/worktrees/`.** `agent-worktree.py` lists and removes
them. Existing Claude and Codex worktrees are reused, as are live devkit boxes whose
project, branch and path match the PR's checkout and head. Upgrade PRs already have
such boxes; refusing them prevents the task from fixing those PRs. Reuse leaves the
box's lease and lifecycle with `worktree.py`. This task creates no boxes or port leases.
`scripts/agent_worktrees.py` owns `holder`, `tree_name` and the `add` argv.

**Three agent modes.** `claude` and `codex` each open a Windows Terminal tab, the one
`agent_tabs.py` opens; `claude-bg` is `claude --bg`, read back with `claude attach` /
`claude logs`. There is no `codex-bg`: `codex exec` streams to the terminal it was
started in and hands back nothing to attach to. Which model and effort a mode opens at
rides along in the same `agent_models.Launch`, and is the picker's answer, never this
module's guess.

**Click-only, by decision.** Nothing schedules this. A session is paid for, and a
dispatch loop that spent one in the background on a failure nobody was going to look
at is the outcome the ledger and the plan exist to avoid; the scheduled tier merges
green PRs and reaps boxes, and stops there.

Every function that decides something is pure and tested in `tests/test_fix_prs.py`
(with the plan's own in `tests/test_fix_plan.py` and the evidence's in
`tests/test_gate_evidence.py`); the ones that spawn take a runner.
"""

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adoption_prs
import agent_models
import agent_tabs
import agent_worktrees as aw
import devkit_project
import fix_ledger
import fix_plan
import fix_prompts
import fix_reports
import gate_evidence
import stray_worktree as stray
import sweep
import task_branch as tb
import task_input
import worktree

# Qualified rather than imported name by name, and that is the load-bearing part: the
# menu's functions call each other through their own module globals, so a `from` import
# would leave two bindings for one function and a caller patching the wrong one -- which
# is exactly what the test suite did on the first cut, silently taking the real `gh`
# path while asserting against a stub.
import broken_pr_menu as menu

REPO_ROOT = Path(__file__).resolve().parents[1]

# The agent modes the task offers. The value is what reaches `--agent`; the mapping is
# to how the session is opened, which is the whole of the difference between them.
TAB = "tab"  # a Windows Terminal tab, watched by whoever clicked
BACKGROUND = "bg"  # `claude --bg`, read back with `claude attach` / `claude logs`
AGENT_MODES: dict[str, tuple[str, str]] = {
    "claude": ("claude", TAB),
    "claude-bg": ("claude", BACKGROUND),
    "codex": ("codex", TAB),
}

# The checkout a shared vendored failure is fixed in: this repo, by its registry name.
DEVKIT = "devkit"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2


# --- what the agent is told -------------------------------------------------------


def tab_safe(text: str) -> str:
    """One line -- what a `wt` command line cannot carry at all.

    A newline ends `wt`'s command outright, and there is no escape for one, so the
    prompt is flattened rather than quoted. Semicolons are *not* touched here:
    `agent_tabs.wt_argv` escapes them for every string that reaches a tab, which it has
    to do anyway for the kill switch's own `;` that this function can never see, and two
    owners for one hazard is how the prefix went unescaped in the first place.
    """
    return " ".join(str(text).split())


# --- the worktree -----------------------------------------------------------------


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
        return None, stray.refusal(project_dir, held, branch)
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
    # has no file behind it: `agent-worktree.py rows` scans `git worktree list
    # --porcelain` when the picker opens, and `aw.nested` selects exactly the directory
    # cut above. The worktree you just cut is in the list because it exists.
    return path


def fix_branch(decision: fix_plan.Decision, now=None) -> str:
    """The fresh branch a fix with no branch of its own starts on.

    Under `tb.BRANCH_PREFIX` rather than the automation namespace: a person clicked,
    and the PR the ship skill opens from it is one they asked for. The date suffix is
    `tb.branch_name`'s, so the branch reads beside every other agent cut; the counter
    against the checkout's own branches is `cut_fresh_tree`'s.
    """
    first = decision.failures[0]
    if decision.action == fix_plan.UPSTREAM:
        fallback = first.workflow or "vendored"
        test = next((s for s in first.signature if "::" in s), fallback).rsplit("::", 1)[-1]
        topic = f"fix {tb.slugify(test, max_len=24)}"
    else:
        topic = f"fix {first.workflow or 'nightly'}"
    return tb.branch_name(tb.slugify(topic), set(), today=now)


def cut_fresh_tree(
    project_dir: Path, branch: str, base: str, runner=subprocess.run
) -> tuple[Path | None, str]:
    """Cut a default-tier worktree on a new `branch` off `origin/<base>`.

    The branch is renamed with a counter when the checkout already has one of that
    name: two clicks on two different nightlies of one project on one day want two
    branches, and git would otherwise refuse the second with the first's name.
    """
    git = sweep.git_for(project_dir)
    runner(["git", "-C", str(project_dir), "fetch", "--quiet", "origin"], check=False)
    name, counter = branch, 2
    while git("rev-parse", "--verify", "--quiet", f"refs/heads/{name}").returncode == 0:
        name, counter = f"{branch}-{counter}", counter + 1
    root = aw.default_root(project_dir)
    taken = [entry.name for entry in root.iterdir()] if root.is_dir() else []
    path = root / aw.tree_name(name, taken)
    # `--no-track`, as `create` is: the upstream belongs to the first push, not to the
    # default branch the worktree was cut from, which is where a bare push would land.
    add = ("worktree", "add", "--no-track", "-b", name, str(path), f"origin/{base}")
    argv = ["git", "-C", str(project_dir), *add]
    if runner(argv, check=False).returncode != 0:
        return None, name
    return path, name


# --- opening the session ----------------------------------------------------------


def open_session(
    launch: agent_models.Launch,
    tree: Path,
    branch: str,
    prompt: str,
    title: str,
    runner=subprocess.run,
) -> int:
    """The one place a mode becomes a tab or a background session."""
    if AGENT_MODES[launch.agent][1] == BACKGROUND:
        off = agent_tabs.harness_switch.hooks_are_off()
        return agent_tabs.launch_background(launch, tree, prompt, off, runner)
    return agent_tabs.open_agent(launch, tree, branch, runner, prompt=prompt, title=title)


def run_one(
    pick: menu.Pick, workspace: Path, launch: agent_models.Launch, runner=subprocess.run
) -> int:
    """One hand-picked PR: read it live, gather its evidence, send it the planned way.

    Returns non-zero for anything that stopped this PR getting an agent. A PR that went
    green, or that left the open set entirely, is `EXIT_OK` and no worktree: the pick was
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
        print(f"{pick.project} #{pick.number}: {state.lower()} since the scan -- nothing to do")
        return EXIT_OK
    # The same re-ask the scan does, for the same reason and one the launch path feels
    # more sharply: between the click and here, anything merging to the base branch puts
    # this PR's verdict back to `UNKNOWN`, and an unresolved verdict read straight off
    # this view says "nothing wrong with it now" -- a session that opens nothing,
    # reports success, and leaves the PR exactly as red as it was.
    menu.settle_mergeability(project_dir, [pr])
    if not menu.broken_reason(pr):
        print(f"{pick.project} #{pick.number}: nothing wrong with it now -- nothing to do")
        return EXIT_OK
    if not pr.get("headRefName"):
        print(f"{pick.project} #{pick.number}: gh reported no head branch -- skipped")
        return EXIT_FAILED
    failure = gate_evidence.read_pr(
        project_dir,
        gate_evidence.pr_failure(pick.project, pr),
        gate_evidence.evidence_root(workspace),
    )
    return dispatch_pr(failure, root, launch, runner)


def run(
    picks: list[menu.Pick], workspace: Path, launch: agent_models.Launch, runner=subprocess.run
) -> int:
    """Every picked PR in turn. The worst exit code, so one failure is still reported.

    In turn rather than at once: several picks are usually several PRs of the *same*
    checkout, `git worktree add` takes that checkout's index lock, and a fetch runs
    before each one. Three at once is three git processes queueing on one lock, with
    the failures arriving interleaved with the tabs.
    """
    worst = EXIT_OK
    for pick in picks:
        worst = max(worst, run_one(pick, workspace, launch, runner))
    return worst


# --- the planned path ---------------------------------------------------------------


def refresh_head(tree: Path, branch: str, git_for=sweep.git_for) -> str:
    """Bring a reused tree's branch to origin's; what stopped it, or "" when nothing.

    The pass updates a behind PR through GitHub, so origin's head carries the base and
    the prompt says so; a local branch checked out before that does not, and a push
    from it is refused. A tree with edits is a session still working, and stays as is.
    """
    git = git_for(tree)
    git("fetch", "--quiet", "origin", branch)
    status = git("status", "--porcelain")
    if (status.stdout or "").strip():
        return "left as is: the tree has uncommitted changes"
    if git("merge", "--ff-only", f"origin/{branch}").returncode != 0:
        return f"left as is: {branch} has diverged from origin/{branch}"
    return ""


def dispatch_pr(
    failure: fix_plan.Failure,
    root: Path,
    launch: agent_models.Launch,
    runner=subprocess.run,
    key: str = "",
) -> int:
    """A planned PR: its own head branch, the gate's logs beside it, the plan's prompt.

    `key` is the ledger key the pass records this under; stamped into the worktree
    (`fix_reports.stamp`) so a blocked report from it can be matched back.
    """
    project_dir = root / failure.project
    name = f"#{failure.number}" if failure.number else failure.head
    print(f"{failure.project} {name} ({failure.reason}) on {failure.head}")
    tree, refused = existing_tree(project_dir, failure.head)
    if refused:
        print(f"  {refused}", file=sys.stderr)
        return EXIT_FAILED
    tree = tree or cut_tree(project_dir, failure.head, runner)
    if tree is None:
        print(f"  no worktree for {failure.head}; nothing opened", file=sys.stderr)
        return EXIT_FAILED
    if failure.kind == fix_plan.PR and (stale := refresh_head(tree, failure.head)):
        print(f"  {stale}")
    gate_evidence.place(failure, tree)
    if key:
        fix_reports.stamp(tree, key, fix_plan.describe(failure))
    print(f"  worktree {tree}")
    prompt = tab_safe(fix_prompts.pr_prompt(failure))
    return open_session(launch, tree, failure.head, prompt, f"{failure.project} {name}", runner)


def dispatch_fresh(
    decision: fix_plan.Decision,
    root: Path,
    launch: agent_models.Launch,
    runner=subprocess.run,
    key: str = "",
) -> int:
    """A nightly, or a vendored failure shared across consumers: a fresh branch."""
    first = decision.failures[0]
    upstream = decision.action == fix_plan.UPSTREAM
    project = DEVKIT if upstream else first.project
    project_dir = root / project
    if not project_dir.is_dir():
        print(f"  no checkout {project!r} in {root}; nothing opened", file=sys.stderr)
        return EXIT_FAILED
    base = tb.detect_default_branch(sweep.git_for(project_dir)) if upstream else first.base
    print(f"{project}: {decision.note}")
    tree, branch = cut_fresh_tree(project_dir, fix_branch(decision), base, runner)
    if tree is None:
        print(f"  could not cut {branch} off origin/{base}; nothing opened", file=sys.stderr)
        return EXIT_FAILED
    for failure in decision.failures:
        # One directory per failure: a devkit session can hold two of one project's.
        gate_evidence.place(failure, tree, gate_evidence.evidence_slot(failure) if upstream else "")
    if key:
        fix_reports.stamp(tree, key, decision.note)
    print(f"  worktree {tree} on {branch}")
    if upstream:
        prompt, title = fix_prompts.upstream_prompt(decision.failures, branch), f"devkit {branch}"
    elif first.kind == fix_plan.BRANCH:
        prompt, title = fix_prompts.branch_prompt(first, branch), f"{project} {first.base}"
    else:
        prompt, title = fix_prompts.nightly_prompt(first, branch), f"{project} {first.workflow}"
    return open_session(launch, tree, branch, tab_safe(prompt), title, runner)


def run_plan(
    workspace: Path,
    launch: agent_models.Launch,
    dry_run: bool,
    redo: bool,
    runner=subprocess.run,
) -> int:
    """Scan, read the evidence, plan, print the plan, then send what is new.

    The ledger is written only for a dispatch that opened: a session that failed to
    start is not one a second click should be told already happened.
    """
    root = workspace.parent
    found = menu.scan(workspace)
    failures = gate_evidence.collect(workspace, found)
    newest = gate_evidence.newest_release(root / DEVKIT)
    decisions = fix_plan.plan(failures, newest, adoption_prs.adoption_prefixes())
    ledger_path = worktree.boxes_root(root) / fix_ledger.LEDGER_NAME
    ledger = fix_ledger.read_ledger(ledger_path)
    print(fix_ledger.render(decisions, ledger))
    if dry_run:
        return EXIT_OK
    worst = EXIT_OK
    for decision in decisions:
        if decision.action in (fix_plan.SKIP, fix_plan.HOLD):
            continue
        if not redo and fix_ledger.already_sent(decision, ledger):
            continue
        first = decision.failures[0]
        key = fix_ledger.decision_key(decision)
        on_branch = first.kind in (fix_plan.PR, fix_plan.COMMIT)
        if decision.action in (fix_plan.DISPATCH, fix_plan.RESOLVE) and on_branch:
            code = dispatch_pr(first, root, launch, runner, key)
        else:
            code = dispatch_fresh(decision, root, launch, runner, key)
        if code == EXIT_OK:
            fix_ledger.record(ledger_path, key, decision.note)
        worst = max(worst, code)
    return worst


# --- the CLI ------------------------------------------------------------------------


def render_scan(found: dict[str, list[dict]]) -> str:
    """`--list`, for the terminal: what is red, per checkout, before any evidence."""
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
        "--agent",
        default="claude",
        choices=sorted(AGENT_MODES),
        help="which CLI opens, and whether it opens in a tab or in the background",
    )
    parser.add_argument(
        "--picks",
        default="",
        help=(
            f"by hand: specific PRs, `<project>{menu.PICK_SEP}<number>` joined by a space; "
            "skips the plan and the ledger"
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="print the plan and open nothing")
    parser.add_argument(
        "--redo", action="store_true", help="send again what the ledger says was already sent"
    )
    parser.add_argument("--list", action="store_true", help="print the broken PRs and stop")
    parser.add_argument("--workspace", type=Path, default=worktree.DEFAULT_WORKSPACE)
    agent_models.add_arguments(parser)
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
        if args.list:
            print(render_scan(menu.scan(workspace)))
            return EXIT_OK
        launch = agent_models.Launch.parse(args.agent, args.model, args.effort)
        tokens = menu.split_picks(args.picks)
        if not tokens:
            return run_plan(workspace, launch, args.dry_run, args.redo)
        picks = [menu.parse_pick(token) for token in tokens]
        return run(picks, workspace, launch)
    except (menu.FixError, worktree.WorktreeError, devkit_project.ProjectError) as exc:
        print(f"fix-prs: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
