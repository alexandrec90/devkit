#!/usr/bin/env python3
"""Global Git branch-lifecycle policy installed by Devkit.

The policy is deliberately local: GitHub Free cannot enforce protected branches in
private repositories. It blocks the mistakes that matter before Git changes anything
remotely:

* commits while detached, on the remote default branch, or on main/master;
* pushes to those protected branches, or to a branch name whose GitHub PR merged;
* pushes that create or move a release tag, which only a release workflow may do.

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
from urllib.parse import quote, urlparse

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


def _pr_list_merged(runner: Runner, repo: str, branch: str) -> MergedPR:
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


def _rest_merged_pr(runner: Runner, repo: str, branch: str) -> MergedPR:
    """The same question over the REST API, for when the GraphQL half of gh is down.

    `state=closed` includes every merged PR; `merged_at` tells the merged ones from
    the merely closed. The branch is percent-encoded because a task branch routinely
    holds `/` and may hold characters a query value cannot.
    """
    owner = repo.split("/", 1)[0]
    head = quote(f"{owner}:{branch}", safe=":")
    argv = ["gh", "api", f"repos/{repo}/pulls?state=closed&head={head}&per_page=100"]
    result = runner(argv)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "gh api exited unsuccessfully").strip()
        return MergedPR(error=detail)
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as error:
        return MergedPR(error=f"gh api returned invalid JSON: {error}")
    if not isinstance(payload, list):
        return MergedPR(error="gh api returned an unexpected response")
    for item in payload:
        if isinstance(item, dict) and item.get("merged_at"):
            url = item.get("html_url")
            return MergedPR(url=url if isinstance(url, str) else f"{repo} merged PR")
    return MergedPR()


def merged_pr(runner: Runner, repo: str, branch: str) -> MergedPR:
    """Whether a PR from `branch` has merged, asked over both APIs before failing.

    `gh pr list` rides GraphQL, and GraphQL has been observed returning 503 while REST
    answered fine -- which, with `failClosed` defaulting on, blocked a commit and a
    push over an outage in the transport rather than any fact about the branch. The
    REST fallback asks the same question over the other API before the error is
    allowed to become a decision; only both failing reports one.
    """
    primary = _pr_list_merged(runner, repo, branch)
    if not primary.error:
        return primary
    fallback = _rest_merged_pr(runner, repo, branch)
    if fallback.error:
        return MergedPR(error=f"{primary.error} (REST fallback: {fallback.error})")
    return fallback


def _parse_ref_updates(raw: str, prefix: str) -> tuple[tuple[str, str, str, str, str], ...]:
    """The `<local ref> <local oid> <remote ref> <remote oid>` lines under `prefix`.

    Malformed lines are dropped rather than raising: this parses git's stdin inside a
    hook, where a line nobody anticipated must not take the push down.
    """
    parsed: list[tuple[str, str, str, str, str]] = []
    for line in raw.splitlines():
        fields = line.split()
        if len(fields) != 4:
            continue
        local_ref, local_oid, remote_ref, remote_oid = fields
        if not remote_ref.startswith(prefix):
            continue
        name = remote_ref[len(prefix) :]
        if name:
            parsed.append((local_ref, local_oid, remote_ref, remote_oid, name))
    return tuple(parsed)


def parse_push_updates(raw: str) -> tuple[PushUpdate, ...]:
    """The branch updates in a pre-push payload. Tag lines are `parse_tag_updates`'s."""
    return tuple(PushUpdate(*fields) for fields in _parse_ref_updates(raw, "refs/heads/"))


def parse_tag_updates(raw: str) -> tuple[TagUpdate, ...]:
    """The tag updates in a pre-push payload.

    These were parsed by nothing at all until a hand-pushed `v0.9.0` got through: the
    branch parser drops every `refs/tags/` line, which made a tag-only push a payload
    the policy saw as empty and waved through.
    """
    return tuple(TagUpdate(*fields) for fields in _parse_ref_updates(raw, "refs/tags/"))


