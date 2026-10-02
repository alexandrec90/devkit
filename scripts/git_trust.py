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
import subprocess
from collections.abc import Callable, Mapping, MutableMapping
from pathlib import Path

KEY = "safe.directory"
AUTH_SETTING = "http.https://github.com/.proactiveAuth"
AUTH = "basic"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

Runner = Callable[..., subprocess.CompletedProcess[str]]


def pattern(root: Path) -> str:
    """The `safe.directory` value that trusts everything under `root`: git's `/*` form."""
    return root.as_posix().rstrip("/") + "/*"


def covered(values: list[str], root: Path) -> bool:
    """`values`, a `safe.directory` list, already trusts `root`'s tree."""
    return "*" in values or pattern(root) in values


def trusted_env(environ: Mapping[str, str], root: Path) -> dict[str, str]:
    """`environ` plus a command-scope `safe.directory` for `root`'s tree and GitHub's
    `proactiveAuth`.

    Appended after any entries already there, and not twice; a `proactiveAuth` already
    set, to any value, is the operator's and is kept. A `GIT_CONFIG_COUNT` that is not a
    count is left alone: git already refuses every command under it, and a guess at what
    it meant would only move the refusal.
    """
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
    if not covered([value for key, value in scoped if key == KEY], root):
        wanted.append((KEY, pattern(root)))
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
    """Trust `root`'s tree in this process, and globally too when `write`: `persist`'s lines."""
    environ.update(trusted_env(environ, root))
    return persist(root, git) if write else ""
