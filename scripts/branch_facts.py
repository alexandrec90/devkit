#!/usr/bin/env python3
"""Three facts about a branch, read from git and never guessed.

`gate_evidence.py` asks them while reading what a gate said: is this run about the
commit the branch is at now, is this PR behind the base it will merge into, and has
the release workflow tagged this commit. Each takes a `git` callable (`sweep.git_for`)
so the tests hand in a table and no test reaches a repository.

Every unknown reads as "no": a sha this checkout has not fetched is not evidence that
the PR is behind, and a tip git cannot resolve is not evidence about any run. Tested in
`tests/test_branch_facts.py`.
"""

from __future__ import annotations

from collections.abc import Callable

Git = Callable[..., object]


def branch_tip(git: Git, base: str) -> str:
    """`origin/<base>`'s sha as this checkout last fetched it; "" when it cannot say."""
    tip = git("rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{base}")
    if getattr(tip, "returncode", 1) != 0:
        return ""
    return str(getattr(tip, "stdout", "") or "").strip()


def is_behind(git: Git, base: str, sha: str) -> bool:
    """Whether `sha` lacks `origin/<base>`'s tip: the PR was last built against an old base.

    A red PR that is merely behind is fixed by updating it, not by a session -- #379
    was red on a pip-audit finding master had already fixed. False whenever either
    side is unknown here: not being able to tell is not evidence of anything.
    """
    tip = branch_tip(git, base)
    if not tip or not sha:
        return False
    known = git("rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}")
    if getattr(known, "returncode", 1) != 0:
        return False
    contained = git("merge-base", "--is-ancestor", tip, sha)
    return getattr(contained, "returncode", 1) == 1


def is_tagged(git: Git, sha: str) -> bool:
    """Whether a tag points at `sha`: the release workflow's own verdict on a release commit."""
    if not sha:
        return False
    pointed = git("tag", "--points-at", sha)
    if getattr(pointed, "returncode", 1) != 0:
        return False
    return bool(str(getattr(pointed, "stdout", "") or "").strip())
