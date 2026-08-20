"""What registering the nightly global-npm pass has to keep true.

`tests/test_scheduled_jobs.py` already holds the properties every devkit job shares --
registered from XML, names an artifact, points `schedule_health` at it. This file holds
the ones specific to this job, and the two that would break it silently:

- **`--yes` is in the registered command.** Without it `global-tools.py` reports and
  installs nothing, so the task would fire nightly, write a clean-looking artifact, and
  update nothing at all. That is worse than no job: it looks like coverage.
- **The task never points into an ephemeral box.** `reconcile` deletes one when its PR
  merges, and the job then fails at 04:30 with `Last Result` and nowhere to read.

Nothing here calls `schtasks`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from support import load_script

installer = load_script("scripts/install-global-tools.py")


class FakeRunner:
    """Records argvs; answers 0 with `stdout` unless told otherwise."""

    def __init__(self, code: int = 0, stdout: str = "", stderr: str = ""):
        self.calls: list[list[str]] = []
        self.code, self.stdout, self.stderr = code, stdout, stderr

    def __call__(self, argv):
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(list(argv), self.code, self.stdout, self.stderr)


def fake_checkout(root: Path) -> Path:
    """A directory shaped enough like a devkit checkout for the installer to accept."""
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "global-tools.py").write_text("", encoding="utf-8")
    return root


# --- the registered command ----------------------------------------------------


def test_the_registered_command_carries_yes():
    """Without it the job fires nightly and installs nothing, which looks like coverage
    and is not."""
    schedule = installer.schedule_for(root=Path(r"C:\ws\devkit"))
    assert schedule.command[-1] == "--yes"


def test_the_registered_command_names_the_runner_in_the_named_checkout():
    schedule = installer.schedule_for(root=Path(r"C:\ws\devkit"))
    assert schedule.script.endswith("global-tools.py")
    assert "devkit" in schedule.script


def test_the_task_document_goes_through_the_builder_that_knows_about_laptops():
    """`schtasks /SC DAILY` cannot say "run on battery" or "catch up a missed run", and
    at 04:30 a laptop is asleep or unplugged more often than not."""
    xml = installer.task_document(
        installer.schedule_for(root=Path(r"C:\ws\devkit")), Path(r"C:\ws\devkit")
    )
    assert "<CalendarTrigger>" in xml and "<DaysInterval>1</DaysInterval>" in xml
    assert "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>" in xml
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in xml


def test_the_task_runs_from_the_checkout_rather_than_system32():
    root = Path(r"C:\ws\devkit")
    assert f"<WorkingDirectory>{root}</WorkingDirectory>" in installer.task_document(
        installer.schedule_for(root=root), root
    )


def test_the_start_time_lands_after_the_prune_it_shares_a_night_with():
    """03:00 upgrade, 03:30 stop-idle, 04:00 prune. Two pythonw jobs starting together
    on a laptop that just woke is how one of them times out."""
    assert installer.DEFAULT_TIME > "04:00"


@pytest.mark.parametrize("at", ["04:30", "00:00", "23:59"])
def test_a_valid_time_is_accepted(at):
    assert installer.valid_time(at)


@pytest.mark.parametrize("at", ["4:30", "24:00", "04:60", "half four", "0430"])
def test_an_invalid_time_is_refused_rather_than_registered(at):
    assert not installer.valid_time(at)


def test_the_crontab_line_is_the_same_command_at_the_same_time():
    line = installer.crontab_line(installer.schedule_for("04:30", Path(r"C:\ws\devkit")))
    assert line.startswith("30 4 * * * ")
    assert line.rstrip().endswith("--yes")


# --- keeping it pointed at something that exists -------------------------------


def test_the_interpreter_belongs_to_the_named_checkout_when_it_has_one(tmp_path):
    """`--devkit` exists so an agent in a box can register against the static checkout.
    Leaving `sys.executable` there registers the box's `.venv`, which reconcile deletes."""
    venv = tmp_path / ".venv" / ("Scripts" if installer.WINDOWS else "bin")
    venv.mkdir(parents=True)
    python = venv / ("python.exe" if installer.WINDOWS else "python")
    python.write_text("", encoding="utf-8")
    assert installer.interpreter(tmp_path) == str(python)


