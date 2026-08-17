"""Tests for the nightly docker-stop-idle installer.

Like `test_install_docker_prune.py`, the thing under test is the **command string**:
nothing re-reads it once `schtasks` has it, so a flag missing at install time is
missing every night until someone re-installs. The behavioural half -- opt-in, the
connection check, the grace window -- lives with `generic_stop_idle` in
`test_docker_maint.py`; here the claims are that the registered command reaches that
pass, leaves an account of itself, and runs on a laptop at all.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from support import load_script

installer = load_script("scripts/install-docker-stop-idle.py")
log_wrap = load_script("scripts/log-wrap.py")

PY = r"C:\py\pythonw.exe"
ROOT = Path(r"C:\ws\devkit")


def command(**kwargs) -> str:
    return installer.stop_idle_arguments(PY, root=ROOT, **kwargs)


def test_the_registered_command_runs_the_stop_idle_mode():
    assert "stop-idle" in command().partition(" -- ")[2]


def test_every_run_is_wrapped_so_it_leaves_an_account_of_itself():
    """`pythonw.exe` sends stdout nowhere. Without the wrapper the only trace of a
    nightly run is an integer in the scheduler."""
    assert str(installer.wrapper_script(ROOT)) in command()
    assert "--always" in command()


def test_the_wrapper_comes_before_the_separator_and_the_job_after_it():
    """Order is the whole meaning of the argv: swap them and `log-wrap` becomes the
    thing being logged."""
    before, _, after = command().partition(" -- ")
    assert "log-wrap.py" in before
    assert "docker-maint.py" in after
    assert "docker-maint.py" not in before


def test_the_artifact_path_is_the_one_the_label_produces():
    """`ARTIFACT` is what `schedule_health` points a reader at; `LABEL` is what decides
    where the file actually lands. A comment claiming they agree is not enough."""
    assert installer.ARTIFACT == f"logs/{log_wrap.slug(installer.LABEL)}.log"


def test_the_inner_interpreter_is_windowless_too():
    """A console-subsystem child spawned from a GUI-subsystem parent gets its own
    console window from Windows even with every handle redirected."""
    _, _, after = command().partition(" -- ")
    assert after.startswith(f'"{PY}"')


def test_paths_are_quoted_for_a_profile_name_with_spaces():
    """The property is that each path survives as *one* argv entry despite the space,
    so the assertion tokenises rather than matching a literal (a literal encodes the
    path separator of whichever platform runs the test)."""
    root = Path(r"C:\Program Files\ws\devkit")
    tokens = shlex.split(installer.stop_idle_arguments(PY, root=root), posix=False)
    assert f'"{installer.maint_script(root)}"' in tokens
    assert f'"{installer.wrapper_script(root)}"' in tokens


def test_the_task_runs_in_the_checkout_so_its_log_is_findable():
    """`log-wrap` resolves `logs/` from the cwd, and a scheduled task's cwd is
    `system32`."""
    document = installer.task_document(PY, "args", "03:30", root=ROOT)
    assert f"<WorkingDirectory>{ROOT}</WorkingDirectory>" in document


def test_the_job_is_daily_at_the_hour_given():
    document = installer.task_document(PY, "args", "03:30", root=ROOT)
    assert "<StartBoundary>2020-01-01T03:30:00</StartBoundary>" in document
    assert "<ScheduleByDay>" in document


def test_the_job_inherits_the_laptop_settings_every_devkit_task_gets():
    """It is registered through `devkit_schtasks`, which is the entire difference
    between this and a hand-registered `schtasks /Create /SC DAILY`."""
    document = installer.task_document(PY, "args", "03:30", root=ROOT)
    assert "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>" in document
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in document
    assert "<RunOnlyIfIdle>false</RunOnlyIfIdle>" in document


def _refuse_to_run(argv):
    raise AssertionError(f"a dry run must not call schtasks, but ran: {argv}")


def test_a_dry_run_prints_the_plan_and_calls_nothing(monkeypatch, capsys):
    monkeypatch.setattr(installer, "WINDOWS", True)
    monkeypatch.setattr(installer, "_run_argv", _refuse_to_run)
    assert installer.main([]) == 0
    printed = capsys.readouterr().out
    assert "Dry run" in printed
    # The plan names the artifact and the opt-in: "what will this stop, and where does
    # it report" are the two questions a reader has about an unattended job.
    assert installer.ARTIFACT in printed
    assert "auto_stop" in printed


def test_installing_from_an_ephemeral_box_is_refused(monkeypatch, capsys):
    """The command carries the checkout path verbatim, and `reconcile` destroys boxes --
    so this would install a task that works until the next reconcile pass and then
    fails nightly, in silence, forever."""
    monkeypatch.setattr(installer, "WINDOWS", True)
    monkeypatch.setattr(
        installer, "REPO_ROOT", Path(r"C:\ws") / installer.sweep.BOXES_DIR_NAME / "devkit--x"
    )
    assert installer.main(["--yes"]) == 2
    assert "ephemeral box" in capsys.readouterr().err


def test_a_dry_run_from_a_box_still_reads(monkeypatch, capsys):
    """The refusal is scoped to `--yes`. Refusing the read-only mode as well would
    break it in the place an agent invokes it from -- a box."""
    monkeypatch.setattr(installer, "WINDOWS", True)
    monkeypatch.setattr(installer, "_run_argv", _refuse_to_run)
    monkeypatch.setattr(
        installer, "REPO_ROOT", Path(r"C:\ws") / installer.sweep.BOXES_DIR_NAME / "devkit--x"
    )
    assert installer.main([]) == 0
    assert "Dry run" in capsys.readouterr().out
