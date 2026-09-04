#!/usr/bin/env python3
"""The four clicks that replace the agent-branch hooks: spawn, attach, ship, delete.

`worktree-guard.py` used to do the first of these implicitly -- an agent edit aimed at a
checkout's home branch was routed into a fresh box, and the operator never chose anything.
That is switchable now (`harness-switch.py`), and a tier that can be switched off needs a
manual spelling or switching it off loses the guarantee rather than moving it. This is the
manual spelling, one verb per workspace task:

| verb | task | what it does |
| --- | --- | --- |
| `spawn` | Agent: Spawn Branch, Worktree, Agent | cut the branch and box, provision, open the agent |
| `attach` | Agent: Run Agent on Worktree | the last step of `spawn`, on a box that exists |
| `ship` | Agent: Ship PR | commit, push and open the PR for a box |
| `delete` | Agent: Delete Branch | destroy the box and its local branch |

**`spawn` and `attach` share `open_agent`, and that is the point of having both.** The
fourth task the operator asked for is literally the tail of the first; two copies of the
terminal-launching logic would be two answers to "which window does the agent open in".

`scripts/worktree-guard.md` carries the two decisions a change here has to preserve --
why `ship` bypasses the pre-commit gate, and why `worktree.py new --json` is an interface
because this reads it. Two more live where they are made: `wt_argv` on which window a tab
lands in, and `choose` on why a prompt here is a whole flushed line.

Every subprocess-spawning function takes a runner, and the argv builders are pure; the
tests drive those rather than git. Tested in `tests/test_agent_box.py`.
"""

from __future__ import annotations

import argparse
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
import project_python
import worktree

# Resolved by the second insert above; `scripts/precommit/` is not a package. Used for the
# one neighbour whose name has a hyphen in it; `worktree` above is imported normally, and
# that distinction is load-bearing. `load_by_path` overwrites `sys.modules[name]` with a
# fresh copy, so loading `worktree` that way would give this process a SECOND worktree
# module -- and a test that patches one copy would then be asserting about the other.
# `tests/test_preview_task.py` failed exactly that way, and only when the whole suite ran.
from _loader import load_by_path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKTREE = REPO_ROOT / "scripts" / "worktree.py"

harness_switch = load_by_path("harness_switch", REPO_ROOT / "scripts" / "harness-switch.py")

AGENTS = ("claude", "codex", "none")

# `-w 0` is "the most recently used window", and it creates one when there is none. See
# the module docstring for why this differs from `resume-sessions.py`.
WT_WINDOW = "0"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_FAILED = 1


@dataclass(frozen=True)
class Candidate:
    """One box the operator could be talking about, as a menu row would name it."""

    name: str
    branch: str
    path: str
    pr: str = ""

    def label(self) -> str:
        """The branch, and its PR number when it has one -- so a second push to an open PR
        is a thing the operator sees before choosing it rather than after."""
        return f"{self.branch}  [PR {self.pr}]" if self.pr else self.branch


# --- pure argv builders -------------------------------------------------------------


def agent_command(agent: str, hooks_off: bool) -> str:
    """The one command line the terminal tab runs.

    A string rather than an argv because `wt` hands everything after `-Command` to
    PowerShell as a single line anyway, and the environment assignment has to be part of
    it: `wt` has no way to set a variable for the child it spawns.
    """
    prefix = (
        f"$env:{harness_switch.HOOKS_OFF_ENV}='{harness_switch.HOOKS_OFF_VALUE}'; "
        if hooks_off
        else ""
    )
    return f"{prefix}{agent}"


def wt_argv(title: str, cwd: Path, command: str) -> list[str]:
    """The `wt.exe` arguments for one agent tab in one box."""
    return [
        "-w",
        WT_WINDOW,
        "new-tab",
        "--title",
        title,
        "-d",
        str(cwd),
        # -NoExit for `resume-sessions.py`'s reason: an agent that dies on startup still
        # leaves its error on screen instead of closing the tab it printed it in.
        "pwsh.exe",
        "-NoLogo",
        "-NoExit",
        "-Command",
        command,
    ]


def lint_fix_argvs(python: str, target: Path) -> list[list[str]]:
    """The autofixers, in the order `lint-fix.py` runs them: format, then fix.

    Deliberately only the deterministic pair. A findings pass belongs to the PR gate, and
    running one here whose output nobody is allowed to act on is a wall of text before
    every ship.
    """
    return [
        [python, "-m", "ruff", "format", str(target)],
        [python, "-m", "ruff", "check", "--fix", str(target)],
    ]


