#!/usr/bin/env python3
"""A worktree at one commit that outlives one run, for a job that must not touch a checkout.

The static checkout belongs to whoever is working in it, and a job that fires every few
minutes cannot wait for it to be clean. So the commit gets a detached tree of its own,
provisioned the way every other tree is, and destroyed when the run is over.

**It is cut in the default tier, `<checkout>/.claude/worktrees/`, not in a temp
directory**, because that is where a project's relative path dependencies resolve:
`worktree_env.link_path_sources` cuts a tree's `../sibling` there, and every project
already ignores the directory. `session_trees.verdict` leaves a detached tree alone, so no
reaper races the run. That also means none clears up after a crash, and `sweep_leftovers`
is that half: the prefix is this module's, and whoever holds the machine's run knows
nothing else of it can be live.

**A tree can outlive every delete.** uv links a package's files out of its cache, and
Windows refuses to unlink any link of an image something has mapped, so a plain `rmtree`
stops at the first `.pyd` and leaves a husk with no `.git`. `worktree prune` then forgets
it, git's list no longer names it, and the next cut at that sha fails every fire. So the
delete sets such a file aside (`box_teardown._set_aside`), git's record goes only once the
directory has, and leftovers are found on disk as well as in git's list.

Tested in `tests/test_disposable_tree.py`.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))  # the vendored tier
import agent_worktrees as aw
import box_teardown
import e2e_results
import sweep
import tree_provision
import worktree_env

# What names a tree this module cut, in a directory every agent CLI also cuts into.
PREFIX = "disposable-"
# Git's checkout and the `post-checkout` hook, which provisions a tree whose checkout has
# a `.venv`; a cold `uv sync` is most of that.
CUT_TIMEOUT = 600.0
# `worktree.py provision`: a cold `npm ci` on top of that.
PROVISION_TIMEOUT = 900.0
REMOVE_TIMEOUT = 300.0

Runner = Callable[..., object]


def path_for(checkout: Path, sha: str) -> Path:
    return aw.default_root(checkout) / f"{PREFIX}{sha[:12]}"


def cut(checkout: Path, sha: str, runner: Runner = sweep.run_bounded) -> tuple[Path | None, str]:
    """`(tree, "")` detached at `sha`, or `(None, why)` when git refused.

    `--force` twice, because `git worktree add` locks a tree `initializing` while it
    works, a killed one leaves that lock on a directory that is gone, and one `--force`
    does not get past a locked entry.
    """
    tree = path_for(checkout, sha)
    tree.parent.mkdir(parents=True, exist_ok=True)
    add = ("worktree", "add", "--quiet", "--force", "--force", "--detach", str(tree), sha)
    done = runner(["git", "-C", str(checkout), *add], CUT_TIMEOUT)
    if e2e_results.ok(done):
        return tree, ""
    return None, e2e_results.said(done)


def provision(tree: Path, runner: Runner = sweep.run_bounded) -> str:
    """ "" once the tree has its toolchain, the post-checkout hook's or `worktree.py`'s;
    else why not, naming a manifest that does not resolve (`worktree_env.unresolvable`)
    over the last line, since the caller reads that as the project's own defect."""
    heard: list[object] = []

    def bounded(argv: list[str], **kwargs: object) -> object:
        # `tree_provision` speaks `subprocess.run`; `run_bounded` always captures.
        kwargs.pop("check", None)
        kwargs.pop("capture_output", None)
        heard.append(runner(argv, PROVISION_TIMEOUT, **kwargs))
        return heard[-1]

    if not tree_provision.needs_provision(tree) or tree_provision.provision(tree, bounded):
        return ""
    done = heard[-1]
    streams = (getattr(done, "stderr", ""), getattr(done, "stdout", ""))
    return worktree_env.unresolvable(*streams) or e2e_results.said(done)


def destroy(checkout: Path, tree: Path, runner: Runner = sweep.run_bounded) -> str:
    """Remove the tree and git's record of it; what stopped that, or "" when it is gone.

    `--force` twice for the lock `cut` says about. What git leaves is deleted by path, and
    git's record pruned only once the directory has gone: a husk it has forgotten is
    found by nothing but the walk of the disk in `leftovers`.
    """
    git = ["git", "-C", str(checkout)]
    remove = ("worktree", "remove", "--force", "--force", str(tree))
    if e2e_results.ok(runner([*git, *remove], REMOVE_TIMEOUT)):
        return ""
    error = box_teardown.remove_tree_longpath(tree, set_aside=True) if tree.exists() else ""
    if tree.exists():
        return f"could not remove {tree}: {error}"
    runner([*git, "worktree", "prune"], REMOVE_TIMEOUT)
    return ""


def leftovers(checkout: Path, porcelain: str) -> list[Path]:
    """This module's trees, in `git worktree list --porcelain` or only on disk."""
    root = aw.default_root(checkout).resolve()
    listed = (Path(path) for path, _branch in aw.parse_worktree_list(porcelain))
    found = {tree.resolve() for tree in listed if tree.parent.resolve() == root}
    found |= {tree.resolve() for tree in root.glob("*") if tree.is_dir()}
    return sorted(tree for tree in found if tree.name.startswith(PREFIX))


def sweep_leftovers(checkout: Path, runner: Runner = sweep.run_bounded) -> list[str]:
    """Destroy what a crashed run left; a line for each that would not go.

    Only for a caller that holds the only run on this machine: a live tree reads as a
    leftover here. Git's list is only half of the answer, so a list that failed still
    leaves the disk to walk.
    """
    listed = runner(["git", "-C", str(checkout), "worktree", "list", "--porcelain"], REMOVE_TIMEOUT)
    trees = leftovers(checkout, e2e_results.out(listed) if e2e_results.ok(listed) else "")
    return [line for tree in trees if (line := destroy(checkout, tree, runner))]
