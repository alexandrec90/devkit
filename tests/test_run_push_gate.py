"""Tests for `scripts/precommit/run_push_gate.py`, the `devkit-push-gate` hook.

The hook is part of the PR gate run locally at the pre-push stage, so what it has to get
right is ordering, stopping, and skipping: lint before anything it could rewrite, the
first failure ending the run with its exit code, and a project that lacks a step's file
told so rather than failed. The steps are exercised through an injected runner where the
shape is the point, and once as a real subprocess where the point is that pre-commit could
run it.

Whether those steps still cover the gate they were copied from is a different question,
and a hardcoded list here cannot answer it: `tests/test_gate_parity.py` reads
`.github/workflows/pr-gate.yml` and holds every difference to a written reason.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from support import REPO_ROOT, load_script

gate = load_script("scripts/precommit/run_push_gate.py")


def completed(argv, returncode=0):
    return subprocess.CompletedProcess(argv, returncode, stdout="", stderr="")


class FakeRunner:
    """Records every command and answers each step's exit code by its first argument."""

    def __init__(self, exits=None):
        self.exits = exits or {}
        self.calls: list[list[str]] = []
        # `None` is recorded as itself, not flattened to `{}`: a step spawned with no
        # `env` inherits git's variables, which is the whole defect, and an empty dict
        # would let the regression test below pass against it.
        self.envs: list[dict[str, str] | None] = []

    def __call__(self, argv, *, cwd, check, env=None):
        self.calls.append(list(argv))
        self.envs.append(None if env is None else dict(env))
        return completed(argv, self.exits.get(argv[1], 0))


def project(root: Path, *files: str) -> Path:
    for rel in files:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    return root


def test_lint_runs_first_because_it_auto_fixes():
    """Lint first, then the suite, then the vendored tier, then the POSIX rehearsal.

    Two positions are load-bearing and the rest is free. `lint-all.py` rewrites files, so
    a step that ran before it would report clean on what it repaired. The rehearsal runs
    *after* the suite because it is the same tests under a faked platform: a real failure
    reported as a platform assumption is the more expensive of the two orders to read.
    The two test tiers between them are free -- this used to claim their order was
    `.github/workflows/pr-gate.yml`'s and was simply wrong (CI runs the vendored tier
    first), which nothing caught because the literals below are hardcoded and no test here
    opens the workflow. `tests/test_gate_parity.py` is the one that does; the shape check
    stays here.
    """
    assert all(isinstance(step, gate.Step) for step in gate.STEPS)
    assert [step.name for step in gate.STEPS] == [
        "lint",
        "tests",
        "hook tests",
        "posix rehearsal",
    ]
    assert gate.STEPS[0].argv == ("scripts/lint-all.py",)
    assert gate.STEPS[1].argv == ("scripts/run-tests.py",)
    assert "pytest" in gate.STEPS[2].argv and "scripts/hooks/tests/" in gate.STEPS[2].argv
    assert gate.STEPS[3].argv == ("scripts/posix-rehearsal.py",)


def test_the_rehearsal_is_skipped_by_a_project_that_does_not_ship_it(tmp_path):
    """It is devkit-only: not in `sync-devkit.py`'s MANIFEST, so a consuming project has
    no such file and must not have its push refused over one."""
    root = project(tmp_path, "scripts/lint-all.py", "scripts/run-tests.py")
    planned = {step.name: command for step, command in gate.plan(root)}
    assert planned["posix rehearsal"] is None


def test_every_step_runs_when_the_project_has_every_file(tmp_path):
    root = project(
        tmp_path,
        "scripts/lint-all.py",
        "scripts/run-tests.py",
        "scripts/hooks/tests/test_x.py",
        "scripts/posix-rehearsal.py",
    )
    runner = FakeRunner()
    assert gate.run_gate(root, runner) == 0
    assert [call[1:] for call in runner.calls] == [list(step.argv) for step in gate.STEPS]


def test_the_first_failure_ends_the_run_with_its_exit_code(tmp_path, capsys):
    """Stopping is what keeps the artifact honest: `run-tests.py` clears
    `logs/test-failures.log` on a pass, so running the hook tests after a failed suite
    would overwrite the one file the refusal points at."""
    root = project(
        tmp_path, "scripts/lint-all.py", "scripts/run-tests.py", "scripts/hooks/tests/test_x.py"
    )
    runner = FakeRunner({"scripts/run-tests.py": 3})
    assert gate.run_gate(root, runner) == 3
    assert [call[1] for call in runner.calls] == ["scripts/lint-all.py", "scripts/run-tests.py"]
    out = capsys.readouterr().out
    assert "tests failed (exit 3)" in out
    # The bypass is named, so a deliberate WIP push does not reach for --no-verify.
    assert f"SKIP={gate.HOOK_ID}" in out


