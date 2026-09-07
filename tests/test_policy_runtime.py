"""Tests for `scripts/policy_runtime.py`, the upgrade run's git-policy reinstall rider.

Two guards are the whole of it: the installer is only run when its own `--check` says
the copy is stale, and only ever from the release tag. Each is pinned in both
directions here, because the failure modes are opposite -- claiming a machine that never
opted in, and installing a working tree as policy -- and neither shows up anywhere.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from support import load_script

pr = load_script("scripts/policy_runtime.py")


def completed(argv, code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(list(argv), code, stdout, stderr)


class FakeRunner:
    """Answers `--check` with `check_code` and `--yes` with `install_code`; records both."""

    def __init__(self, check_code, install_code=0, install_stderr=""):
        self.check_code = check_code
        self.install_code = install_code
        self.install_stderr = install_stderr
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        if "--check" in argv:
            return completed(argv, self.check_code, stderr="drifted\n" if self.check_code else "")
        return completed(argv, self.install_code, stderr=self.install_stderr)


def outcome(name, code, detail):
    return (name, code, detail)


def refresh(runner, *, dry_run=False, every=True, tag="v1.2.3"):
    return pr.refresh(Path("D:/devkit"), tag, dry_run, every, outcome, runner)


def test_a_stale_runtime_is_reinstalled_from_the_tag_and_only_the_tag():
    """The one path that writes. `--ref <tag>` and nothing about the working tree:
    the escape hatch that once put uncommitted policy on a machine is never spelled."""
    runner = FakeRunner(pr.CHECK_STALE)
    assert refresh(runner) == []
    assert [call[2:] for call in runner.calls] == [["--check"], ["--yes", "--ref", "v1.2.3"]]
    assert not any("--from-worktree" in call for call in runner.calls)


def test_a_current_runtime_is_not_touched(capsys):
    runner = FakeRunner(pr.CHECK_CURRENT)
    assert refresh(runner) == []
    assert [call[2:] for call in runner.calls] == [["--check"]]
    assert "is current" in capsys.readouterr().out


def test_a_machine_without_the_policy_is_left_alone(capsys):
    """Exit 2 is "nothing installed here" or "a hooks path this does not own". Either
    way installing would claim a machine that never opted in, which is the one outcome
    worse than a stale copy on one that did."""
    runner = FakeRunner(pr.CHECK_ABSENT)
    assert refresh(runner) == []
    assert [call[2:] for call in runner.calls] == [["--check"]]
    assert "left alone" in capsys.readouterr().out


def test_a_dry_run_says_what_it_would_do_and_installs_nothing(capsys):
    runner = FakeRunner(pr.CHECK_STALE)
    assert refresh(runner, dry_run=True) == []
    assert [call[2:] for call in runner.calls] == [["--check"]]
    assert "would reinstall" in capsys.readouterr().out


def test_a_run_naming_one_project_never_asks_about_the_machine():
    """The rider belongs to the unattended `--all` pass; a named-project run is about
    that project, and must not spawn the installer as a side effect."""
    runner = FakeRunner(pr.CHECK_STALE)
    assert refresh(runner, every=False) == []
    assert runner.calls == []


def test_a_failed_install_is_a_run_level_outcome_with_the_remedy():
    """Under `pythonw` stderr goes nowhere, so the artifact and the exit code are the
    only record. Code 2, like every other failure the run cannot fix itself."""
    runner = FakeRunner(pr.CHECK_STALE, install_code=2, install_stderr="REFUSED -- no\n")
    outcomes = refresh(runner)
    assert len(outcomes) == 1
    name, code, detail = outcomes[0]
    assert name == pr.POLICY_SCOPED
    assert code == 2
    assert "REFUSED -- no" in detail
    assert "install-git-policy.py --yes" in detail


def test_the_argv_runs_the_installer_in_the_named_devkit_checkout():
    """`--devkit` is the checkout the run pulls from; the installer beside it reads the
    same tags, so the two cannot disagree about what "the release" is."""
    devkit = Path("D:/devkit")
    console = pr.git_policy.console_python()
    assert console == sys.executable  # a console interpreter is its own console twin
    assert pr.check_argv(devkit) == [console, str(devkit / pr.INSTALLER), "--check"]
    assert pr.install_argv(devkit, "v2.0.0")[-2:] == ["--ref", "v2.0.0"]


def test_the_installer_is_spawned_by_a_console_interpreter_under_pythonw(monkeypatch, tmp_path):
    """The installer runs `git` several times, and a console-less parent hands each one
    a visible console window. `console_python()` is the twin that does not.

    A real directory holding both spellings, rather than a literal Windows path: the
    twin is resolved through `Path`, whose separators are the *running* platform's, so
    a hardcoded one is a single filename off Windows and the branch under test never
    runs there -- which is every CI machine this suite has.
    """
    console = tmp_path / "python.exe"
    console.write_text("", encoding="utf-8")
    monkeypatch.setattr(pr.git_policy.sys, "executable", str(tmp_path / "pythonw.exe"))
    assert pr.check_argv(Path("D:/devkit"))[0] == str(console)


def test_last_line_prefers_stderr_and_falls_back_to_the_exit_code():
    assert pr.last_line(completed([], 1, stdout="a\nb", stderr="x\ny\n")) == "y"
    assert pr.last_line(completed([], 1, stdout="a\nb")) == "b"
    assert pr.last_line(completed([], 3)) == "exit 3"


def test_the_runner_spawns_without_a_console_window(monkeypatch):
    """This runs under the scheduled `pythonw` job; a spawn without the flag opens a
    console on the desktop every night. `tests/test_scheduled_jobs.py` gates the
    reachable set too; this pins the one call site by hand."""
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        return completed(argv)

    monkeypatch.setattr(pr.subprocess, "run", fake_run)
    pr.run_command(["python", "-c", "pass"])
    assert seen["creationflags"] == pr.sweep.NO_WINDOW
    assert seen["check"] is False
