"""Tests for Devkit's global commit/push branch policy."""

import io
import json
import os
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
        # `env` is recorded rather than ignored because it is load-bearing for one
        # caller: the pre-commit framework's `language: system` hooks are resolved
        # from it. See `test_the_framework_runs_with_its_own_directory_first_on_path`.
        self.envs: list[dict[str, str] | None] = []
        # Recorded for the same reason as `env`: the push stage asks for a passthrough
        # run so its output is not held until the gate finishes, and a call that stops
        # asking is invisible in every other assertion here.
        self.streamed: list[bool] = []

    def __call__(self, argv, *, input_text=None, cwd=None, env=None, stream=False):
        key = tuple(argv)
        self.calls.append(key)
        self.envs.append(env)
        self.streamed.append(stream)
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


def test_the_retirement_block_names_the_way_forward_not_just_the_refusal():
    """The reported dead end: a branch retired by its first merged PR still had a
    second, open PR on it, so the fix for that PR's failing gate could not be
    committed or pushed. The retirement is correct -- the merged commits are on the
    default branch already -- but a refusal that stops at "permanently" reads as
    having no answer, and the session went looking for an override instead of the one
    `git switch -c` it needed."""
    responses = git_responses(branch="claude/retired")
    responses.update(
        merged_response(
            "claude/retired",
            [{"number": 8, "url": "https://github.com/acme/widgets/pull/8", "mergedAt": "now"}],
        )
    )
    error = git_policy.evaluate_pre_commit(FakeRunner(responses)).errors[0]
    assert "git switch -c" in error
    # The other half of what was missing: that an open PR does not lift it, so the
    # next reader does not spend the turn establishing that for themselves.
    assert "open PR" in error


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
        git_policy.framework, "_pre_commit_command", lambda _root, _runner: ["pre-commit-test"]
    )
    monkeypatch.setattr(
        git_policy.dispatch, "_project_hook_command", lambda _path, _args: ["project-hook-test"]
    )
    runner = FakeRunner(responses)
    assert git_policy.run_hook("pre-commit", [], runner=runner) == 0
    assert runner.calls.index(("pre-commit-test", "run", "--hook-stage", "pre-commit")) < (
        runner.calls.index(("project-hook-test",))
    )