def test_a_checkout_with_no_virtualenv_falls_back_rather_than_failing(tmp_path):
    assert installer.interpreter(tmp_path)


def test_installing_from_a_box_is_refused(tmp_path):
    """A task pointing into .worktrees/ works until the PR merges and then fails at
    04:30, nightly, with an exit code and nowhere to read about it."""
    box = fake_checkout(tmp_path / installer.BOXES_DIR / "devkit--something-0819")
    runner = FakeRunner()
    assert installer.main(["--yes", "--devkit", str(box)], runner=runner) == 2
    assert runner.calls == []


def test_the_plan_can_still_be_read_from_a_box(tmp_path):
    """Refusing the read-only mode too would make it useless in the place an agent
    most often invokes it from."""
    box = fake_checkout(tmp_path / installer.BOXES_DIR / "devkit--something-0819")
    assert installer.main(["--devkit", str(box)], runner=FakeRunner()) == 0


def test_a_checkout_with_no_runner_in_it_is_not_scheduled(tmp_path):
    assert installer.main(["--yes", "--devkit", str(tmp_path)], runner=FakeRunner()) == 2


# --- --check -------------------------------------------------------------------


def test_a_task_pointing_at_this_checkout_is_healthy():
    schedule = installer.schedule_for(root=Path(r"C:\ws\devkit"))
    assert installer.drifted(f'pythonw.exe "{schedule.script}" --yes', schedule) == ""


def test_a_task_pointing_somewhere_else_is_named_as_drift():
    schedule = installer.schedule_for(root=Path(r"C:\ws\devkit"))
    reason = installer.drifted(r'pythonw.exe "C:\gone\scripts\global-tools.py" --yes', schedule)
    assert "not this checkout" in reason


def test_nothing_registered_is_drift_too_rather_than_silence():
    """An installer that only checked existence would call a task pointing at a deleted
    directory healthy."""
    assert (
        installer.drifted("", installer.schedule_for(root=Path(r"C:\ws\devkit")))
        == "nothing is scheduled"
    )


def test_the_registered_command_is_read_out_of_schtasks_list_output():
    stdout = "Folder: \\\r\nTaskName:      \\devkit-global-tools\r\nTask To Run:   pythonw.exe x.py --yes\r\n"
    assert installer.registered_command(stdout) == "pythonw.exe x.py --yes"


def test_output_naming_no_task_yields_no_command():
    assert installer.registered_command("ERROR: The system cannot find the file specified.") == ""


# --- the plan and the removal --------------------------------------------------


def test_the_plan_says_what_it_would_change_and_where_that_is_recorded():
    plan = installer.render_plan(installer.schedule_for(root=Path(r"C:\ws\devkit")), windows=True)
    assert installer.ARTIFACT in plan
    assert "except npm and Claude Code" in plan


def test_a_posix_machine_is_given_the_line_to_paste_rather_than_a_faked_install():
    plan = installer.render_plan(installer.schedule_for(root=Path("/ws/devkit")), windows=False)
    assert "crontab" in plan and "* * *" in plan


def test_uninstall_removes_the_task_by_name(tmp_path):
    runner = FakeRunner()
    assert (
        installer.main(["--uninstall", "--devkit", str(fake_checkout(tmp_path))], runner=runner)
        == 0
    )
    assert runner.calls == [["schtasks", "/Delete", "/TN", installer.TASK_NAME, "/F"]]


def test_the_bare_invocation_registers_nothing(tmp_path):
    runner = FakeRunner()
    assert installer.main(["--devkit", str(fake_checkout(tmp_path))], runner=runner) == 0
    assert runner.calls == []