def commit_argv(message: str) -> list[str]:
    """`--no-verify` per the module docstring: the gate that would refuse this commit is
    the one whose refusal strands the work in a box."""
    return ["git", "commit", "--no-verify", "-m", message]


def push_argv(branch: str) -> list[str]:
    return ["git", "push", "--no-verify", "-u", "origin", branch]


def pr_argv(title: str, body: str, base: str) -> list[str]:
    return ["gh", "pr", "create", "--base", base, "--title", title, "--body", body]


def ship_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """The environment the git calls run under.

    `--no-verify` already skips the hooks, so this is belt and braces for the one path it
    does not cover: a repo whose policy runs from somewhere other than `core.hooksPath`.
    """
    env = dict(os.environ if base is None else base)
    env["DEVKIT_SKIP_BRANCH_POLICY"] = "1"
    return env


def commit_message(branch: str, files: int) -> str:
    """A subject the branch already contains, because nobody is here to write one.

    `agent/fix-the-thing-0903` -> `Fix the thing`. The PR body carries the real account of
    the change; a commit subject that says "wip" is worse than one derived from the name
    the operator chose when they cut the branch.
    """
    topic = branch.partition("/")[2] or branch
    words = [word for word in topic.split("-") if word and not word.isdigit()]
    subject = " ".join(words).strip().capitalize() or "Update"
    return f"{subject}\n\n{files} file(s) changed on {branch}."


def candidate_menu(candidates: list[Candidate], noun: str) -> str:
    """The numbered list `--branch`-less runs print. One place, so every verb asks the
    same way and a screenshot of one is a screenshot of all four."""
    if not candidates:
        return f"no box to {noun}."
    rows = [f"  {index + 1}. {c.label()}" for index, c in enumerate(candidates)]
    return f"Which box should this {noun}?\n" + "\n".join(rows)


def parse_choice(raw: str, count: int) -> int:
    """A 1-based menu answer as a 0-based index, or -1 for anything else.

    Blank included: an operator who hits enter at a destructive prompt meant "no", and
    defaulting to the first row is how that becomes "yes, the first one".
    """
    try:
        chosen = int(raw.strip())
    except (TypeError, ValueError):
        return -1
    return chosen - 1 if 1 <= chosen <= count else -1


# --- reading the world --------------------------------------------------------------


def boxes_for(
    project: str, workspace: Path, unmerged_only: bool = False, runner=subprocess.run
) -> list[Candidate]:
    """Every live box leased to `project`; with `unmerged_only`, the ones `ship` can act on.

    Read from the lease table rather than from `git branch`, because a branch is not a box:
    the whole point of these verbs is that they act on a worktree, and a branch whose box
    has been reaped has nothing for `attach` or `ship` to run in.

    `unmerged_only` drops a box whose PR has **merged** and keeps one whose PR is open --
    pushing another commit to an open PR is the ordinary way to answer review, and a picker
    that hid it would send the operator looking for a verb that does not exist. It is a
    parameter rather than a second function because the PR lookup is what fills in
    `Candidate.pr`, so the filtered list and the labelled list are the same pass.
    """
    root = workspace.parent
    found = [
        Candidate(name=box.name, branch=box.branch, path=str(worktree.box_path(root, box.name)))
        for box in worktree.live_boxes(root).values()
        if box.project == project
    ]
    if not unmerged_only:
        return found
    source = devkit_project.resolve_project(project, worktree.known_projects(workspace), root)
    offered = []
    for candidate in found:
        number, merged = pr_state_for(source, candidate.branch, runner)
        if not merged:
            offered.append(Candidate(candidate.name, candidate.branch, candidate.path, pr=number))
    return offered


def pr_state_for(source: Path, branch: str, runner=subprocess.run) -> tuple[str, bool]:
    """`(pr number or "", already merged)` for one branch. Never raises.

    `gh` failing means no answer, and no answer has to read as "not shipped": the verb
    this feeds is `ship`, and hiding a box because GitHub was unreachable is how work gets
    left in one.
    """
    try:
        done = runner(
            ["gh", "pr", "list", "--head", branch, "--state", "all", "--json", "number,state"],
            capture_output=True,
            text=True,
            cwd=str(source),
            check=False,
        )
    except OSError:
        return "", False
    if done.returncode != 0:
        return "", False
    try:
        rows = json.loads(done.stdout or "[]")
    except ValueError:
        return "", False
    if not rows:
        return "", False
    row = rows[0]
    return str(row.get("number", "")), str(row.get("state", "")).upper() == "MERGED"


