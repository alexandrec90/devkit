"""`install-tray.py`: the two settings that make a *resident* task different from a pass.

`tests/test_scheduled_jobs.py` holds this to the contract every devkit job shares. What
is left for here is what only this installer decides, and both of its decisions exist
because every other job in the repo is a pass that finishes:

- **no execution time limit** -- the inherited hour would kill the tray an hour after
  logon, every day, surfacing as an icon that "sometimes isn't there";
- **a logon trigger, not a boot trigger** -- a boot trigger fires before there is a
  desktop to draw into.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from support import REPO_ROOT, load_script

installer = load_script("scripts/install-tray.py")


def completed(stdout: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def schedule(poll: int = 120) -> object:
    return installer.Schedule(
        name=installer.TASK_NAME,
        python=r"C:\py\pythonw.exe",
        script=r"C:\ws\devkit\scripts\tray.py",
        poll_seconds=poll,
    )


# --- the argv ---------------------------------------------------------------


def test_the_command_runs_the_tray_at_the_configured_interval():
    argv = schedule(90).command
    assert argv[:2] == [r"C:\py\pythonw.exe", r"C:\ws\devkit\scripts\tray.py"]
    assert argv[argv.index("--poll-seconds") + 1] == "90"


def test_the_real_checkout_resolves_to_this_ones_tray():
    resolved = installer.schedule_for(root=REPO_ROOT)
    assert Path(resolved.script) == (REPO_ROOT / "scripts" / "tray.py").resolve()


def test_the_interpreter_is_the_windowless_one():
    """A console-subsystem tray leaves a black window open on the desktop for the whole
    session, next to the icon it drew."""
    resolved = installer.windowless_python(sys.executable)
    assert resolved.endswith("pythonw.exe") or resolved == sys.executable


# --- the poll interval ------------------------------------------------------


@pytest.mark.parametrize("seconds", [10, 120, 3600])
def test_a_usable_poll_is_accepted(seconds):
    assert installer.valid_poll(seconds)


@pytest.mark.parametrize(
    "seconds",
    [0, 9, 3601, "120", 1.5, True],
    ids=["zero", "too-fast", "too-slow", "string", "float", "bool"],
)
def test_an_unusable_poll_is_rejected(seconds):
    """The lower bound is not arbitrary: every poll spawns a `schtasks`, and the fastest
    devkit job runs every fifteen minutes, so a tight loop asks a question whose answer
    cannot have changed."""
    assert not installer.valid_poll(seconds)


def test_the_cli_refuses_a_bad_poll_rather_than_registering_one():
    with pytest.raises(SystemExit) as caught:
        installer.main(["--poll-seconds", "1"])
    assert caught.value.code == 2


# --- the document -----------------------------------------------------------


def test_the_tray_is_never_killed_by_a_time_limit():
    assert f"<ExecutionTimeLimit>{installer.NO_TIME_LIMIT}</ExecutionTimeLimit>" in (
        installer.task_document(schedule())
    )
    assert installer.NO_TIME_LIMIT == "PT0S"


def test_the_trigger_waits_for_a_desktop_to_draw_into():
    xml = installer.task_document(schedule())
    assert "<LogonTrigger>" in xml
    assert "<BootTrigger>" not in xml


def test_the_working_directory_is_the_checkout():
    assert "<WorkingDirectory>C:\\ws\\devkit</WorkingDirectory>" in installer.task_document(
        schedule()
    )


def test_the_document_is_the_utf16_shape_schtasks_demands():
    assert installer.task_document(schedule()).startswith('<?xml version="1.0" encoding="UTF-16"?>')


# --- drift and the modes ----------------------------------------------------


def test_a_query_that_names_this_checkout_is_healthy():
    assert installer.drifted(r"C:\py\pythonw.exe C:\ws\devkit\scripts\tray.py", schedule()) == ""


def test_nothing_registered_is_named_as_such():
    assert installer.drifted("", schedule()) == "nothing is scheduled"


def test_a_task_pointing_somewhere_else_is_drift():
    reason = installer.drifted(r"C:\py\pythonw.exe C:\old\devkit\scripts\tray.py", schedule())
    assert "not this checkout" in reason


def test_the_command_is_read_out_of_the_query_output():
    stdout = "TaskName:  \\devkit-tray\nTask To Run:  C:\\py\\pythonw.exe C:\\x.py\n"
    assert installer.registered_command(stdout) == r"C:\py\pythonw.exe C:\x.py"


def test_a_query_with_no_such_line_reports_nothing_registered():
    assert installer.registered_command("TaskName: \\devkit-tray\n") == ""


def test_check_is_red_when_the_task_is_missing(monkeypatch):
    monkeypatch.setattr(installer, "WINDOWS", True)
    code, message = installer.run_check(schedule(), runner=lambda argv: completed(returncode=1))
    assert code == 1 and "nothing is scheduled" in message


def test_check_is_green_when_it_points_here(monkeypatch):
    monkeypatch.setattr(installer, "WINDOWS", True)
    stdout = "Task To Run:  C:\\py\\pythonw.exe C:\\ws\\devkit\\scripts\\tray.py\n"
    code, message = installer.run_check(schedule(), runner=lambda argv: completed(stdout))
    assert code == 0 and installer.TASK_NAME in message


def test_query_argv_asks_for_the_verbose_list_the_parser_reads():
    argv = installer.query_argv()
    assert "/V" in argv and argv[argv.index("/TN") + 1] == installer.TASK_NAME


def test_run_command_captures_rather_than_streaming():
    assert installer.run_command([sys.executable, "-c", "print('x')"]).stdout.strip() == "x"


def test_installing_off_windows_says_there_is_nowhere_to_draw(monkeypatch):
    monkeypatch.setattr(installer, "WINDOWS", False)
    ok, message = installer.install(schedule())
    assert ok is False and "notification area" in message


def test_the_plan_describes_the_colours_it_will_show(capsys):
    installer.main([])
    out = capsys.readouterr().out
    assert "green" in out and "amber" in out and "red" in out
    assert "Nothing was registered" in out


def test_the_posix_plan_offers_an_autostart_line_rather_than_pretending():
    text = installer.render_plan(schedule(), windows=False)
    assert "autostart" in text and installer.autostart_line(schedule()) in text


def test_a_checkout_with_no_tray_is_refused(tmp_path, capsys):
    assert installer.main(["--devkit", str(tmp_path)]) == 2
    assert "no tray at" in capsys.readouterr().err


def test_installing_from_an_ephemeral_box_is_refused(tmp_path, capsys):
    box = tmp_path / installer.BOXES_DIR / "devkit--x"
    (box / "scripts").mkdir(parents=True)
    (box / "scripts" / "tray.py").write_text("", encoding="utf-8")
    assert installer.main(["--yes", "--devkit", str(box)]) == 2
    assert "ephemeral box" in capsys.readouterr().err
