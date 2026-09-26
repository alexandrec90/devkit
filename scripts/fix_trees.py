"""The worktree a fixer is opened in: found, cut, and made able to run its checks.

The tree half of `scripts/fix-prs.py`, split out along the section header that module
already drew. `existing_tree` finds a tree already holding the PR's head branch,
`cut_tree` cuts one under `.claude/worktrees/` when nothing does, `cut_fresh_tree`
cuts a new branch for a failure that has none, and `provision_tree`
installs the toolchain into whichever came back without one. Where a worktree for a
branch goes and what git is asked to do are `scripts/agent_worktrees.py`'s; a matching
live box is `worktree.py`'s, reused and never leased from here.

Every function here is tested in `tests/test_fix_prs.py`, through the names that module
imports; the two that cut take a runner.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import agent_worktrees as aw
import project_python
import stray_worktree as stray
import sweep
import worktree


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


def provision_tree(
    tree: Path, plan=worktree.plan_provision, run=worktree.run_provision
) -> list[str]:
    """Install the toolchain into a tree that has no `.venv`; the notes to print.

    A linked worktree checks out tracked files only, and neither `git worktree add` nor
    `claude --worktree` -- which cut most of the trees `existing_tree` reuses -- installs
    anything. So every fixer opened in a checkout that could not run its own tests or
    linter, and spent its first turns on `uv sync` before it could verify the fix it
    was sent for. A tree that already has a `.venv` is left alone: a reused one may hold
    a session still working, and a warm `uv sync` there buys nothing. A failed install
    is a note, not a refusal -- the fixer is told to close that gap itself.
    """
    if (tree / project_python.VENV_DIR).is_dir():
        return []
    steps = plan(tree)
    if not steps:
        return []
    _, notes = run(tree, steps)
    return notes


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
