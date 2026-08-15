"""Tests for the nightly docker-prune installer.

Like `test_install_reconcile_task.py`, the thing under test is the **command string**:
nothing re-reads it once `schtasks` has it, so a flag missing at install time is missing
every night until someone re-installs.

Two of these are regression tests for the task that was found already registered on this
machine, by hand, with neither: it pruned without `--idle-only` (so an unattended 04:00
run could stop a running stack to reclaim disk) and it wrapped nothing (so its exit 1
left no account of itself anywhere).
"""

from __future__ import annotations

import shlex
from pathlib import Path

from support import load_script

installer = load_script("scripts/install-docker-prune.py")
log_wrap = load_script("scripts/log-wrap.py")

PY = r"C:\py\pythonw.exe"
ROOT = Path(r"C:\ws\devkit")


def command(**kwargs) -> str:
    return installer.prune_arguments(PY, root=ROOT, **kwargs)


def test_the_unattended_run_declines_rather_than_stopping_a_running_stack():
    """`generic_prune`'s own docstring calls this the scheduled caller's spelling: the
    compaction half needs `wsl --shutdown`, which kills every container. The registered
    task did not pass it."""
    assert "--idle-only" in command()


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
    console window from Windows even with every handle redirected -- a window flashing
    up at 04:00, which is what made these jobs windowless in the first place."""
    _, _, after = command().partition(" -- ")
    assert after.startswith(f'"{PY}"')


def test_paths_are_quoted_for_a_profile_name_with_spaces():
    """The property is that each path survives as *one* argv entry despite the space, so
    the assertion tokenises rather than matching a literal. A literal is what this test
    had first, and it read `...\\devkit\\scripts\\...`: `Path` joins with the separator of
    the platform running the test, so the whole of it held on this Windows machine and
    none of it on the Linux runner, which is a red PR gate for a test the code passes.
    """
    root = Path(r"C:\Program Files\ws\devkit")
    tokens = shlex.split(installer.prune_arguments(PY, root=root), posix=False)
    assert f'"{installer.maint_script(root)}"' in tokens
    assert f'"{installer.wrapper_script(root)}"' in tokens


def test_the_task_runs_in_the_checkout_so_its_log_is_findable():
    """The other half of the same fix: `log-wrap` resolves `logs/` from the cwd, and a
    scheduled task's cwd is `system32`."""
    document = installer.task_document(PY, "args", "04:00", root=ROOT)
    assert f"<WorkingDirectory>{ROOT}</WorkingDirectory>" in document


def test_the_job_is_daily_at_the_hour_given():
    document = installer.task_document(PY, "args", "04:00", root=ROOT)
    assert "<StartBoundary>2020-01-01T04:00:00</StartBoundary>" in document
    assert "<ScheduleByDay>" in document


def test_the_job_inherits_the_laptop_settings_every_devkit_task_gets():
    """It is registered through `devkit_schtasks`, which is the entire difference
    between this and the hand-registered task it replaces."""
    document = installer.task_document(PY, "args", "04:00", root=ROOT)
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
    # The plan names the artifact: "where does this report to" is the question a reader
    # has about an unattended job, and nothing else on screen answers it.
    assert installer.ARTIFACT in printed


def test_installing_from_an_ephemeral_box_is_refused(monkeypatch, capsys):
    """The command carries the checkout path verbatim, and `reconcile` destroys boxes --
    so this would install a task that works until the next reconcile pass and then fails
    nightly, in silence, forever."""
    monkeypatch.setattr(installer, "WINDOWS", True)
    monkeypatch.setattr(
        installer, "REPO_ROOT", Path(r"C:\ws") / installer.sweep.BOXES_DIR_NAME / "devkit--x"
    )
    assert installer.main(["--yes"]) == 2
    assert "ephemeral box" in capsys.readouterr().err


def test_a_dry_run_from_a_box_still_reads(monkeypatch, capsys):
    """The refusal is scoped to `--yes`. Refusing the read-only mode as well would break
    it in the place an agent invokes it from -- a box."""
    monkeypatch.setattr(installer, "WINDOWS", True)
    monkeypatch.setattr(installer, "_run_argv", _refuse_to_run)
    monkeypatch.setattr(
        installer, "REPO_ROOT", Path(r"C:\ws") / installer.sweep.BOXES_DIR_NAME / "devkit--x"
    )
    assert installer.main([]) == 0
    assert "Dry run" in capsys.readouterr().out
