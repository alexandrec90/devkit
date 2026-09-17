"""`install-reap-schedule.py`: the document it registers, and the drift it reports.

`tests/test_scheduled_jobs.py` holds this job to the contract every unattended devkit
job shares. What is left for here is what only this installer decides: the repetition,
the time limit, the working directory, that there is no boot trigger, and whether
`--check` can tell a moved checkout from a healthy one.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from support import REPO_ROOT, load_script

installer = load_script("scripts/install-reap-schedule.py")


def completed(stdout: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def schedule(every: int = 15) -> object:
    return installer.Schedule(
        name=installer.TASK_NAME,
        python=r"C:\py\pythonw.exe",
        script=r"C:\ws\devkit\scripts\reap-stale.py",
        every=every,
        workspace=r"C:\ws\alex-projects.code-workspace",
    )


# --- the argv ---------------------------------------------------------------


def test_the_command_names_the_mode_and_the_workspace():
    argv = schedule().command
    assert argv[:3] == [r"C:\py\pythonw.exe", r"C:\ws\devkit\scripts\reap-stale.py", "maintain"]
    assert argv[argv.index("--workspace") + 1] == r"C:\ws\alex-projects.code-workspace"


def test_a_machine_with_no_workspace_file_still_produces_a_runnable_command():
    bare = installer.Schedule(installer.TASK_NAME, "py", "reap.py", 15, "")
    assert bare.command == ["py", "reap.py", "maintain"]


def test_the_real_checkout_resolves_to_this_ones_runner():
    resolved = installer.schedule_for(root=REPO_ROOT)
    assert Path(resolved.script) == (REPO_ROOT / "scripts" / "reap-stale.py").resolve()
    assert resolved.every == installer.DEFAULT_INTERVAL


# --- the interval -----------------------------------------------------------


@pytest.mark.parametrize("every", [1, 15, 1440])
def test_a_usable_interval_is_accepted(every):
    assert installer.valid_interval(every)


@pytest.mark.parametrize("every", [0, -1, 1441, True, "15"])
def test_an_unusable_interval_is_rejected(every):
    assert not installer.valid_interval(every)


def test_the_cli_refuses_a_bad_interval_rather_than_registering_one(capsys):
    with pytest.raises(SystemExit):
        installer.main(["--every", "0"])
    assert "--every" in capsys.readouterr().err


# --- the document -----------------------------------------------------------


def test_the_document_repeats_at_the_configured_interval():
    assert "<Interval>PT7M</Interval>" in installer.task_document(schedule(7))


def test_the_document_carries_the_settings_a_command_line_cannot_express():
    xml = installer.task_document(schedule())
    for tag in (
        "<StartWhenAvailable>true",
        "<DisallowStartIfOnBatteries>false",
        "<RunOnlyIfIdle>false",
    ):
        assert tag in xml


def test_the_time_limit_is_shorter_than_the_gap_between_fires():
    assert "<ExecutionTimeLimit>PT10M</ExecutionTimeLimit>" in installer.task_document(schedule())


def test_the_working_directory_is_the_checkout_so_the_artifact_lands_in_it():
    assert r"<WorkingDirectory>C:\ws\devkit</WorkingDirectory>" in installer.task_document(
        schedule()
    )


def test_there_is_no_boot_trigger_because_a_reboot_leaves_nothing_to_reap():
    assert "<BootTrigger>" not in installer.task_document(schedule())


def test_the_crontab_line_repeats_at_the_same_interval():
    assert installer.crontab_line(schedule(5)).startswith("*/5 * * * * ")


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


def test_a_task_pointing_somewhere_else_is_drift(monkeypatch):
    monkeypatch.setattr(installer, "WINDOWS", True)
    moved = installer.task_document(
        installer.Schedule(
            installer.TASK_NAME, r"C:\py\pythonw.exe", r"C:\elsewhere\reap-stale.py", 15
        )
    )
    code, message = installer.run_check(schedule(), lambda argv: completed(moved))
    assert code == 1 and r"C:\elsewhere" in message


def test_check_off_windows_has_nothing_to_query(monkeypatch):
    monkeypatch.setattr(installer, "WINDOWS", False)
    assert installer.run_check(schedule())[0] == 0


def test_run_command_captures_rather_than_streaming():
    result = installer.run_command([sys.executable, "-c", "print('hi')"])
    assert result.stdout.strip() == "hi"


def test_windowless_python_resolves_a_gui_subsystem_interpreter():
    resolved = installer.windowless_python(sys.executable)
    assert resolved.endswith("pythonw.exe") or resolved == sys.executable


# --- the CLI ----------------------------------------------------------------


def test_render_plan_names_the_job_and_what_it_never_touches():
    text = installer.render_plan(schedule(), windows=True)
    assert installer.TASK_NAME in text and "Interactive sessions are never candidates" in text
    assert "devkit.reapStale" in text


def test_render_plan_off_windows_hands_over_a_crontab_line():
    assert "*/15 * * * *" in installer.render_plan(schedule(), windows=False)


def test_the_bare_invocation_registers_nothing(capsys):
    assert installer.main(["--devkit", str(REPO_ROOT)]) == 0
    assert "Nothing was registered" in capsys.readouterr().out


def test_a_checkout_with_no_runner_is_refused(tmp_path, capsys):
    assert installer.main(["--devkit", str(tmp_path)]) == 2
    assert "no runner" in capsys.readouterr().err


def test_installing_from_an_ephemeral_box_is_refused(tmp_path, capsys):
    box = tmp_path / installer.BOXES_DIR / "b1"
    (box / "scripts").mkdir(parents=True)
    (box / "scripts" / "reap-stale.py").write_text("", encoding="utf-8")
    assert installer.main(["--yes", "--devkit", str(box)]) == 2
    assert "ephemeral box" in capsys.readouterr().err


def test_check_is_routed_through_run_check(monkeypatch, capsys):
    monkeypatch.setattr(installer, "run_check", lambda schedule: (1, "schedule: nothing"))
    assert installer.main(["--check", "--devkit", str(REPO_ROOT)]) == 1
    assert "nothing" in capsys.readouterr().err


def test_yes_is_routed_through_install(monkeypatch, capsys, tmp_path):
    # A *static* checkout, built rather than borrowed: `--yes` refuses a path with
    # BOXES_DIR in its parts, and `REPO_ROOT` is exactly that whenever the suite runs
    # inside a box -- which is where `worktree.py new` puts every agent.
    static = tmp_path / "devkit"
    (static / "scripts").mkdir(parents=True)
    (static / "scripts" / "reap-stale.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(installer, "install", lambda schedule: (True, "scheduled"))
    assert installer.main(["--yes", "--devkit", str(static)]) == 0
    assert "scheduled" in capsys.readouterr().out


def test_build_parser_accepts_every_verb_and_the_apply_flag_with_them():
    """The CLI, as its own function so `main` holds decisions rather than declarations.

    The assertion that matters is `--uninstall --yes`: while `--yes` sat in the same
    mutually-exclusive group as the verbs, argparse rejected that combination outright, so
    the uninstall had no dry run to offer and the bare verb had to act on the machine.
    """
    parser = installer.build_parser()
    assert parser.parse_args(["--uninstall", "--yes"]).uninstall is True
    assert parser.parse_args(["--check"]).check is True
    parser.parse_args([])
