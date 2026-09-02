"""`install-rc-schedule.py`: the document it registers, and the drift it reports.

`tests/test_scheduled_jobs.py` already holds this job to the contract every unattended
devkit job shares -- that it goes through `devkit_schtasks`, names an artifact under
`logs/`, and is pointed at by `schedule_health`. What is left for here is what only this
installer decides: the repetition, the time limit, the working directory, and whether
`--check` can tell a moved checkout from a healthy one.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from support import REPO_ROOT, load_script

installer = load_script("scripts/install-rc-schedule.py")


def completed(stdout: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def schedule(every: int = 15) -> object:
    return installer.Schedule(
        name=installer.TASK_NAME,
        python=r"C:\py\pythonw.exe",
        script=r"C:\ws\devkit\scripts\rc-servers.py",
        every=every,
        workspace=r"C:\ws\alex-projects.code-workspace",
    )


# --- the argv ---------------------------------------------------------------


def test_the_command_names_the_mode_and_the_workspace():
    argv = schedule().command
    assert argv[:3] == [r"C:\py\pythonw.exe", r"C:\ws\devkit\scripts\rc-servers.py", "maintain"]
    assert argv[argv.index("--workspace") + 1] == r"C:\ws\alex-projects.code-workspace"


def test_a_machine_with_no_workspace_file_still_produces_a_runnable_command():
    """`sweep.default_workspace` can come back empty on a checkout that is not inside
    one. The task should still be registrable; the runner reports the missing file into
    its artifact, which is a diagnosable state, unlike a task that would not install."""
    bare = installer.Schedule(installer.TASK_NAME, "py", "rc.py", 15, "")
    assert bare.command == ["py", "rc.py", "maintain"]


def test_the_real_checkout_resolves_to_this_ones_runner():
    resolved = installer.schedule_for(root=REPO_ROOT)
    assert Path(resolved.script) == (REPO_ROOT / "scripts" / "rc-servers.py").resolve()
    assert resolved.every == installer.DEFAULT_INTERVAL


# --- the interval -----------------------------------------------------------


@pytest.mark.parametrize("every", [1, 15, 60, 1440])
def test_a_usable_interval_is_accepted(every):
    assert installer.valid_interval(every)


@pytest.mark.parametrize(
    "every",
    [0, -1, 1441, "15", 1.5, True],
    ids=["zero", "negative", "too-long", "string", "float", "bool"],
)
def test_an_unusable_interval_is_rejected(every):
    """`True` among them deliberately: `isinstance(True, int)` would otherwise register
    a task that fires every minute from a flag someone spelled as a switch."""
    assert not installer.valid_interval(every)


def test_the_cli_refuses_a_bad_interval_rather_than_registering_one():
    with pytest.raises(SystemExit) as caught:
        installer.main(["--every", "0"])
    assert caught.value.code == 2


# --- the document -----------------------------------------------------------


def test_the_document_repeats_at_the_configured_interval():
    xml = installer.task_document(schedule(every=15))
    assert "<Interval>PT15M</Interval>" in xml
    assert "<Repetition>" in xml


def test_the_repetition_has_no_duration_and_so_never_stops():
    """A `<Duration>` present is a stopping point; absent means indefinitely. One that
    looked harmless would turn the job off after a day."""
    assert "<Duration>" not in installer.task_document(schedule())


def test_the_document_carries_the_settings_a_command_line_cannot_express():
    xml = installer.task_document(schedule())
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in xml
    assert "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>" in xml
    assert "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>" in xml


def test_the_time_limit_is_shorter_than_the_gap_between_fires():
    """Under `IgnoreNew` a run still going when the next is due suppresses every later
    fire until the limit expires. A limit longer than the interval turns one wedged
    update into a job that looks dead."""
    xml = installer.task_document(schedule(every=15))
    assert "<ExecutionTimeLimit>PT15M</ExecutionTimeLimit>" in xml


def test_the_working_directory_is_the_checkout_so_the_artifact_lands_in_it():
    """A task with no `<WorkingDirectory>` starts in `system32`, where `logs/` is both
    unfindable and likely unwritable."""
    assert "<WorkingDirectory>C:\\ws\\devkit</WorkingDirectory>" in installer.task_document(
        schedule()
    )


def test_the_document_is_the_utf16_shape_schtasks_demands():
    assert installer.task_document(schedule()).startswith('<?xml version="1.0" encoding="UTF-16"?>')


# --- the POSIX fallback -----------------------------------------------------


def test_the_crontab_line_repeats_at_the_same_interval():
    assert installer.crontab_line(schedule(every=15)).startswith("*/15 * * * * ")


def test_installing_off_windows_prints_the_line_rather_than_faking_it(monkeypatch):
    monkeypatch.setattr(installer, "WINDOWS", False)
    ok, message = installer.install(schedule())
    assert ok is False and "crontab" in message


# --- drift ------------------------------------------------------------------


def test_a_query_that_names_this_checkout_is_healthy():
    registered = r"C:\py\pythonw.exe C:\ws\devkit\scripts\rc-servers.py maintain"
    assert installer.drifted(registered, schedule()) == ""


def test_nothing_registered_is_named_as_such():
    assert installer.drifted("", schedule()) == "nothing is scheduled"


def test_a_task_pointing_somewhere_else_is_drift():
    """The failure this mode exists for: a checkout that moved leaves a task running
    something else, or nothing, while `schtasks` still reports it as present."""
    reason = installer.drifted(
        r"C:\py\pythonw.exe C:\old\devkit\scripts\rc-servers.py maintain", schedule()
    )
    assert "not this checkout" in reason


def test_a_differing_interpreter_alone_is_not_drift():
    """A venv rebuilt or a Python upgraded in place changes the interpreter and nothing
    that matters. Rewriting the task over it would be noise."""
    registered = r"C:\other\pythonw.exe C:\ws\devkit\scripts\rc-servers.py maintain"
    assert installer.drifted(registered, schedule()) == ""


def test_the_command_is_read_out_of_the_query_output():
    stdout = "Folder: \\\nTaskName:     \\devkit-rc-servers\nTask To Run:  C:\\py\\pythonw.exe C:\\x.py\n"
    assert installer.registered_command(stdout) == r"C:\py\pythonw.exe C:\x.py"


def test_a_query_with_no_such_line_reports_nothing_registered():
    assert installer.registered_command("TaskName: \\devkit-rc-servers\n") == ""


def test_check_is_red_when_the_task_is_missing(monkeypatch):
    monkeypatch.setattr(installer, "WINDOWS", True)
    code, message = installer.run_check(schedule(), runner=lambda argv: completed(returncode=1))
    assert code == 1 and "nothing is scheduled" in message


def test_check_is_green_when_it_points_here(monkeypatch):
    monkeypatch.setattr(installer, "WINDOWS", True)
    stdout = "Task To Run:  C:\\py\\pythonw.exe C:\\ws\\devkit\\scripts\\rc-servers.py maintain\n"
    code, message = installer.run_check(schedule(), runner=lambda argv: completed(stdout))
    assert code == 0 and installer.TASK_NAME in message


# --- the three modes --------------------------------------------------------


def test_query_argv_asks_for_the_verbose_list_the_parser_reads():
    """`registered_command` looks for a `Task To Run:` label, which only `/V` prints."""
    argv = installer.query_argv()
    assert argv[:2] == ["schtasks", "/Query"]
    assert "/V" in argv and argv[argv.index("/TN") + 1] == installer.TASK_NAME


def test_run_command_captures_rather_than_streaming():
    """The installer prints its own message; a `schtasks` that wrote straight to the
    terminal would double it and lose the failure text on the error path."""
    result = installer.run_command([sys.executable, "-c", "print('x')"])
    assert result.stdout.strip() == "x"


def test_windowless_python_resolves_a_gui_subsystem_interpreter():
    """A scheduled task that put a console on the desktop every fifteen minutes would be
    the most visible thing this job does."""
    resolved = installer.windowless_python(sys.executable)
    assert resolved.endswith("pythonw.exe") or resolved == sys.executable


def test_render_plan_names_the_job_and_what_it_runs():
    text = installer.render_plan(schedule(), windows=True)
    assert installer.TASK_NAME in text and "maintain" in text
    assert "scheduled task registered from XML" in text


def test_render_plan_off_windows_hands_over_a_crontab_line():
    text = installer.render_plan(schedule(), windows=False)
    assert "crontab" in text and "*/15 * * * *" in text


def test_the_bare_invocation_registers_nothing(capsys):
    assert installer.main([]) == 0
    out = capsys.readouterr().out
    assert "Nothing was registered" in out
    assert "maintain" in out


def test_the_plan_says_where_the_opt_in_lives(capsys):
    """The job serves nothing until `devkit.remoteControl` names a project, and a plan
    that did not say so reads as "installed and broken"."""
    installer.main([])
    assert "devkit.remoteControl" in capsys.readouterr().out


def test_a_checkout_with_no_runner_is_refused(tmp_path, capsys):
    assert installer.main(["--devkit", str(tmp_path)]) == 2
    assert "no runner at" in capsys.readouterr().err


def test_installing_from_an_ephemeral_box_is_refused(tmp_path, capsys):
    """A task pointing into `.worktrees/` dies the moment reconcile reaps the box."""
    box = tmp_path / installer.BOXES_DIR / "devkit--x"
    (box / "scripts").mkdir(parents=True)
    (box / "scripts" / "rc-servers.py").write_text("", encoding="utf-8")
    assert installer.main(["--yes", "--devkit", str(box)]) == 2
    assert "ephemeral box" in capsys.readouterr().err


def test_reading_the_plan_from_a_box_is_still_allowed(tmp_path, capsys):
    """Read-only modes stay usable from the place an agent most often invokes them."""
    box = tmp_path / installer.BOXES_DIR / "devkit--x"
    (box / "scripts").mkdir(parents=True)
    (box / "scripts" / "rc-servers.py").write_text("", encoding="utf-8")
    assert installer.main(["--devkit", str(box)]) == 0
    assert "Nothing was registered" in capsys.readouterr().out


def test_the_document_also_fires_after_a_reboot():
    """A reboot kills every server, and the next repetition can be a full interval away.
    Fifteen minutes of an unreachable phone is the failure this job exists to prevent."""
    xml = installer.task_document(schedule())
    assert "<BootTrigger>" in xml and "<TimeTrigger>" in xml
    assert xml.count("<Triggers>") == 1