def choose(candidates: list[Candidate], branch: str, noun: str, reader=input) -> Candidate | None:
    """`--branch` when given, otherwise the numbered prompt. None means "nothing to do".

    The prompt is a whole flushed LINE rather than `input("> ")`, and that detail is what
    lets these verbs keep the failure artifact every workspace task owes. `log-wrap.py`
    reads the child's stdout a line at a time; a bare `input` prompt has no newline and
    would sit in a pipe buffer forever, so the operator would see a task that hangs with
    an empty terminal rather than a menu. stdin is inherited either way.
    """
    if branch:
        for candidate in candidates:
            if branch in (candidate.branch, candidate.name):
                return candidate
        print(f"agent-box: no live box on '{branch}'", file=sys.stderr)
        return None
    if not candidates:
        print(candidate_menu(candidates, noun))
        return None
    if len(candidates) == 1:
        print(f"One box: {candidates[0].label()}")
        return candidates[0]
    print(candidate_menu(candidates, noun))
    print("Type a number and press enter:", flush=True)
    index = parse_choice(reader(), len(candidates))
    if index < 0:
        print("agent-box: nothing chosen", file=sys.stderr)
        return None
    return candidates[index]


# --- the verbs ----------------------------------------------------------------------


def open_agent(agent: str, box: Path, branch: str, runner=subprocess.run) -> int:
    """Open one agent tab in `box`. Shared by `spawn` and `attach`."""
    if agent == "none":
        print(f"no agent requested; the box is at {box}")
        return EXIT_OK
    terminal = shutil.which("wt.exe") or shutil.which("wt")
    command = agent_command(agent, harness_switch.hooks_are_off())
    if not terminal:
        print(f"Windows Terminal not found; run this yourself:\n  cd {box}\n  {command}")
        return EXIT_OK
    argv = wt_argv(branch, box, command)
    print(f"opening {agent} in {box}")
    done = runner([terminal, *argv], check=False)
    return EXIT_OK if done.returncode == 0 else EXIT_FAILED


def spawn(
    project: str,
    workspace: Path,
    slug: str,
    base: str,
    agent: str,
    runner=subprocess.run,
) -> int:
    """Cut the branch and box, provision it, then hand it to the agent."""
    argv = [
        sys.executable,
        str(WORKTREE),
        "new",
        project,
        "--slug",
        slug or project,
        "--yes",
        "--json",
        "--workspace",
        str(workspace),
    ]
    if base:
        argv += ["--base", base]
    done = runner(argv, capture_output=True, text=True, check=False)
    sys.stderr.write(done.stderr or "")
    if done.returncode != 0:
        print("agent-box: the box was not cut; nothing to open", file=sys.stderr)
        return EXIT_FAILED
    try:
        plan = json.loads(done.stdout or "{}")
    except ValueError:
        print(done.stdout)
        print("agent-box: could not read the spawn plan", file=sys.stderr)
        return EXIT_FAILED
    box, branch = Path(plan["path"]), plan["box"]["branch"]
    for note in plan.get("notes", []):
        print(f"  {note}")
    print(f"box {plan['box']['name']} on {branch}\n  {box}")
    return open_agent(agent, box, branch, runner)


