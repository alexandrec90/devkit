"""`scripts/install-fix-trigger.py`: the fix pass's trigger's task document.

The contract every installer owes is `tests/test_installer_contract.py`'s and the job
contract `tests/test_scheduled_jobs.py`'s; these are the properties particular to this
job.
"""

from __future__ import annotations

import re

from support import REPO_ROOT, load_script

installer = load_script("scripts/install-fix-trigger.py")
log_wrap = load_script("scripts/log-wrap.py")
trigger = load_script("scripts/fix_trigger.py")

PYTHON = r"C:\py\pythonw.exe"


def test_the_artifact_is_the_one_the_wrapper_writes_for_its_label():
    assert installer.ARTIFACT == f"logs/{log_wrap.slug(installer.LABEL)}.log"


def test_the_task_runs_the_trigger_under_the_ledger_wrapper_from_this_checkout():
    assert installer.runner_script(REPO_ROOT) == REPO_ROOT / "scripts" / "fix_trigger.py"
    assert installer.runner_script(REPO_ROOT).is_file()
    arguments = installer.pass_arguments(PYTHON, REPO_ROOT)
    assert arguments.startswith(f'"{REPO_ROOT / "scripts" / "log-wrap.py"}" --always')
    assert arguments.endswith(f'"{REPO_ROOT / "scripts" / "fix_trigger.py"}"')


def test_it_fires_every_two_minutes_and_its_limit_outlasts_its_calls():
    document = installer.task_document(PYTHON, "x", installer.EVERY_MINUTES, REPO_ROOT)
    assert "<Interval>PT2M</Interval>" in document
    minutes = int(re.fullmatch(r"PT(\d+)M", installer.TIME_LIMIT).group(1))
    assert minutes * 60 > trigger.GRAPHQL_SECONDS + trigger.START_SECONDS
    assert f"<ExecutionTimeLimit>{installer.TIME_LIMIT}</ExecutionTimeLimit>" in document
    assert installer.build_parser().parse_args(["--minutes", "5", "--yes"]).minutes == 5


def test_a_dry_run_prints_the_plan_and_registers_nothing(monkeypatch, capsys):
    monkeypatch.setattr(installer, "WINDOWS", True)
    monkeypatch.setattr(
        installer.devkit_schtasks, "register", lambda *_a: (_ for _ in ()).throw(AssertionError)
    )
    assert installer.main([]) == 0
    out = capsys.readouterr().out
    assert "every 2 minutes, where devkit.fixPass is dispatch" in out
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
