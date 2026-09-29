"""Tests for the collectors job's scheduled-task installer.

The registered command is what matters, because nothing re-reads it. The one decision
specific to this job is that the task is the same on every workstation -- the machine a
collector runs on is an assignment the runner reads, never a flag in the task.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from support import REPO_ROOT, load_script

installer = load_script("scripts/install-collectors.py")

SCRIPT = Path(r"C:\ws\devkit\scripts\collectors.py")


def document() -> str:
    return installer.task_document(r"C:\py\pythonw.exe", "args", 15, SCRIPT)


def test_the_task_runs_maintain_and_names_no_machine_or_project():
    arguments = installer.collectors_arguments(SCRIPT)
    assert arguments == f'"{SCRIPT}" maintain'


def test_it_fires_every_quarter_hour_and_at_logon():
    body = document()
    assert "<Interval>PT15M</Interval>" in body
    assert "<LogonTrigger>" in body


def test_it_runs_on_battery_and_catches_up():
    body = document()
    assert "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>" in body
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in body


def test_it_starts_in_the_checkout():
    assert r"<WorkingDirectory>C:\ws\devkit</WorkingDirectory>" in document()


def test_it_is_maintenance_and_says_where_it_reports():
    assert installer.GROUP == "maintenance"
    assert installer.ARTIFACT == "logs/collectors.log"
    assert installer.TASK_NAME == "devkit-collectors"


def test_the_runner_it_names_exists():
    assert installer.collectors_script(REPO_ROOT).is_file()


def test_a_dry_run_never_calls_schtasks(monkeypatch, capsys):
    monkeypatch.setattr(installer, "WINDOWS", True)
    monkeypatch.setattr(
        installer, "_run_argv", lambda argv: (_ for _ in ()).throw(AssertionError(argv))
    )
    assert installer.main([]) == 0
    assert "Dry run" in capsys.readouterr().out


def test_check_is_red_when_nothing_is_registered(monkeypatch):
    monkeypatch.setattr(installer, "WINDOWS", True)
    monkeypatch.setattr(
        installer, "_run_argv", lambda argv: subprocess.CompletedProcess(list(argv), 1, "", "none")
    )
    assert installer.main(["--check"]) == 1


def test_check_is_green_when_the_scheduler_holds_what_yes_would_register(monkeypatch):
    monkeypatch.setattr(installer, "WINDOWS", True)
    script = installer.collectors_script()
    registered = installer.task_document(
        installer.windowless(sys.executable),
        installer.collectors_arguments(script),
        installer.DEFAULT_INTERVAL_MINUTES,
        script,
    )
    monkeypatch.setattr(
        installer,
        "_run_argv",
        lambda argv: subprocess.CompletedProcess(list(argv), 0, registered, ""),
    )
    assert installer.main(["--check"]) == 0


def test_the_parser_is_read_only_by_default():
    args = installer.build_parser().parse_args([])
    assert args.apply is False and args.minutes == installer.DEFAULT_INTERVAL_MINUTES


def test_off_windows_it_says_so_and_exits_clean(monkeypatch, capsys):
    monkeypatch.setattr(installer, "WINDOWS", False)
    assert installer.main(["--yes"]) == 0
    assert "Windows-only" in capsys.readouterr().out
