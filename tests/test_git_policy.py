"""Tests for Devkit's global commit/push branch policy."""

import json
import pathlib
import subprocess
import sys

import support
from support import git_policy


class FakeRunner:
    """Command runner with exact argv responses and a safe missing-command default."""

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv, *, input_text=None, cwd=None):
        key = tuple(argv)
        self.calls.append(key)
        return self.responses.get(
            key,
            subprocess.CompletedProcess(argv, 1, stdout="", stderr="not configured"),
        )


def completed(argv, stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)


def git_responses(branch="claude/fresh", remote_url="https://github.com/acme/widgets.git"):
    return {
        ("git", "branch", "--show-current"): completed(
            ["git"], stdout=f"{branch}\n" if branch else ""
        ),
        ("git", "config", "--type=bool", "--get", "devkit.branchPolicy.failClosed"): completed(
            ["git"], returncode=1
        ),
        ("git", "config", "--get-all", "devkit.branchPolicy.protectedBranch"): completed(
            ["git"], returncode=1
        ),
        ("git", "config", "--get", "devkit.branchPolicy.remote"): completed(["git"], returncode=1),
        ("git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"): completed(
            ["git"], stdout="origin/main\n"
        ),
        ("git", "remote", "get-url", "origin"): completed(["git"], stdout=f"{remote_url}\n"),
    }


def merged_response(branch, payload):
    argv = (
        "gh",
        "pr",
        "list",
        "--repo",
        "acme/widgets",
        "--head",
        branch,
        "--state",
        "merged",
        "--limit",
        "1",
        "--json",
        "number,url,mergedAt",
    )
    return {argv: completed(argv, stdout=json.dumps(payload))}


def test_github_repo_parses_https_ssh_and_rejects_other_hosts():
    assert git_policy.github_repo("https://github.com/acme/widgets.git") == "acme/widgets"
    assert git_policy.github_repo("git@github.com:acme/widgets.git") == "acme/widgets"
    assert git_policy.github_repo("ssh://git@github.com/acme/widgets.git") == "acme/widgets"
    assert git_policy.github_repo("https://gitlab.com/acme/widgets.git") is None
    assert git_policy.github_repo("") is None


def test_pre_commit_rejects_default_branch_and_detached_head_without_network():
    on_main = FakeRunner(git_responses(branch="main"))
    decision = git_policy.evaluate_pre_commit(on_main)
    assert not decision.ok
    assert any("protected branch 'main'" in error for error in decision.errors)
    assert not any(call[0] == "gh" for call in on_main.calls)

    detached = FakeRunner(git_responses(branch=""))
    decision = git_policy.evaluate_pre_commit(detached)
    assert not decision.ok
    assert any("detached HEAD" in error for error in decision.errors)


def test_pre_commit_allows_the_default_branch_when_there_is_no_remote():
    """A remoteless repo has no PR to route through, so the policy cannot apply.

    `core.hooksPath` is global, so this fires in every throwaway repo on the machine.
    Blocking there does not redirect the commit onto a branch — it refuses the only
    commit that is possible. Two real victims, neither visible in CI (no global hook
    on a runner):

    * `new-project.py`'s `git_init()` — `git init -b main`, `add -A`, `commit`, all
      before the GitHub repo is created, which is deliberate ordering.
    * every pytest fixture that builds a scratch repo (this file included).

    The protection is unchanged for repos that have an origin, which is all of them.
    """
    # `git remote get-url` exits non-zero when no remote is configured.
    no_remote = git_responses(branch="main")
    no_remote[("git", "remote", "get-url", "origin")] = completed(["git"], returncode=1)
    decision = git_policy.evaluate_pre_commit(FakeRunner(no_remote))
    assert decision.ok, f"remoteless repo should commit freely, got {decision.errors}"

    # Belt and braces: a configured-but-empty URL is the same situation.
    empty_url = git_responses(branch="master", remote_url="")
    assert git_policy.evaluate_pre_commit(FakeRunner(empty_url)).ok


def test_pre_commit_still_blocks_the_default_branch_once_a_remote_exists():
    """The guard above must not become a way to bypass the policy.

    Pinned separately because "allow when no remote" and "block when there is one" are
    the two halves of the same decision, and a regression in either direction is
    silent — one strands work on master, the other bricks `git init`.
    """
    for branch in ("main", "master"):
        with_remote = git_responses(branch=branch)
        decision = git_policy.evaluate_pre_commit(FakeRunner(with_remote))
        assert not decision.ok, f"{branch} must stay protected when origin exists"
        assert any(f"protected branch '{branch}'" in e for e in decision.errors)


