"""`install-tray.py`: the two settings that make a *resident* task different from a pass.

`tests/test_scheduled_jobs.py` holds this to the contract every devkit job shares. What
is left for here is what only this installer decides, and every one of those decisions
exists because every other job in the repo is a pass that finishes:

- **no execution time limit** -- the inherited hour would kill the tray an hour after
  logon, every day, surfacing as an icon that "sometimes isn't there";
- **a logon trigger, not a boot trigger** -- a boot trigger fires before there is a
  desktop to draw into;
- **`--restart`** -- a pass picks up an edit on its next run, whereas the tray holds the
  `tray.py` it imported at logon until the session ends, so an edited icon is invisible
  with nothing failing to say so.
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


def test_check_is_green_when_the_scheduler_holds_this_document(monkeypatch):
    monkeypatch.setattr(installer, "WINDOWS", True)
    document = installer.task_document(schedule())
    code, message = installer.run_check(schedule(), runner=lambda argv: completed(document))
    assert code == 0 and installer.TASK_NAME in message


def test_check_is_red_when_the_task_is_missing(monkeypatch):
    monkeypatch.setattr(installer, "WINDOWS", True)
    code, message = installer.run_check(schedule(), runner=lambda argv: completed(returncode=1))
    assert code == 1 and "nothing is scheduled" in message


def test_a_task_pointing_somewhere_else_is_drift(monkeypatch):
    monkeypatch.setattr(installer, "WINDOWS", True)
    moved = installer.task_document(
        installer.Schedule(installer.TASK_NAME, r"C:\py\pythonw.exe", r"C:\old\tray.py", 120)
    )
    code, message = installer.run_check(schedule(), runner=lambda argv: completed(moved))
    assert code == 1 and r"C:\old" in message


def test_check_off_windows_has_nothing_to_query(monkeypatch):
    monkeypatch.setattr(installer, "WINDOWS", False)
    assert installer.run_check(schedule(), runner=lambda argv: completed(returncode=1))[0] == 0


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


# --- --restart ---------------------------------------------------------------
#
# The tray holds `tray.py` in memory from logon to logout, so an edited icon is invisible
# until the process is replaced -- with nothing failing anywhere to say so.


def test_end_argv_stops_the_named_task():
    argv = installer.end_argv()
    assert argv[:2] == ["schtasks", "/End"]
    assert argv[argv.index("/TN") + 1] == installer.TASK_NAME


def test_start_argv_runs_the_named_task():
    argv = installer.start_argv()
    assert argv[:2] == ["schtasks", "/Run"]
    assert argv[argv.index("/TN") + 1] == installer.TASK_NAME


def test_a_restart_stops_the_old_process_before_starting_one(monkeypatch):
    monkeypatch.setattr(installer, "WINDOWS", True)
    calls = []

    def runner(argv):
        calls.append(list(argv))
        return completed()

    ok, _ = installer.restart(schedule(), runner=runner)
    assert ok is True
    assert [argv[1] for argv in calls] == ["/End", "/Run"]


def test_a_restart_names_the_task_rather_than_this_checkouts_tray(monkeypatch):
    """A worktree restarts the registered tray, not the one it would have installed."""
    monkeypatch.setattr(installer, "WINDOWS", True)
    calls = []
    installer.restart(schedule(), runner=lambda argv: calls.append(list(argv)) or completed())
    for argv in calls:
        assert argv[argv.index("/TN") + 1] == installer.TASK_NAME
        assert not any("tray.py" in part for part in argv)


def test_a_tray_that_was_not_running_still_starts(monkeypatch):
    """`/End` reports non-zero with nothing to end -- the state a restart produces."""
    monkeypatch.setattr(installer, "WINDOWS", True)
    ok, message = installer.restart(
        schedule(),
        runner=lambda argv: completed(returncode=1 if argv[1] == "/End" else 0),
    )
    assert ok is True and installer.TASK_NAME in message


def test_a_restart_that_could_not_start_it_is_a_failure(monkeypatch):
    monkeypatch.setattr(installer, "WINDOWS", True)
    ok, message = installer.restart(
        schedule(),
        runner=lambda argv: completed("ERROR: cannot find the task", returncode=1),
    )
    assert ok is False and "cannot find the task" in message


def test_restarting_off_windows_says_there_is_nowhere_to_draw(monkeypatch):
    monkeypatch.setattr(installer, "WINDOWS", False)
    ok, message = installer.restart(schedule())
    assert ok is False and "notification area" in message


def test_the_cli_restarts_without_registering_anything(monkeypatch, capsys):
    monkeypatch.setattr(installer, "WINDOWS", True)
    monkeypatch.setattr(
        installer, "install", lambda *a, **k: pytest.fail("--restart must not register")
    )
    monkeypatch.setattr(installer, "restart", lambda *a, **k: (True, "restarted devkit-tray"))
    assert installer.main(["--restart"]) == 0
    assert "restarted devkit-tray" in capsys.readouterr().out


def test_a_failed_restart_exits_nonzero_on_stderr(monkeypatch, capsys):
    monkeypatch.setattr(installer, "restart", lambda *a, **k: (False, "ERROR: no such task"))
    assert installer.main(["--restart"]) == 2
    assert "no such task" in capsys.readouterr().err


def test_a_restart_needs_no_checkout_to_point_at(monkeypatch, tmp_path):
    """It acts on the registered task, so the --devkit guards must not reject it."""
    monkeypatch.setattr(installer, "restart", lambda *a, **k: (True, "restarted"))
    assert installer.main(["--restart", "--devkit", str(tmp_path)]) == 0


def test_restart_cannot_be_combined_with_installing():
    with pytest.raises(SystemExit):
        installer.main(["--restart", "--yes"])


def test_installing_from_an_ephemeral_box_is_refused(tmp_path, capsys):
    box = tmp_path / installer.BOXES_DIR / "devkit--x"
    (box / "scripts").mkdir(parents=True)
    (box / "scripts" / "tray.py").write_text("", encoding="utf-8")
    assert installer.main(["--yes", "--devkit", str(box)]) == 2
    assert "ephemeral box" in capsys.readouterr().err