def test_a_project_without_a_wrapper_skips_that_step_out_loud(tmp_path, capsys):
    """The wrappers are project-owned and a missing one is a documented skip, never a
    refused push -- but a silent skip would be the inert gate engineering.md forbids."""
    root = project(tmp_path, "scripts/lint-all.py")
    runner = FakeRunner()
    assert gate.run_gate(root, runner) == 0
    assert [call[1] for call in runner.calls] == ["scripts/lint-all.py"]
    out = capsys.readouterr().out
    assert "no scripts/run-tests.py in this project -- skipped" in out
    assert "no scripts/hooks/tests in this project -- skipped" in out
    assert "no scripts/posix-rehearsal.py in this project -- skipped" in out


def test_plan_pairs_each_step_with_a_command_or_none(tmp_path):
    root = project(tmp_path, "scripts/run-tests.py")
    planned = dict((step.name, command) for step, command in gate.plan(root))
    assert planned["lint"] is None
    assert planned["hook tests"] is None
    assert planned["tests"][1:] == ["scripts/run-tests.py"]


def test_the_interpreter_is_this_one_when_the_project_ships_no_helper(tmp_path):
    assert gate.interpreter(tmp_path) == sys.executable


def test_the_interpreter_comes_from_the_projects_own_helper(tmp_path):
    """A push from an agent's shell is never from an activated venv; `project_python`
    is what finds the venv's pytest, so the hook has to ask it rather than PATH."""
    helper = tmp_path / "scripts" / "project_python.py"
    helper.parent.mkdir()
    helper.write_text(
        "def interpreter(root, module=''):\n    return f'venv-python-for-{module}'\n",
        encoding="utf-8",
    )
    assert gate.interpreter(tmp_path) == "venv-python-for-pytest"


def test_an_unparsable_helper_falls_back_rather_than_refusing_every_push(tmp_path):
    """A half-written helper answers nothing about the interpreter, so the hook uses
    the fallback the helper's own callers use. Only the load-time failures are caught:
    a helper that raises on its own logic is a defect the traceback should show."""
    helper = tmp_path / "scripts" / "project_python.py"
    helper.parent.mkdir()
    helper.write_text("def interpreter(\n", encoding="utf-8")
    assert gate.interpreter(tmp_path) == sys.executable


def test_run_as_pre_commit_would_the_hook_exits_with_the_failing_step(tmp_path):
    """End to end, as a subprocess with the project as cwd -- which is how pre-commit
    invokes a `language: script` hook -- against stub wrappers that exit as told."""
    root = tmp_path / "consumer"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "lint-all.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    (root / "scripts" / "run-tests.py").write_text("raise SystemExit(4)\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(gate.__file__)],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 4, result.stdout + result.stderr
    assert "push-gate: tests failed (exit 4)" in result.stdout
    assert gate.main.__name__ == "main"


# --- git's own variables never reach a step -----------------------------------


def test_gate_env_drops_the_variables_git_exports_into_a_hook():
    """Each of these overrides a subprocess's `cwd=`, so a fixture building a throwaway
    repo would commit into the repository being pushed instead.

    Driven off the list so the assertion is behavioural -- every name it carries is
    really popped, rather than the first three being popped and the rest decorative.
    What stops the list itself shrinking is the pin below, which this cannot do: a
    name deleted from `LEAKED_GIT_VARS` also disappears from `dirty` here.
    """
    dirty = {name: f"/repo/{name}" for name in gate.LEAKED_GIT_VARS}
    assert gate.gate_env({"PATH": "/usr/bin", **dirty}) == {"PATH": "/usr/bin"}


def test_the_scrub_list_is_pinned_to_every_name_that_redirects_a_repository():
    """The reversion check for the list itself. The first three are the ones that
    actually rewrote a branch; the rest reach the same resolution by another route --
    the object store, the index format, the ceiling git stops searching at -- and a
    list that quietly lost one would still look like a scrub and stop nothing.

    A literal, not a property: this is the one place a deletion has to fail.
    """
    assert gate.LEAKED_GIT_VARS == (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_CEILING_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_PREFIX",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_QUARANTINE_PATH",
        "GIT_INDEX_VERSION",
        "GIT_NAMESPACE",
        "GIT_CONFIG",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
        "GIT_IMPLICIT_WORK_TREE",
        "GIT_GRAFT_FILE",
        "GIT_NO_REPLACE_OBJECTS",
        "GIT_REPLACE_REF_BASE",
        "GIT_SHALLOW_FILE",
    )


