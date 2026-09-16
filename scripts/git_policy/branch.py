"""The policy proper: which branch may take a commit, and which may take a push.

Everything here is a *decision* about a ref, reached from git and GitHub and returning a
`Decision` rather than printing or exiting. That is what makes the whole tier testable
against a fake runner, and it is why the dispatcher -- which does print and exit -- is a
separate module.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from urllib.parse import quote, urlparse

from ._core import (
    ALWAYS_PROTECTED,
    DEFAULT_REMOTE,
    FAIL_CLOSED_KEY,
    PROTECTED_BRANCH_KEY,
    RELEASE_TAG_RE,
    REMOTE_KEY,
    SKIP_ENV_VAR,
    Decision,
    MergedPR,
    PushUpdate,
    Runner,
    TagUpdate,
    _config_bool,
    _config_value,
    _config_values,
    _git,
    _OFF_VALUES,
    _stdout,
    run_command,
)


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
        # The remedy is named because the refusal alone is a dead end, and an agent
        # reading it goes looking for an override. The case that produced this: a
        # branch whose first PR had merged still had a *second*, open PR on it, and
        # the fix for that PR's failing gate could not be committed or pushed at all.
        # The retirement is still right -- the merged commits are on the default
        # branch, so a push here would reopen settled history -- but "permanently"
        # with nothing after it reads as "there is no way to do this", and what the
        # session actually needed was one `git switch -c`. Said unconditionally
        # rather than after checking for an open PR: that check is another `gh` call
        # on every commit, and the answer does not change the remedy.
        return Decision(
            errors=(
                f"{action}: branch '{branch}' is permanently retired because its PR merged "
                f"({result.url}). A name is retired once, for good, and an open PR still "
                f"on it does not lift that. Carry the work to a new branch: "
                f"git switch -c <new-name> -- an open PR's head can be repointed, or it "
                f"can be replaced by a PR from the new branch.",
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
