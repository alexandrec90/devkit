#!/usr/bin/env python3
"""Cut a devkit release: bump the fallback ref, commit, tag, push -- in that order.

`RELEASING.md` documents this as a five-step manual checklist, and the ordering in
steps 3-5 is the whole point: `FALLBACK_DEVKIT_REF` must be bumped **and committed
before** the tag exists, or `test_fallback_devkit_ref_tracks_the_newest_tag` is
permanently red -- it compares the constant against `git describe --tags`, so a tag
that lands while the constant still names the previous release passes nothing and
breaks every project generated afterwards.

That ordering constraint is exactly why "tag every merge to main" is the wrong
automation: a merge commit has no opportunity to bump the constant first. So this
runs as a deliberate release step instead, doing the checklist in order, and the
`.github/workflows/release.yml` job is a thin trigger over it.

Why a consumer cares at all: an untagged devkit is one no project can pin, and
`sync-devkit.py --pull` now refuses to vendor from one. Cutting releases promptly
is what keeps that guard from becoming an obstacle.

Pure and stdlib-only; the decisions are importable and tested in
`tests/test_release.py`.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

# What `prepare` and the branch-lifecycle helpers below spawn through. They take the
# caller's runner rather than this module's `_git`: their caller is
# `release-pipeline.py`, which targets a devkit path given on its command line rather
# than this file's `REPO_ROOT`, and whose single spawn site carries the console
# discipline the console-less nightly job needs.
GitRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]

REPO_ROOT = Path(__file__).resolve().parents[1]

# `release-pipeline.py` imports this module, and that pipeline is reachable from the
# nightly `devkit-release` job -- whose interpreter is `pythonw.exe`, i.e. console-less,
# which is the condition that makes Windows give every console child a visible window of
# its own. Nothing here spawns on the scheduled path today (only pure helpers are
# imported), but "today" is not a property a reader can check, and the flag costs an
# interactive run nothing.
NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)

NEW_PROJECT = REPO_ROOT / "scripts" / "new-project.py"
FALLBACK_CONST = "FALLBACK_DEVKIT_REF"

# `vMAJOR.MINOR.PATCH`. Deliberately strict: the tag is what consumers pin, and a
# `0.5.3` without the `v`, or a stray suffix, produces a pin that resolves nowhere
# and a failure that surfaces in someone else's repo days later.
VERSION_RE = re.compile(r"^v\d+\.\d+\.\d+$")


def valid_version(version: str) -> bool:
    return bool(VERSION_RE.fullmatch(version))


def bump_fallback(text: str, version: str, const: str = FALLBACK_CONST) -> tuple[str, str | None]:
    """Retarget `const` in new-project.py's source to `version`.

    Returns `(new_text, previous)`; `previous` is None when the constant is absent,
    which means the file is not what this thinks it is -- a caller must treat that
    as a failure rather than releasing anyway.
    """
    pattern = re.compile(rf'^({re.escape(const)}\s*=\s*)"([^"]*)"', re.MULTILINE)
    match = pattern.search(text)
    if match is None:
        return text, None
    return pattern.sub(rf'\g<1>"{version}"', text, count=1), match.group(2)


def prepare_plan(version: str, previous: str, existing_tags: set[str]) -> tuple[list[str], str]:
    """`(steps, refusal)` for phase 1: get the fallback bump onto `main` via a PR.

    Phase 1 does **not** tag. `main` is protected -- devkit's own pre-push policy
    refuses a direct push to it -- so the bump has to travel through a PR, and a tag
    cut before that PR merges names a commit that may never land on `main` at all
    (a squash merge rewrites it). Tagging is `tag_plan` below, after the merge.

    That ordering was learned the hard way: cutting v0.5.3 pushed the tag and then
    had the branch push refused, leaving `main` briefly with a newer tag than its
    own `FALLBACK_DEVKIT_REF` -- the exact red state this constant exists to avoid.
    """
    if not valid_version(version):
        return [], f"{version!r} is not a vMAJOR.MINOR.PATCH tag"
    if version in existing_tags:
        return [], f"{version} already exists -- releases are immutable, pick the next patch"
    if previous == version:
        return [], f"{FALLBACK_CONST} already says {version}; nothing to bump"
    return (
        [
            f"set {FALLBACK_CONST} = {version!r} in scripts/new-project.py (was {previous!r})",
            f"git checkout -b {branch_for(version)}",
            f"git commit -am 'Release {version}'",
            f"git push -u origin {branch_for(version)}",
            f"gh pr create --base main --title 'Release {version}'",
            f"-> merge it, then: release.py {version} --tag",
        ],
        "",
    )


def tag_plan(version: str, main_fallback: str, existing_tags: set[str]) -> tuple[list[str], str]:
    """`(steps, refusal)` for phase 2: tag `main` once the bump has landed there.

    Refuses unless `main` already carries the bump. Tagging a `main` whose constant
    still names the previous release is the failure mode in `prepare_plan`'s note,
    just reached from the other direction.
    """
    if not valid_version(version):
        return [], f"{version!r} is not a vMAJOR.MINOR.PATCH tag"
    if version in existing_tags:
        return [], f"{version} already exists -- releases are immutable"
    if main_fallback != version:
        return [], (
            f"origin/main still has {FALLBACK_CONST} = {main_fallback!r}; "
            f"merge the phase-1 PR before tagging"
        )
    return (
        [
            f"git tag {version} origin/main",
            f"git push origin {version}",
        ],
        "",
    )


def branch_for(version: str) -> str:
    return f"release/{version}"


# --- the release branch's lifecycle -------------------------------------------
#
# Here rather than in `release-pipeline.py` because that module is already over every
# structural limit it has, and because what these need -- `branch_for`, `bump_fallback`,
# `NEW_PROJECT` -- is this module's vocabulary.


def local_branch_exists(devkit: Path, branch: str, run: GitRunner) -> bool:
    """Whether `branch` is a local ref in `devkit`."""
    ref = f"refs/heads/{branch}"
    return run(["git", "-C", str(devkit), "rev-parse", "--verify", "--quiet", ref]).returncode == 0


def pushed_to_origin(devkit: Path, branch: str, run: GitRunner) -> bool | None:
    """Whether `branch` is on origin. `None` when origin could not be asked.

    Put to the remote rather than to a remote-tracking ref, because the checkout asking
    is one a release already failed in and may not have fetched since: the question is
    whether the work escaped, not what this clone last saw. The third answer is the one
    that matters -- the failure that strands a branch is usually the network, so the run
    that meets one is the likeliest to be offline, and it must not read "I could not
    ask" as "never pushed" and delete the branch on the strength of it.
    """
    result = run(["git", "-C", str(devkit), "ls-remote", "--heads", "origin", branch])
    if result.returncode != 0:
        return None
    return bool(result.stdout.strip())


def worktree_holding(devkit: Path, branch: str, run: GitRunner) -> str:
    """The path of the worktree checked out on `branch`, or "" when none is.

    `git worktree list --porcelain` emits a blank-line-separated record per worktree,
    `worktree <path>` first and `branch <ref>` last for a non-detached one.
    """
    listing = run(["git", "-C", str(devkit), "worktree", "list", "--porcelain"])
    if listing.returncode != 0:
        return ""
    ref = f"refs/heads/{branch}"
    path = ""
    for line in (listing.stdout or "").splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree ") :].strip()
        elif line.strip() == f"branch {ref}":
            return path
    return ""


def discard_stale_branch(devkit: Path, branch: str, run: GitRunner) -> tuple[bool, str]:
    """Clear a local `branch` an interrupted release left behind.

    Unpushed, such a branch is not a resumption point but a wall: nobody can see it,
    `git worktree add -b` refuses to reuse it, and so every later run dies in `prepare`
    until someone deletes the ref by hand -- which is how one failed push at 2am stopped
    the nightly release three nights running. There is nothing in it but a bump the next
    run recomputes from a newer `main`.

    Answers `(True, note)` when the caller may proceed -- `note` empty when there was
    nothing to clear -- and `(False, reason)` when the state needs a human.
    """
    if not local_branch_exists(devkit, branch, run):
        return True, ""
    on_origin = pushed_to_origin(devkit, branch, run)
    if on_origin is None:
        return False, (
            f"{branch} exists locally and origin could not be reached to say whether it "
            "was ever pushed -- re-run once origin answers"
        )
    if on_origin:
        return False, (
            f"{branch} is on origin but has no open PR -- reopen its PR, or delete the "
            "branch on both sides, then re-run"
        )
    # A run that was KILLED rather than returned -- OOM, a closed terminal -- leaves its
    # throwaway worktree registered, and git refuses to delete a branch a worktree holds.
    # Dropping the ref was only ever half the wall: `--yes` would then fail with
    # "cannot delete branch 'release/vX.Y.Z' used by worktree at <temp path>" on every
    # later pass, which is the same three-nights-running failure this function exists to
    # end, reached by the other door. `prune` clears registrations whose directory is
    # already gone; one that survives has to be removed, and it is this run's own
    # scratch directory holding nothing but a bump the next pass recomputes.
    run(["git", "-C", str(devkit), "worktree", "prune"])
    holder = worktree_holding(devkit, branch, run)
    if holder:
        released = run(["git", "-C", str(devkit), "worktree", "remove", holder, "--force"])
        if released.returncode != 0:
            failure = (released.stderr or released.stdout).strip()
            return False, (
                f"{branch} is held by the worktree at {holder}, which could not be "
                f"removed: {failure}"
            )
    dropped = run(["git", "-C", str(devkit), "branch", "-D", branch])
    if dropped.returncode != 0:
        failure = (dropped.stderr or dropped.stdout).strip()
        return False, f"could not delete the stale {branch}: {failure}"
    held = f" (released the worktree at {holder})" if holder else ""
    return True, f"discarded the stale unpushed {branch} left by an interrupted run{held}"


def prepare(
    devkit: Path, version: str, run: GitRunner, say: Callable[[str], None]
) -> tuple[bool, str]:
    """Bump, commit and push `release/<version>`, in a worktree cut from origin/main."""
    branch = branch_for(version)
    cleared, note = discard_stale_branch(devkit, branch, run)
    if not cleared:
        return False, note
    if note:
        say(note)
    pushed = False
    with tempfile.TemporaryDirectory(prefix="devkit-release-") as tmp:
        path = Path(tmp) / branch.replace("/", "-")
        add = run(
            ["git", "-C", str(devkit), "worktree", "add", "-b", branch, str(path), "origin/main"]
        )
        if add.returncode != 0:
            return False, (add.stderr or add.stdout).strip()
        try:
            target = path / NEW_PROJECT.relative_to(REPO_ROOT)
            updated, previous = bump_fallback(target.read_text(encoding="utf-8"), version)
            if previous is None:
                return False, f"no {FALLBACK_CONST} in {target.name}"
            target.write_text(updated, encoding="utf-8", newline="\n")
            for step in (
                ("commit", "-am", f"Release {version}"),
                ("push", "-u", "origin", branch),
            ):
                result = run(["git", "-C", str(path), *step])
                if result.returncode != 0:
                    # Both streams: a pre-push hook's findings arrive on stdout, and
                    # git's stderr alone is "failed to push some refs".
                    said = "\n".join(s.strip() for s in (result.stdout, result.stderr) if s)
                    return False, f"`git {' '.join(step)}`: {said.strip()}"
            pushed = True
        finally:
            run(["git", "-C", str(devkit), "worktree", "remove", "--force", str(path)])
            # The branch outlives the worktree only once the push has put it on the
            # remote, where the local ref is what `gh pr create --head` resolves. Left
            # behind after a failed push it strands every later run instead, so every
            # exit but that one takes it along -- the bump is one line, recomputed here.
            if not pushed:
                run(["git", "-C", str(devkit), "branch", "-D", branch])
    return True, branch


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True,
        text=True,
        check=False,
        creationflags=NO_WINDOW,
    )


def existing_tags() -> set[str]:
    """Every local tag; raises `RuntimeError` with git's words when git cannot say.

    An empty set here would pass both plans' "that tag already exists" refusal, so an
    unreadable checkout must not be read as one with no tags.
    """
    result = _git("tag", "--list")
    if result.returncode != 0:
        raise RuntimeError(
            (result.stderr or result.stdout).strip() or f"git exit {result.returncode}"
        )
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def main_fallback() -> str:
    """`FALLBACK_DEVKIT_REF` as it stands on `origin/main`, or "" if unreadable."""
    _git("fetch", "--quiet", "origin")
    result = _git("show", f"origin/main:{NEW_PROJECT.relative_to(REPO_ROOT).as_posix()}")
    if result.returncode != 0:
        return ""
    return bump_fallback(result.stdout, "unused")[1] or ""


def _run(steps: tuple[tuple[str, ...], ...]) -> int:
    """Run git steps in order, stopping at the first failure."""
    for step in steps:
        result = _git(*step)
        if result.returncode != 0:
            print(f"release: FAILED at `git {' '.join(step)}`", file=sys.stderr)
            print((result.stderr or result.stdout).rstrip(), file=sys.stderr)
            return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("version", help="the tag to cut, e.g. v0.5.3")
    parser.add_argument(
        "--tag",
        action="store_true",
        help="phase 2: tag origin/main, once the phase-1 PR has merged",
    )
    apply_mode = parser.add_mutually_exclusive_group()
    apply_mode.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    apply_mode.add_argument("--yes", dest="dry_run", action="store_false")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    # Phase 2's `main_fallback` fetches first, so the tags are read after it: a tag
    # pushed since the last fetch has to be seen by the "already exists" refusal.
    landed = main_fallback() if args.tag else ""
    try:
        tags = existing_tags()
    except RuntimeError as exc:
        print(f"release: could not read devkit's tags: {exc}", file=sys.stderr)
        return 2

    if args.tag:
        steps, refusal = tag_plan(args.version, landed, tags)
    else:
        source = NEW_PROJECT.read_text(encoding="utf-8")
        updated, previous = bump_fallback(source, args.version)
        if previous is None:
            print(f"release: no {FALLBACK_CONST} in {NEW_PROJECT.name}", file=sys.stderr)
            return 2
        steps, refusal = prepare_plan(args.version, previous, tags)

    if refusal:
        print(f"release: {refusal}", file=sys.stderr)
        return 2

    print(f"release: {args.version} ({'tag' if args.tag else 'prepare'})")
    for index, step in enumerate(steps, 1):
        print(f"  {index}. {step}")
    if args.dry_run:
        print("\nDry run -- nothing was changed. Re-run with --yes to apply.")
        return 0

    if args.tag:
        # Tagging `origin/main` by name, not HEAD: the merge happened on the remote,
        # and a stale local checkout would otherwise tag the wrong commit.
        code = _run((("tag", args.version, "origin/main"), ("push", "origin", args.version)))
        if code == 0:
            print(f"release: {args.version} tagged on main. Consumers can adopt it now.")
        return code

    branch = branch_for(args.version)
    NEW_PROJECT.write_text(updated, encoding="utf-8", newline="\n")
    code = _run(
        (
            ("checkout", "-b", branch),
            ("commit", "-am", f"Release {args.version}"),
            ("push", "-u", "origin", branch),
        )
    )
    if code != 0:
        return code
    print(
        f"release: pushed {branch}. Open its PR against main and merge it, then:\n"
        f"  python scripts/release.py {args.version} --tag --yes"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
