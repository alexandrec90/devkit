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
before the session's first turn, whoever cut the tree, so it runs the project's own
provisioner -- the manifest's `install_command` when it is one plain command, else the
`uv sync` that `scripts/hooks/toolchain.py` would name -- when the checkout it was cut
from already has a `.venv`, which is the one on-disk fact that says this machine
provisions this project and its cache is warm. A cold checkout gets nothing and
`ship.py --preflight` still names the command; `DEVKIT_SKIP_WORKTREE_PROVISION=1`
skips the step for a `git worktree add` that wants a bare tree. Anything else that
leaves a tree unprovisioned -- a failure, a missing `uv`, a command that needs a shell --
goes in the tree's `logs/friction.md` as well as the hook's output, which `claude
--worktree` swallows: the fix pass files it, so it is fixed at the cause.

**`post-checkout` alone misses a tree cut `--no-checkout`** and filled with
`git reset --hard`, since git skips `post-checkout` for a no-checkout add. So the same
set-up also answers `post-index-change` (`index_change_main`), gated on a working-tree
update in a tree not yet provisioned.

**Neither hook reaches `claude --worktree` any more.** Claude Code runs its own git with
`-c core.hooksPath=/dev/null` (verified 2026-09-29: a plain `git worktree add` fired
both hooks in a scratch repo, `claude -p --worktree` fired neither), and its only
replacement seam is an agent hook, which this harness does not wire. Its trees borrow
the checkout's `node_modules` through `worktree.symlinkDirectories` instead, which
`--pull` fills into every project's settings (`project_settings.dependency_dirs`), and
its `.venv` only where the project installs no package of its own; a project installed
editable builds each tree its own through `toolchain.rerun_in_venv`.


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
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

# `worktree.py` and `upgrade-project.py` import this module, so a scheduled job reaches it
# and every spawn here is held to `tests/test_scheduled_jobs.py`'s windowless rule. Spelled
# here, like `console_python`, because the hook is installed alone with nothing to import.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run_windowless(*args, **kwargs) -> subprocess.CompletedProcess:
    """`sweep.run_windowless`, spelled here for the reason `NO_WINDOW` is."""
    flags = kwargs.pop("creationflags", 0)
    return subprocess.run(*args, creationflags=flags | NO_WINDOW, **kwargs)


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


def _git(root: Path, *args: str, env: Mapping[str, str] | None = None, timeout: float = 10) -> str:
    """Run git in `root` and return stripped stdout; "" on any failure.

    Never raises and never blocks: a `post-checkout` hook runs inside the command that
    just created somebody's worktree, so the worst this may do is decline to help.
    `env` is the whole child environment when given; see `git_env` for when it must be.
    """
    try:
        done = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=NO_WINDOW,
            env=None if env is None else dict(env),
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
                creationflags=NO_WINDOW,
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
# What says a tree *is* provisioned. Not `.venv` alone: `uv sync` creates the interpreter
# before it resolves anything, so a failed one left `.venv/Scripts/python.exe` behind and
# every later hook read the tree as done (c1391297). Written only after an install that
# succeeded, cleared before each attempt; `worktree.run_provision` keeps it the same way.
PROVISIONED = Path(VENV_DIR) / ".devkit-provisioned"
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
            own_venv=(here / PROVISIONED).is_file(),
            checkout_venv=(checkout / VENV_DIR).is_dir(),
            uv=shutil.which("uv") if uv is None else uv,
            install_command=install_command,
            python_version=python_version,
        )

    def command(self) -> tuple[str, ...]:
        """The argv to run, or () when this tree is not one to provision here.

        The manifest's `install_command` when it is one plain command, else the
        uv-locked model's `uv sync`. A shell is never run in the middle of somebody's
        `git worktree add`: an `install_command` with shell syntax, and the other
        ladders in `toolchain.python_fix` (two commands joined by `&&`), are left to the
        box tier's provisioner. Refusing *every* manifest command as "a shell string" left
        each carameli worktree with no `.venv`, though its `python scripts/bootstrap.py`
        needs no shell at all.
        """
        if self.own_venv or not self.checkout_venv:
            return ()
        if self.install_command:
            return plain_argv(self.install_command)
        if not self.locked or not self.uv:
            return ()
        pin = ("--python", self.python_version) if self.python_version else ()
        return (self.uv, *UV_SYNC[1:], *pin)

    def gap(self) -> str:
        """Why a tree that should be provisioned here is not, or "" when leaving it is
        right: already provisioned, a cold checkout, or nothing to install."""
        if self.own_venv or not self.checkout_venv:
            return ""
        if self.install_command and not plain_argv(self.install_command):
            return (
                f"the manifest install_command `{self.install_command}` needs a shell, which "
                "the hook does not run inside `git worktree add` -- make it one plain command"
            )
        if not self.install_command and self.locked and not self.uv:
            return "uv is not on PATH, so the hook could not run `uv sync`"
        return ""


