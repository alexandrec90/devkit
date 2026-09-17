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

**The same seam gives the worktree its own `.venv`.** A linked worktree checks out
tracked files only, so a `claude --worktree` session starts with no interpreter of its
own and every test run in it borrows the checkout's (`project_python.borrowed_from`
says so on every re-exec). The edit-time and session-start agent hooks that used to
close that gap are exactly the ones an operator switches off with `DEVKIT_HOOKS_OFF`,
and `worktree.py provision <path>` is a verb somebody has to remember. This hook fires
before the session's first turn, whoever cut the tree, so it runs the `uv sync` that
`scripts/hooks/toolchain.py` would name -- under three conditions that keep it seconds
rather than minutes: the project is uv-locked, `uv` is on `PATH`, and the checkout it
was cut from already has a `.venv`, which is the one on-disk fact that says this machine
provisions this project and its uv cache is warm. A cold checkout gets nothing and
`ship.py --preflight` still names the command; `DEVKIT_SKIP_WORKTREE_PROVISION=1`
skips the step for a `git worktree add` that wants a bare tree.

Every decision here is a pure function; `main` is the only part that touches git or the
disk. Tested in `tests/test_worktree_env.py`.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
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


def name_compose_project(here: Path, checkout: Path) -> str:
    """Write the worktree's compose project name; the line to print, or "" when nothing was."""
    if not has_compose_file(here):
        return ""
    # Only where the project already treats `.env` as local state. Creating an untracked
    # file in a repo that tracks it would put a permanent entry in every `git status`,
    # which is the cost this harness refuses to impose elsewhere for the same reason.
    # Read off the exit code rather than through `_git`: `check-ignore --quiet` prints
    # nothing either way, so stdout cannot tell the two answers apart.
    if not ignores_env(here):
        return ""
    name = compose_project(checkout.name, here.name)
    target = here / ENV_FILE
    try:
        existing = target.read_text(encoding="utf-8") if target.is_file() else ""
        updated = rendered(existing, name)
        if updated == existing:
            return ""
        target.write_text(updated, encoding="utf-8")
    except OSError:
        return ""
    return f"devkit: {ENV_FILE} {KEY}={name} (this worktree's own compose project)"


# --- the worktree's own interpreter -------------------------------------------

LOCKFILE = "uv.lock"
VENV_DIR = ".venv"
# The `uv sync` spelling `scripts/hooks/toolchain.py` and `worktree.provision_steps`
# both use, so the three cannot name different commands.
UV_SYNC = ("uv", "sync", "--all-extras", "--all-groups")
# Generous because it is a ceiling, not an expectation: a warm-cache sync is seconds, and
# the checkout-has-a-venv condition is what keeps this on the warm path. The timeout is
# for the day the network is gone, so `git worktree add` still returns.
PROVISION_TIMEOUT = 600
SKIP_PROVISION_VAR = "DEVKIT_SKIP_WORKTREE_PROVISION"
# The worktree's own vendored copy of the manifest reader -- this hook is installed
# machine-wide with no repo of its own, so the project's `[python]` table is read through
# the code that ships beside it.
HARNESS_CONFIG = Path("scripts") / "hooks" / "harness_config.py"


@dataclass(frozen=True)
class Toolchain:
    """The facts that decide whether this worktree gets a `uv sync`, and the command."""

    locked: bool
    own_venv: bool
    checkout_venv: bool
    uv: str | None
    install_command: str = ""
    python_version: str = ""

    @classmethod
    def observe(cls, here: Path, checkout: Path, uv: str | None = None) -> Toolchain:
        install_command, python_version = manifest_python(here)
        return cls(
            locked=(here / LOCKFILE).is_file(),
            own_venv=(here / VENV_DIR).is_dir(),
            checkout_venv=(checkout / VENV_DIR).is_dir(),
            uv=shutil.which("uv") if uv is None else uv,
            install_command=install_command,
            python_version=python_version,
        )

    def command(self) -> tuple[str, ...]:
        """The argv to run, or () when this tree is not one to provision here.

        Only the uv-locked model, deliberately. The other ladders in
        `toolchain.python_fix` are two commands joined by a shell `&&`, and a manifest
        `install_command` is a shell string by contract; a post-checkout hook that ran
        either would be a shell in the middle of somebody's `git worktree add`. Those
        projects keep the box tier's provisioner, which runs them through one.
        """
        if not self.locked or self.own_venv or not self.checkout_venv or not self.uv:
            return ()
        if self.install_command:
            return ()
        pin = ("--python", self.python_version) if self.python_version else ()
        return (self.uv, *UV_SYNC[1:], *pin)