def test_pre_commit_allows_a_fresh_unique_branch():
    responses = git_responses()
    responses.update(merged_response("claude/fresh", []))
    decision = git_policy.evaluate_pre_commit(FakeRunner(responses))
    assert decision.ok


def test_pre_commit_permanently_retires_a_merged_branch_name():
    responses = git_responses(branch="claude/already-shipped")
    responses.update(
        merged_response(
            "claude/already-shipped",
            [{"number": 17, "url": "https://github.com/acme/widgets/pull/17", "mergedAt": "now"}],
        )
    )
    decision = git_policy.evaluate_pre_commit(FakeRunner(responses))
    assert not decision.ok
    assert "pull/17" in decision.errors[0]


def rest_merged_response(branch, payload):
    """The REST fallback's exact argv, mirroring `_rest_merged_pr`'s encoding."""
    from urllib.parse import quote

    head = quote(f"acme:{branch}", safe=":")
    argv = ("gh", "api", f"repos/acme/widgets/pulls?state=closed&head={head}&per_page=100")
    return {argv: completed(argv, stdout=json.dumps(payload))}


def test_a_graphql_outage_falls_back_to_rest_before_failing_closed():
    """`gh pr list` rides GraphQL, which has returned 503 while REST answered fine.

    With `failClosed` defaulting on, that transport outage blocked a commit and a push
    over no fact about the branch at all -- the reporter had to reach for
    DEVKIT_SKIP_BRANCH_POLICY after confirming the answer over REST by hand. The
    fallback asks REST the same question before the error becomes a decision.
    """
    # `gh pr list` is absent from the responses, so it takes the failing default.
    responses = git_responses()
    responses.update(
        rest_merged_response(
            "claude/fresh",
            [{"number": 3, "merged_at": None, "html_url": "https://x/pull/3"}],
        )
    )
    decision = git_policy.evaluate_pre_commit(FakeRunner(responses))
    assert decision.ok, f"REST said not merged, got {decision.errors}"


def test_the_rest_fallback_still_retires_a_merged_branch():
    responses = git_responses(branch="claude/already-shipped")
    responses.update(
        rest_merged_response(
            "claude/already-shipped",
            [
                {"number": 4, "merged_at": None, "html_url": "https://x/pull/4"},
                {
                    "number": 17,
                    "merged_at": "2026-08-17T00:00:00Z",
                    "html_url": "https://github.com/acme/widgets/pull/17",
                },
            ],
        )
    )
    decision = git_policy.evaluate_pre_commit(FakeRunner(responses))
    assert not decision.ok
    assert "pull/17" in decision.errors[0]


def test_both_apis_failing_names_both_in_the_error():
    decision = git_policy.evaluate_pre_commit(FakeRunner(git_responses()))
    assert not decision.ok
    assert any("REST fallback" in error for error in decision.errors)


def test_github_lookup_failure_is_closed_by_default_and_configurably_open():
    responses = git_responses()
    runner = FakeRunner(responses)
    decision = git_policy.evaluate_pre_commit(runner)
    assert not decision.ok
    assert any("could not verify" in error for error in decision.errors)

    responses[
        (
            "git",
            "config",
            "--type=bool",
            "--get",
            "devkit.branchPolicy.failClosed",
        )
    ] = completed(["git"], stdout="false\n")
    decision = git_policy.evaluate_pre_commit(FakeRunner(responses))
    assert decision.ok
    assert any("could not verify" in warning for warning in decision.warnings)


def test_non_github_remote_skips_pr_lookup_but_still_protects_main():
    feature = FakeRunner(
        git_responses(branch="feature/x", remote_url="https://gitlab.com/acme/widgets.git")
    )
    assert git_policy.evaluate_pre_commit(feature).ok
    assert not any(call[0] == "gh" for call in feature.calls)

    main = FakeRunner(
        git_responses(branch="main", remote_url="https://gitlab.com/acme/widgets.git")
    )
    assert not git_policy.evaluate_pre_commit(main).ok