# What makes a manifest command need a shell. Without any of these it is argv already.
SHELL_SYNTAX = frozenset("&|;<>$`'\"*?()\\%^\n")
# Spellings of "the interpreter": this hook's own is certain to exist, unlike whatever
# `python` resolves to in the environment git was started from.
PYTHON_NAMES = frozenset({"python", "python3", "py"})


def plain_argv(command: str) -> tuple[str, ...]:
    """`command` as argv when it needs no shell; () when it does."""
    if not command.strip() or SHELL_SYNTAX & set(command):
        return ()
    words = command.split()
    if words[0].lower() in PYTHON_NAMES:
        words[0] = console_python()
    return tuple(words)


def console_python() -> str:
    """The console interpreter beside `sys.executable`: `sweep.console_python`, copied
    because this hook is installed alone. Under `pythonw.exe` a Python child would be
    console-less, and Windows would give each of *its* children a visible window."""
    executable = Path(sys.executable)
    if executable.name.lower() != "pythonw.exe":
        return sys.executable
    console = executable.with_name("python.exe")
    return str(console) if console.exists() else sys.executable


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


# --- the sibling a path source names ------------------------------------------

PYPROJECT = "pyproject.toml"


def path_sources(here: Path) -> list[str]:
    """The `[tool.uv.sources]` paths that climb out of the tree, as written.

    ibkr_trader's `data-lake = { path = "../data-lake" }` is the case: right from the
    checkout, where it names the sibling repo, and wrong from every worktree, where it
    names `.claude/worktrees/data-lake` -- so `uv sync` and every `uv run` fail there
    with `Distribution not found`. A source may be one table or a list of them (per
    marker); anything unreadable is no source, never an error inside `worktree add`.
    """
    try:
        data = tomllib.loads((here / PYPROJECT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    sources = data.get("tool", {}).get("uv", {}).get("sources", {})
    if not isinstance(sources, dict):
        return []
    found: list[str] = []
    for spec in sources.values():
        for entry in spec if isinstance(spec, list) else [spec]:
            path = entry.get("path") if isinstance(entry, dict) else None
            if isinstance(path, str) and path.replace("\\", "/").startswith("../"):
                found.append(path)
    return found


WORKFLOWS = Path(".github") / "workflows"
# A step's start: a sequence item. Every key of one checkout step is read between two.
STEP_START = re.compile(r"^\s*-\s", re.M)
# `actions/checkout`'s two inputs that name another repo and where it stands.
CHECKOUT_KEY = re.compile(r"^\s*(repository|ref)\s*:\s*(.*?)\s*(?:\s#.*)?$")


def pinned_ref(here: Path, name: str) -> str:
    """The ref this tree's own CI checks out its sibling repo `name` at; "" if none pins it.

    b0b2f7d7: ibkr_trader's PR gate pins data-lake to the commit its `uv.lock` was
    resolved against (its c221765), and the sibling was cut at data-lake's `origin/HEAD`
    -- so a session's tests imported a module main had retired and the gate's pin still
    had, and its `uv sync` relocked a `uv.lock` it never meant to touch. The gate is the
    authority a session is held to, so its pin is the ref; `pr-gate*` is read first, as
    a nightly may check the same repo out at its default branch on purpose. Matched by
    the repository's last segment, the name the path source climbs to. A ref that is a
    workflow expression is no pin.
    """
    try:
        files = sorted(
            (here / WORKFLOWS).glob("*.y*ml"),
            key=lambda path: (not path.name.startswith("pr-gate"), path.name),
        )
    except OSError:
        return ""
    for workflow in files:
        try:
            text = workflow.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for step in STEP_START.split(text):
            keys: dict[str, str] = {}
            for line in step.splitlines():
                found = CHECKOUT_KEY.match(line)
                if found:
                    keys.setdefault(found.group(1), found.group(2).strip("'\""))
            repo = keys.get("repository", "").rstrip("/").rsplit("/", 1)[-1]
            ref = keys.get("ref", "")
            if repo.lower() == name.lower() and ref and "${{" not in ref:
                return ref
    return ""


def resolve_pin(source: Path, pin: str, env: Mapping[str, str]) -> str:
    """`pin` as `source` can check it out: a branch by its remote-tracking ref, which the
    fetch just moved, ahead of a local branch of that name; "" when `source` has neither."""
    for ref in (f"origin/{pin}", pin):
        if _git(source, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", env=env):
            return ref
    return ""


def link_path_sources(
    here: Path,
    checkout: Path,
    runner=run_windowless,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    """Cut each missing sibling as a detached worktree of the repo the checkout sees there.

    At the ref the tree's own CI pins that repo to (`pinned_ref`), else at its
    `origin/HEAD`; never at its checkout's working state: ibkr's CLAUDE.md
    records that building against the static data-lake checkout re-resolves `uv.lock`
    and smuggles its specifier bumps into whatever branch is open. One tree serves every
    worktree of the tier, since they all resolve the same `..`; a task that edits the
    sibling cuts its own branch there. The nested `worktree add` skips its own
    provisioning -- the sibling is built from source by this tree's sync, not its own.
    The sibling repo is fetched first, and a sibling tree already there is moved to that
    ref by `advance_sibling`: "origin/HEAD" meant the last fetch's, at the first cut's.
    One line per sibling cut, moved or refused; nothing for one already current.
    """
    env = git_env(os.environ if environ is None else environ)
    env[SKIP_PROVISION_VAR] = "1"
    lines: list[str] = []
    for relative in path_sources(here):
        target = Path(os.path.normpath(here / relative))
        source = Path(os.path.normpath(checkout / relative))
        if not (source / ".git").exists():
            continue
        # The remote-tracking ref is only as new as the sibling checkout's last fetch.
        _git(source, "fetch", "--quiet", "origin", env=env, timeout=60)
        pin = pinned_ref(here, source.name)
        ref = resolve_pin(source, pin, env) if pin else ""
        if pin and not ref:
            lines.append(f"devkit: CI pins {source.name} to {pin}, which {source} lacks")
        ref = ref or (
            _git(source, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", env=env) or "HEAD"
        )
        if target.exists():
            lines.append(advance_sibling(target, source, ref, env))
            continue
        argv = ["git", "-C", str(source), "worktree", "add", "--detach", str(target), ref]
        try:
            done = runner(argv, capture_output=True, text=True, env=env, timeout=60, check=False)
            failed = done.returncode != 0
            detail = " | ".join((done.stderr or "").strip().splitlines()[-2:])
        except (OSError, subprocess.SubprocessError) as exc:
            failed, detail = True, str(exc)
        if failed:
            lines.append(f"devkit: could not cut {target} from {source} ({detail})")
        else:
            lines.append(f"devkit: {relative} is {target}, a detached {source.name} at {ref}")
    return [line for line in lines if line]


def _common_dir(root: Path, env: Mapping[str, str]) -> str:
    found = _git(root, "rev-parse", "--path-format=absolute", "--git-common-dir", env=env)
    return os.path.normcase(os.path.normpath(found)) if found else ""


def advance_sibling(target: Path, source: Path, ref: str, env: Mapping[str, str]) -> str:
    """Move the shared sibling tree to `ref`; "" when it is there already or is work.

    2e681e63: the tree was cut once and never moved, so ibkr's `uv lock --check` judged
    its lock against a data-lake weeks behind main and disagreed with CI. Only the tree
    `link_path_sources` cuts is moved -- detached, with no tracked change; a branch
    checked out there is a task's own. One that is not the sibling's at all is named.
    """
    if _common_dir(target, env) != _common_dir(source, env):
        return f"devkit: {target} is not a worktree of {source}; remove it to have it re-cut"
    if _git(target, "symbolic-ref", "-q", "HEAD", env=env):
        return ""
    want = _git(source, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", env=env)
    have = _git(target, "rev-parse", "HEAD", env=env)
    if not want or want == have:
        return ""
    if _git(target, "status", "--porcelain", "--untracked-files=no", env=env):
        return f"devkit: {target} has local changes, so it was left at {have[:7]}, not {ref}"
    _git(target, "checkout", "--quiet", "--detach", want, env=env, timeout=60)
    if _git(target, "rev-parse", "HEAD", env=env) != want:
        return f"devkit: could not move {target} from {have[:7]} to {ref}"
    return f"devkit: {target} moved from {have[:7]} to {ref} ({want[:7]})"


# Names git reads before `-C`, so each points a command at a repository other than the
# one it was aimed at. The fallback when `git rev-parse --local-env-vars` cannot answer.
GIT_REPO_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_PREFIX")


def git_env(environ: Mapping[str, str]) -> dict[str, str]:
    """`environ` less every variable that aims git at a repository `-C` did not name.

    6e056a10: git runs `post-checkout` for `worktree add` with `GIT_DIR` and
    `GIT_WORK_TREE` naming the tree it just cut, and those outrank `-C`, so the nested
    `git -C <data-lake> worktree add` cut `../data-lake` as another worktree of
    ibkr_trader, at ibkr's own `origin/HEAD`. The list is git's own answer.
    """
    cleaned = {k: v for k, v in environ.items() if k not in GIT_REPO_VARS}
    try:
        listed = subprocess.run(
            ["git", "rev-parse", "--local-env-vars"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            creationflags=NO_WINDOW,
            env=cleaned,
        ).stdout.split()
    except (OSError, subprocess.SubprocessError):
        listed = []
    return {k: v for k, v in cleaned.items() if k not in listed}


def provision(
    here: Path,
    checkout: Path,
    runner=run_windowless,
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
    tool = Toolchain.observe(here, checkout, uv=uv)
    command = tool.command()
    if not command:
        gap = tool.gap()
        return _trouble(here, f"devkit: {gap}") if gap else ""
    # Spelled as typed rather than resolved: the line is a command to paste, and
    # `C:\...\Scripts\uv.EXE sync` is not one anybody types.
    spelled = tool.install_command or " ".join((UV_SYNC[0], *command[1:]))
    mark_provisioned(here, False)
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
        return _trouble(
            here, f"devkit: `{spelled}` did not finish in {PROVISION_TIMEOUT}s; run it here by hand"
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _trouble(here, f"devkit: could not run `{spelled}` ({exc}); run it here by hand")
    if done.returncode != 0:
        tail = " | ".join((done.stderr or "").strip().splitlines()[-3:])
        return _trouble(here, f"devkit: `{spelled}` failed ({tail}); run it here by hand")
    elapsed = time.monotonic() - started
    mark_provisioned(here, True)
    return f"devkit: {VENV_DIR} provisioned by `{spelled}` in {elapsed:.0f}s (this worktree's own)"


def mark_provisioned(here: Path, ok: bool) -> None:
    """Set or clear `PROVISIONED`. Only inside a `.venv` the install made: creating one to
    hold the mark would be the empty `.venv` this exists to stop reading as done. Never
    raises -- a hook that cannot write the mark leaves the tree to be provisioned again."""
    try:
        if not ok:
            (here / PROVISIONED).unlink(missing_ok=True)
        elif (here / VENV_DIR).is_dir():
            (here / PROVISIONED).write_text("", encoding="utf-8")
    except OSError:
        pass


# The file each package manager writes inside `node_modules` once an install has finished:
# npm 7+, pnpm, yarn 1 and yarn berry. The directory alone proves nothing. A roguelike
# tree linked `node_modules` to its checkout's, which existed but was empty, so `vite`
# was missing and nothing had checked (fcf111b9).
NODE_INSTALL_MARKS = (".package-lock.json", ".modules.yaml", ".yarn-integrity", ".yarn-state.yml")


def node_modules_installed(node_modules: Path) -> bool:
    """Whether `node_modules`, or the directory it links to, holds a finished install."""
    return any((node_modules / mark).is_file() for mark in NODE_INSTALL_MARKS)


def borrowed(path: Path) -> Path | None:
    """The real directory `path` links to, or None when `path` is its own or is absent.

    A `claude --worktree` tree's dependency directories link into the checkout
    (`worktree.symlinkDirectories`), as a symlink or, on Windows, possibly a junction,
    and `is_symlink` is False for a junction. So this compares resolved paths instead.
    """
    try:
        real = path.resolve(strict=True)
        own = path.parent.resolve(strict=True) / path.name
    except OSError:
        return None
    return None if os.path.normcase(str(real)) == os.path.normcase(str(own)) else real


# `fix_reports.FRICTION_FILE`: the fix pass files each line of it on the harness-defect
# ledger. Spelled here because this hook is installed alone, with nothing to import.
FRICTION_FILE = Path("logs") / "friction.md"


def _trouble(here: Path, line: str) -> str:
    """Leave `line` in the tree's friction file as well as printing it: `claude
    --worktree` swallows a hook's output, so a tree came up with no `.venv` and nothing
    anywhere said why. `logs/` is ignored in every project. Never raises."""
    what = line.removeprefix("devkit: ")
    try:
        path = here / FRICTION_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"- devkit's worktree hook left this worktree unprovisioned: {what}\n")
    except OSError:
        pass
    return line


def is_unprovisioned_tree_update(args: list[str], here: Path) -> bool:
    """Whether this `post-index-change` call is a tree update in a tree with no `.venv`.

    `claude --worktree` cuts its tree with `git worktree add --no-checkout` and fills it
    with `git reset --hard`, and git runs `post-checkout` after a `worktree add` *unless*
    `--no-checkout` was given -- so the hook above never saw a single Claude worktree.
    The reset does fire `post-index-change`, with `1` as its first argument because it
    updated the working tree; `git add` and every other index-only write pass `0`.

    Nothing here says the tree is new, so the tree's own missing `PROVISIONED` mark stands
    in: it is one `stat`, taken before any git spawn, so the hook costs nothing in a
    provisioned tree and runs `provision` until one succeeds.
    """
    return bool(args) and args[0].strip() == BRANCH_CHECKOUT and not (here / PROVISIONED).is_file()


def main(
    argv: list[str] | None = None,
    root: Path | None = None,
    runner=run_windowless,
    environ: Mapping[str, str] | None = None,
) -> int:
    """The hook. Always exits 0: git ignores a `post-checkout` status, and a traceback
    printed over somebody's `worktree add` is the only harm this could do."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) < 3 or not is_fresh_checkout(args[0], args[2]):
        return 0
    return _set_up(Path.cwd() if root is None else root, runner, environ)


def index_change_main(
    argv: list[str] | None = None,
    root: Path | None = None,
    runner=run_windowless,
    environ: Mapping[str, str] | None = None,
) -> int:
    """The `post-index-change` hook: the same set-up, for a tree cut `--no-checkout`.

    Always exits 0: git ignores this hook's status too.
    """
    args = sys.argv[1:] if argv is None else argv
    here = Path.cwd() if root is None else root
    if not is_unprovisioned_tree_update(args, here):
        return 0
    return _set_up(here, runner, environ)


def _set_up(here: Path, runner, environ: Mapping[str, str] | None) -> int:
    checkout = checkout_of(here)
    if checkout is None:
        return 0
    for line in (
        name_compose_project(here, checkout),
        # Before the sync, which cannot resolve a path source that is not there yet.
        *link_path_sources(here, checkout, runner=runner, environ=environ),
        provision(here, checkout, runner=runner, environ=environ),
    ):
        if line:
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
