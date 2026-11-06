"""`scripts/install-after-merge-schedule.py`: the queue's runner, every five minutes.

The contract every installer owes is `tests/test_installer_contract.py`'s and the job
contract `tests/test_scheduled_jobs.py`'s; these are the properties particular to this
job.
"""

from __future__ import annotations

from support import REPO_ROOT, load_script

installer = load_script("scripts/install-after-merge-schedule.py")
log_wrap = load_script("scripts/log-wrap.py")
runner = load_script("scripts/after-merge-pass.py")
switch = load_script("scripts/harness-switch.py")

PYTHON = r"C:\py\pythonw.exe"


def test_the_artifact_is_the_one_the_wrapper_writes_for_its_label():
    assert installer.ARTIFACT == f"logs/{log_wrap.slug(installer.LABEL)}.log"


def test_the_runner_is_the_pass_beside_this_installer_and_the_cli_takes_an_interval():
    assert installer.runner_script(REPO_ROOT) == REPO_ROOT / "scripts" / "after-merge-pass.py"
    assert installer.build_parser().parse_args(["--minutes", "7", "--yes"]).minutes == 7


def test_the_task_runs_the_pass_under_the_ledger_wrapper_from_this_checkout():
    arguments = installer.pass_arguments(PYTHON, REPO_ROOT)
    assert arguments.startswith(f'"{REPO_ROOT / "scripts" / "log-wrap.py"}" --always')
    assert arguments.endswith(f'"{REPO_ROOT / "scripts" / "after-merge-pass.py"}" run')


def test_it_fires_every_five_minutes_for_ever():
    assert installer.DEFAULT_INTERVAL_MINUTES == 5
    document = installer.task_document(PYTHON, "x", installer.DEFAULT_INTERVAL_MINUTES, REPO_ROOT)
    assert "<Interval>PT5M</Interval>" in document
    assert "<Duration>" not in document


def test_the_scheduler_limit_outlasts_the_longest_pass():
    """A pass starts its last command with `BUDGET_SECONDS` spent, then has its timeout."""
    assert runner.BUDGET_SECONDS + runner.RUN_TIMEOUT < 3600


def test_the_task_starts_in_the_checkout_and_is_a_delivery_job():
    document = installer.task_document(PYTHON, "x", 5, REPO_ROOT)
    assert f"<WorkingDirectory>{REPO_ROOT}</WorkingDirectory>" in document
    assert installer.GROUP == "delivery"
    assert installer.TASK_NAME in switch.BRANCH_DELIVERY_JOBS


def test_a_dry_run_prints_the_plan_and_registers_nothing(monkeypatch, capsys):
    monkeypatch.setattr(installer, "WINDOWS", True)
    monkeypatch.setattr(
        installer.devkit_schtasks, "register", lambda *_a: (_ for _ in ()).throw(AssertionError)
    )
    assert installer.main([]) == 0
    assert "every 5 min" in capsys.readouterr().out


def test_elsewhere_it_says_so_and_does_nothing(monkeypatch, capsys):
    monkeypatch.setattr(installer, "WINDOWS", False)
    assert installer.main(["--yes"]) == 0
    assert "Windows-only" in capsys.readouterr().out


def test_a_box_cannot_register_a_task_that_points_into_itself(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(installer, "WINDOWS", True)
    monkeypatch.setattr(installer.sweep, "source_checkout", lambda root: tmp_path)
    assert installer.main(["--yes"]) == 2
    assert "temporary checkout" in capsys.readouterr().err


def test_a_good_registration_says_so(monkeypatch, capsys):
    monkeypatch.setattr(installer, "WINDOWS", True)
    monkeypatch.setattr(installer.sweep, "source_checkout", lambda root: root)
    monkeypatch.setattr(installer.devkit_schtasks, "register", lambda *_a: (True, ""))
    assert installer.main(["--yes", "--minutes", "7"]) == 0
    assert "every 7 min" in capsys.readouterr().out


def test_run_argv_turns_a_spawn_failure_into_a_returncode():
    done = installer._run_argv(["no-such-binary-anywhere-xyz"])
    assert done.returncode == 1 and done.stderr