def test_pre_push_checks_destinations_not_the_current_branch():
    responses = git_responses()
    runner = FakeRunner(responses)
    raw = f"refs/heads/feature/x {'1' * 40} refs/heads/main {'2' * 40}\n"
    decision = git_policy.evaluate_pre_push("origin", "", raw, runner)
    assert not decision.ok
    assert any("push to protected branch 'main'" in error for error in decision.errors)
    assert not any(call[0] == "gh" for call in runner.calls)


def test_pre_push_rejects_recreating_a_merged_remote_branch():
    responses = git_responses()
    responses.update(
        merged_response(
            "claude/retired",
            [{"number": 8, "url": "https://github.com/acme/widgets/pull/8", "mergedAt": "now"}],
        )
    )
    raw = f"refs/heads/new {'1' * 40} refs/heads/claude/retired {'0' * 40}\n"
    decision = git_policy.evaluate_pre_push("origin", "", raw, FakeRunner(responses))
    assert not decision.ok
    assert "permanently retired" in decision.errors[0]


def test_pre_push_allows_deleting_a_retired_branch_without_querying_github():
    responses = git_responses()
    runner = FakeRunner(responses)
    raw = f"(delete) {'0' * 40} refs/heads/claude/retired {'1' * 40}\n"
    decision = git_policy.evaluate_pre_push("origin", "", raw, runner)
    assert decision.ok
    assert not any(call[0] == "gh" for call in runner.calls)


def test_push_input_parser_ignores_malformed_lines_and_tags():
    raw = (
        "bad line\n"
        f"refs/tags/v1 {'1' * 40} refs/tags/v1 {'0' * 40}\n"
        f"refs/heads/x {'1' * 40} refs/heads/x {'0' * 40}\n"
    )
    assert [update.branch for update in git_policy.parse_push_updates(raw)] == ["x"]


def tag_push(tag, *, delete=False):
    """A pre-push payload line for `tag`, as git writes it."""
    if delete:
        return f"(delete) {'0' * 40} refs/tags/{tag} {'1' * 40}\n"
    return f"refs/tags/{tag} {'1' * 40} refs/tags/{tag} {'0' * 40}\n"


def test_pre_push_blocks_a_hand_pushed_release_tag():
    # The v0.9.0 regression: the tag was pushed from a workstation, so it named a
    # commit no `phase=tag` run had ever validated as tagged.
    runner = FakeRunner(git_responses())
    decision = git_policy.evaluate_pre_push("origin", "", tag_push("v0.9.0"), runner)
    assert not decision.ok
    assert "push of release tag 'v0.9.0' blocked" in decision.errors[0]
    assert "release.yml" in decision.errors[0]
    # Pure: a tag needs no PR lookup, so nothing may reach the network for one.
    assert not any(call[0] == "gh" for call in runner.calls)


def test_pre_push_blocks_a_release_tag_riding_along_with_a_branch():
    # `push.followTags`, or `git push origin HEAD v0.9.0`: the branch half is
    # unobjectionable and the whole push still has to fail.
    raw = f"refs/heads/claude/fresh {'1' * 40} refs/heads/claude/fresh {'0' * 40}\n" + tag_push(
        "v1.2.3"
    )
    decision = git_policy.evaluate_pre_push("origin", "", raw, FakeRunner(git_responses()))
    assert not decision.ok
    assert "'v1.2.3'" in decision.errors[0]


def test_pre_push_allows_deleting_a_release_tag():
    # Deletion is the recovery move for a tag already published, and the rest of the
    # policy exempts deletions too. Blocking it would trap the mistake instead of the
    # act that makes one.
    runner = FakeRunner(git_responses())
    decision = git_policy.evaluate_pre_push("origin", "", tag_push("v0.9.0", delete=True), runner)
    assert decision.ok


def test_pre_push_allows_a_tag_that_is_not_a_release():
    # A marker, a nightly, a vendor pin: nothing downstream resolves those the way a
    # consumer resolves `rev:`, so the gate has no claim on them.
    for tag in ("nightly-2026-08-17", "v1.2", "release-candidate", "v1.2.3-rc1"):
        decision = git_policy.evaluate_pre_push(
            "origin", "", tag_push(tag), FakeRunner(git_responses())
        )
        assert decision.ok, f"{tag} should not be treated as a release tag"


