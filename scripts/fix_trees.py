"""The worktree a fixer is opened in: found, cut, and made able to run its checks.

The tree half of `scripts/fix-prs.py`, split out along the section header that module
already drew. `existing_tree` finds a tree already holding the PR's head branch,
`cut_tree` cuts one under `.claude/worktrees/` when nothing does, `cut_fresh_tree`
cuts a new branch for a failure that has none, `provision_tree`
installs the toolchain into whichever came back without one, `locked_caches` names
what an elevated session left in it that the fixer cannot open, and `provenance` says
who made a reused one. Where a worktree for a
branch goes and what git is asked to do are `scripts/agent_worktrees.py`'s; a matching
live box is `worktree.py`'s, reused and never leased from here.

Every function here is tested in `tests/test_fix_prs.py` (see `COVERED_BY` in
`tests/test_test_contract.py`); the two that cut take a runner.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import agent_worktrees as aw
import fix_reports
import stray_worktree as stray
import sweep
import tree_provision
import worktree

# `project_python.VENV_DIR`, spelled out rather than imported: the fix pass is a
# scheduled job, and importing that module puts its spawns -- `re_exec` streams, so it
# cannot take `NO_WINDOW` -- in the set `tests/test_scheduled_jobs.py` checks, for a
# string. `worktree.plan_provision` writes the same literal.
VENV_DIR = ".venv"

# A tool cache an elevated run leaves owner-only (`locked_caches`), and the environment
# variable that sends that tool's next run somewhere else.
LOCKABLE = {".pytest_cache": "PYTEST_ADDOPTS=-p no:cacheprovider"}


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


def cut_tree(project_dir: Path, branch: str, runner=sweep.run_windowless) -> Path | None:
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
    was sent for. A tree that already has a `.venv` is left alone, since a reused one may
    hold a session still working and a warm `uv sync` there buys nothing. The exception
    is a frontend tier whose `node_modules` holds no finished install
    (`tree_provision.frontend_missing`): installing into an empty directory takes nothing
    away from anyone. A failed install is a note, not a refusal -- the fixer is told to
    close that gap itself.
    """
    if (tree / VENV_DIR).is_dir() and not tree_provision.frontend_missing(tree):
        return []
    steps = plan(tree)
    if not steps:
        return []
    _, notes = run(tree, steps)
    return notes


def locked_caches(tree: Path, opener=os.scandir) -> dict[str, str]:
    """The entries of `LOCKABLE` at `tree`'s root this process may not even open, each
    with the setting that routes around it.

    Python's `mkdtemp` makes a directory owner-only, and pytest makes its cache with it,
    so a reused tree an *elevated* session ran pytest in holds a `.pytest_cache` whose
    owner is Administrators -- deny-only in the pass's token and in the fixer's. Nothing
    unelevated can open, rename or delete it (probed: `os.rename` in place is refused
    too), so it cannot be cleared from here; the fixer is told instead, or it spends
    turns finding `WinError 5` (sports_betting #48's, 2026-09-29). ruff and mypy make
    theirs with a plain `mkdir`, which inherits the tree's ACL, so they are not listed.
    """
    locked: dict[str, str] = {}
    for name, setting in LOCKABLE.items():
        try:
            with opener(tree / name):
                pass
        except PermissionError:
            locked[name] = setting
        except OSError:
            continue
    return locked


# The well-known SID of `BUILTIN\Administrators`, which owns what an elevated process
# creates on Windows (see `git_trust.py`).
ADMINISTRATORS_SID = "S-1-5-32-544"


def owner_sid(path: Path) -> str:
    """The string SID of `path`'s owner; "" off Windows or when it cannot be read.

    `sys.platform` for the reason `wt_profile.is_elevated` gives: it is the spelling of
    "not Windows" mypy narrows on the Linux CI, where `ctypes.windll` does not exist.
    """
    if sys.platform != "win32":
        return ""
    import ctypes

    advapi32, kernel32 = ctypes.windll.advapi32, ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    owner, descriptor, text = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    # SE_FILE_OBJECT (1), OWNER_SECURITY_INFORMATION (1); the owner points into the
    # descriptor, which is the one allocation to free.
    failed = advapi32.GetNamedSecurityInfoW(
        str(path), 1, 1, ctypes.byref(owner), None, None, None, ctypes.byref(descriptor)
    )
    if failed:
        return ""
    try:
        if not advapi32.ConvertSidToStringSidW(owner, ctypes.byref(text)) or not text.value:
            return ""
        try:
            return ctypes.wstring_at(text.value)
        finally:
            kernel32.LocalFree(text)
    finally:
        kernel32.LocalFree(descriptor)


def provenance(tree: Path, owner=owner_sid) -> str:
    """Who made a tree the pass is reusing, as its fixer is told; always a sentence.

    `existing_tree` hands a fixer whichever tree holds the PR's head, and that is often
    an operator's own `claude --worktree` checkout, elevated on this machine. Nothing
    said so, and two devkit sessions each spent about eight calls proving that the
    Administrators-owned tree they were in was the operator's and not the dispatcher's
    (df43b14e). The pass's own mark is `fix_reports.ORIGIN_FILE` or a dispatch stamp,
    read before this dispatch stamps it.
    """
    if (tree / fix_reports.ORIGIN_FILE).is_file() or fix_reports.read_stamp(tree):
        maker = "the fix pass cut it for an earlier fixer"
    else:
        maker = (
            "a session a person started made it -- a claude --worktree or a git worktree "
            "add, not the fix pass"
        )
    elevated = owner(tree) == ADMINISTRATORS_SID
    how = (
        ", elevated: the Administrators group owns it, so what that session wrote may "
        "refuse you and nothing unelevated can take it back"
        if elevated
        else ""
    )
    return f" This tree was not cut for you: {maker}{how}. That is settled; spend no turns on who made it."


def cut_fresh_tree(
    project_dir: Path, branch: str, base: str, runner=sweep.run_windowless
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