def manifest_python(here: Path) -> tuple[str, str]:
    """`[python] install_command` and `version` from this tree's own `.devkit.toml`.

    Through the `harness_config` vendored into the tree rather than a TOML parse of our
    own, so the defaults and aliases stay one copy; a tree without the harness answers
    ("", ""), which is the unpinned `uv sync` and correct for it.
    """
    module_path = here / HARNESS_CONFIG
    if not module_path.is_file():
        return "", ""
    # The same recipe as `scripts/precommit/_loader.load_by_path`, inlined against the
    # rule in `scripts/CLAUDE.md` that says not to: this file is installed alone into
    # `~/.devkit/git-hooks` (`install_policy_layout.RUNTIME_FILES`) with no `_loader`
    # beside it, and a hook that imports a sibling it was not installed with is one that
    # dies on every `worktree add` on the machine.
    name = "_worktree_env_harness_config"
    try:
        spec = importlib.util.spec_from_file_location(name, module_path)
        if spec is None or spec.loader is None:
            return "", ""
        module = importlib.util.module_from_spec(spec)
        # Registered before it runs: `dataclass` resolves a class's module through
        # `sys.modules`, and `harness_config` is nothing but frozen dataclasses.
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
            python = module.load(here).python
        finally:
            sys.modules.pop(name, None)
        return str(python.install_command or ""), str(python.version or "")
    except (ImportError, OSError, SyntaxError, AttributeError, TypeError, ValueError):
        # A half-vendored module, a manifest that does not parse (`TOMLDecodeError` is a
        # `ValueError`), a `load` whose signature or result moved. A hook must not die
        # over a manifest it could not read, and the unpinned sync is the right answer
        # for a tree that could not say its pin.
        return "", ""


def provision(
    here: Path,
    checkout: Path,
    runner=subprocess.run,
    environ: Mapping[str, str] | None = None,
    uv: str | None = None,
) -> str:
    """Give the worktree its own `.venv` when the conditions hold; the line to print.

    "" when nothing ran. Captured rather than streamed: `uv sync` on the warm path prints
    a progress screen worth nothing to the person whose `worktree add` this is inside,
    and the failure tail is relayed with the command so the fix is one paste.
    """
    env = os.environ if environ is None else environ
    if env.get(SKIP_PROVISION_VAR):
        return ""
    command = Toolchain.observe(here, checkout, uv=uv).command()
    if not command:
        return ""
    # Spelled with the bare `uv` rather than the resolved executable: the line is a
    # command to paste, and `C:\...\Scripts\uv.EXE sync` is not one anybody types.
    spelled = " ".join((UV_SYNC[0], *command[1:]))
    started = time.monotonic()
    try:
        done = runner(
            list(command),
            cwd=str(here),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PROVISION_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return f"devkit: `{spelled}` did not finish in {PROVISION_TIMEOUT}s; run it here by hand"
    except (OSError, subprocess.SubprocessError) as exc:
        return f"devkit: could not run `{spelled}` ({exc}); run it here by hand"
    if done.returncode != 0:
        tail = " | ".join((done.stderr or "").strip().splitlines()[-3:])
        return f"devkit: `{spelled}` failed ({tail}); run it here by hand"
    elapsed = time.monotonic() - started
    return f"devkit: {VENV_DIR} provisioned by `{spelled}` in {elapsed:.0f}s (this worktree's own)"


def main(
    argv: list[str] | None = None,
    root: Path | None = None,
    runner=subprocess.run,
    environ: Mapping[str, str] | None = None,
) -> int:
    """The hook. Always exits 0: git ignores a `post-checkout` status, and a traceback
    printed over somebody's `worktree add` is the only harm this could do."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) < 3 or not is_fresh_checkout(args[0], args[2]):
        return 0
    here = Path.cwd() if root is None else root
    checkout = checkout_of(here)
    if checkout is None:
        return 0
    for line in (
        name_compose_project(here, checkout),
        provision(here, checkout, runner=runner, environ=environ),
    ):
        if line:
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