def test_the_release_tag_block_is_waived_by_the_skip_env_var(tmp_path, monkeypatch):
    # The escape hatch has to reach this check too: `release.py --yes` run by hand is a
    # legitimate caller, and `--no-verify` would take the project's own gate with it.
    responses = git_responses()
    responses[("git", "rev-parse", "--git-path", "devkit-branch-policy.json")] = completed(
        ["git"], returncode=1
    )
    responses[("git", "rev-parse", "--show-toplevel")] = completed(["git"], stdout=f"{tmp_path}\n")
    code = git_policy.run_hook(
        "pre-push",
        ["origin", "https://github.com/acme/widgets.git"],
        input_text=tag_push("v0.9.0"),
        runner=FakeRunner(responses),
        env={git_policy.SKIP_ENV_VAR: "1"},
    )
    assert code == 0


def test_the_release_tag_pattern_matches_the_release_scripts():
    """The duplicated regex is the price of the hook running with no checkout in reach.

    `git_policy.py` is *copied* into `~/.devkit/git-hooks`, so it cannot import
    `release.py`. If the two ever disagree about what a release version looks like, the
    gate stops covering the versions the workflow can actually cut — silently, since a
    tag it fails to recognise is one it waves through.
    """
    release = support.load_script("scripts/release.py")
    for version in ("v0.9.1", "v1.0.0", "v10.20.30"):
        assert release.VERSION_RE.fullmatch(version)
        assert git_policy.RELEASE_TAG_RE.fullmatch(version)
    for other in ("v1.2", "1.2.3", "v1.2.3-rc1", "release/v1.2.3"):
        assert not release.VERSION_RE.fullmatch(other)
        assert not git_policy.RELEASE_TAG_RE.fullmatch(other)


