#!/usr/bin/env python3
"""Merge a remote's default branch into whatever branch a checkout is on.

It **merges** the trunk in, which is what long-lived work wants -- the history is
already published, so it must not be rewritten, and the integration deserves a commit
of its own.

There was a sibling, `git-sync-keep.py`, and this was deliberately not a mode of it:
that one **rebased** the current branch onto `origin/<default>`, which is what a task
branch wants. It is gone, with its task, because VS Code's own Source Control view
rebases a checkout onto its trunk without a picker or a script. Nothing there does what
the rest of this file is for.

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

Nothing is pushed, and no branch is created or deleted. Uncommitted work is stashed
and restored when git would otherwise refuse over it -- see `merge`.
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

# Named, not generated: it is what a reader of `git stash list` sees weeks later, and
# what every message here tells them to look for.
STASH_MESSAGE = "git-merge-default: set aside so the trunk could be merged"

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


def held_in_the_stash(stashed: bool) -> str:
    """The paragraph every failure path owes the reader once work has been set aside.

    Silence here is the one unrecoverable outcome this script can produce. The changes
    are safe -- `git stash` does not lose things -- but a working tree that emptied
    itself during a task the user clicked for a *merge* reads as the task having eaten
    the work, and the recovery is one command nobody guesses under that impression.
    """
    if not stashed:
        return ""
    return (
        f"\nYour uncommitted work is NOT lost: it is stashed as {STASH_MESSAGE!r}.\n"
        "  `git stash list` to see it, `git stash pop` to put it back once the tree is\n"
        "  in a state you want it back in.\n"
    )


def remediation(repo: Path, branch: str, target: str, paths: list[str], stashed: bool) -> str:
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
        f"{held_in_the_stash(stashed)}"
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


def reason(result: subprocess.CompletedProcess[str], what: str) -> str:
    """Why it failed, and never the empty string.

    `said` returning nothing is not hypothetical: a captured stream can arrive as
    `None`, and a git command can fail having written to neither. Printing that as-is
    puts a blank line in `logs/<task>.log` where the diagnosis belongs -- an artifact
    whose only content is `# exit: 1`, which is the one failure mode this task's whole
    log-to-a-file design exists to prevent. The exit code is a poor explanation but it
    is a real one, and it says which command produced it.
    """
    return said(result) or f"{what} failed with exit code {result.returncode}, saying nothing."


def fetch(run: Runner, remote: str) -> subprocess.CompletedProcess[str]:
    """Bring the remote-tracking refs up to date, pruning if the filesystem allows it.

    `--prune` is housekeeping and this task is a merge, so a prune that fails must not
    take the merge down with it -- but git reports the whole fetch as failed when only
    the prune half did, having already updated every ref the merge needs.

    That is not a hypothetical. A prune deletes its refs in one transaction, and each
    deletion locks `<ref>.lock` -- a *path*. On Windows and macOS that path is
    case-insensitive, so two dead branches differing only in case, `feature/41415_Hide`
    and `feature/41415_hide`, lock the same file and the second fails `File exists`.
    Git then advises terminating the other git process, of which there is none, and the
    condition is permanent: every fetch afterwards fails identically until someone
    deletes one of the refs by hand. A shared checkout of a big repo with a long history
    of branch names accumulates these.

    So a failed prune is retried without it, and only a fetch that fails on its own
    terms is a failure. The prune's message is printed either way -- the stale refs are
    real, someone has to clear one of them eventually, and if the retry fails too then
    this was the first of two things that went wrong rather than a footnote to it.

    The retry says `--no-prune` rather than merely dropping the flag, and that is the
    difference between this working and not: `fetch.prune=true` is a common setting --
    it is set in the checkout this was written for -- and under it a bare `git fetch`
    prunes, so the retry would reproduce the failure it exists to route around.
    """
    pruned = git(run, "fetch", "--prune", remote)
    if pruned.returncode == 0:
        return pruned
    print(
        f"Note: pruning stale {remote} refs failed; retrying without --prune. "
        "git said:\n"
        f"{reason(pruned, f'git fetch --prune {remote}')}",
        file=sys.stderr,
        flush=True,
    )
    return git(run, "fetch", "--no-prune", remote)


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


def refused_over_local_changes(merged: subprocess.CompletedProcess[str]) -> bool:
    """git declined before starting, because the merge needs a file you have edited.

    Matched on git's own sentence rather than on the exit code, which is 1 for every
    refusal and for a conflict alike. Both spellings -- "Your local changes to the
    following files would be overwritten by merge" and the untracked-file variant --
    end in the same clause, which is the part being matched.
    """
    return merged.returncode != 0 and "would be overwritten by merge" in said(merged)


def merge_around_local_changes(
    run: Runner,
    target: str,
    refusal: subprocess.CompletedProcess[str],
) -> tuple[subprocess.CompletedProcess[str], bool]:
    """Set the working tree aside, merge, and report whether the stash is still held.

    This is the case the task exists for and the one it used to fail on. The checkout it
    was written for carries months of uncommitted work -- 147 files when this was
    written -- and the trunk moves under it constantly, so "git refuses when the merge
    touches a file you have edited" is not an edge case there, it is every run. A
    one-click task whose outcome on its own target is always "refused, do it yourself"
    is a task that does not work.

    It is deliberately reached only *after* git has refused, not whenever the tree is
    dirty. git merges around edits it does not need to touch, and that path leaves the
    working tree untouched -- no stash, no restore, nothing to go wrong. Stashing up
    front would trade a guarantee for a convenience on every run that never needed it.

    `--include-untracked` because an incoming file landing on an untracked path is one
    of the two refusals being answered. Ignored files stay put; `git stash` does not
    take those without `--all`, and sweeping a build tree into a stash entry is not
    something a merge task should ever do.
    """
    print(
        "git refused: the merge needs files you have edited. Setting them aside and retrying.",
        flush=True,
    )
    pushed = git(run, "stash", "push", "--include-untracked", "-m", STASH_MESSAGE)
    if pushed.returncode != 0:
        # Nothing was set aside, so nothing has to be put back and the refusal stands.
        print(reason(pushed, "git stash push"), file=sys.stderr)
        return refusal, False
    return git(run, "merge", "--no-edit", target), True


def restore(run: Runner) -> bool:
    """Put the stashed work back. False when it did not go back cleanly.

    A pop that conflicts leaves the entry in the stash *and* the conflict markers in the
    tree -- git's behaviour, and the right one -- so the work exists twice rather than
    not at all. That is worth saying out loud, because a working tree full of markers
    after a task that reported success is otherwise indistinguishable from damage.
    """
    print("Restoring your uncommitted work ...", flush=True)
    popped = git(run, "stash", "pop")
    if popped.returncode == 0:
        return True
    print(
        f"{said(popped)}\n\n"
        "Your work came back with conflicts against what was merged, so it is BOTH in\n"
        "the working tree (with conflict markers) and still in the stash. Resolve the\n"
        "files, then `git stash drop` the entry once you are happy with them."
        f"{held_in_the_stash(True)}",
        file=sys.stderr,
    )
    return False


def merge(run: Runner, repo: Path, remote: str, requested_base: str) -> int:
    """Fetch, then merge `<remote>/<base>` into the checked-out branch.

    0 when merged or already current, 1 when git refused or the merge conflicted, 2
    when the request itself was wrong and nothing was attempted.

    Uncommitted work is stashed only if git refuses over it, and put back on every path
    out of here that leaves the tree quiescent -- after a clean merge, and after a
    second refusal that started nothing. The one path that deliberately leaves it in the
    stash is a *conflicted* merge, where popping into half-merged files would bury the
    conflicts the next step has to resolve; `remediation` says where it is.
    """
    if git(run, "rev-parse", "--show-toplevel").returncode != 0:
        print(f"{repo} is not a git repository.", file=sys.stderr)
        return 2

    branch = said(git(run, "branch", "--show-current"))
    if not branch:
        print("HEAD is detached. Check out a branch first.", file=sys.stderr)
        return 2

    print(f"Fetching {remote} in {repo} ...", flush=True)
    fetched = fetch(run, remote)
    if fetched.returncode != 0:
        print(reason(fetched, f"git fetch {remote}"), file=sys.stderr)
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
        # Not a warning about a refusal any more -- a refusal is now handled below --
        # but still worth stating, because it is what makes the stash step legible when
        # it happens.
        print(
            f"Note: {len(dirty.splitlines())} file(s) have uncommitted changes. "
            "They will be set aside and restored if the merge needs to touch one."
        )

    print(f"Merging {target} into {branch!r} ({behind} commit(s)) ...", flush=True)
    merged = git(run, "merge", "--no-edit", target)
    stashed = False
    if refused_over_local_changes(merged):
        merged, stashed = merge_around_local_changes(run, target, merged)

    if merged.returncode == 0:
        print(said(merged))
        if stashed and not restore(run):
            return 1
        print(f"Done. {branch!r} now contains {target}. Nothing was pushed.")
        return 0

    paths = conflicted_paths(said(git(run, "diff", "--name-only", "--diff-filter=U")))
    if not paths:
        # Refused rather than conflicted -- unrelated histories, a merge already in
        # progress, or local changes a stash could not be taken of. Nothing was started,
        # so the tree is where it was and anything set aside belongs back in it.
        print(reason(merged, f"git merge --no-edit {target}"), file=sys.stderr)
        if stashed and not restore(run):
            return 1
        return 1
    print(remediation(repo, branch, target, paths, stashed), file=sys.stderr)
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