def test_the_scrub_covers_every_name_git_calls_repo_local():
    """The pin above says the list may not shrink; this says it may not fall behind git.
    `git rev-parse --local-env-vars` is the authority on which variables make a git
    command answer for a repository other than its cwd's, and the gate spells that list
    out so it stays stdlib-only -- a copy that cannot rot is the only kind worth having.

    A subset, not an equality: `GIT_CEILING_DIRECTORIES`, `GIT_QUARANTINE_PATH` and
    `GIT_INDEX_VERSION` are scrubbed too and git does not call them repo-local.

    No guard for a machine without git: every fixture in this suite already spawns it,
    so a skip here would only hide the one failure the test is for.
    """
    listed = subprocess.run(
        ["git", "rev-parse", "--local-env-vars"], capture_output=True, text=True, check=True
    ).stdout.split()
    missing = sorted(set(listed) - set(gate.LEAKED_GIT_VARS))
    assert not missing, f"git calls these repo-local and the gate does not scrub them: {missing}"


def test_the_suites_own_bootstrap_drops_the_same_variables():
    """Two layers, because the gate is not the only thing that can run this suite from
    inside a git hook. `tests/support.py` clears the same set at import; if the two lists
    diverge, the one nobody is looking at is the one that lets a fixture write to the
    real repository."""
    bootstrap = (REPO_ROOT / "tests" / "support.py").read_text(encoding="utf-8")
    for name in gate.LEAKED_GIT_VARS:
        assert f'"{name}"' in bootstrap, f"tests/support.py does not clear {name}"


def test_a_terminal_push_with_none_of_them_set_is_unchanged():
    """`git push` from a shell exports none of these; the scrub must be a no-op there
    rather than something a developer can tell happened."""
    assert gate.gate_env({"PATH": "/usr/bin"}) == {"PATH": "/usr/bin"}


def test_gate_env_keeps_everything_else_including_other_git_variables():
    """Only the four that redirect a repository are dropped. `GIT_CONFIG_GLOBAL` and the
    author identity are inherited on purpose -- a step that runs git legitimately still
    needs a usable configuration."""
    env = gate.gate_env({"GIT_CONFIG_GLOBAL": "/gc", "GIT_AUTHOR_NAME": "t", "HOME": "/h"})
    assert env == {"GIT_CONFIG_GLOBAL": "/gc", "GIT_AUTHOR_NAME": "t", "HOME": "/h"}


def test_gate_env_reads_the_real_environment_by_default(monkeypatch):
    monkeypatch.setenv("GIT_DIR", "/repo/.git")
    monkeypatch.setenv("DEVKIT_MARKER", "kept")
    env = gate.gate_env()
    assert "GIT_DIR" not in env
    assert env["DEVKIT_MARKER"] == "kept"


def test_every_step_is_spawned_with_the_scrubbed_environment(tmp_path, monkeypatch):
    """The regression: a push ran the suite with `GIT_DIR` still set, and the fixtures
    under it rewrote the branch being pushed. Asserted per step, not once -- the scrub is
    only worth having if no step is spawned without it."""
    monkeypatch.setenv("GIT_DIR", str(tmp_path / ".git"))
    root = project(tmp_path, "scripts/lint-all.py", "scripts/run-tests.py", "scripts/hooks/tests")
    runner = FakeRunner()
    assert gate.run_gate(root, runner) == 0
    assert len(runner.envs) == len(runner.calls) == 3
    for step, env in zip(runner.calls, runner.envs, strict=True):
        assert env is not None, f"{step[1]} inherits the hook's environment"
        for name in gate.LEAKED_GIT_VARS:
            assert name not in env, f"{step[1]} runs with {name} set, pointing it at the wrong repo"


# --- a release prepare is the one push this gate must not judge ---------------


