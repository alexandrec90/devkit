"""Constants, the small frozen types, and the one place this package spawns.

The tier every other module here imports and which imports none of them. It is the
floor of the package rather than a junk drawer: a name belongs here when the branch
policy, the framework tier and the dispatcher would each otherwise need their own copy.
"""

from __future__ import annotations

import re
import subprocess
import sys
import typing
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


FAIL_CLOSED_KEY = "devkit.branchPolicy.failClosed"
PROTECTED_BRANCH_KEY = "devkit.branchPolicy.protectedBranch"
REMOTE_KEY = "devkit.branchPolicy.remote"
PROJECT_HOOKS_KEY = "devkit.branchPolicy.projectHooksPath"
DEFAULT_REMOTE = "origin"
DEFAULT_PROJECT_HOOKS = ".githooks"
ALWAYS_PROTECTED = frozenset({"main", "master"})
ZERO_OID_RE = re.compile(r"^0+$")
SUPPORTED_HOOKS = ("pre-commit", "pre-push")

# Windows only. `run_command` is the single spawn point for two callers that run with no
# console: the nightly trunk-merge job (`git-merge-default.py`, under `pythonw.exe`) and
# the installed git hooks. Windows gives a console child of a console-less process a
# brand new console **window**, so without this every `git` the merge runs is a window
# flashing on the desktop -- and the merge runs a dozen of them. The flagged child gets a
# window-less console that its own descendants inherit. Zero off Windows.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def console_python() -> str:
    """The console interpreter beside `sys.executable`, for spawning a Python child.

    `NO_WINDOW` is necessary and **not sufficient**. Windows ignores
    `CREATE_NO_WINDOW` for a GUI-subsystem child, so passing the flag alongside
    `pythonw.exe` -- which is what `sys.executable` is under a scheduled job -- leaves
    that child console-*less*, the exact condition that makes Windows open a fresh
    visible console for each of *its* children. Spawn a console interpreter with the
    flag instead and the child gets a hidden console that every descendant inherits.
    Pair the two; neither alone suppresses a window. Identity off Windows, and under
    any session that already has a console.
    """
    executable = Path(sys.executable)
    if executable.name.lower() != "pythonw.exe":
        return sys.executable
    console = executable.with_name("python.exe")
    # An embedded install could ship `pythonw.exe` with no console twin next to it.
    return str(console) if console.exists() else sys.executable


# A release tag is the one ref consumers pin, so the commit it names must be one whose
# suite passed *as tagged*. devkit's `release.yml phase=tag` is what guarantees that: it
# stages the tag locally, runs lint and the full suite against that exact commit, and
# pushes only then. A tag pushed from a workstation skips every part of it.
#
# That is not hypothetical. `v0.9.0` was pushed by hand six minutes after its prepare
# run and before its own fallback bump had merged, so the published tag named a commit
# whose `FALLBACK_DEVKIT_REF` still said `v0.8.0` and whose vendored tree already
# differed from `main`. Nothing was red anywhere -- the cost was a drift-red PR gate
# waiting in every consumer that adopted it, and three open PRs failing one shared test
# until the bump landed. `RELEASING.md` had warned against exactly this ordering in
# prose for months, which is the evidence that prose was not enough.
#
# Duplicated from `release.py`'s `VERSION_RE` on purpose: this module is *copied* into
# `~/.devkit/git-hooks` and runs with no checkout in reach, so it cannot import it.
# `test_the_release_tag_pattern_matches_the_release_scripts` holds the two together.
RELEASE_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")

# Escape hatch for scripted repo setup -- a generator that seeds an initial commit, a
# test fixture, a migration script. Named to match `DEVKIT_SKIP_STOP_VERIFY`.
#
# It costs nothing in enforcement: this is a client-side hook, so `git commit
# --no-verify` already bypasses it entirely. What it buys is a bypass that is
# *scriptable* without also disabling the project's own pre-commit gate, which is what
# `--no-verify` does.
SKIP_ENV_VAR = "DEVKIT_SKIP_BRANCH_POLICY"
# Values that read as "off" to a human must not switch the policy off. Anything else
# that is set turns it off. The asymmetry is deliberate: `DEVKIT_SKIP_BRANCH_POLICY=0`
# means "enforce" to everyone who writes it, and honouring it as "skip" would disable
# the gate for someone who was trying to turn it on.
_OFF_VALUES = frozenset({"", "0", "false", "no", "off"})


@dataclass(frozen=True)
class Decision:
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors


def _is_deletion(local_ref: str, local_oid: str) -> bool:
    return local_ref == "(delete)" or bool(ZERO_OID_RE.fullmatch(local_oid))