def release_tag_decision(raw_updates: str) -> Decision:
    """Refuse a push that creates or moves a release tag.

    Pure -- no git, no network -- so it holds in a repo with no remote, no GitHub, or
    no `gh`, and cannot be the thing that makes a push hang.

    A *deletion* is deliberately allowed. Deleting is the recovery move when a bad tag
    is already published, and the rest of the pre-push policy exempts deletions for the
    same reason: this gate exists to stop an unverified tag being published, not to trap
    one that already was.
    """
    tags = sorted(
        {
            update.tag
            for update in parse_tag_updates(raw_updates)
            if not update.deletion and RELEASE_TAG_RE.fullmatch(update.tag)
        }
    )
    if not tags:
        return Decision()
    rendered = ", ".join(f"'{tag}'" for tag in tags)
    return Decision(
        errors=(
            f"push of release tag {rendered} blocked: a release tag is what consumers "
            "pin, so it must be cut by the release workflow that runs lint and the full "
            "suite against the exact commit first (devkit: `gh workflow run release.yml "
            "-f version=<tag> -f phase=tag`, after its prepare PR has merged). "
            f"To push it by hand anyway, set {SKIP_ENV_VAR}=1.",
        )
    )


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
    # Before the branch checks, and before their early return: a `git push --tags` or a
    # `push.followTags` ride-along carries no branch update at all, so anything that
    # reads `updates` first has already decided there is nothing to check.
    tag_decision = release_tag_decision(raw_updates)
    if not tag_decision.ok:
        return tag_decision

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


def _venv_roots(root: Path, runner: Runner) -> tuple[Path, ...]:
    """`root`, then the checkout it is a worktree of when that is a different directory.

    A worktree checks out **tracked files only**, and `.venv` is gitignored in every
    project here — so a worktree nobody provisioned has no virtualenv of its own, and
    looking in one place made "commit from a worktree" impossible: the framework was
    reported as not installed and the commit was refused, in a repo whose checkout has
    `pre-commit` sitting in `.venv` two directories up.

    Read off `--git-common-dir` rather than off a path convention, because the two
    worktree tiers on this machine sit at different depths — `<workspace>/.worktrees/`
    for a provisioned box and `<checkout>/.claude/worktrees/` for the plain kind
    `claude --worktree` and `scripts/agent-worktree.py` cut — and git already knows the
    answer for both, and for whatever the third tier turns out to be. A box has its own
    `.venv`, so this changes nothing for one; the ordering keeps it that way.

    Through the injected `runner` rather than a bare `subprocess.run`, for the reason
    `NO_WINDOW` is declared at the top of this module: this file is in the reachable set
    of the unattended jobs, and a spawn without `creationflags` opens a console window
    under `pythonw.exe`. `tests/test_scheduled_jobs.py` is the gate, and it caught this
    one on its first commit.
    """
    main = _stdout(
        _git(runner, "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir")
    )
    if not main:
        return (root,)
    checkout = Path(main).parent
    return (root,) if checkout == root else (root, checkout)


def _pre_commit_command(root: Path, runner: Runner) -> list[str] | None:
    candidates = tuple(
        base / ".venv" / tail
        for base in _venv_roots(root, runner)
        for tail in (Path("Scripts") / "pre-commit.exe", Path("bin") / "pre-commit")
    )
    for candidate in candidates:
        if candidate.is_file():
            return [str(candidate)]
    executable = shutil.which("pre-commit")
    if executable:
        return [executable]
    if importlib.util.find_spec("pre_commit") is not None:
        return [console_python(), "-m", "pre_commit"]
    return None


# Two agents reported this message in one week, both saying it names a refusal and no
# remedy -- so it reads as policy declining the commit rather than a tool being missing,
# and the first guess is `--no-verify`. The stale-copy line is not padding: this file is
# COPIED to `~/.devkit/git-hooks` and the hooks run the copy, so `_venv_roots` can be
# fixed here and still be missing where it fires -- which is how both reports were
# produced. Nothing about a stale copy looks wrong; only `--check` answers it.
NO_PRE_COMMIT = (
    "[devkit branch policy] project has .pre-commit-config.yaml but pre-commit is not installed",
    "  looked in: .venv of this tree and of the checkout it was cut from, PATH, "
    "and this interpreter",
    "  install it:      uv pip install pre-commit   (or: pip install pre-commit)",
    "  provision a box: python scripts/worktree.py provision <box>",
    "  if it IS installed, the hooks may be running a stale copy of this policy:",
    "                   python scripts/install-git-policy.py --check",
)


def _run_pre_commit_framework(root: Path, runner: Runner) -> int:
    if not (root / ".pre-commit-config.yaml").is_file():
        return 0
    command = _pre_commit_command(root, runner)
    if command is None:
        for line in NO_PRE_COMMIT:
            print(line, file=sys.stderr)
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
