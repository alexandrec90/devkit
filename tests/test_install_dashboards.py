"""Tests for the dashboard gateway's scheduled-task installer.

What matters is the registered command and the triggers: a resident gateway with no time
limit, started at logon, restarted by its own repetition, and running `serve`.
"""

from __future__ import annotations

from pathlib import Path

from support import load_script

installer = load_script("scripts/install-dashboards.py")

SCRIPT = Path(r"C:\ws\devkit\scripts\dashboards.py")


def document() -> str:
    return installer.task_document(
        r"C:\py\pythonw.exe", installer.dashboards_arguments(SCRIPT), SCRIPT
    )


def test_the_task_runs_serve():
    assert installer.dashboards_arguments(SCRIPT) == f'"{SCRIPT}" serve'


def test_it_is_resident_with_no_time_limit():
    assert "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in document()


def test_it_starts_at_logon_and_is_restarted_by_its_repetition():
    body = document()
    assert "<LogonTrigger>" in body
    assert f"<Interval>PT{installer.RESTART_MINUTES}M</Interval>" in body
    # The repetition must never start a second gateway beside a running one.
    assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in body


def test_it_starts_in_the_checkout_whose_logs_it_writes():
    assert r"<WorkingDirectory>C:\ws\devkit</WorkingDirectory>" in document()


def test_it_is_not_wrapped_in_log_wrap():
    assert "log-wrap.py" not in document()


def test_the_artifact_is_the_gateways_log():
    runner = load_script("scripts/dashboards.py")
    assert installer.ARTIFACT == runner.ARTIFACT.as_posix()


def test_off_windows_it_says_so_and_does_nothing(monkeypatch, capsys):
    monkeypatch.setattr(installer, "WINDOWS", False)
    assert installer.main([]) == 0
    assert "Windows-only" in capsys.readouterr().out


def test_the_dry_run_registers_nothing(monkeypatch, capsys):
    monkeypatch.setattr(installer, "WINDOWS", True)

    def never(*_args, **_kwargs):
        raise AssertionError("a dry run reached the scheduler")

    monkeypatch.setattr(installer.devkit_schtasks, "register", never)
    monkeypatch.setattr(installer, "_run_argv", never)
    assert installer.main([]) == 0
    assert "Dry run" in capsys.readouterr().out
