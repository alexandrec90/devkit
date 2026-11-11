"""`scripts/install-tier-schedule.py`: the medium test tier's task document.

The contract every installer owes is `tests/test_installer_contract.py`'s and the job
contract `tests/test_scheduled_jobs.py`'s; these are the properties particular to this
job.
"""

from __future__ import annotations

import re

from support import REPO_ROOT, load_script

installer = load_script("scripts/install-tier-schedule.py")
log_wrap = load_script("scripts/log-wrap.py")
tier_pass = load_script("scripts/tier-pass.py")

PYTHON = r"C:\py\pythonw.exe"


def test_the_artifact_is_the_one_the_wrapper_writes_for_its_label():
    assert installer.ARTIFACT == f"logs/{log_wrap.slug(installer.LABEL)}.log"


def test_the_runner_is_the_pass_beside_this_installer_and_the_cli_takes_an_interval():
    assert installer.runner_script(REPO_ROOT) == REPO_ROOT / "scripts" / "tier-pass.py"
    assert installer.build_parser().parse_args(["--minutes", "30", "--yes"]).minutes == 30


def test_the_task_runs_the_pass_under_the_ledger_wrapper_from_this_checkout():
    arguments = installer.pass_arguments(PYTHON, REPO_ROOT)
    assert arguments.startswith(f'"{REPO_ROOT / "scripts" / "log-wrap.py"}" --always')
    assert arguments.endswith(f'"{REPO_ROOT / "scripts" / "tier-pass.py"}" run')


def test_it_fires_every_15_minutes_regardless_of_the_person():
    document = installer.task_document(PYTHON, "x", installer.EVERY_MINUTES, REPO_ROOT)
    assert "<Interval>PT15M</Interval>" in document
    assert "<RunOnlyIfIdle>false</RunOnlyIfIdle>" in document
    assert "<Hidden>" in document


def test_a_fire_during_a_pass_is_dropped_rather_than_run_beside_it():
    document = installer.task_document(PYTHON, "x", installer.EVERY_MINUTES, REPO_ROOT)
    assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in document


def test_the_limit_outlasts_the_runner_s_whole_pass():
    """A limit shorter than the pass plus the run it may have just started would have
    the scheduler killing a run the pass bounded itself, and recording no verdict."""
    minutes = int(re.fullmatch(r"PT(\d+)M", installer.TIME_LIMIT).group(1))
    setup = tier_pass.CLAIM_MARGIN_SECONDS / 60
    assert minutes >= tier_pass.DEFAULT_PASS_MINUTES + tier_pass.DEFAULT_TIMEOUT_MINUTES + setup
    document = installer.task_document(PYTHON, "x", installer.EVERY_MINUTES, REPO_ROOT)
    assert f"<ExecutionTimeLimit>{installer.TIME_LIMIT}</ExecutionTimeLimit>" in document


def test_the_scheduled_run_never_loses_the_verb_that_makes_it_do_anything():
    assert "run" in installer.RUN_ARGS


def test_a_dry_run_prints_the_plan_and_registers_nothing(monkeypatch, capsys):
    monkeypatch.setattr(installer, "WINDOWS", True)
    monkeypatch.setattr(
        installer.devkit_schtasks, "register", lambda *_a: (_ for _ in ()).throw(AssertionError)
    )
    assert installer.main([]) == 0
    out = capsys.readouterr().out
    assert "every 15 minutes, whatever the person is doing, where devkit.fixPass" in out
    assert "Dry run -- re-run with --yes." in out


def test_yes_registers_the_document_from_the_static_checkout(monkeypatch, capsys):
    monkeypatch.setattr(installer, "WINDOWS", True)
    monkeypatch.setattr(installer.sweep, "source_checkout", lambda root: root)
    seen = {}

    def register(name, document, _run):
        seen.update(name=name, document=document)
        return True, ""

    monkeypatch.setattr(installer.devkit_schtasks, "register", register)
    assert installer.main(["--yes", "--minutes", "30"]) == 0
    assert seen["name"] == installer.TASK_NAME
    assert "<Interval>PT30M</Interval>" in seen["document"]
    assert "installed devkit-medium-tier (every 30 minutes)" in capsys.readouterr().out


def test_it_refuses_to_register_from_a_temporary_checkout(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(installer, "WINDOWS", True)
    monkeypatch.setattr(installer.sweep, "source_checkout", lambda _root: tmp_path)
    assert installer.main(["--yes"]) == 2
    assert "temporary checkout" in capsys.readouterr().err


def test_off_windows_it_says_so_and_does_nothing(monkeypatch, capsys):
    monkeypatch.setattr(installer, "WINDOWS", False)
    assert installer.main(["--yes"]) == 0
    assert "Windows-only" in capsys.readouterr().out
