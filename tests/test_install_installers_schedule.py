"""`install-installers-schedule.py`: the document it registers, and the drift it reports.

`tests/test_scheduled_jobs.py` holds this job to the contract every unattended devkit
job shares and `tests/test_installer_contract.py` to the one every installer shares.
What is left for here is what only this installer decides: the two triggers, the time
limit, the working directory, and that the mode it schedules is the one that acts.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from support import REPO_ROOT, load_script

installer = load_script("scripts/install-installers-schedule.py")


def completed(stdout: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def schedule(at: str = "08:45") -> object:
    return installer.Schedule(
        name=installer.TASK_NAME,
        python=r"C:\py\pythonw.exe",
        script=r"C:\ws\devkit\scripts\installers.py",
        at=at,
    )


# --- the argv ---------------------------------------------------------------


def test_the_command_names_the_mode_that_acts():
    """`status` is read-only by design; a task that lost the word would fire daily and
    register nothing while `schtasks` reported success."""
    assert schedule().command == [
        r"C:\py\pythonw.exe",
        r"C:\ws\devkit\scripts\installers.py",
        "maintain",
    ]


def test_the_real_checkout_resolves_to_this_ones_runner():
    resolved = installer.schedule_for(root=REPO_ROOT)
    assert Path(resolved.script) == (REPO_ROOT / "scripts" / "installers.py").resolve()
    assert resolved.at == installer.DEFAULT_AT


def test_windowless_python_resolves_a_gui_subsystem_interpreter():
    resolved = installer.windowless_python(sys.executable)
    assert resolved.endswith("pythonw.exe") or resolved == sys.executable


# --- the time --------------------------------------------------------------


@pytest.mark.parametrize("at", ["08:45", "00:00", "23:59"])
def test_a_valid_time_is_accepted(at):
    assert installer.valid_time(at)


@pytest.mark.parametrize("at", ["8:45", "24:00", "08:60", "0845", "", "dawn"])
def test_an_invalid_time_is_rejected(at):
    assert not installer.valid_time(at)


def test_the_cli_refuses_a_bad_time_rather_than_registering_one(capsys):
    with pytest.raises(SystemExit):
        installer.main(["--at", "25:00"])
    assert "--at" in capsys.readouterr().err


def test_the_slot_is_before_the_status_pass_it_feeds():
    """The 09:00 workspace-status toast should describe a machine this has already put
    right, and the small-hours jobs should have finished."""
    assert "04:30" < installer.DEFAULT_AT < "09:00"


# --- the document -----------------------------------------------------------


def test_the_document_fires_daily_and_after_logon():
    xml = installer.task_document(schedule("08:45"))
    assert "<CalendarTrigger>" in xml and "T08:45:00</StartBoundary>" in xml
    assert "<LogonTrigger>" in xml and f"<Delay>{installer.LOGON_DELAY}</Delay>" in xml
    assert xml.count("<Triggers>") == 1


def test_the_document_carries_the_settings_a_command_line_cannot_express():
    xml = installer.task_document(schedule())
    for tag in (
        "<StartWhenAvailable>true",
        "<DisallowStartIfOnBatteries>false",
        "<RunOnlyIfIdle>false",
    ):
        assert tag in xml


def test_the_time_limit_is_finite():
    assert "<ExecutionTimeLimit>PT30M</ExecutionTimeLimit>" in installer.task_document(schedule())


def test_the_working_directory_is_the_checkout_so_the_artifact_lands_in_it():
    assert r"<WorkingDirectory>C:\ws\devkit</WorkingDirectory>" in installer.task_document(
        schedule()
    )


def test_a_job_stood_down_by_name_is_registered_disabled(monkeypatch):
    monkeypatch.setattr(
        installer.harness_state, "stood_down", lambda *_a, **_k: frozenset({installer.TASK_NAME})
    )
    body = installer.task_document(schedule())
    assert "<Enabled>false</Enabled>" in body[body.index("<Settings>") :]


def test_the_crontab_line_puts_minutes_first():
    assert installer.crontab_line(schedule("08:45")).startswith("45 8 * * * ")


def test_installing_off_windows_prints_the_line_rather_than_faking_it(monkeypatch):
    monkeypatch.setattr(installer, "WINDOWS", False)
    ok, message = installer.install(schedule())
    assert not ok and "crontab" in message


def test_installing_goes_through_the_document_builder(monkeypatch):
    monkeypatch.setattr(installer, "WINDOWS", True)
    seen = {}

    def register(name, xml, run):
        seen.update(name=name, xml=xml)
        return True, "ok"

    monkeypatch.setattr(installer.devkit_schtasks, "register", register)
    ok, message = installer.install(schedule())
    assert ok and installer.TASK_NAME in message
    assert seen["name"] == installer.TASK_NAME and "<Task" in seen["xml"]


# --- drift ------------------------------------------------------------------


def test_check_is_green_when_the_scheduler_holds_this_document(monkeypatch):
    monkeypatch.setattr(installer, "WINDOWS", True)
    document = installer.task_document(schedule())
    code, message = installer.run_check(schedule(), lambda argv: completed(document))
    assert code == 0 and installer.TASK_NAME in message


def test_check_is_red_when_the_task_is_missing(monkeypatch):
    monkeypatch.setattr(installer, "WINDOWS", True)
    code, message = installer.run_check(schedule(), lambda argv: completed(returncode=1))
    assert code == 1 and "nothing is scheduled" in message


def test_check_off_windows_has_nothing_to_query(monkeypatch):
    monkeypatch.setattr(installer, "WINDOWS", False)
    assert installer.run_check(schedule())[0] == 0


def test_run_command_captures_rather_than_streaming():
    assert installer.run_command([sys.executable, "-c", "print('hi')"]).stdout.strip() == "hi"


# --- the CLI ----------------------------------------------------------------


def test_render_plan_says_what_the_pass_does_and_where_options_live():
    text = installer.render_plan(schedule(), windows=True)
    assert installer.TASK_NAME in text and "devkit.installers" in text
    assert installer.ARTIFACT in text


def test_render_plan_off_windows_hands_over_a_crontab_line():
    assert "45 8 * * *" in installer.render_plan(schedule(), windows=False)


def test_the_bare_invocation_registers_nothing(capsys):
    assert installer.main(["--devkit", str(REPO_ROOT)]) == 0
    assert "Nothing was registered" in capsys.readouterr().out


def test_a_checkout_with_no_runner_is_refused(tmp_path, capsys):
    assert installer.main(["--devkit", str(tmp_path)]) == 2
    assert "no runner" in capsys.readouterr().err


def test_installing_from_a_temporary_checkout_is_refused(tmp_path, capsys):
    worktree = tmp_path / "devkit" / ".claude" / "worktrees" / "lake"
    (worktree / "scripts").mkdir(parents=True)
    (worktree / "scripts" / "installers.py").write_text("", encoding="utf-8")
    assert installer.main(["--yes", "--devkit", str(worktree)]) == 2
    assert "temporary checkout" in capsys.readouterr().err


def test_check_is_routed_through_run_check(monkeypatch, capsys):
    monkeypatch.setattr(installer, "run_check", lambda schedule: (1, "schedule: nothing"))
    assert installer.main(["--check", "--devkit", str(REPO_ROOT)]) == 1
    assert "nothing" in capsys.readouterr().err


def test_yes_is_routed_through_install(monkeypatch, capsys):
    monkeypatch.setattr(installer, "install", lambda schedule: (True, "scheduled"))
    monkeypatch.setattr(installer.sweep, "source_checkout", lambda root: root)
    assert installer.main(["--yes", "--devkit", str(REPO_ROOT)]) == 0
    assert "scheduled" in capsys.readouterr().out
