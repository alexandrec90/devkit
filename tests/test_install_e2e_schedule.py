"""`scripts/install-e2e-schedule.py`: the local end-to-end job's task document.

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


def test_the_runner_is_the_pass_beside_this_installer_and_the_cli_takes_an_interval():
    assert installer.runner_script(REPO_ROOT) == REPO_ROOT / "scripts" / "e2e-pass.py"
    assert installer.build_parser().parse_args(["--minutes", "60", "--yes"]).minutes == 60


def test_the_task_runs_the_pass_under_the_ledger_wrapper_from_this_checkout():
    arguments = installer.pass_arguments(PYTHON, REPO_ROOT)
    assert arguments.startswith(f'"{REPO_ROOT / "scripts" / "log-wrap.py"}" --always')
    assert arguments.endswith(f'"{REPO_ROOT / "scripts" / "e2e-pass.py"}" run --due')


def test_it_fires_every_half_hour_and_its_limit_outlasts_a_run():
    document = installer.task_document(PYTHON, "x", installer.EVERY_MINUTES, REPO_ROOT)
    assert "<Interval>PT30M</Interval>" in document
    assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in document
    hours = int(re.fullmatch(r"PT(\d+)H", installer.TIME_LIMIT).group(1))
    assert hours * 60 > e2e_pass.DEFAULT_TIMEOUT_MINUTES
    assert f"<ExecutionTimeLimit>{installer.TIME_LIMIT}</ExecutionTimeLimit>" in document
    assert e2e_pass.build_parser().parse_args(list(installer.RUN_ARGS)).due


def test_the_wait_is_the_runners_so_the_scheduler_never_skips_a_fire_for_activity():
    document = installer.task_document(PYTHON, "x", installer.EVERY_MINUTES, REPO_ROOT)
    assert "<RunOnlyIfIdle>false</RunOnlyIfIdle>" in document
    assert "run" in installer.RUN_ARGS and "--no-wait" not in installer.RUN_ARGS


def test_a_dry_run_prints_the_plan_and_registers_nothing(monkeypatch, capsys):
    monkeypatch.setattr(installer, "WINDOWS", True)
    monkeypatch.setattr(
        installer.devkit_schtasks, "register", lambda *_a: (_ for _ in ()).throw(AssertionError)
    )
    assert installer.main([]) == 0
    out = capsys.readouterr().out
    assert "every 30 minutes, what moved, while the PC is idle" in out
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
