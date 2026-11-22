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
machine for every process, which is the operator's to choose -- so on such a git a
dispatching pass also gives each tree under the workspace that git refuses its own
exact-path global entry (`persist_exact`), which every git reads: without it a push gate
or a fixer in an elevated session's tree was refused until git was upgraded.

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
import sys
from collections.abc import Callable, Iterable, Mapping, MutableMapping
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))  # the vendored tier
import worktree_tiers

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


# How git names the directory it refused, the exact value `safe.directory` must hold.
DUBIOUS = re.compile(r"dubious ownership in repository at '([^']+)'")
# The command-scope variables `trusted_env` writes, which a probe must not carry or it
# would be trusted by the very entry it is asking whether git needs.
_SCOPED = re.compile(r"GIT_CONFIG_(COUNT|KEY_\d+|VALUE_\d+)$")


def _bare_git(*args: str) -> subprocess.CompletedProcess[str]:
    """`_git` without the command scope: what a fixer's session or a push gate sees."""
    env = {key: value for key, value in os.environ.items() if not _SCOPED.match(key)}
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=False,
        creationflags=NO_WINDOW,
        env=env,
    )


def trees(root: Path) -> list[Path]:
    """Every checkout under `root`, each one's agent worktrees, and the workspace's boxes:
    the trees an elevated session can have made that the pass or a fixer acts in."""
    found: list[Path] = []
    try:
        children = sorted(path for path in root.iterdir() if path.is_dir())
    except OSError:
        return []
    for child in children:
        if child.name == worktree_tiers.BOXES_DIR_NAME:
            nest = child
        elif (child / ".git").exists():
            found.append(child)
            nest = child.joinpath(*worktree_tiers.DEFAULT_TIER.segments)
        else:
            continue
        try:
            found += sorted(path for path in nest.iterdir() if (path / ".git").exists())
        except OSError:
            continue
    return found


def refused(tree: Path, probe: Runner = _bare_git, tries: int = 3) -> list[str]:
    """The directories git refuses `tree` over, as it spells them, in the order it names
    them: a linked worktree's own directory, and its checkout's when that is foreign too.
    Each is found by asking again with the last one trusted, `tries` times at most."""
    named: list[str] = []
    for _ in range(tries):
        scope = [arg for value in named for arg in ("-c", f"{KEY}={value}")]
        done = probe(*scope, "-C", str(tree), "rev-parse", "--git-dir")
        found = DUBIOUS.search(done.stderr or "")
        if not found or found[1] in named:
            return named
        named.append(found[1])
    return named


def persist_exact(
    root: Path, git: Runner = _git, probe: Runner = _bare_git, under: Iterable[Path] | None = None
) -> str:
    """For a git older than `LEADING_PATH`: give each tree under `root` that git refuses
    its own global `safe.directory` entry, which every git reads, and drop the entries
    this added for trees that are gone. The lines to record, or "".

    The `/*` entry trusts nothing on such a git, and the pass's own `*` reaches no fixer
    session and no push gate: on 2026-11-21 a fixer in an elevated session's tree failed
    every bare git call and six of its tests (d2f2c4b0, 7a8de777) and a ship in another
    failed its first `git status` (46d64a5a). An exact path trusts that one directory and
    nothing else, which is the narrow grant `persist` makes, spelled so this git reads it.
    """
    listed = (git("config", "--global", "--get-all", KEY).stdout or "").splitlines()
    prefix = root.as_posix().rstrip("/").lower() + "/"
    lines = []
    for value in listed:
        if value.lower().startswith(prefix) and value != pattern(root) and not Path(value).exists():
            git("config", "--global", "--fixed-value", "--unset-all", KEY, value)
            lines.append(f"git-trust: dropped {value} from git's global {KEY}: it is gone")
    for tree in trees(root) if under is None else under:
        for value in refused(tree, probe):
            if value in listed:
                continue
            added = git("config", "--global", "--add", KEY, value)
            listed.append(value)
            lines.append(
                f"git-trust: added {value} to git's global {KEY}: another account owns it"
                if added.returncode == 0
                else f"git-trust: could not add {value} to git's global {KEY}: {_said(added)}"
            )
    return "\n".join(lines)


def adopt(
    root: Path,
    write: bool,
    environ: MutableMapping[str, str] = os.environ,
    git: Runner = _git,
    probe: Runner = _bare_git,
) -> str:
    """Trust `root`'s tree in this process, and globally too when `write`: `persist`'s lines,
    and on a git that cannot read the global entry `persist` writes, `persist_exact`'s and
    one saying so."""
    leading = reads_leading_path(git)
    environ.update(trusted_env(environ, root, None if leading else EVERYTHING))
    lines = persist(root, git) if write else ""
    if leading:
        return lines
    exact = persist_exact(root, git, probe) if write else ""
    old = (
        f"git-trust: this git predates {KEY}'s `/*` form ({'.'.join(map(str, LEADING_PATH))}),"
        f" so this pass trusts {EVERYTHING} in its own scope, and globally each tree under"
        f" {root.as_posix()} that another account owns by its own path"
    )
    return "\n".join(line for line in (lines, exact, old) if line)
