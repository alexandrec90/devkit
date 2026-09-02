"""`scripts/precommit/run_ruff.py` — the pre-commit entry point for ruff.

The failure it repairs is one a test cannot reach by importing anything: `git commit`
from an agent's shell printed ``Executable `ruff` not found`` and refused the commit,
because the hook's entry was a bare `ruff` resolved against `PATH` and an agent's shell
is never an activated one. So the assertions here are about the *spawn* — which
interpreter, which module, which arguments survive — plus the two degradations that must
not become a crash inside a commit hook.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from support import REPO_ROOT, load_script

run_ruff = load_script("scripts/precommit/run_ruff.py")


def test_it_spawns_ruff_through_an_interpreter_not_through_path():
    """The whole point. A bare `ruff` is what failed; `-m ruff` reaches the same
    executable through an interpreter this repo can name."""
    cmd = run_ruff.ruff_command(["check", "--fix"])
    assert cmd[1:] == ["-m", "ruff", "check", "--fix"]
    assert Path(cmd[0]).exists(), cmd[0]


def test_the_arguments_pre_commit_appends_are_passed_through():
    """pre-commit appends the staged filenames after the configured entry, so anything
    dropped here is a file that silently goes unlinted."""
    cmd = run_ruff.ruff_command(["format", "--force-exclude", "a.py", "b.py"])
    assert cmd[-4:] == ["format", "--force-exclude", "a.py", "b.py"]


def test_without_the_helper_it_falls_back_to_a_bare_ruff(tmp_path):
    """Reproduces the OLD behaviour rather than inventing a new one: if this file is
    ever shipped without `project_python.py`, a PATH lookup still beats refusing to run.

    A subprocess rather than an in-process call, and that is the point: the suite has
    already imported `project_python` for other modules, so `sys.modules` holds it and a
    local import here would succeed no matter what is on disk. Only a fresh interpreter
    with an empty tree actually takes the branch.
    """
    (tmp_path / "scripts" / "precommit").mkdir(parents=True)
    copy = tmp_path / "scripts" / "precommit" / "run_ruff.py"
    copy.write_text(Path(run_ruff.__file__).read_text(encoding="utf-8"), encoding="utf-8")
    probe = (
        "import importlib.util, sys;"
        f"spec = importlib.util.spec_from_file_location('rr', r'{copy}');"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m);"
        "print(m.ruff_command(['check'])[0])"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, cwd=tmp_path
    )
    assert result.stdout.strip() == "ruff", result.stdout + result.stderr


def test_a_ruff_that_cannot_start_exits_one_with_a_readable_line(monkeypatch, capsys):
    """The one case the old message was right about. It must stay a message and an exit
    code -- an exception here is a traceback in the middle of `git commit`."""

    def explode(*_a, **_kw):
        raise OSError("no such file")

    monkeypatch.setattr(run_ruff.subprocess, "run", explode)
    assert run_ruff.main(["check"]) == 1
    assert "pyproject.toml" in capsys.readouterr().err


def test_it_hands_ruffs_exit_code_up_unchanged(monkeypatch):
    """A findings exit code that became 0 here would make the gate pass on a dirty
    tree."""
    monkeypatch.setattr(
        run_ruff.subprocess, "run", lambda cmd, **_kw: subprocess.CompletedProcess(cmd, 1)
    )
    assert run_ruff.main(["check"]) == 1


def test_the_hook_runs_against_this_repo_for_real():
    """End-to-end, because every assertion above is about an argv. This is the one that
    would have caught the original defect: it invokes the file exactly as pre-commit
    does, with the interpreter that has no ruff on its PATH."""
    result = subprocess.run(
        [sys.executable, "scripts/precommit/run_ruff.py", "--version"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "not found" not in (result.stdout + result.stderr).lower()
