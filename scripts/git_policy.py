#!/usr/bin/env python3
"""Global Git branch-lifecycle policy installed by Devkit.

The policy is deliberately local: GitHub Free cannot enforce protected branches in
private repositories. It blocks the two mistakes that matter before Git changes
anything remotely:

* commits while detached, on the remote default branch, or on main/master;
* pushes to those protected branches, or to a branch name whose GitHub PR merged.

After the policy passes, the dispatcher runs the repository's pre-commit framework
configuration (for ``pre-commit``) and an optional ``.githooks/<hook>``. Everything
is stdlib-only because the hook runs before a project environment is guaranteed.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

FAIL_CLOSED_KEY = "devkit.branchPolicy.failClosed"
PROTECTED_BRANCH_KEY = "devkit.branchPolicy.protectedBranch"
REMOTE_KEY = "devkit.branchPolicy.remote"
PROJECT_HOOKS_KEY = "devkit.branchPolicy.projectHooksPath"
DEFAULT_REMOTE = "origin"
DEFAULT_PROJECT_HOOKS = ".githooks"
ALWAYS_PROTECTED = frozenset({"main", "master"})
ZERO_OID_RE = re.compile(r"^0+$")
SUPPORTED_HOOKS = ("pre-commit", "pre-push")

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


@dataclass(frozen=True)
class PushUpdate:
    local_ref: str
    local_oid: str
    remote_ref: str
    remote_oid: str
    branch: str

    @property
    def deletion(self) -> bool:
        return self.local_ref == "(delete)" or bool(ZERO_OID_RE.fullmatch(self.local_oid))


@dataclass(frozen=True)
class MergedPR:
    url: str = ""
    error: str = ""


Runner = Callable[..., subprocess.CompletedProcess[str]]


def run_command(
    argv: Sequence[str],
    *,
    input_text: str | None = None,
    cwd: Path | None = None,
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
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
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


def default_branch(runner: Runner, remote: str) -> str:
    """Resolve a remote's default branch locally, with main/master fallbacks."""
    symbolic = _stdout(
        _git(runner, "symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD")
    )
    prefix = f"{remote}/"
    if symbolic.startswith(prefix):
        return symbolic[len(prefix) :]
    for candidate in ("main", "master"):
        exists = _git(
            runner,
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/remotes/{remote}/{candidate}",
        )
        if exists.returncode == 0:
            return candidate
    return ""


def protected_branches(runner: Runner, remote: str) -> frozenset[str]:
    configured = set(_config_values(runner, PROTECTED_BRANCH_KEY))
    detected = default_branch(runner, remote)
    if detected:
        configured.add(detected)
    return frozenset(ALWAYS_PROTECTED | configured)


def github_repo(remote_url: str) -> str | None:
    """Return OWNER/REPO for github.com HTTPS/SSH/scp URLs; otherwise None."""
    value = remote_url.strip()
    if not value:
        return None

    host = ""
    path = ""
    if "://" in value:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        path = parsed.path
    else:
        match = re.fullmatch(r"(?:[^@/\s]+@)?([^:/\s]+):(.+)", value)
        if not match:
            return None
        host, path = match.groups()
        host = host.lower()

    if host != "github.com":
        return None
    path = path.strip("/").removesuffix(".git")
    parts = path.split("/")
    if len(parts) != 2 or not all(parts):
        return None
    return "/".join(parts)


def merged_pr(runner: Runner, repo: str, branch: str) -> MergedPR:
    argv = [
        "gh",
        "pr",
        "list",
        "--repo",
        repo,
        "--head",
        branch,
        "--state",
        "merged",
        "--limit",
        "1",
        "--json",
        "number,url,mergedAt",
    ]
    result = runner(argv)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "gh exited unsuccessfully").strip()
        return MergedPR(error=detail)
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as error:
        return MergedPR(error=f"gh returned invalid JSON: {error}")
    if not isinstance(payload, list):
        return MergedPR(error="gh returned an unexpected response")
    if not payload:
        return MergedPR()
    first = payload[0]
    if not isinstance(first, dict):
        return MergedPR(error="gh returned an unexpected pull-request record")
    url = first.get("url")
    return MergedPR(url=url if isinstance(url, str) else f"{repo} merged PR")


def parse_push_updates(raw: str) -> tuple[PushUpdate, ...]:
    updates: list[PushUpdate] = []
    prefix = "refs/heads/"
    for line in raw.splitlines():
        fields = line.split()
        if len(fields) != 4:
            continue
        local_ref, local_oid, remote_ref, remote_oid = fields
        if not remote_ref.startswith(prefix):
            continue
        branch = remote_ref[len(prefix) :]
        if branch:
            updates.append(PushUpdate(local_ref, local_oid, remote_ref, remote_oid, branch))
    return tuple(updates)


def _remote_url(runner: Runner, remote: str, supplied_url: str = "") -> str:
    configured = _stdout(_git(runner, "remote", "get-url", remote))
    return configured or supplied_url


def _merged_decision(
    runner: Runner,
    repo: str | None,
    branch: str,
    fail_closed: bool,
    action: str,
) -> Decision:
    if repo is None:
        return Decision()
    result = merged_pr(runner, repo, branch)
    if result.url:
        return Decision(
            errors=(
                f"{action}: branch '{branch}' is permanently retired because its PR merged "
                f"({result.url})",
            )
        )
    if not result.error:
        return Decision()
    message = f"{action}: could not verify whether '{branch}' already merged: {result.error}"
    return Decision(errors=(message,)) if fail_closed else Decision(warnings=(message,))


