"""`install-ship-schedule.py`: the document it registers.

`tests/test_installer_contract.py` and `tests/test_scheduled_jobs.py` hold this job to the
contract every unattended devkit job shares. What is left for here is what only this
installer decides: the two-minute repetition, the time limit, and UTF-8 mode.
"""

from __future__ import annotations

import pytest
from support import REPO_ROOT, load_script

installer = load_script("scripts/install-ship-schedule.py")
runner = load_script("scripts/ship-pass.py")


def schedule(every: int = installer.DEFAULT_INTERVAL):
    return installer.Schedule(
        name=installer.TASK_NAME,
        python=r"C:\py\pythonw.exe",
        script=r"C:\ws\devkit\scripts\ship-pass.py",
        every=every,
        workspace=r"C:\ws\alex-projects.code-workspace",
    )


def test_it_fires_every_two_minutes_as_a_delivery_job():
    assert installer.TASK_NAME == "devkit-ship"
    assert installer.GROUP == "delivery"
    assert installer.DEFAULT_INTERVAL == 2
    assert "<Interval>PT2M</Interval>" in installer.task_document(schedule())


def test_the_artifact_is_the_one_the_runner_writes():
    assert installer.ARTIFACT == runner.ARTIFACT.as_posix()


def test_it_runs_in_utf8_mode_and_names_its_workspace():
    """A `”` in a commit subject is a byte the locale cannot decode, and every git call
    here reads text (`fix-pass.in_utf8_mode`)."""
    assert schedule().arguments[:3] == ["-X", "utf8", r"C:\ws\devkit\scripts\ship-pass.py"]
    arguments = schedule().arguments
    assert arguments[arguments.index("--workspace") + 1] == r"C:\ws\alex-projects.code-workspace"
    assert "--workspace" not in installer.Schedule("n", "py", "s.py", 2).arguments


def test_the_task_limit_outlasts_a_run_and_the_lock_outlasts_the_limit():
    """A killed run leaves its lock; the next fire breaks it once it is past the limit."""
    assert installer.TIME_LIMIT == "PT15M"
    limit = 15 * 60
    assert limit > runner.WINDOW_SECONDS
    assert runner.ship_step.SHIP_LOCK_STALE > limit
    assert f"<ExecutionTimeLimit>{installer.TIME_LIMIT}</ExecutionTimeLimit>" in (
        installer.task_document(schedule())
    )


def test_the_document_is_logged_and_lands_disabled_when_stood_down(monkeypatch):
    document = installer.task_document(schedule())
    assert "log-wrap.py" in document and "--always" in document
    assert "<Enabled>true</Enabled>" in document
    monkeypatch.setattr(installer.harness_state, "stood_down", lambda: {installer.TASK_NAME})
    assert "<Enabled>false</Enabled>" in installer.task_document(schedule())


def test_the_real_checkout_resolves_to_this_ones_runner():
    resolved = installer.schedule_for(root=REPO_ROOT)
    assert resolved.script == str((REPO_ROOT / "scripts" / "ship-pass.py").resolve())
    assert resolved.every == installer.DEFAULT_INTERVAL


@pytest.mark.parametrize("every", [1, 2, 1440])
def test_a_whole_number_of_minutes_is_an_interval(every):
    assert installer.valid_interval(every)


@pytest.mark.parametrize("every", [0, -1, 1441, True])
def test_anything_else_is_refused(every):
    assert not installer.valid_interval(every)


def test_the_plan_says_what_a_fire_does():
    plan = installer.render_plan(schedule())
    assert "every 2 minute(s)" in plan and "ship-pass.py" in plan and "-X utf8" in plan
    assert "devkit.fixPass" in plan


def test_the_parser_keeps_yes_apart_from_the_verbs():
    args = installer.build_parser().parse_args(["--uninstall", "--yes"])
    assert args.uninstall and args.yes and args.every == installer.DEFAULT_INTERVAL


def test_the_runner_hands_back_the_process(monkeypatch):
    done = installer.run_command(["python", "-c", "print('ok')"])
    assert done.returncode == 0 and done.stdout.strip() == "ok"


def test_the_bare_invocation_registers_nothing_and_says_so(monkeypatch, capsys):
    monkeypatch.setattr(installer, "WINDOWS", True)

    def never(*_args):
        raise AssertionError("a plan must not reach the scheduler")

    monkeypatch.setattr(installer.devkit_schtasks, "register", never)
    assert installer.main([]) == 0
    out = capsys.readouterr().out
    assert "Nothing was registered" in out and "every 2 minute(s)" in out


def test_off_windows_it_says_so_and_exits_clean(monkeypatch, capsys):
    monkeypatch.setattr(installer, "WINDOWS", False)
    assert installer.main(["--yes"]) == 0
    assert "Windows-only" in capsys.readouterr().out


def test_yes_from_a_temporary_checkout_is_refused(monkeypatch, tmp_path, capsys):
    """The registered command carries the checkout's path verbatim."""
    monkeypatch.setattr(installer, "WINDOWS", True)
    monkeypatch.setattr(installer.sweep, "source_checkout", lambda root: tmp_path)
    assert installer.main(["--yes"]) == 2
    assert "temporary checkout" in capsys.readouterr().err
