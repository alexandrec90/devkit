"""Tests for the fix pass's scheduled-task installer.

The command string is what matters, because nothing re-reads it: once `schtasks` has
it, it runs every half hour for as long as the workstation exists. The one property this
job has that the others do not is that the registered command carries no switch at all
-- `--scheduled` reads it from the workspace file on every fire, so turning the pass on
is a setting rather than a re-install.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from support import REPO_ROOT, load_script, sweep

installer = load_script("scripts/install-fix-pass-task.py")

PY = r"C:\py\python.exe"
SCRIPT = Path(r"C:\ws\devkit\scripts\fix-pass.py")
WORKSPACE = Path(r"C:\ws\alex-projects.code-workspace")


def test_the_scheduled_run_reads_its_switch_from_the_workspace_file():
    """No `--mode` in the registered command: the switch lives in the workspace file,
    so flipping it is one setting and the task never needs re-registering."""
    command = installer.pass_arguments(SCRIPT, WORKSPACE)
    assert "--scheduled" in command
    assert "--mode" not in command and "--agent" not in command


def test_the_workspace_is_named_not_inferred():
    command = installer.pass_arguments(SCRIPT, WORKSPACE)
    assert "--workspace" in command and str(WORKSPACE) in command


def test_paths_are_quoted_for_a_profile_name_with_spaces():
    quoted = installer.pass_arguments(Path(r"C:\Program Files\ws\fix-pass.py"), WORKSPACE)
    assert '"C:\\Program Files\\ws\\fix-pass.py"' in quoted


def test_the_interval_is_half_an_hour():
    assert "<Interval>PT30M</Interval>" in installer.task_document(PY, "args", 30)
    assert installer.DEFAULT_INTERVAL_MINUTES == 30


def test_the_scheduled_task_runs_on_battery_and_catches_up():
    body = installer.task_document(PY, "args", 30)
    assert "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>" in body
    assert "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>" in body
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in body


def test_the_job_is_branch_delivery_and_says_where_it_reports():
    assert installer.GROUP == "delivery"
    assert installer.ARTIFACT == "logs/fix-pass.log"
    assert installer.TASK_NAME == "devkit-fix-pass"


def test_uninstall_names_the_task_and_does_not_prompt():
    argv = installer.uninstall_argv("devkit-fix-pass")
    assert argv[:2] == ["schtasks", "/Delete"]
    assert "devkit-fix-pass" in argv and "/F" in argv


def test_a_dry_run_never_calls_schtasks(monkeypatch, capsys):
    monkeypatch.setattr(installer, "WINDOWS", True)
    monkeypatch.setattr(
        installer,
        "_run_argv",
        lambda argv: (_ for _ in ()).throw(AssertionError("called schtasks")),
    )
    assert installer.main([]) == 0
    out = capsys.readouterr().out
    assert "Dry run" in out and "devkit.fixPass" in out


def test_the_parser_is_read_only_by_default():
    args = installer.build_parser().parse_args([])
    assert args.apply is False and args.minutes == 30


def test_status_and_uninstall_are_answered_before_any_document_is_built(monkeypatch, capsys):
    monkeypatch.setattr(installer, "WINDOWS", True)
    monkeypatch.setattr(
        installer,
        "_run_argv",
        lambda argv: subprocess.CompletedProcess(list(argv), 0, f"ran {argv[1]}", ""),
    )
    assert installer.main(["--status"]) == 0
    assert "ran /Query" in capsys.readouterr().out
    assert installer.main(["--uninstall"]) == 0
    assert "Dry run" in capsys.readouterr().out
    assert (
        installer.query_or_remove(
            argparse.Namespace(name="devkit-x", status=False, uninstall=False, apply=False)
        )
        is None
    )


def _the_document_main_would_register() -> str:
    python = installer.windowless(sys.executable)
    workspace = sweep.default_workspace(REPO_ROOT).resolve()
    arguments = installer.pass_arguments(installer.pass_script(), workspace)
    return installer.task_document(python, arguments, installer.DEFAULT_INTERVAL_MINUTES)


def test_check_is_green_when_the_scheduler_holds_the_document_yes_would_register(
    monkeypatch, capsys
):
    monkeypatch.setattr(installer, "WINDOWS", True)
    document = _the_document_main_would_register()
    monkeypatch.setattr(
        installer,
        "_run_argv",
        lambda argv: subprocess.CompletedProcess(list(argv), 0, document, ""),
    )
    assert installer.main(["--check"]) == 0
    assert installer.TASK_NAME in capsys.readouterr().out


def test_check_is_red_when_nothing_is_registered(monkeypatch, capsys):
    monkeypatch.setattr(installer, "WINDOWS", True)
    monkeypatch.setattr(
        installer,
        "_run_argv",
        lambda argv: subprocess.CompletedProcess(list(argv), 1, "", "no task"),
    )
    assert installer.main(["--check"]) == 1


def test_off_windows_it_says_so_and_exits_clean(monkeypatch, capsys):
    monkeypatch.setattr(installer, "WINDOWS", False)
    assert installer.main(["--yes"]) == 0
    assert "Windows-only" in capsys.readouterr().out


def test_the_script_it_names_is_the_pass():
    assert installer.pass_script().name == "fix-pass.py"
    assert installer.query_argv("devkit-fix-pass")[:2] == ["schtasks", "/Query"]
