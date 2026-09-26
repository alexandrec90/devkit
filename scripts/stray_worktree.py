#!/usr/bin/env python3
"""A worktree that holds a PR's head branch and belongs to no tier `fix-prs.py` reuses.

Git permits one worktree per branch, so a tree like that blocks every fixer sent at the
PR. `fix-prs.existing_tree` decides whether the holder is reusable; this module answers
what happens when it is not: release it if it holds no work, otherwise refuse and name
the directory. Split out of `fix-prs.py`, which is at its structure limits and whose own
subject is launching sessions.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_worktrees as aw
import sweep

sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))
import worktree_tiers as wt


def release(project_dir: Path, held: Path, branch: str) -> bool:
    """Remove a stray worktree that holds `branch` and nothing else. True once it is gone.

    A session that pushed a PR from a tree in its own scratchpad leaves the branch held
    there after it ends, and every later fixer for that PR was refused on the directory
    alone -- three at once on one pass. A tree that is clean and whose branch has no
    commit its upstream lacks holds no work, so it is released and the fixer cuts its own.

    Never the checkout itself, never a box (its lease and reaper would leak), never a tree
    whose upstream cannot be read, and never with `--force`: git's own refusal of a dirty
    tree, or Windows' of a directory a live process sits in, stands as the refusal.
    """
    if wt.same_dir(held, project_dir) or held.parent.name == wt.BOXES_DIR_NAME:
        return False
    inner = sweep.git_for(held)
    status = inner("status", "--porcelain")
    if status.returncode != 0 or (status.stdout or "").strip():
        return False
    ahead = inner("rev-list", "--count", f"{branch}@{{u}}..{branch}")
    if ahead.returncode != 0 or (ahead.stdout or "").strip() != "0":
        return False
    removed = sweep.git_for(project_dir)("worktree", "remove", str(held))
    if removed.returncode != 0:
        return False
    print(f"  released {held}: clean and pushed, so nothing held {branch} but the directory")
    return True


def refusal(project_dir: Path, held: str, branch: str) -> str:
    """`""` once `release` has freed `branch`, else the refusal naming the directory.

    `held` is the path as `git worktree list` spelled it, and the refusal keeps that
    spelling so the operator sees the directory git knows it by.
    """
    if release(project_dir, Path(held), branch):
        return ""
    return (
        f"{branch} is already checked out at {held}, which is not in "
        f"{aw.TIER_SUMMARY} or a matching live devkit box -- finish the PR from there"
    )
