"""`scripts/install-e2e-schedule.py`: the nightly local end-to-end job's task document.

The contract every installer owes is `tests/test_installer_contract.py`'s and the job
contract `tests/test_scheduled_jobs.py`'s; these are the properties particular to this
job.
"""

from __future__ import annotations

import re

from support import REPO_ROOT, load_script

installer = load_script("scripts/install-e2e-schedule.py")
log_wrap = load_script("scripts/log-wrap.py")
e2e_pass = load_script("scripts/e2e-pass.py")

PYTHON = r"C:\py\pythonw.exe"


def test_the_artifact_is_the_one_the_wrapper_writes_for_its_label():
    assert installer.ARTIFACT == f"logs/{log_wrap.slug(installer.LABEL)}.log"


def test_the_runner_is_the_pass_beside_this_installer_and_the_cli_takes_a_time():
    assert installer.runner_script(REPO_ROOT) == REPO_ROOT / "scripts" / "e2e-pass.py"
    assert installer.build_parser().parse_args(["--at", "02:15", "--yes"]).at == "02:15"


def test_the_task_runs_the_pass_under_the_ledger_wrapper_from_this_checkout():
    arguments = installer.pass_arguments(PYTHON, REPO_ROOT)
    assert arguments.startswith(f'"{REPO_ROOT / "scripts" / "log-wrap.py"}" --always')
    assert arguments.endswith(f'"{REPO_ROOT / "scripts" / "e2e-pass.py"}" run')


def test_it_fires_nightly_and_its_limit_outlasts_the_runners_whole_waiting_window():
    """The task fires once at night and the runner waits for idle until its `--until`;
    a limit shorter than that window would kill a run the scheduler started."""
    document = installer.task_document(PYTHON, "x", installer.DEFAULT_AT, REPO_ROOT)
    assert f"T{installer.DEFAULT_AT}:00</StartBoundary>" in document
    assert "<CalendarTrigger>" in document
    hours = int(re.fullmatch(r"PT(\d+)H", installer.TIME_LIMIT).group(1))
    start = int(installer.DEFAULT_AT.split(":")[0])
    until = int(e2e_pass.DEFAULT_UNTIL.split(":")[0]) + 1
    assert start + hours >= until + e2e_pass.DEFAULT_TIMEOUT_MINUTES / 60
    assert f"<ExecutionTimeLimit>{installer.TIME_LIMIT}</ExecutionTimeLimit>" in document


def test_the_wait_is_the_runners_so_the_scheduler_never_skips_a_fire_for_activity():
    document = installer.task_document(PYTHON, "x", installer.DEFAULT_AT, REPO_ROOT)
    assert "<RunOnlyIfIdle>false</RunOnlyIfIdle>" in document
    assert "run" in installer.RUN_ARGS and "--no-wait" not in installer.RUN_ARGS


def test_a_dry_run_prints_the_plan_and_registers_nothing(monkeypatch, capsys):
    monkeypatch.setattr(installer, "WINDOWS", True)
    monkeypatch.setattr(
        installer.devkit_schtasks, "register", lambda *_a: (_ for _ in ()).throw(AssertionError)
    )
    assert installer.main([]) == 0
    out = capsys.readouterr().out
    assert "nightly at 01:00, then waits for the PC to be idle" in out
    assert "Dry run -- re-run with --yes." in out


def test_it_refuses_to_register_from_a_temporary_checkout(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(installer, "WINDOWS", True)
    monkeypatch.setattr(installer.sweep, "source_checkout", lambda _root: tmp_path)
    assert installer.main(["--yes"]) == 2
    assert "temporary checkout" in capsys.readouterr().err


def test_off_windows_it_says_so_and_does_nothing(monkeypatch, capsys):
    monkeypatch.setattr(installer, "WINDOWS", False)
    assert installer.main(["--yes"]) == 0
    assert "Windows-only" in capsys.readouterr().out