def test_a_release_branch_is_named_to_the_steps_it_spawns(tmp_path):
    """The collision this exists for: the release bump is red by construction until the
    tag exists, CI is built to merge that one red anyway, and this gate has no PR to read
    that verdict from. It reports the state and lets the test excuse itself."""
    root = project(tmp_path, "scripts/lint-all.py", "scripts/run-tests.py")
    runner = FakeRunner()
    assert gate.run_gate(root, runner, release_branch="release/v1.2.3") == 0
    assert runner.envs
    for env in runner.envs:
        assert env[gate.RELEASE_PREPARE_ENV] == "release/v1.2.3"


def test_an_ordinary_push_carries_no_release_marker(tmp_path):
    """The exemption may not leak to a branch that merely fails the same test."""
    root = project(tmp_path, "scripts/lint-all.py", "scripts/run-tests.py")
    runner = FakeRunner()
    assert gate.run_gate(root, runner, release_branch="") == 0
    assert runner.envs
    for env in runner.envs:
        assert gate.RELEASE_PREPARE_ENV not in env


def test_an_inherited_marker_does_not_survive_into_an_ordinary_push(tmp_path, monkeypatch):
    """The regression, and it stopped a release rather than merely being untidy.

    On a release branch the gate spawns the suite *with* the marker set, and devkit's own
    suite runs `run_gate`. So the nested run inherited a marker its own `release_branch`
    never asked for, the test above failed, `run-tests.py` went red, and the push it was
    gating -- the release push -- was refused. The first release this whole exemption
    exists to allow was the one it blocked.

    Read the other way round it is the safety property: a marker in the pushing shell
    must not excuse a guard on a branch the gate did not judge to be a release.
    """
    monkeypatch.setenv(gate.RELEASE_PREPARE_ENV, "release/v9.9.9")
    root = project(tmp_path, "scripts/lint-all.py", "scripts/run-tests.py")
    runner = FakeRunner()

    assert gate.run_gate(root, runner, release_branch="") == 0
    assert runner.envs
    for env in runner.envs:
        assert gate.RELEASE_PREPARE_ENV not in env


def test_the_gates_own_answer_still_wins_over_an_inherited_one(tmp_path, monkeypatch):
    """Scrubbed first, then set from this run's branch -- so the marker always names the
    branch being pushed rather than whatever an outer run was cutting."""
    monkeypatch.setenv(gate.RELEASE_PREPARE_ENV, "release/v9.9.9")
    root = project(tmp_path, "scripts/lint-all.py", "scripts/run-tests.py")
    runner = FakeRunner()

    assert gate.run_gate(root, runner, release_branch="release/v1.2.3") == 0
    for env in runner.envs:
        assert env[gate.RELEASE_PREPARE_ENV] == "release/v1.2.3"


def test_only_a_release_version_branch_counts():
    """`release/v1.2.3` and nothing that merely starts like it -- the marker turns off a
    guard, so the shape that turns it on is exact."""
    matches = gate.RELEASE_BRANCH_RE.fullmatch
    assert matches("release/v0.11.17")
    assert not matches("release/v1.2")
    assert not matches("release/v1.2.3-rc1")
    assert not matches("release/candidate")
    assert not matches("feature/release/v1.2.3")


def test_the_branch_is_read_from_head_of_the_pushed_repo(tmp_path):
    """Read with `cwd` at the repo being pushed, which is the only thing that answers for
    a throwaway worktree the release pipeline cut moments earlier."""
    root = tmp_path
    subprocess.run(["git", "init", "--quiet", str(root)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "checkout", "--quiet", "-b", "release/v9.9.9"],
        check=True,
        capture_output=True,
    )

    assert gate.detect_release_branch(root) == "release/v9.9.9"


def test_a_directory_that_is_not_a_checkout_is_not_a_release(tmp_path):
    """`detect_release_branch` runs before any step and must not be the thing that fails
    a push in a project git cannot answer for."""
    assert gate.detect_release_branch(tmp_path) == ""


def test_a_detached_head_is_not_a_release_branch(tmp_path):
    """Every CI runner checks out detached, and `rev-parse --abbrev-ref` answers the
    literal `HEAD` there -- which is why the question is put to `symbolic-ref`. If this
    ever answered a branch, CI would excuse the red it exists to catch."""
    root = tmp_path
    subprocess.run(["git", "init", "--quiet", str(root)], check=True, capture_output=True)
    for args in (
        ["config", "user.email", "t@example.com"],
        ["config", "user.name", "t"],
        ["commit", "--quiet", "--allow-empty", "-m", "seed"],
    ):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "checkout", "--quiet", "--detach"], check=True, capture_output=True
    )

    assert gate.detect_release_branch(root) == ""