def ship(project: str, workspace: Path, branch: str, runner=subprocess.run, reader=input) -> int:
    """Autofix, commit everything, push, and open the PR. See the docstring for `--no-verify`."""
    offered = boxes_for(project, workspace, unmerged_only=True, runner=runner)
    candidate = choose(offered, branch, "ship", reader)
    if candidate is None:
        return EXIT_FAILED
    box = Path(candidate.path)
    if not box.is_dir():
        print(
            f"agent-box: {box} is gone; `worktree.py resume {project}` puts it back",
            file=sys.stderr,
        )
        return EXIT_FAILED

    def git(*args: str, **kwargs):
        return runner(
            ["git", "-C", str(box), *args], text=True, env=ship_env(), check=False, **kwargs
        )

    python = project_python.interpreter(box)
    for fixer in lint_fix_argvs(python, box):
        # Non-blocking by design: a finding ruff cannot fix is the PR gate's to report.
        runner(fixer, cwd=str(box), check=False)

    changed = git("status", "--porcelain", capture_output=True).stdout.strip()
    if changed:
        git("add", "-A")
        message = commit_message(candidate.branch, len(changed.splitlines()))
        done = runner([*commit_argv(message)], cwd=str(box), text=True, env=ship_env(), check=False)
        if done.returncode != 0:
            print("agent-box: the commit failed", file=sys.stderr)
            return EXIT_FAILED
        print(f"committed {len(changed.splitlines())} path(s)")
    else:
        print("nothing uncommitted")

    if runner([*push_argv(candidate.branch)], cwd=str(box), env=ship_env(), check=False).returncode:
        print("agent-box: the push failed", file=sys.stderr)
        return EXIT_FAILED
    print(f"pushed {candidate.branch}")

    if candidate.pr:
        print(f"PR {candidate.pr} already open; it now carries the push")
        return EXIT_OK
    root = workspace.parent
    source = devkit_project.resolve_project(project, worktree.known_projects(workspace), root)
    default = worktree.tb.detect_default_branch(worktree.sweep.git_for(source), fallback="main")
    title = commit_message(candidate.branch, 0).splitlines()[0]
    body = f"Shipped from the box `{candidate.name}` by `Agent: Ship PR`."
    if runner(pr_argv(title, body, default), cwd=str(box), check=False).returncode:
        print("agent-box: `gh pr create` failed; the branch is pushed", file=sys.stderr)
        return EXIT_FAILED
    return EXIT_OK


def delete(project: str, workspace: Path, branch: str, runner=subprocess.run, reader=input) -> int:
    """Destroy the box and its local branch.

    `reap --force` rather than `reap`: this verb exists to abandon work, and a reap that
    refuses because the box is dirty is refusing the thing that was asked for. The remote
    branch is left alone and named -- deleting a pushed branch closes its PR, which is a
    different decision from throwing away a worktree.
    """
    candidate = choose(boxes_for(project, workspace), branch, "delete", reader)
    if candidate is None:
        return EXIT_FAILED
    done = runner(
        [
            sys.executable,
            str(WORKTREE),
            "reap",
            candidate.name,
            "--force",
            "--yes",
            "--workspace",
            str(workspace),
        ],
        check=False,
    )
    if done.returncode != 0:
        print("agent-box: the box was not reaped; the branch is untouched", file=sys.stderr)
        return EXIT_FAILED
    root = workspace.parent
    source = devkit_project.resolve_project(project, worktree.known_projects(workspace), root)
    runner(["git", "-C", str(source), "branch", "-D", candidate.branch], check=False)
    print(f"deleted {candidate.branch}; origin's copy, if any, is untouched")
    return EXIT_OK


def attach(
    project: str, workspace: Path, branch: str, agent: str, runner=subprocess.run, reader=input
) -> int:
    candidate = choose(boxes_for(project, workspace), branch, "run an agent in", reader)
    if candidate is None:
        return EXIT_FAILED
    return open_agent(agent, Path(candidate.path), candidate.branch, runner)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="verb", required=True)

    new = sub.add_parser("spawn", help="cut a branch and box and open an agent in it")
    new.add_argument("--project", required=True)
    new.add_argument("--slug", default="", help="what the branch is about")
    new.add_argument("--base", default="", help="base branch (default: the project's own)")
    new.add_argument("--agent", default="claude", choices=AGENTS)

    run = sub.add_parser("attach", help="open an agent in a box that already exists")
    run.add_argument("--project", required=True)
    run.add_argument("--branch", default="")
    run.add_argument("--agent", default="claude", choices=AGENTS)

    out = sub.add_parser("ship", help="commit, push and open the PR for a box")
    out.add_argument("--project", required=True)
    out.add_argument("--branch", default="")

    gone = sub.add_parser("delete", help="destroy a box and its local branch")
    gone.add_argument("--project", required=True)
    gone.add_argument("--branch", default="")

    for one in (new, run, out, gone):
        one.add_argument("--workspace", type=Path, default=worktree.DEFAULT_WORKSPACE)

    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    workspace = args.workspace.resolve()
    if not workspace.is_file():
        print(f"agent-box: no workspace file at {workspace}", file=sys.stderr)
        return EXIT_USAGE

    try:
        if args.verb == "spawn":
            return spawn(args.project, workspace, args.slug, args.base, args.agent)
        if args.verb == "attach":
            return attach(args.project, workspace, args.branch, args.agent)
        if args.verb == "ship":
            return ship(args.project, workspace, args.branch)
        return delete(args.project, workspace, args.branch)
    except (devkit_project.ProjectError, worktree.WorktreeError) as exc:
        print(f"agent-box: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