def policy_skipped(env: Mapping[str, str]) -> bool:
    """True when `SKIP_ENV_VAR` is set to anything that does not read as "off"."""
    return env.get(SKIP_ENV_VAR, "").strip().lower() not in _OFF_VALUES


def evaluate_pre_commit(runner: Runner = run_command) -> Decision:
    branch_result = _git(runner, "branch", "--show-current")
    if branch_result.returncode != 0:
        return Decision(errors=("commit blocked: could not determine the current branch",))
    branch = branch_result.stdout.strip()
    if not branch:
        return Decision(
            errors=(
                "commit blocked on detached HEAD; create a fresh branch from the parked commit first",
            )
        )

    remote = _config_value(runner, REMOTE_KEY, DEFAULT_REMOTE)
    remote_url = _remote_url(runner, remote)

    # A repo with no remote has no PR to route through -- no GitHub repo, no base
    # branch, nothing to merge into. Enforcing "go via a PR" there does not redirect
    # the commit, it refuses the only commit that is possible. `core.hooksPath` is
    # global, so this fires in every throwaway repo on the machine: it blocked
    # `new-project.py`'s initial commit (which lands on the default branch *before*
    # the GitHub repo is created, deliberately) and every pytest fixture that builds a
    # scratch repo. Neither is caught by CI, where no global hook is installed.
    #
    # This does not weaken the policy: every repo it exists to protect has an origin.
    if not remote_url:
        return Decision()

    protected = protected_branches(runner, remote)
    if branch in protected:
        return Decision(
            errors=(
                f"commit blocked on protected branch '{branch}'; create a fresh task branch first",
            )
        )

    repo = github_repo(remote_url)
    fail_closed = _config_bool(runner, FAIL_CLOSED_KEY, True)
    return _merged_decision(runner, repo, branch, fail_closed, "commit blocked")


def evaluate_pre_push(
    remote: str,
    supplied_url: str,
    raw_updates: str,
    runner: Runner = run_command,
) -> Decision:
    updates = tuple(update for update in parse_push_updates(raw_updates) if not update.deletion)
    if not updates:
        return Decision()

    protected = protected_branches(runner, remote)
    protected_updates = sorted({update.branch for update in updates if update.branch in protected})
    if protected_updates:
        rendered = ", ".join(f"'{branch}'" for branch in protected_updates)
        return Decision(
            errors=(f"push to protected branch {rendered} blocked; push a task branch",)
        )

    repo = github_repo(_remote_url(runner, remote, supplied_url))
    fail_closed = _config_bool(runner, FAIL_CLOSED_KEY, True)
    errors: list[str] = []
    warnings: list[str] = []
    for branch in sorted({update.branch for update in updates}):
        decision = _merged_decision(runner, repo, branch, fail_closed, "push blocked")
        errors.extend(decision.errors)
        warnings.extend(decision.warnings)
    return Decision(tuple(errors), tuple(warnings))


def _repo_root(runner: Runner) -> Path | None:
    raw = _stdout(_git(runner, "rev-parse", "--show-toplevel"))
    return Path(raw).resolve() if raw else None


def _pre_commit_command(root: Path) -> list[str] | None:
    candidates = (
        root / ".venv" / "Scripts" / "pre-commit.exe",
        root / ".venv" / "bin" / "pre-commit",
    )
    for candidate in candidates:
        if candidate.is_file():
            return [str(candidate)]
    executable = shutil.which("pre-commit")
    if executable:
        return [executable]
    if importlib.util.find_spec("pre_commit") is not None:
        return [sys.executable, "-m", "pre_commit"]
    return None


def _run_pre_commit_framework(root: Path, runner: Runner) -> int:
    if not (root / ".pre-commit-config.yaml").is_file():
        return 0
    command = _pre_commit_command(root)
    if command is None:
        print(
            "[devkit branch policy] project has .pre-commit-config.yaml but pre-commit "
            "is not installed",
            file=sys.stderr,
        )
        return 1
    result = runner([*command, "run", "--hook-stage", "pre-commit"], cwd=root)
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return result.returncode


def _project_hook_command(path: Path, args: Sequence[str]) -> list[str] | None:
    if os.name != "nt":
        return [str(path), *args]
    if path.suffix.lower() == ".py":
        return [sys.executable, str(path), *args]
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
        print(
            f"[devkit branch policy] cannot execute project hook {hook}: sh is unavailable",
            file=sys.stderr,
        )
        return 1
    result = runner(command, input_text=input_text or None, cwd=root)
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
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
        print(f"[devkit branch policy] WARNING: {warning}", file=sys.stderr)
    if not decision.ok:
        for error in decision.errors:
            print(f"[devkit branch policy] {error}", file=sys.stderr)
        if artifact is not None:
            print(f"[devkit branch policy] details: {artifact}", file=sys.stderr)
        return 1

    root = _repo_root(runner)
    if root is None:
        print("[devkit branch policy] cannot locate repository root", file=sys.stderr)
        return 1
    if hook_name == "pre-commit":
        framework_result = _run_pre_commit_framework(root, runner)
        if framework_result:
            return framework_result
    return _run_project_hook(hook_name, args, input_text, root, runner)


def main(hook_name: str, argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    input_text = ""
    if hook_name == "pre-push" and sys.stdin is not None:
        try:
            input_text = sys.stdin.read()
        except (OSError, ValueError):
            input_text = ""
    return run_hook(hook_name, args, input_text=input_text)


if __name__ == "__main__":
    print("Invoke this module through the installed pre-commit/pre-push hooks.", file=sys.stderr)
    sys.exit(2)
