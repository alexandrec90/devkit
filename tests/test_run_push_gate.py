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

from support import load_script

gate = load_script("scripts/precommit/run_push_gate.py")


def completed(argv, returncode=0):
    return subprocess.CompletedProcess(argv, returncode, stdout="", stderr="")


class FakeRunner:
    """Records every command and answers each step's exit code by its first argument."""

    def __init__(self, exits=None):
        self.exits = exits or {}
        self.calls: list[list[str]] = []
        self.envs: list[dict[str, str] | None] = []

    def __call__(self, argv, *, cwd, check, env=None):
        self.calls.append(list(argv))
        self.envs.append(env)
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


def test_gits_hook_redirects_are_stripped_from_every_step(tmp_path):
    """The regression, and it cost a recovery: git exports `GIT_DIR` and friends into
    every hook it runs, and each takes precedence over a child's working directory when
    git resolves which repository it is in.

    Inherited, they reach every `git` the gate's own test suites spawn -- so a test that
    seeds a throwaway repo and passes `cwd=<tmp>` writes its commits into the repository
    being pushed. The first push through this gate left this worktree on a detached
    `seed` commit with the task branch reset to a fixture's `c1`.
    """
    root = project(tmp_path, "scripts/lint-all.py", "scripts/run-tests.py")
    runner = FakeRunner()
    gate.run_gate(root, runner)
    for env in runner.envs:
        assert env is not None, "a step ran with the inherited environment"
        for name in gate.GIT_REDIRECTS:
            assert name not in env, f"{name} reached a step and points it at the wrong repo"


def test_the_rest_of_the_environment_is_handed_through(tmp_path):
    """Stripping is surgical: PATH, the venv and anything the project's wrappers read
    have to survive, or the gate cannot run the tools it exists to run."""
    env = gate.environment({"PATH": "/usr/bin", "VIRTUAL_ENV": "/v", "GIT_DIR": "/x/.git"})
    assert env == {"PATH": "/usr/bin", "VIRTUAL_ENV": "/v"}


def test_a_terminal_push_with_no_redirects_set_is_unchanged(tmp_path):
    """`git push` from a shell exports none of these; the scrub must be a no-op there
    rather than something a developer can tell happened."""
    assert gate.environment({"PATH": "/usr/bin"}) == {"PATH": "/usr/bin"}


def test_the_redirect_list_names_the_one_that_actually_bit():
    """A list that lost `GIT_DIR` would still look like a scrub and stop nothing."""
    assert "GIT_DIR" in gate.GIT_REDIRECTS
    assert "GIT_WORK_TREE" in gate.GIT_REDIRECTS
    assert "GIT_INDEX_FILE" in gate.GIT_REDIRECTS


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
