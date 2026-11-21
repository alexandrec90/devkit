"""Let git trust the workspace's checkouts, whichever token created them.

git refuses a repository whose directory another account owns ("detected dubious
ownership"), and on Windows an *elevated* process creates what it writes as
`BUILTIN\\Administrators`, not as the user. So a checkout or worktree an elevated session
made -- the operator's terminal elevates by default on this machine -- is one the
unelevated fix pass cannot read, push from or re-gate: 5025d284 (a ship's push from an
elevated `claude --worktree` tree) and e1463857 (`gh workflow run` in a social-scraper
clone made elevated), both on 2026-09-29, four days after the release job died the same
way. Each was read as that one tree's problem, and the next elevated tree hit it again.

No unelevated process can take that ownership back, so the fix is git's own:
`safe.directory`, for `<workspace root>/*` -- the directory every checkout, box and agent
worktree the pass acts on sits under, all of it this user's. Two halves, because neither
reaches everything the other does:

- `trusted_env`: the pass's process and all it spawns, in every mode and with nothing
  written, through git's command-scope variables (`GIT_CONFIG_COUNT`), which git honours
  for `safe.directory` as it does its global config.
- `persist`: the global entry, added once by a dispatching pass. The push gate strips
  git's variables from its steps (`run_push_gate.gate_env`) and a fixer's session
  inherits nothing from the pass, so only the global config reaches those.

**The `/*` form is new in git 2.46** (`LEADING_PATH`); an older git matches it against
nothing, so on this machine's 2.40 neither half trusted anything, and on 2026-11-20 the
pass's ship of an elevated `claude --worktree` tree failed on its first `git status`,
exiting the scheduled pass 1. On such a git the command scope carries `*` instead: that
reaches only the pass's own process tree, which acts on nothing outside the workspace.
The global entry stays the narrow one -- `*` there would trust every repository on the
machine for every process, which is the operator's to choose -- so on an old git a push
gate or a fixer in such a tree is still refused until git is upgraded.

The same two halves carry `http.https://github.com/.proactiveAuth`, for a second refusal
that was read as one tree's problem. GitHub answers *anonymous* requests from a blocked
address with 403, not 401, and git only asks its credential helper after a 401, so on
2026-10-02 every fetch of a public repository failed while every push (401, then the
helper) went through: the watchdog's self-update (902dad3f) and the release pass's
upgrade boxes for roguelike and social-scraper. `basic` makes git ask the helper before
the first request; scoped to GitHub's URL, it touches no other remote.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Mapping, MutableMapping
from pathlib import Path

KEY = "safe.directory"
AUTH_SETTING = "http.https://github.com/.proactiveAuth"
AUTH = "basic"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# The first git whose `safe.directory` reads `<dir>/*` as a leading path.
LEADING_PATH = (2, 46)
EVERYTHING = "*"

Runner = Callable[..., subprocess.CompletedProcess[str]]


def pattern(root: Path) -> str:
    """The `safe.directory` value that trusts everything under `root`: git's `/*` form."""
    return root.as_posix().rstrip("/") + "/*"


def covered(values: list[str], root: Path) -> bool:
    """`values`, a `safe.directory` list, already trusts `root`'s tree."""
    return EVERYTHING in values or pattern(root) in values


def version(said: str) -> tuple[int, int] | None:
    """`(major, minor)` off `git version`'s line, or None when it names none."""
    found = re.search(r"git version (\d+)\.(\d+)", said)
    return (int(found[1]), int(found[2])) if found else None


def reads_leading_path(git: Runner) -> bool:
    """Whether this git honours `pattern`'s `/*` form. A git that cannot say is taken as
    one that does not, since `EVERYTHING` in the command scope works on every version."""
    done = git("version")
    found = version(done.stdout or "") if done.returncode == 0 else None
    return found is not None and found >= LEADING_PATH


def trusted_env(environ: Mapping[str, str], root: Path, value: str | None = None) -> dict[str, str]:
    """`environ` plus a command-scope `safe.directory` for `root`'s tree and GitHub's
    `proactiveAuth`. `value` is the entry, `pattern(root)` unless the caller's git cannot
    read that (`reads_leading_path`), when `adopt` passes `EVERYTHING`.

    Appended after any entries already there, and not twice; a `proactiveAuth` already
    set, to any value, is the operator's and is kept. A `GIT_CONFIG_COUNT` that is not a
    count is left alone: git already refuses every command under it, and a guess at what
    it meant would only move the refusal.
    """
    value = value or pattern(root)
    env = dict(environ)
    try:
        count = int(env.get("GIT_CONFIG_COUNT", "0"))
    except ValueError:
        return env
    scoped = [
        (env.get(f"GIT_CONFIG_KEY_{i}", ""), env.get(f"GIT_CONFIG_VALUE_{i}", ""))
        for i in range(count)
    ]
    wanted = []
    trusted = [entry for key, entry in scoped if key == KEY]
    if EVERYTHING not in trusted and value not in trusted:
        wanted.append((KEY, value))
    if not any(key.lower() == AUTH_SETTING.lower() for key, _ in scoped):
        wanted.append((AUTH_SETTING, AUTH))
    for key, value in wanted:
        env[f"GIT_CONFIG_KEY_{count}"] = key
        env[f"GIT_CONFIG_VALUE_{count}"] = value
        count += 1
    if wanted:
        env["GIT_CONFIG_COUNT"] = str(count)
    return env


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=False, creationflags=NO_WINDOW
    )


def _said(done: subprocess.CompletedProcess[str]) -> str:
    return " ".join((done.stderr or done.stdout or "").split()) or f"exit {done.returncode}"


def persist(root: Path, git: Runner = _git) -> str:
    """Add `root`'s tree to git's global `safe.directory` list and set GitHub's
    `proactiveAuth` there: the lines to record, or "" when both were already there. A
    `proactiveAuth` set to anything is kept. A failure is said, never raised -- the pass
    goes on with what its own environment carries."""
    lines = []
    listed = git("config", "--global", "--get-all", KEY)
    if not covered((listed.stdout or "").splitlines(), root):
        added = git("config", "--global", "--add", KEY, pattern(root))
        lines.append(
            f"git-trust: added {pattern(root)} to git's global {KEY}"
            if added.returncode == 0
            else f"git-trust: could not add {pattern(root)} to git's global {KEY}: {_said(added)}"
        )
    if git("config", "--global", "--get", AUTH_SETTING).returncode != 0:
        added = git("config", "--global", AUTH_SETTING, AUTH)
        lines.append(
            f"git-trust: set git's global {AUTH_SETTING}={AUTH}"
            if added.returncode == 0
            else f"git-trust: could not set git's global {AUTH_SETTING}: {_said(added)}"
        )
    return "\n".join(lines)


def adopt(
    root: Path,
    write: bool,
    environ: MutableMapping[str, str] = os.environ,
    git: Runner = _git,
) -> str:
    """Trust `root`'s tree in this process, and globally too when `write`: `persist`'s lines,
    and one saying so when this git cannot read the global entry `persist` writes."""
    leading = reads_leading_path(git)
    environ.update(trusted_env(environ, root, None if leading else EVERYTHING))
    lines = persist(root, git) if write else ""
    if leading:
        return lines
    old = (
        f"git-trust: this git predates {KEY}'s `/*` form ({'.'.join(map(str, LEADING_PATH))}),"
        f" so this pass trusts {EVERYTHING} in its own scope and the global {pattern(root)}"
        " trusts nothing until git is upgraded"
    )
    return "\n".join(line for line in (lines, old) if line)
