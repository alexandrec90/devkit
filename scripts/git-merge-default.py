#!/usr/bin/env python3
"""Merge a remote's default branch into whatever branch a checkout is on.

The sibling of `git-sync-keep.py`, and deliberately not a mode of it. That one
**rebases** the current branch onto `origin/<default>`, which is what a task branch
wants: a clean series of commits on top of the trunk, ending in a fast-forward PR.
This one **merges** the trunk in, which is what long-lived work wants -- the history
is already published, so it must not be rewritten, and the integration deserves a
commit of its own.

Two consequences, and they are the whole design:

**It is a workspace task, not a dispatched project action.** `devkit_project.py`
resolves checkouts through `known_projects`, which subtracts `NOT_PROJECTS` --
correctly, because every action it dispatches needs a harness a reference checkout
does not have. Merging the trunk in needs nothing but git: no `.devkit.toml`, no
virtualenv, no vendored tier. So it is the one operation a reference checkout *can*
take, `--checkout` resolves against the raw registry, and nothing about the
dispatcher's contract moves to allow it.

**A conflict is an expected outcome, not a crash.** The merge is left in progress
with every unmerged path named, because resolving it is the next step and aborting
would throw away exactly the state that work needs. The task wraps this in
`log-wrap.py`, so the list survives as `logs/<task>.log` -- something to hand an
agent, rather than something to scrape out of a terminal that has scrolled.

Nothing is pushed, and no branch is created or deleted.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devkit_project
import git_policy
import sweep

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKSPACE = sweep.default_workspace(REPO_ROOT)

# `--base auto` means "ask the remote". Spelled as a value rather than as the absence
# of the flag because the VS Code picker feeding it must supply one real token in
# every branch -- an empty string reaches argparse as a stray positional.
AUTO = "auto"
DEFAULT_REMOTE = "origin"

Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


class MergeError(ValueError):
    """The request cannot be carried out as asked, before anything has been changed."""


# --- pure helpers -----------------------------------------------------------


def every_checkout(workspace_text: str) -> list[str]:
    """Every folder in the registry, reference checkouts included.

    `devkit_project.known_projects` subtracts `NOT_PROJECTS`, and `sweep` subtracts its
    own `DEFAULT_EXCLUDE`, both for the same good reason: those checkouts ship no
    harness, so every action either tool dispatches would fail in one. This task is the
    exception that proves it -- it runs git and nothing else -- and applying the
    exclusion here would remove the checkout the task was written for.
    """
    return sweep.parse_workspace(workspace_text, frozenset())


def conflicted_paths(diff_output: str) -> list[str]:
    """Unmerged paths, from `git diff --name-only --diff-filter=U`."""
    return [line.strip() for line in diff_output.splitlines() if line.strip()]


def remediation(repo: Path, branch: str, target: str, paths: list[str]) -> str:
    """What happens next, written for whoever reads the artifact rather than the run.

    Usually that is not the person who clicked the task -- it is the agent they hand
    the file to -- so the paths, the two branches and the checkout are all named here
    rather than left implicit in the terminal above.
    """
    listed = "\n".join(f"  {path}" for path in paths)
    return (
        f"MERGE CONFLICT: {len(paths)} file(s) unmerged, merging {target} into {branch!r}.\n"
        "The merge is left IN PROGRESS on purpose -- resolving it is the next step, and\n"
        "aborting would discard the state that work needs.\n\n"
        f"{listed}\n\n"
        "Hand to a coding agent, verbatim:\n"
        f'  "Resolve the merge conflicts in {repo}. {target} is being merged into '
        f'{branch}; finish the merge, do not abort it."\n\n'
        f"Or finish it by hand, in {repo}:\n"
        "  resolve each file, then `git add <file>` and `git commit --no-edit`\n"
        "  or back out entirely with `git merge --abort`\n"
    )


def target_repo(checkout: str, workspace: Path) -> Path:
    """The directory to run in: a named checkout, or the cwd when none is given.

    Validated against the registry rather than by `is_dir()`, so a stale picker entry
    fails naming the real checkouts instead of running somewhere that happens to exist.
    """
    if not checkout:
        return Path.cwd()
    try:
        text = workspace.read_text(encoding="utf-8")
    except OSError as exc:
        raise MergeError(f"cannot read the workspace registry at {workspace}: {exc}") from exc
    return devkit_project.resolve_project(
        checkout, every_checkout(text), workspace.parent, noun="checkout"
    )


# --- git --------------------------------------------------------------------


def runner_for(repo: Path) -> Runner:
    """A `Runner` bound to one checkout. `git_policy.run_command` never raises."""
    return lambda argv: git_policy.run_command(argv, cwd=repo)


def git(run: Runner, *args: str) -> subprocess.CompletedProcess[str]:
    return run(["git", *args])


def said(result: subprocess.CompletedProcess[str]) -> str:
    """Whatever the command reported, whichever stream it chose."""
    return ((result.stdout or "") + (result.stderr or "")).strip()


def resolve_base(run: Runner, remote: str, requested: str) -> str:
    """The branch to merge FROM: the one asked for, or the remote's own default.

    Detection is `git_policy.default_branch` rather than a fourth copy of the same
    symbolic-ref-then-guess ladder. It reads `refs/remotes/<remote>/HEAD`, which is how
    a repo whose trunk is called neither `main` nor `master` -- `develop`, say -- gets
    resolved without anything here knowing the name.

    The one thing it deliberately does not do is ask the remote: it backs a pre-commit
    hook, where a network round trip is a hang. Here there is no such constraint and we
    have just fetched, so an unpopulated `<remote>/HEAD` is repaired first. Without
    that, the ladder falls through to probing `main` then `master` -- which is a *guess*
    in a repo that has both a `main` and a `develop`, and guesses wrong in the direction
    that merges the wrong branch into someone's work.
    """
    if requested and requested != AUTO:
        return requested
    if git(run, "symbolic-ref", "--quiet", f"refs/remotes/{remote}/HEAD").returncode != 0:
        git(run, "remote", "set-head", remote, "--auto")
    detected = git_policy.default_branch(run, remote)
    if not detected:
        raise MergeError(
            f"could not determine {remote}'s default branch; pass --base <branch> to say which"
        )
    return detected


def merge(run: Runner, repo: Path, remote: str, requested_base: str) -> int:
    """Fetch, then merge `<remote>/<base>` into the checked-out branch.

    0 when merged or already current, 1 when git refused or the merge conflicted, 2
    when the request itself was wrong and nothing was attempted.
    """
    if git(run, "rev-parse", "--show-toplevel").returncode != 0:
        print(f"{repo} is not a git repository.", file=sys.stderr)
        return 2

    branch = said(git(run, "branch", "--show-current"))
    if not branch:
        print("HEAD is detached. Check out a branch first.", file=sys.stderr)
        return 2

    print(f"Fetching {remote} in {repo} ...", flush=True)
    fetched = git(run, "fetch", "--prune", remote)
    if fetched.returncode != 0:
        print(said(fetched), file=sys.stderr)
        return 1

    try:
        base = resolve_base(run, remote, requested_base)
    except MergeError as exc:
        print(f"git-merge-default: {exc}", file=sys.stderr)
        return 2
    target = f"{remote}/{base}"

    exists = git(run, "rev-parse", "--verify", "--quiet", f"refs/remotes/{remote}/{base}")
    if exists.returncode != 0:
        print(f"{target} does not exist on this remote.", file=sys.stderr)
        return 2

    behind = said(git(run, "rev-list", "--count", f"HEAD..{target}"))
    if behind == "0":
        print(f"Already up to date: {branch!r} contains every commit on {target}.")
        return 0

    dirty = said(git(run, "status", "--porcelain"))
    if dirty:
        # Not a refusal: git merges happily around local edits it does not need to
        # touch, and refuses -- cleanly, changing nothing -- when it does. Saying so up
        # front is what makes that refusal legible when it arrives.
        print(
            f"Note: {len(dirty.splitlines())} file(s) have uncommitted changes. "
            "git will refuse the merge if it needs to overwrite one of them."
        )

    print(f"Merging {target} into {branch!r} ({behind} commit(s)) ...", flush=True)
    merged = git(run, "merge", "--no-edit", target)
    if merged.returncode == 0:
        print(said(merged))
        print(f"Done. {branch!r} now contains {target}. Nothing was pushed.")
        return 0

    paths = conflicted_paths(said(git(run, "diff", "--name-only", "--diff-filter=U")))
    if not paths:
        # Refused rather than conflicted -- uncommitted changes in the way, unrelated
        # histories, a merge already in progress. Nothing was started, so there is
        # nothing to resolve and git's own message is the useful one.
        print(said(merged), file=sys.stderr)
        return 1
    print(remediation(repo, branch, target, paths), file=sys.stderr)
    return 1


# --- entrypoint -------------------------------------------------------------


def main(argv: list[str] | None = None, run_factory: Callable[[Path], Runner] = runner_for) -> int:
    parser = argparse.ArgumentParser(
        description="Merge a remote's default branch into the branch a checkout is on."
    )
    parser.add_argument(
        "--checkout",
        default="",
        help="a checkout name from the workspace registry; omit to use the current directory",
    )
    parser.add_argument(
        "--base",
        default=AUTO,
        help=f"branch to merge FROM, or {AUTO!r} (default) to read it off the remote",
    )
    parser.add_argument("--remote", default=DEFAULT_REMOTE)
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    args = parser.parse_args(argv)

    try:
        repo = target_repo(args.checkout, args.workspace)
    except (MergeError, devkit_project.ProjectError) as exc:
        print(f"git-merge-default: {exc}", file=sys.stderr)
        return 2

    return merge(run_factory(repo), repo, args.remote, args.base)


if __name__ == "__main__":
    raise SystemExit(main())