def _push_responses(tmp_path):
    responses = git_responses()
    responses.update(merged_response("claude/fresh", []))
    responses[("git", "rev-parse", "--git-path", "devkit-branch-policy.json")] = completed(
        ["git"], returncode=1
    )
    responses[("git", "rev-parse", "--show-toplevel")] = completed(["git"], stdout=f"{tmp_path}\n")
    responses[("git", "config", "--get", "devkit.branchPolicy.projectHooksPath")] = completed(
        ["git"], returncode=1
    )
    responses[("pre-commit-test", "run", "--hook-stage", "pre-push", "--all-files")] = completed(
        ["pre-commit-test"]
    )
    (tmp_path / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")
    return responses


PUSH_STAGE = ("pre-commit-test", "run", "--hook-stage", "pre-push", "--all-files")


def test_a_push_that_publishes_a_branch_runs_the_pre_push_stage(tmp_path, monkeypatch):
    """The dispatcher owns `core.hooksPath`, so `pre-commit install` never wires the
    framework's own pre-push hook here; the PR gate at push time exists only if this
    runs it. `--all-files`, because the stage's hooks are `always_run` gates over the
    tree and the staged-diff default would stash unstaged work to run them."""
    monkeypatch.setattr(
        git_policy.framework, "_pre_commit_command", lambda _root, _runner: ["pre-commit-test"]
    )
    runner = FakeRunner(_push_responses(tmp_path))
    raw = f"refs/heads/claude/fresh {'1' * 40} refs/heads/claude/fresh {'0' * 40}\n"
    assert git_policy.run_hook("pre-push", ["origin"], input_text=raw, runner=runner) == 0
    assert PUSH_STAGE in runner.calls


def test_the_pre_push_stage_failing_refuses_the_push(tmp_path, monkeypatch):
    monkeypatch.setattr(
        git_policy.framework, "_pre_commit_command", lambda _root, _runner: ["pre-commit-test"]
    )
    responses = _push_responses(tmp_path)
    responses[PUSH_STAGE] = completed(["pre-commit-test"], stdout="tests failed\n", returncode=1)
    runner = FakeRunner(responses)
    raw = f"refs/heads/claude/fresh {'1' * 40} refs/heads/claude/fresh {'0' * 40}\n"
    assert git_policy.run_hook("pre-push", ["origin"], input_text=raw, runner=runner) == 1


def test_a_gate_failure_survives_a_cp1252_console(tmp_path, monkeypatch):
    """The hook's stdout is a pipe under git, so on Windows Python picks the locale
    codec for it -- cp1252 -- while the gate's output is UTF-8 and, after
    `run_command`'s `errors="replace"`, can carry U+FFFD, which no codepage encodes.
    Before this guard the relay itself raised, so a red gate presented as a CPython
    traceback with the failure it was relaying nowhere in it."""
    monkeypatch.setattr(
        git_policy.framework, "_pre_commit_command", lambda _root, _runner: ["pre-commit-test"]
    )
    responses = _push_responses(tmp_path)
    responses[PUSH_STAGE] = completed(
        ["pre-commit-test"],
        stdout="run-tests.py — 1 failed �\n",
        stderr="lint-all.py → E999 ✓\n",
        returncode=1,
    )
    stdout = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    stderr = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    raw = f"refs/heads/claude/fresh {'1' * 40} refs/heads/claude/fresh {'0' * 40}\n"
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    assert git_policy.main("pre-push", ["origin"], runner=FakeRunner(responses)) == 1
    stdout.flush()
    stderr.flush()
    assert "run-tests.py — 1 failed �" in stdout.buffer.getvalue().decode("utf-8")
    assert "lint-all.py → E999 ✓" in stderr.buffer.getvalue().decode("utf-8")


def test_the_console_guard_tolerates_a_stream_that_cannot_be_reconfigured(monkeypatch):
    """`sys.stdout` is None under `pythonw`, and a harness's capture object need not
    be a `TextIOWrapper`; neither is a reason for the hook to raise."""
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    git_policy.dispatch._utf8_console()


def test_a_deletion_or_tag_only_push_skips_the_pre_push_stage(tmp_path, monkeypatch):
    """The stage is minutes of tests, and nothing a deletion could break is in it."""
    monkeypatch.setattr(
        git_policy.framework, "_pre_commit_command", lambda _root, _runner: ["pre-commit-test"]
    )
    for raw in (
        f"(delete) {'0' * 40} refs/heads/claude/old {'1' * 40}\n",
        tag_push("nightly-2026-09-07"),
    ):
        runner = FakeRunner(_push_responses(tmp_path))
        assert git_policy.run_hook("pre-push", ["origin"], input_text=raw, runner=runner) == 0
        assert PUSH_STAGE not in runner.calls


def test_an_unreadable_push_payload_runs_the_gate_rather_than_standing_down(tmp_path, monkeypatch):
    """An empty payload is not a tag-only push, and used to be treated as one.

    The skip was `all(u.deletion for u in parse_push_updates(raw))`, and `all([])` is
    vacuously true -- so *any* payload with no branch lines skipped the stage. A
    tag-only push has `refs/tags/` lines and no branch ones and is meant to skip; a
    payload that is empty because `dispatch.main` could not read git's stdin
    (`input_text=""` on `OSError`/`ValueError`) is the opposite case, and it turned the
    whole PR gate off for that push with nothing printed.

    Reversion check: restore the bare `all(...)` and this is the test that fails.
    """
    monkeypatch.setattr(
        git_policy.framework, "_pre_commit_command", lambda _root, _runner: ["pre-commit-test"]
    )
    runner = FakeRunner(_push_responses(tmp_path))
    assert git_policy.run_hook("pre-push", ["origin"], input_text="", runner=runner) == 0
    assert PUSH_STAGE in runner.calls, "an unreadable payload must not disable the gate"


def test_the_commit_stage_argv_is_unchanged_by_the_push_stage(tmp_path, monkeypatch):
    """The commit stage keeps pre-commit's staged-diff default: the fixers there act
    on the files being committed, and `--all-files` would rewrite the whole tree."""
    monkeypatch.setattr(
        git_policy.framework, "_pre_commit_command", lambda _root, _runner: ["pre-commit-test"]
    )
    (tmp_path / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")
    runner = FakeRunner(
        {("pre-commit-test", "run", "--hook-stage", "pre-commit"): completed(["pre-commit-test"])}
    )
    assert git_policy.framework._run_pre_commit_framework(tmp_path, runner) == 0
    assert runner.calls == [("pre-commit-test", "run", "--hook-stage", "pre-commit")]


def test_the_framework_runs_with_its_own_directory_first_on_path(tmp_path, monkeypatch):
    """The reported dead end: committing from a plain worktree with no `.venv`.

    `_pre_commit_command` found `pre-commit.exe` in the parent checkout's venv and the
    commit then failed with `Executable detect-secrets-hook not found` -- pre-commit
    resolves a `language: system` hook from the subprocess `PATH`, which did not hold
    the venv it had itself been found in. First, not appended: looking past `PATH` is
    the whole point of `_venv_roots`, so a different copy already on it must not win.
    """
    venv_bin = tmp_path / "checkout" / ".venv" / "Scripts"
    monkeypatch.setattr(
        git_policy.framework,
        "_pre_commit_command",
        lambda _root, _runner: [str(venv_bin / "pre-commit")],
    )
    monkeypatch.setenv("PATH", "/usr/bin")
    (tmp_path / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")
    runner = FakeRunner(
        {
            (str(venv_bin / "pre-commit"), "run", "--hook-stage", "pre-commit"): completed(
                ["pre-commit"]
            )
        }
    )

    assert git_policy.framework._run_pre_commit_framework(tmp_path, runner) == 0
    assert runner.envs[0]["PATH"] == f"{venv_bin}{os.pathsep}/usr/bin"


def test_framework_env_keeps_the_rest_of_the_environment(tmp_path):
    """Only `PATH` moves. The framework needs everything else the session had --
    `GIT_*` scoping from the hook included, since the hooks act on that repository."""
    env = git_policy.framework_env(
        [str(tmp_path / "bin" / "pre-commit")], {"PATH": "/usr/bin", "HOME": "/h"}
    )
    assert env["HOME"] == "/h"
    assert env["PATH"].startswith(str(tmp_path / "bin"))


def test_framework_env_survives_an_environment_with_no_path(tmp_path):
    """No `PATH` at all is not a reason to raise, and an empty first entry would put
    the working directory on it -- which is how a repo file becomes an executable."""
    env = git_policy.framework_env([str(tmp_path / "bin" / "pre-commit")], {})
    assert env["PATH"] == str(tmp_path / "bin")


def test_a_missing_framework_names_every_remedy_not_just_the_refusal(tmp_path, monkeypatch, capsys):
    """Two agents reported this message in one week; both said it names no remedy, so it
    reads as policy declining the commit rather than as a tool being missing."""
    (tmp_path / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")
    monkeypatch.setattr(git_policy.framework, "_pre_commit_command", lambda _root, _runner: None)

    assert git_policy.framework._run_pre_commit_framework(tmp_path, FakeRunner()) == 1
    err = capsys.readouterr().err
    assert "is not installed" in err
    assert "pip install pre-commit" in err
    assert "worktree.py provision" in err
    # The copy at ~/.devkit/git-hooks is what the hooks run, so the fix for this can be
    # committed here and still be missing where it fires. Nothing else answers that.
    assert "install-git-policy.py --check" in err


def test_a_project_with_no_pre_commit_config_says_nothing_at_all(tmp_path, capsys):
    """The framework tier is opt-in: no config file, no message and no refusal."""
    assert git_policy.framework._run_pre_commit_framework(tmp_path, FakeRunner()) == 0
    assert capsys.readouterr().err == ""


def test_the_framework_relays_both_streams(tmp_path, monkeypatch, capsys):
    """The wiring, not just the guard: a stream the framework forgets to relay is the
    gate's verdict arriving with nothing to act on. What keeps the non-ASCII in these
    two from killing the hook is `_utf8_console`, covered end to end above."""
    (tmp_path / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")
    monkeypatch.setattr(
        git_policy.framework, "_pre_commit_command", lambda _root, _runner: ["pre-commit-test"]
    )
    result = completed(["pre-commit-test"])
    result.stdout, result.stderr = "out→\n", "err�\n"
    runner = FakeRunner({("pre-commit-test", "run", "--hook-stage", "pre-commit"): result})

    assert git_policy.framework._run_pre_commit_framework(tmp_path, runner) == 0
    captured = capsys.readouterr()
    assert "out" in captured.out
    assert "err" in captured.err


def test_the_framework_run_is_streamed_not_held_until_it_finishes(tmp_path, monkeypatch):
    """The push stage is minutes of lint and tests, and captured it printed nothing at
    all until the gate was over -- which reads as a hung `git push`, and the answer to a
    hung push is another push, each starting its own full gate on the same machine.

    Reversion check: drop `stream=True` at the call site and this is the test that fails.
    """
    (tmp_path / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")
    monkeypatch.setattr(
        git_policy.framework, "_pre_commit_command", lambda _root, _runner: ["pre-commit-test"]
    )
    publishing = f"refs/heads/claude/fresh {'1' * 40} refs/heads/claude/fresh {'0' * 40}\n"
    for stage, args, raw in (
        ("pre-commit", ("run", "--hook-stage", "pre-commit"), ""),
        ("pre-push", ("run", "--hook-stage", "pre-push", "--all-files"), publishing),
    ):
        runner = FakeRunner({("pre-commit-test", *args): completed(["pre-commit-test"])})
        verdict = git_policy.framework._run_pre_commit_framework(
            tmp_path, runner, stage=stage, raw_updates=raw
        )
        assert verdict == 0, stage
        assert runner.streamed == [True], stage


def test_the_push_stage_says_it_will_be_quiet_before_it_goes_quiet(tmp_path, monkeypatch, capsys):
    """Streaming gets pre-commit's banner out at once, but pre-commit still buffers each
    hook's own output until that hook exits -- so the minutes in between are silent, and
    that silence is what was read as a hang. The notice has to precede the wait, and the
    commit stage must not get it: those hooks are sub-second."""
    (tmp_path / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")
    monkeypatch.setattr(
        git_policy.framework, "_pre_commit_command", lambda _root, _runner: ["pre-commit-test"]
    )
    publishing = f"refs/heads/claude/fresh {'1' * 40} refs/heads/claude/fresh {'0' * 40}\n"
    args = ("run", "--hook-stage", "pre-push", "--all-files")
    runner = FakeRunner({("pre-commit-test", *args): completed(["pre-commit-test"])})

    git_policy.framework._run_pre_commit_framework(
        tmp_path, runner, stage="pre-push", raw_updates=publishing
    )
    out = capsys.readouterr().out
    assert "takes minutes" in out
    assert "SKIP=devkit-push-gate" in out

    commit_args = ("run", "--hook-stage", "pre-commit")
    quiet = FakeRunner({("pre-commit-test", *commit_args): completed(["pre-commit-test"])})
    git_policy.framework._run_pre_commit_framework(tmp_path, quiet)
    assert capsys.readouterr().out == ""


def test_a_streamed_run_leaves_the_childs_output_on_the_inherited_handles(tmp_path, capfd):
    """The point of the flag: the child writes to this process's stdout itself, so there
    is nothing left to return -- and `[str]` stays honest rather than becoming `None`."""
    result = git_policy.run_command(
        [sys.executable, "-c", "import sys; print('live'); print('bad', file=sys.stderr)"],
        cwd=tmp_path,
        stream=True,
    )
    captured = capfd.readouterr()
    assert result.returncode == 0
    assert result.stdout == "" and result.stderr == ""
    assert "live" in captured.out
    assert "bad" in captured.err


def test_a_streamed_run_still_captures_when_there_is_no_stdout_to_lend(tmp_path, monkeypatch):
    """`pythonw.exe` gives a scheduled job `sys.stdout is None`. Passing that through
    would leave the child with nowhere to write, so the flag has to degrade to the
    capture-and-relay path rather than lose the gate's output entirely."""
    monkeypatch.setattr(sys, "stdout", None)
    result = git_policy.run_command(
        [sys.executable, "-c", "print('captured')"], cwd=tmp_path, stream=True
    )
    assert result.returncode == 0
    assert "captured" in result.stdout


def test_a_command_that_does_not_exist_is_still_an_exit_code_when_streaming(tmp_path):
    """`run_command` never raises into git's hook reporting, streaming or not."""
    result = git_policy.run_command(
        ["definitely-not-a-real-command-xyz"], cwd=tmp_path, stream=True
    )
    assert result.returncode == 127
    assert result.stderr


def test_inheritable_streams_rejects_a_stream_with_no_descriptor(monkeypatch):
    """A harness's capture object is not a file and `pythonw` supplies no stdout at all.
    Neither is a reason to raise, and both mean the child cannot be handed the handle."""
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    assert git_policy.inheritable_streams() is False
    monkeypatch.setattr(sys, "stdout", None)
    assert git_policy.inheritable_streams() is False


def _common_dir(main_git: pathlib.Path | None):
    """A runner answering `rev-parse --git-common-dir`, or failing like a non-repository."""

    def runner(argv, *, input_text=None, cwd=None, env=None, stream=False):
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
    assert git_policy.framework._pre_commit_command(worktree, _common_dir(checkout / ".git")) == [
        str(tool)
    ]


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
    assert git_policy.framework._pre_commit_command(box, _common_dir(checkout / ".git")) == [
        str(inner)
    ]


def test_a_directory_git_cannot_answer_for_falls_back_to_itself(tmp_path):
    """`_pre_commit_command` is called with the repo root, so this should not happen --
    and it runs inside a commit hook, where a raise is a commit refused with a traceback
    instead of a reason."""
    assert git_policy.framework._venv_roots(tmp_path, _common_dir(None)) == (tmp_path,)


def test_a_plain_checkout_looks_in_exactly_one_place(tmp_path):
    """`--git-common-dir` in a non-worktree names that checkout's own `.git`, so the
    fallback must collapse rather than listing the same directory twice."""
    assert git_policy.framework._venv_roots(tmp_path, _common_dir(tmp_path / ".git")) == (tmp_path,)


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
        git_policy.framework, "_pre_commit_command", lambda _root, _runner: ["pre-commit-test"]
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

    # `_core` is where the spawn lives, so it is where `subprocess` is looked up.
    monkeypatch.setattr(git_policy._core.subprocess, "run", spy)
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


# --- the types and queries the package split moved -------------------------------
#
# These eight symbols were on `.devkit-untested.txt` against `scripts/git_policy.py`,
# and moving them to `_core` and `branch` made four of them read as *covered* without a
# line of test being written -- the corpus for a module is the test files that name it,
# and the new module names are named by this file for other reasons. Re-keying the
# baseline would have recorded the same debt at a new path; dropping the four would have
# laundered it. Both are worse than the third option, which is what the testing rule
# asks for when a change touches something untested: cover them.


def test_a_decision_is_ok_only_when_it_carries_no_errors():
    """`ok` is what every caller branches on, and warnings must not flip it -- a warning
    is precisely the case the policy decided *not* to block."""
    assert git_policy.Decision().ok
    assert git_policy.Decision(warnings=("slow",)).ok
    assert not git_policy.Decision(errors=("blocked",)).ok
    assert not git_policy.Decision(errors=("blocked",), warnings=("slow",)).ok


def test_a_push_update_reads_both_spellings_of_a_deletion():
    """Git spells a deleted ref two ways and the policy must not block either: a
    deletion has nothing to enforce, and refusing one strands the branch."""
    zero = "0" * 40
    assert git_policy.PushUpdate("(delete)", zero, "refs/heads/x", "abc", "x").deletion
    assert git_policy.PushUpdate("refs/heads/x", zero, "refs/heads/x", "abc", "x").deletion
    assert not git_policy.PushUpdate("refs/heads/x", "abc", "refs/heads/x", zero, "x").deletion


def test_a_tag_update_is_its_own_type_carrying_the_tag():
    """A separate type from `PushUpdate` on purpose -- a branch is looked up on GitHub
    and a tag is matched against a shape, so a field lying about which it holds is how
    the wrong one reaches the wrong check."""
    update = git_policy.TagUpdate("refs/tags/v1.2.3", "abc", "refs/tags/v1.2.3", "0" * 40, "v1.2.3")
    assert update.tag == "v1.2.3"
    assert not update.deletion
    assert not hasattr(update, "branch")


def test_default_branch_prefers_the_remote_head_symbolic_ref():
    runner = FakeRunner(
        {
            ("git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"): completed(
                ["git"], stdout="origin/trunk\n"
            )
        }
    )
    assert git_policy.default_branch(runner, "origin") == "trunk"


def test_default_branch_falls_back_to_main_then_master_then_gives_up():
    """A fresh clone often has no `origin/HEAD`, so the fallbacks are the ordinary path
    rather than the edge case, and "" has to mean "unknown" rather than "main"."""
    for candidate in ("main", "master"):
        runner = FakeRunner(
            {
                ("git", "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{candidate}"): (
                    completed(["git"])
                )
            }
        )
        assert git_policy.default_branch(runner, "origin") == candidate
    assert git_policy.default_branch(FakeRunner(), "origin") == ""


def test_protected_branches_always_holds_main_and_master():
    """`ALWAYS_PROTECTED` is unconditional: a repo whose default branch is something
    else must still not take a commit on `main`."""
    protected = git_policy.protected_branches(FakeRunner(), "origin")
    assert {"main", "master"} <= protected


def test_protected_branches_adds_the_configured_and_the_detected_ones():
    responses = {
        ("git", "config", "--get-all", "devkit.branchPolicy.protectedBranch"): completed(
            ["git"], stdout="release\nstaging\n"
        ),
        ("git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"): completed(
            ["git"], stdout="origin/trunk\n"
        ),
    }
    protected = git_policy.protected_branches(FakeRunner(responses), "origin")
    assert {"main", "master", "release", "staging", "trunk"} == set(protected)


def test_parse_tag_updates_reads_tag_lines_and_ignores_branch_lines():
    """These were parsed by nothing at all until a hand-pushed v0.9.0 got through: the
    branch parser drops every `refs/tags/` line, so a tag-only push read as empty."""
    raw = (
        f"refs/tags/v1.0.0 abc refs/tags/v1.0.0 {'0' * 40}\n"
        "refs/heads/work def refs/heads/work ghi\n"
        "malformed line\n"
    )
    updates = git_policy.parse_tag_updates(raw)
    assert [u.tag for u in updates] == ["v1.0.0"]
    assert [u.branch for u in git_policy.parse_push_updates(raw)] == ["work"]


def test_release_tag_decision_blocks_a_release_tag_and_allows_everything_else():
    """Pure -- no git, no network -- so it holds in a repo with no remote at all."""
    blocked = git_policy.release_tag_decision(f"refs/tags/v1.2.3 abc refs/tags/v1.2.3 {'0' * 40}\n")
    assert not blocked.ok
    assert "v1.2.3" in blocked.errors[0]

    zero = "0" * 40
    # A deletion, and a tag that is not a release tag, are both somebody else's business.
    assert git_policy.release_tag_decision(f"(delete) {zero} refs/tags/v1.2.3 abc\n").ok
    assert git_policy.release_tag_decision("refs/tags/nightly abc refs/tags/nightly def\n").ok
    assert git_policy.release_tag_decision("").ok


def test_merged_pr_reports_the_url_and_survives_a_broken_answer():
    """The direct test for the query `_merged_decision` turns into a refusal. Both
    failure shapes matter: a transport error must read as *unknown* rather than as
    "not merged", or `failClosed` has nothing to fail closed on."""
    argv = next(iter(merged_response("topic", [])))
    found = git_policy.merged_pr(
        FakeRunner({argv: completed(list(argv), stdout='[{"url": "https://x/pull/9"}]')}),
        "acme/widgets",
        "topic",
    )
    assert found.url == "https://x/pull/9"
    assert not found.error

    none = git_policy.merged_pr(
        FakeRunner({argv: completed(list(argv), stdout="[]")}), "acme/widgets", "topic"
    )
    assert none.url == "" and not none.error

    # Both APIs failing is the only case that may report an error.
    broken = git_policy.merged_pr(FakeRunner(), "acme/widgets", "topic")
    assert broken.error and not broken.url


# --- relaying output to a console that cannot encode it ----------------------------


class NarrowStream(io.StringIO):
    """A `cp1252` console. `io.StringIO` accepts anything, so the codepage is modelled
    rather than inherited -- what the real `TextIOWrapper` does is encode on write and
    raise before emitting a byte, which is what makes the retry in `emit` safe."""

    encoding = "cp1252"

    def write(self, text: str) -> int:
        text.encode(self.encoding)  # raises UnicodeEncodeError, exactly as a console does
        return super().write(text)


def test_emit_survives_a_console_that_cannot_encode_the_replacement_character():
    """The regression. `run_command` decodes with `errors="replace"` on purpose, so any
    output this package relays can carry U+FFFD; printing that to a `cp1252` stdout
    raised `UnicodeEncodeError` *inside the hook*, which git reports as a failed hook.
    One replacement character in `pre-commit`'s output blocked every push on the
    machine, and the traceback named the encoder rather than anything the user did."""
    stream = NarrowStream()
    git_policy.emit("ruff: ok \ufffd done", stream=stream, end="")
    written = stream.getvalue()
    assert "ruff: ok" in written and "done" in written
    assert "\ufffd" not in written  # degraded, not lost


def test_emit_does_not_degrade_output_a_console_can_encode():
    """Lenient on the failing path only. A UTF-8 console gets the text exactly; a blanket
    re-encode would make the common case lossy to protect a case it is not in."""
    stream = io.StringIO()  # encodes anything, like a UTF-8 console
    git_policy.emit("caf\u00e9 \ufffd", stream=stream, end="")
    assert stream.getvalue() == "caf\u00e9 \ufffd"


def test_emit_writes_each_payload_once_when_the_retry_runs():
    """`TextIOWrapper.write` encodes the whole string before writing any of it, so the
    failing attempt emits nothing. If that ever stopped holding, the fallback would
    double every line it rescued."""
    stream = NarrowStream()
    git_policy.emit("\ufffd", stream=stream, end="")
    assert len(stream.getvalue()) == 1


def test_the_dispatcher_relays_hook_output_through_emit():
    """The end of the wire. `emit` existing is worth nothing if the sites that relay a
    subprocess's captured output still call `print`."""
    source = (support.REPO_ROOT / "scripts" / "git_policy" / "dispatch.py").read_text(
        encoding="utf-8"
    )
    framework = (support.REPO_ROOT / "scripts" / "git_policy" / "framework.py").read_text(
        encoding="utf-8"
    )
    for name, text in (("dispatch.py", source), ("framework.py", framework)):
        assert "print(" not in text, (
            f"{name} calls print(); this package relays strings decoded with "
            'errors="replace" and must go through emit() so a narrow console cannot '
            "turn a hook's output into a failed hook"
        )
