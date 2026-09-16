"""Which of the policy's two layouts an install is dealing with, and where it lives.

The first cut of the seam `.devkit-structure.txt` named on 2026-09-15: *"it holds the
runtime layout, the receipt, the drift comparison, ref reading, hooks-path
configuration, plan rendering and the CLI. That is the seam if it is raised again; do
not let it reach five."* This is the raise after that one, so the layout goes.

It earns a module rather than a section because it is the tier with a *decision* in it.
`RUNTIME_FILES` lists the policy in both layouts -- the flat `devkit_git_policy.py` and
the `devkit_git_policy/` package -- so that an install from a ref cut on either side of
the split gets the one that ref actually has. Everything here follows from that one
listing: which destinations count as an entrypoint, which the install must not be
allowed to finish without, and which one has to be removed so Python's resolution order
never gets to pick between two releases.

Stdlib only and free of the installer's I/O, so the rules can be tested without a
filesystem -- `shadowing_entrypoint` and `install_refusal` are pure functions of what an
install wrote.
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from pathlib import Path

RUNTIME_FILES = {
    # THE POLICY IN BOTH LAYOUTS, deliberately, and this entry is why an install from an
    # older tag still works. `scripts/git_policy.py` was one module until it became the
    # `scripts/git_policy/` package; a ref cut before that has the file and not the
    # package, and a ref cut after has the package and not the file. `install_files`
    # already skips a `RUNTIME_FILES` entry the ref does not hold, so listing both means
    # each ref installs the layout it actually has -- and dropping the flat entry would
    # make `--yes` from the newest tag install *no policy at all* until the next release,
    # on every machine, with the hooks left importing a module that is not there.
    # `install_refusal` is the backstop that makes that unrepresentable rather than
    # merely unlikely.
    #
    # The two can also coexist in one install directory, and the winner is the right one:
    # Python prefers a package to a same-named flat module on `sys.path`, so a
    # `devkit_git_policy.py` left by an older install is shadowed by the package rather
    # than preferred, and the upgrade needs no cleanup step to be safe.
    "scripts/git_policy.py": "devkit_git_policy.py",
    "scripts/git_policy/__init__.py": "devkit_git_policy/__init__.py",
    "scripts/git_policy/_core.py": "devkit_git_policy/_core.py",
    "scripts/git_policy/branch.py": "devkit_git_policy/branch.py",
    "scripts/git_policy/dispatch.py": "devkit_git_policy/dispatch.py",
    "scripts/git_policy/framework.py": "devkit_git_policy/framework.py",
    "scripts/worktree_env.py": "devkit_worktree_env.py",
    "scripts/git-hooks/pre-commit": "pre-commit",
    "scripts/git-hooks/pre-push": "pre-push",
    "scripts/git-hooks/post-checkout": "post-checkout",
}
# The destinations that between them have to yield an importable `devkit_git_policy`.
# Derived from the map so a future layout change cannot forget it.
POLICY_ENTRYPOINTS = frozenset({"devkit_git_policy.py", "devkit_git_policy/__init__.py"})
# Which of those git execs, and therefore which need the executable bit. Derived from the
# map rather than listed twice: a hook added to one and forgotten in the other installs
# as a plain file, and git skips a hook it cannot execute WITHOUT SAYING SO -- the same
# silence `worktree-guard-launch.py` was vendored into for a release.
HOOK_NAMES = frozenset(
    destination for source, destination in RUNTIME_FILES.items() if "/git-hooks/" in source
)
# Records what was installed and from where, beside the runtime it describes.
# Without it, "which policy is actually running?" can only be answered by diffing
# against a checkout -- which is a question about *this* machine that no artifact
# on this machine could answer.


def shadowing_entrypoint(installed: Mapping[str, str]) -> str:
    """The policy layout an install holding `installed` did NOT write, when it wrote one.

    `RUNTIME_FILES` lists both layouts so either ref installs the one it has, and the
    comment there used to argue the two could safely coexist: Python prefers a package
    to a same-named flat module, so a `devkit_git_policy.py` left by an older install is
    shadowed rather than preferred. True, and it only covers the upgrade direction.

    Going the other way -- reinstalling from a tag that predates the package, which is
    what `main()` does by default and what `installers.py` re-runs nightly -- writes the
    flat module underneath a `devkit_git_policy/` an earlier install left behind. The
    package still wins, so every hook on the machine imports the STALE package while
    `--check` reports the runtime current at the ref it just installed. Nothing is red
    and nothing is what it says it is.

    Returns "" when the install wrote neither entrypoint or (impossibly) both, because
    `install_refusal` owns the first case and the second is not a shadow.
    """
    written = set(installed) & POLICY_ENTRYPOINTS
    if len(written) != 1:
        return ""
    return next(iter(POLICY_ENTRYPOINTS - written))


def entrypoint_path(target: Path, entrypoint: str) -> Path:
    """What to look at on disk for `entrypoint` -- the package DIRECTORY, not its
    `__init__.py`. An empty `devkit_git_policy/` is a namespace package and still
    shadows a flat module, so the directory is the thing that has to be gone."""
    if "/" in entrypoint:
        return target / entrypoint.split("/", 1)[0]
    return target / entrypoint


def clear_shadowing_entrypoint(target: Path, installed: Mapping[str, str]) -> str:
    """Remove the layout this install did not write. Returns what went, or "".

    Deleting rather than warning, because the two live at a path this installer owns
    entirely and a warning at install time is read by nobody at commit time.
    """
    stale = shadowing_entrypoint(installed)
    if not stale:
        return ""
    path = entrypoint_path(target, stale)
    if path.is_dir():
        shutil.rmtree(path)
    elif path.is_file():
        path.unlink()
    else:
        return ""
    return stale


def install_refusal(hashes: Mapping[str, str], ref: str) -> str:
    """Why this install must not stand, or "" when it may.

    The one thing the per-file skip cannot be allowed to do. Skipping a file the ref
    does not hold is right for a runtime that *gained* a file; it is catastrophic for
    the policy module itself, because a skip there leaves the hooks importing a
    `devkit_git_policy` that is not there -- and they run on every commit in every
    repository on the machine, so the failure is total and arrives with no warning.

    Pure, and checked against what was actually written rather than against
    `RUNTIME_FILES`, so it stays true whichever layout the ref turned out to have.
    """
    if set(hashes) & POLICY_ENTRYPOINTS:
        return ""
    return (
        f"{ref} holds neither the policy module nor the policy package, so this install "
        "would leave the git hooks with nothing to import. Install from a ref that has "
        "one: the tags before the package have scripts/git_policy.py, and the tags from "
        "the package on have scripts/git_policy/."
    )