def test_policy_runs_pre_commit_framework_then_project_hook(tmp_path, monkeypatch):
    responses = git_responses()
    responses.update(merged_response("claude/fresh", []))
    responses[("git", "rev-parse", "--git-path", "devkit-branch-policy.json")] = completed(
        ["git"], returncode=1
    )
    responses[("git", "rev-parse", "--show-toplevel")] = completed(["git"], stdout=f"{tmp_path}\n")
    responses[("git", "config", "--get", "devkit.branchPolicy.projectHooksPath")] = completed(
        ["git"], returncode=1
    )
    responses[("pre-commit-test", "run", "--hook-stage", "pre-commit")] = completed(
        ["pre-commit-test"]
    )
    responses[("project-hook-test",)] = completed(["project-hook-test"])
    (tmp_path / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")
    project_hook = tmp_path / ".githooks" / "pre-commit"
    project_hook.parent.mkdir()
    project_hook.write_text("# project hook\n", encoding="utf-8")

    monkeypatch.setattr(
        git_policy, "_pre_commit_command", lambda _root, _runner: ["pre-commit-test"]
    )
    monkeypatch.setattr(
        git_policy, "_project_hook_command", lambda _path, _args: ["project-hook-test"]
    )
    runner = FakeRunner(responses)
    assert git_policy.run_hook("pre-commit", [], runner=runner) == 0
    assert runner.calls.index(("pre-commit-test", "run", "--hook-stage", "pre-commit")) < (
        runner.calls.index(("project-hook-test",))
    )


def _common_dir(main_git: pathlib.Path | None):
    """A runner answering `rev-parse --git-common-dir`, or failing like a non-repository."""

    def runner(argv, *, input_text=None, cwd=None):
        if main_git is None:
            return completed(argv, returncode=1)
        return completed(argv, stdout=f"{main_git}\n")

    return runner


def test_a_worktree_finds_the_venv_of_the_checkout_it_belongs_to(tmp_path):
    """A worktree checks out tracked files only and `.venv` is gitignored, so a worktree
    nobody provisioned has none of its own.

    Regression: this made committing from a `.claude/worktrees/` worktree impossible.
    Every commit was refused with "project has .pre-commit-config.yaml but pre-commit is
    not installed", in a repo whose checkout had `pre-commit` in `.venv` two directories
    up -- and refusing is the correct half of that behaviour, so the failure looked like
    policy rather than like a lookup that stopped one directory short.
    """
    checkout = tmp_path / "devkit"
    tool = checkout / ".venv" / "Scripts" / "pre-commit.exe"
    tool.parent.mkdir(parents=True)
    tool.write_text("", encoding="utf-8")
    worktree = checkout / ".claude" / "worktrees" / "topic"
    worktree.mkdir(parents=True)
    assert git_policy._pre_commit_command(worktree, _common_dir(checkout / ".git")) == [str(tool)]


def test_a_worktree_with_its_own_venv_keeps_using_it(tmp_path):
    """The ordering, asserted rather than assumed: a provisioned box has a virtualenv of
    its own with the project's own pinned tools in it, and reaching past that to the
    source checkout's would run a different `pre-commit` than the box installed."""
    checkout = tmp_path / "devkit"
    outer = checkout / ".venv" / "Scripts" / "pre-commit.exe"
    outer.parent.mkdir(parents=True)
    outer.write_text("", encoding="utf-8")
    box = tmp_path / ".worktrees" / "devkit--topic-0905"
    inner = box / ".venv" / "Scripts" / "pre-commit.exe"
    inner.parent.mkdir(parents=True)
    inner.write_text("", encoding="utf-8")
    assert git_policy._pre_commit_command(box, _common_dir(checkout / ".git")) == [str(inner)]


def test_a_directory_git_cannot_answer_for_falls_back_to_itself(tmp_path):
    """`_pre_commit_command` is called with the repo root, so this should not happen --
    and it runs inside a commit hook, where a raise is a commit refused with a traceback
    instead of a reason."""
    assert git_policy._venv_roots(tmp_path, _common_dir(None)) == (tmp_path,)


def test_a_plain_checkout_looks_in_exactly_one_place(tmp_path):
    """`--git-common-dir` in a non-worktree names that checkout's own `.git`, so the
    fallback must collapse rather than listing the same directory twice."""
    assert git_policy._venv_roots(tmp_path, _common_dir(tmp_path / ".git")) == (tmp_path,)


def test_skip_env_var_reads_only_explicit_off_values_as_off():
    """The opt-out is opt-in: unset means enforce, and `0`/`false`/`no`/`off` mean enforce.

    Pinned because the inverse -- a var whose mere presence with the value `0` disables
    the policy -- is the classic footgun: `DEVKIT_SKIP_BRANCH_POLICY=0` reads to everyone
    as "off", and silently disabling the gate is the one outcome this must never have.
    """
    for value in ("", "0", "false", "FALSE", "no", "off", "  off  "):
        assert not git_policy.policy_skipped({git_policy.SKIP_ENV_VAR: value}), value
    for value in ("1", "true", "TRUE", "yes", "on"):
        assert git_policy.policy_skipped({git_policy.SKIP_ENV_VAR: value}), value
    assert not git_policy.policy_skipped({})


def test_skip_env_var_bypasses_branch_checks_but_still_runs_downstream_hooks(tmp_path, monkeypatch):
    """Opting out skips the *branch policy*, not the project's own commit gate.

    If it skipped everything, the escape hatch for "let me commit on main" would also
    silently disable the consumer's pre-commit config -- a far bigger hammer than the
    one asked for, and invisible at the moment it matters.
    """
    responses = git_responses(branch="main")  # would be blocked without the opt-out
    artifact = tmp_path / "policy.json"
    responses[("git", "rev-parse", "--git-path", "devkit-branch-policy.json")] = completed(
        ["git"], stdout=f"{artifact}\n"
    )
    responses[("git", "rev-parse", "--show-toplevel")] = completed(["git"], stdout=f"{tmp_path}\n")
    responses[("git", "config", "--get", "devkit.branchPolicy.projectHooksPath")] = completed(
        ["git"], returncode=1
    )
    responses[("pre-commit-test", "run", "--hook-stage", "pre-commit")] = completed(
        ["pre-commit-test"]
    )
    (tmp_path / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")
    monkeypatch.setattr(
        git_policy, "_pre_commit_command", lambda _root, _runner: ["pre-commit-test"]
    )

    runner = FakeRunner(responses)
    code = git_policy.run_hook("pre-commit", [], runner=runner, env={git_policy.SKIP_ENV_VAR: "1"})

    assert code == 0, "the opt-out must let a commit on a protected branch through"
    assert ("pre-commit-test", "run", "--hook-stage", "pre-commit") in runner.calls
    # The branch check never ran, so no PR lookup was attempted.
    assert not any(call and call[0] == "gh" for call in runner.calls)


def test_skip_env_var_announces_itself_on_every_run(tmp_path, capsys):
    """A var exported into a shell profile disables the gate forever; say so each time.

    Recorded as a warning (not a bare print) so it lands in the failure artifact too --
    the artifact is what a later agent reads to explain why a protected-branch commit
    was allowed.
    """
    responses = git_responses(branch="main")
    artifact = tmp_path / "policy.json"
    responses[("git", "rev-parse", "--git-path", "devkit-branch-policy.json")] = completed(
        ["git"], stdout=f"{artifact}\n"
    )
    responses[("git", "rev-parse", "--show-toplevel")] = completed(["git"], stdout=f"{tmp_path}\n")
    responses[("git", "config", "--get", "devkit.branchPolicy.projectHooksPath")] = completed(
        ["git"], returncode=1
    )
    git_policy.run_hook(
        "pre-commit", [], runner=FakeRunner(responses), env={git_policy.SKIP_ENV_VAR: "1"}
    )

    assert git_policy.SKIP_ENV_VAR in capsys.readouterr().err
    assert (
        git_policy.SKIP_ENV_VAR in json.loads(artifact.read_text(encoding="utf-8"))["warnings"][0]
    )


def test_skip_env_var_does_not_excuse_an_unsupported_hook():
    """The opt-out waives the branch checks, not argument validation."""
    runner = FakeRunner(git_responses())
    code = git_policy.run_hook("post-merge", [], runner=runner, env={git_policy.SKIP_ENV_VAR: "1"})
    assert code == 1


# --- the runner every caller decodes git through -----------------------------
#
# `run_command` backs the hooks, `sweep`, `workspace-status` and the trunk-merge task,
# and its failure mode was not an exception: `text=True` alone decodes with the locale
# codepage, the `UnicodeDecodeError` is raised on `subprocess`'s reader thread where
# nothing propagates it, and the caller gets the real exit code with the stream set to
# `None`. So a fetch failed and the task logged nothing but `# exit: 1`.


def _emitting(expression: str) -> list[str]:
    """A child that writes exact bytes to both streams, bypassing any text layer."""
    return [
        sys.executable,
        "-c",
        f"import sys;b={expression};sys.stdout.buffer.write(b);sys.stderr.buffer.write(b)",
    ]


def test_the_decoding_is_pinned_to_utf8_rather_than_the_ambient_locale(monkeypatch):
    """The ratchet, asserted on the call rather than on the result, because the result
    depends on the machine: this bug is invisible on a UTF-8 runner and fatal on the
    cp1252 workstation the tasks actually run on."""
    seen = {}

    def spy(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(git_policy.subprocess, "run", spy)
    git_policy.run_command(["git", "status"])
    assert seen["encoding"] == "utf-8"
    assert seen["errors"] == "replace"


def test_utf8_output_survives_being_read():
    """A curly quote in a commit subject or a remote's banner is ordinary git output."""
    result = git_policy.run_command(_emitting(r"'fatal: “nope”'.encode()"))
    assert result.stdout == "fatal: “nope”"
    assert result.stderr == "fatal: “nope”"


def test_undecodable_output_degrades_instead_of_losing_the_stream():
    """Bytes that are not UTF-8 at all must cost a character, never the whole message
    and never the exit code -- the two things a caller reports a failure with."""
    result = git_policy.run_command(_emitting(r"b'fatal: \xff' + b'ok'"))
    assert result.stdout is not None
    assert result.stdout.startswith("fatal: ")
    assert result.stdout.endswith("ok")
    assert result.stderr is not None


def test_a_failing_commands_exit_code_survives_undecodable_output():
    argv = _emitting(r"b'\xff'")
    argv[-1] += ";sys.exit(128)"
    result = git_policy.run_command(argv)
    assert result.returncode == 128
    assert result.stdout is not None


def test_failed_policy_never_runs_downstream_hooks(tmp_path):
    responses = git_responses(branch="main")
    responses[("git", "rev-parse", "--git-path", "devkit-branch-policy.json")] = completed(
        ["git"], returncode=1
    )
    responses[("git", "rev-parse", "--show-toplevel")] = completed(["git"], stdout=f"{tmp_path}\n")
    runner = FakeRunner(responses)
    assert git_policy.run_hook("pre-commit", [], runner=runner) == 1
    assert ("git", "rev-parse", "--show-toplevel") not in runner.calls