@dataclass(frozen=True)
class PushUpdate:
    local_ref: str
    local_oid: str
    remote_ref: str
    remote_oid: str
    branch: str

    @property
    def deletion(self) -> bool:
        return _is_deletion(self.local_ref, self.local_oid)


@dataclass(frozen=True)
class TagUpdate:
    """One `refs/tags/...` line of a pre-push payload.

    A separate type from `PushUpdate` rather than a reused one with `branch` holding a
    tag name: the two are asked different questions -- a branch is looked up on GitHub,
    a tag is matched against a shape -- and a field lying about which it holds is how
    the wrong one gets passed to the wrong check.
    """

    local_ref: str
    local_oid: str
    remote_ref: str
    remote_oid: str
    tag: str

    @property
    def deletion(self) -> bool:
        return _is_deletion(self.local_ref, self.local_oid)


@dataclass(frozen=True)
class MergedPR:
    url: str = ""
    error: str = ""


Runner = Callable[..., subprocess.CompletedProcess[str]]


def emit(text: str, *, stream: typing.TextIO | None = None, end: str = "\n") -> None:
    """Write `text` to a console that may not be able to encode it.

    The other half of `run_command`'s `errors="replace"`, and it has been missing for as
    long as that decision has existed. Decoding a tool's output leniently is what stops a
    stream being lost; it also means every string this package relays can contain U+FFFD,
    plus whatever else the tool emitted that UTF-8 carried and the console's codepage does
    not. `print` to a `cp1252` stdout then raises `UnicodeEncodeError` *from inside the
    hook*, which git reports as a failed hook: one replacement character anywhere in
    `pre-commit`'s output blocked every push in every repository on the machine, and the
    traceback named the encoder rather than anything the user had done.

    Lenient on the failing path only. The common console is UTF-8 and encodes the text
    exactly; re-encoding everything defensively would make *that* output lossy to protect
    a case it is not in. `TextIOWrapper.write` encodes the whole string before writing any
    of it, so nothing is emitted twice when the retry runs.
    """
    stream = sys.stdout if stream is None else stream
    payload = text + end
    try:
        stream.write(payload)
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "ascii"
        stream.write(payload.encode(encoding, "replace").decode(encoding, "replace"))


def run_command(
    argv: Sequence[str],
    *,
    input_text: str | None = None,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command without ever raising into Git's sparse hook error reporting.

    The encoding is named rather than left to `text=True`, and that is not a nicety.
    `text=True` alone decodes with the *locale* codec -- `cp1252` on a Windows
    workstation -- while git speaks UTF-8, so a branch name, a commit subject or a
    remote's banner carrying anything outside that codepage is undecodable. What
    that costs is worse than a crash, because it is not one: the decode happens on
    `subprocess`'s reader thread, so the `UnicodeDecodeError` is printed by
    `threading` and swallowed, `run()` returns normally with the command's real exit
    code, and the stream arrives as **`None`**. The caller then reports a failure
    with no reason attached -- which is how the trunk-merge task came to log a failed
    fetch whose message had been destroyed by the reading of it.

    `errors="replace"` is the other half: output that is genuinely not UTF-8 -- a
    path in some other codepage, a tool writing raw bytes -- must degrade to a
    replacement character, never to a lost stream.
    """
    try:
        return subprocess.run(
            list(argv),
            cwd=cwd,
            env=None if env is None else dict(env),
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            creationflags=NO_WINDOW,
        )
    except OSError as error:
        return subprocess.CompletedProcess(list(argv), 127, stdout="", stderr=str(error))


def _git(runner: Runner, *args: str) -> subprocess.CompletedProcess[str]:
    return runner(["git", *args])


def _stdout(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout.strip() if result.returncode == 0 else ""


def _config_value(runner: Runner, key: str, default: str = "") -> str:
    return _stdout(_git(runner, "config", "--get", key)) or default


def _config_values(runner: Runner, key: str) -> tuple[str, ...]:
    raw = _stdout(_git(runner, "config", "--get-all", key))
    return tuple(line.strip() for line in raw.splitlines() if line.strip())


def _config_bool(runner: Runner, key: str, default: bool) -> bool:
    raw = _stdout(_git(runner, "config", "--type=bool", "--get", key)).lower()
    if raw == "true":
        return True
    if raw == "false":
        return False
    return default


def _repo_root(runner: Runner) -> Path | None:
    """Here rather than beside its first caller: both `framework` and `dispatch` ask."""
    raw = _stdout(_git(runner, "rev-parse", "--show-toplevel"))
    return Path(raw).resolve() if raw else None
