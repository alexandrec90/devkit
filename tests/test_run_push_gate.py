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
        self.envs: list[dict] = []

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
    """Lint first, then the suite, then the vendored tier.

    Only the first position is load-bearing: `lint-all.py` rewrites files, so a step that
    ran before it would report clean on what it repaired. The two test tiers' order is
    free -- this used to claim it was `.github/workflows/pr-gate.yml`'s order and was
    simply wrong (CI runs the vendored tier first), which nothing caught because the
    literals below are hardcoded and no test here opens the workflow.
    `tests/test_gate_parity.py` is the one that does; the shape check stays here.
    """
    assert all(isinstance(step, gate.Step) for step in gate.STEPS)
    assert [step.name for step in gate.STEPS] == ["lint", "tests", "hook tests"]
    assert gate.STEPS[0].argv == ("scripts/lint-all.py",)
    assert gate.STEPS[1].argv == ("scripts/run-tests.py",)
    assert "pytest" in gate.STEPS[2].argv and "scripts/hooks/tests/" in gate.STEPS[2].argv


def test_every_step_runs_when_the_project_has_every_file(tmp_path):
    root = project(
        tmp_path, "scripts/lint-all.py", "scripts/run-tests.py", "scripts/hooks/tests/test_x.py"
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


def test_the_steps_do_not_inherit_the_pushs_git_scoping(tmp_path, monkeypatch):
    """The regression, and it cost a whole push: a hook inherits `GIT_DIR` and
    `GIT_INDEX_FILE`, every test that builds a repo in `tmp_path` and shells out to
    `git -C <tmp>` then acted on the *pushing* repo, and 96 tests across five modules
    failed inside the gate while passing in any terminal."""
    monkeypatch.setenv("GIT_DIR", str(tmp_path / ".git"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / ".git" / "index"))
    root = project(tmp_path, "scripts/lint-all.py", "scripts/run-tests.py")
    runner = FakeRunner()
    gate.run_gate(root, runner)
    assert runner.envs, "no step ran"
    for env in runner.envs:
        assert "GIT_DIR" not in env
        assert "GIT_INDEX_FILE" not in env


def test_the_suites_own_bootstrap_drops_the_same_variables():
    """Two layers, because the gate is not the only thing that can run this suite from
    inside a git hook. `tests/support.py` clears the same set at import; if the two lists
    diverge, the one nobody is looking at is the one that lets a fixture write to the
    real repository."""
    bootstrap = (REPO_ROOT / "tests" / "support.py").read_text(encoding="utf-8")
    for name in gate.GIT_SCOPING_VARS:
        assert f'"{name}"' in bootstrap, f"tests/support.py does not clear {name}"


def test_a_machines_own_git_configuration_survives(monkeypatch):
    """Scoping is dropped by name rather than by sweeping `GIT_*`: a gate that dropped
    `GIT_SSH_COMMAND` would break the tests that shell out to git properly."""
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i /keys/id")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/etc/gitconfig")
    monkeypatch.setenv("GIT_DIR", "/repo/.git")
    env = gate.gate_env()
    assert env["GIT_SSH_COMMAND"] == "ssh -i /keys/id"
    assert env["GIT_CONFIG_GLOBAL"] == "/etc/gitconfig"
    assert "GIT_DIR" not in env


def test_gate_env_leaves_the_rest_of_the_environment_alone():
    """It is the developer's shell; the gate narrows it, it does not replace it."""
    before = {"PATH": "/usr/bin", "VIRTUAL_ENV": "/venv", "GIT_PREFIX": "sub/"}
    assert gate.gate_env(before) == {"PATH": "/usr/bin", "VIRTUAL_ENV": "/venv"}


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
