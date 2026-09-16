"""Give a freshly cut worktree a compose project name that is not its checkout's.

**The problem is one compose fallback and two runtimes that name directories
differently.** Docker Compose takes its project name from `-p`, then
`COMPOSE_PROJECT_NAME`, then a `name:` in the file, and finally from the directory's
base name. A worktree checks out tracked files only and `.env` is gitignored, so
nothing sets that variable in one and the directory name is what runs.

`claude --worktree` names the directory `glowing-sparking-swing`, which collides with
nothing: the stack comes up as its own project and merely fails to bind ports the
checkout is already publishing. Loud, and nothing of the checkout's is touched.
`codex --worktree` names it after the repo -- `~/.codex/worktrees/<digest>/carameli`
-- so it normalises to `carameli`, the **same project as the static checkout**.
Compose then does not fail at all. It adopts that project's containers, network and
volumes, and a `down -v` issued from the worktree deletes the checkout's dev database.

**Why this is a git hook and not an agent hook.** A PreToolUse gate only fires for an
agent session, so a person running `docker compose` in that worktree gets nothing --
and the tier that created the hazard is the one whose sessions a devkit hook reaches
least. `git worktree add` runs `post-checkout` in the new worktree with a null old-OID,
whoever invoked it: `claude --worktree`, `codex --worktree`, or a person at a prompt.
devkit already owns the machine's global `core.hooksPath` (`install-git-policy.py`), so
there is one place to put this and it covers every runtime including the ones that do
not exist yet.

**The checkout is resolved through `git rev-parse --git-common-dir`**, not through a
path convention, for the reason `git_policy.framework._venv_roots` gives: the worktree tiers on
this machine sit at different depths and outside the checkout entirely, and git already
knows the answer for all of them -- and for whatever the next tier turns out to be.

Every decision here is a pure function; `main` is the only part that touches git or the
disk. Tested in `tests/test_worktree_env.py`.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# `post-checkout` is handed `<old-oid> <new-oid> <branch-flag>`. A **fresh** checkout --
# `git worktree add`, and also `git clone` -- reports an all-zero old OID, which is what
# separates "this tree was just created" from an ordinary `git checkout <branch>` in a
# tree that already existed. Matched on the character set rather than on a length, so
# this keeps working in a sha256 repository where the OID is 64 zeros rather than 40.
BRANCH_CHECKOUT = "1"

# The compose files a project might carry, in the order compose itself looks for them.
# A worktree with none gets nothing written: devkit's own worktrees have no stack, and a
# `.env` created there would be a file nobody asked for in a repo with nothing to
# configure.
COMPOSE_FILES = (
    "compose.yaml",
    "compose.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
)

ENV_FILE = ".env"
KEY = "COMPOSE_PROJECT_NAME"

# What this hook writes, and the marker that makes a second run idempotent. Phrased as a
# sentence rather than a tag because the reader is somebody who opened a `.env` they did
# not write and wants to know what put it there.
MANAGED_NOTE = (
    "# Written by devkit's global post-checkout hook when this worktree was created.\n"
    "# Without it compose falls back to this directory's name for its project, which for\n"
    "# a worktree named after its repo is the CHECKOUT's stack -- same containers, same\n"
    "# volumes. Delete the line to get that behaviour back.\n"
)


def is_fresh_checkout(old_oid: str, flag: str) -> bool:
    """Whether this `post-checkout` call is a tree that has just come into existence.

    Two conditions, and both matter. `flag` is `1` for a branch checkout and `0` for a
    file checkout (`git checkout -- some/path`), which moves no tree and must not be
    read as one. An all-zero `old_oid` is git's way of saying there was no previous
    HEAD, which is true for `git worktree add` and `git clone` and false for every
    `git checkout <branch>` in a tree that already existed -- the common case, and the
    one where a `.env` already exists and is none of this hook's business.
    """
    cleaned = (old_oid or "").strip()
    return flag.strip() == BRANCH_CHECKOUT and bool(cleaned) and set(cleaned) == {"0"}


def compose_project(repo: str, worktree: str) -> str:
    """The project name a worktree of `repo` gets: `<repo>-<worktree>`, compose-legal.

    Both halves, rather than the worktree name alone, because compose project names are
    global to the **docker daemon** rather than scoped per repository: two repos each
    with a worktree called `main` would otherwise be one project. And rather than the
    branch, which is not available -- the Codex worktree on the machine this was written
    for is on a detached HEAD, with no branch to read -- and would collide across repos
    the same way.

    Lowercased with everything outside `[a-z0-9_-]` dropped, which is compose's own
    normalisation; a leading separator is trimmed because compose requires the first
    character to be alphanumeric.
    """
    joined = f"{repo}-{worktree}"
    return re.sub(r"[^a-z0-9_-]+", "", joined.lower()).lstrip("_-")


def already_named(text: str) -> bool:
    """Whether an existing `.env` already sets the key, in any spelling git left there.

    An `export ` prefix and leading whitespace both count: this is asking "would compose
    read a value", and a hook that appended a second assignment because it did not
    recognise the first would leave two, with the last one silently winning.
    """
    return any(re.match(rf"\s*(export\s+)?{KEY}\s*=", line) for line in (text or "").splitlines())


def rendered(existing: str, name: str) -> str:
    """`existing` with the managed assignment appended; unchanged when it is already set.

    Appended rather than rewritten because this file is the project's, not devkit's --
    the box tier owns a whole managed block in a `.env` it seeded itself, and this hook
    seeds nothing: it adds one line to whatever is (usually not) there.
    """
    if already_named(existing):
        return existing
    prefix = existing if not existing or existing.endswith("\n") else existing + "\n"
    separator = "\n" if prefix else ""
    return f"{prefix}{separator}{MANAGED_NOTE}{KEY}={name}\n"


def has_compose_file(root: Path) -> bool:
    """Whether this tree has a stack at all."""
    return any((root / name).is_file() for name in COMPOSE_FILES)


def _git(root: Path, *args: str) -> str:
    """Run git in `root` and return stripped stdout; "" on any failure.

    Never raises and never blocks: a `post-checkout` hook runs inside the command that
    just created somebody's worktree, so the worst this may do is decline to help.
    """
    try:
        done = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (done.stdout or "").strip() if done.returncode == 0 else ""


def ignores_env(root: Path) -> bool:
    """Whether this project git-ignores `.env`, which is how it says the file is local
    state rather than something a tree is supposed to track.

    `check-ignore --quiet` writes nothing and answers in its exit code, so this reads the
    code directly rather than going through `_git`. Any failure answers False: the hook
    declines rather than creating a file in a repo it could not ask about.
    """
    try:
        return (
            subprocess.run(
                ["git", "-C", str(root), "check-ignore", "--quiet", "--", ENV_FILE],
                capture_output=True,
                timeout=10,
                check=False,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


def checkout_of(root: Path) -> Path | None:
    """The checkout `root` was cut from; None when `root` is not a linked worktree.

    `--git-common-dir` is the main repository's `.git` for a worktree and this tree's own
    for a checkout, so the two cases are told apart by comparing it against `--git-dir`
    rather than by any path convention -- which is what makes this answer for a nested
    Claude worktree, a detached Codex one, and a `git worktree add` somebody typed.
    """
    common = _git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    own = _git(root, "rev-parse", "--path-format=absolute", "--git-dir")
    if not common or not own or Path(common) == Path(own):
        return None
    return Path(common).parent


def main(argv: list[str] | None = None, root: Path | None = None) -> int:
    """The hook. Always exits 0: git ignores a `post-checkout` status, and a traceback
    printed over somebody's `worktree add` is the only harm this could do."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) < 3 or not is_fresh_checkout(args[0], args[2]):
        return 0
    here = Path.cwd() if root is None else root
    checkout = checkout_of(here)
    if checkout is None or not has_compose_file(here):
        return 0
    # Only where the project already treats `.env` as local state. Creating an untracked
    # file in a repo that tracks it would put a permanent entry in every `git status`,
    # which is the cost this harness refuses to impose elsewhere for the same reason.
    # Read off the exit code rather than through `_git`: `check-ignore --quiet` prints
    # nothing either way, so stdout cannot tell the two answers apart.
    if not ignores_env(here):
        return 0
    name = compose_project(checkout.name, here.name)
    target = here / ENV_FILE
    try:
        existing = target.read_text(encoding="utf-8") if target.is_file() else ""
        updated = rendered(existing, name)
        if updated != existing:
            target.write_text(updated, encoding="utf-8")
            print(f"devkit: {ENV_FILE} {KEY}={name} (this worktree's own compose project)")
    except OSError:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
