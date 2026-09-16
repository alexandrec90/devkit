"""The hook entry point: what git actually calls, and everything that prints.

The only tier that writes to a stream or returns an exit code. Keeping it apart from
`branch` is what lets the policy be tested as pure decisions, and it is where the
console guard lives because this is the one place output leaves the process.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from ._core import (
    DEFAULT_PROJECT_HOOKS,
    DEFAULT_REMOTE,
    PROJECT_HOOKS_KEY,
    SKIP_ENV_VAR,
    SUPPORTED_HOOKS,
    Decision,
    Runner,
    _config_value,
    _git,
    _repo_root,
    _stdout,
    console_python,
    emit,
    run_command,
)

from .branch import evaluate_pre_commit, evaluate_pre_push, policy_skipped
from .framework import _run_pre_commit_framework


def _project_hook_command(path: Path, args: Sequence[str]) -> list[str] | None:
    if os.name != "nt":
        return [str(path), *args]
    if path.suffix.lower() == ".py":
        return [console_python(), str(path), *args]
    shell = shutil.which("sh")
    return [shell, str(path), *args] if shell else None


def _run_project_hook(
    hook_name: str,
    args: Sequence[str],
    input_text: str,
    root: Path,
    runner: Runner,
) -> int:
    configured = _config_value(runner, PROJECT_HOOKS_KEY, DEFAULT_PROJECT_HOOKS)
    directory = Path(configured)
    if not directory.is_absolute():
        directory = root / directory
    hook = directory / hook_name
    if not hook.is_file():
        return 0
    command = _project_hook_command(hook, args)
    if command is None:
        emit(
            f"[devkit branch policy] cannot execute project hook {hook}: sh is unavailable",
            stream=sys.stderr,
        )
        return 1
    result = runner(command, input_text=input_text or None, cwd=root)
    if result.stdout:
        emit(result.stdout, end="")
    if result.stderr:
        emit(result.stderr, end="", stream=sys.stderr)
    return result.returncode


def _artifact_path(runner: Runner) -> Path | None:
    raw = _stdout(_git(runner, "rev-parse", "--git-path", "devkit-branch-policy.json"))
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else Path.cwd() / path


def _write_artifact(
    hook_name: str,
    decision: Decision,
    runner: Runner,
) -> Path | None:
    path = _artifact_path(runner)
    if path is None:
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "hook": hook_name,
                    "errors": list(decision.errors),
                    "warnings": list(decision.warnings),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError:
        return None
    return path


def run_hook(
    hook_name: str,
    args: Sequence[str],
    *,
    input_text: str = "",
    runner: Runner = run_command,
    env: Mapping[str, str] | None = None,
) -> int:
    if hook_name not in SUPPORTED_HOOKS:
        # Checked before the opt-out: the escape hatch waives the branch checks, not
        # the question of whether this is a hook we know how to dispatch at all.
        decision = Decision(errors=(f"unsupported hook: {hook_name}",))
    elif policy_skipped(os.environ if env is None else env):
        # A warning rather than a bare print, so it reaches the failure artifact as
        # well as stderr: an exported variable disables the gate on every commit
        # thereafter, and the artifact is what explains a protected-branch commit
        # to whoever reads it later. The downstream hooks below still run.
        decision = Decision(warnings=(f"branch checks skipped by {SKIP_ENV_VAR}",))
    elif hook_name == "pre-commit":
        decision = evaluate_pre_commit(runner)
    else:
        remote = args[0] if args else DEFAULT_REMOTE
        supplied_url = args[1] if len(args) > 1 else ""
        decision = evaluate_pre_push(remote, supplied_url, input_text, runner)

    artifact = _write_artifact(hook_name, decision, runner)
    for warning in decision.warnings:
        emit(f"[devkit branch policy] WARNING: {warning}", stream=sys.stderr)
    if not decision.ok:
        for error in decision.errors:
            emit(f"[devkit branch policy] {error}", stream=sys.stderr)
        if artifact is not None:
            emit(f"[devkit branch policy] details: {artifact}", stream=sys.stderr)
        return 1

    root = _repo_root(runner)
    if root is None:
        emit("[devkit branch policy] cannot locate repository root", stream=sys.stderr)
        return 1
    framework_result = _run_pre_commit_framework(root, runner, hook_name, input_text)
    if framework_result:
        return framework_result
    return _run_project_hook(hook_name, args, input_text, root, runner)


def _utf8_console() -> None:
    """Make the relay of the gate's output unable to raise.

    The write half of the codec note on `run_command`. Under git the hook's streams
    are pipes, so Python encodes them with the *locale* codec -- cp1252 on a Windows
    workstation -- while everything this module relays is UTF-8: a test runner's em
    dashes and arrows, and the U+FFFD that `errors="replace"` puts where a byte could
    not be decoded, which **no codepage encodes**. Before this, a red pre-push gate
    whose output carried one such character died at the `print` that relayed it, so
    the developer saw a `UnicodeEncodeError` traceback with the failure it was
    relaying nowhere in it -- which is how a repo-corrupting bug came to present as a
    CPython crash in the hook. `errors="replace"` keeps the guard total: a stream that
    somehow still cannot take a character costs a `?`, never the report.

    `sys.stdout` is `None` under `pythonw`, and a capture object need not be a
    `TextIOWrapper`; neither is a reason to raise, so the guard asks first.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main(hook_name: str, argv: Sequence[str] | None = None, *, runner: Runner | None = None) -> int:
    _utf8_console()
    args = list(sys.argv[1:] if argv is None else argv)
    input_text = ""
    if hook_name == "pre-push" and sys.stdin is not None:
        try:
            input_text = sys.stdin.read()
        except (OSError, ValueError):
            input_text = ""
    return run_hook(
        hook_name, args, input_text=input_text, runner=run_command if runner is None else runner
    )
