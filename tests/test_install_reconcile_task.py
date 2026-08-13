"""Tests for the scheduled-task installer.

What matters here is the *command string*, because nothing re-reads it: once
`schtasks` has it, it runs every 15 minutes with `--yes` for as long as the
workstation exists. A wrong flag baked in at install time is a wrong flag forever, and
the two that decide blast radius are `--merge` (does it touch PRs at all) and
`--workspace` (which set of boxes is it reconciling).
"""

from __future__ import annotations

from pathlib import Path

from support import load_script

installer = load_script("scripts/install-reconcile-task.py")

PY = r"C:\py\python.exe"
SCRIPT = Path(r"C:\ws\devkit\scripts\worktree.py")
WORKSPACE = Path(r"C:\ws\alex-projects.code-workspace")


def command(**kwargs) -> str:
    return installer.reconcile_arguments(SCRIPT, WORKSPACE, **kwargs)


def test_the_scheduled_run_applies_rather_than_dry_running():
    """A cleanup that prints a plan into a scheduler's void does nothing at all."""
    assert "--yes" in command()


def test_merging_is_off_unless_asked_for():
    """The default has to be the safe one: this runs unattended, forever."""
    assert "--no-merge" in command()
    assert "--merge " not in command() + " "


def test_merging_can_be_turned_on_explicitly():
    assert "--merge" in command(automerge=True)
    assert "--no-merge" not in command(automerge=True)


def test_the_workspace_is_named_not_inferred():
    """A scheduled task starts in system32; leaving the default relies on the checkout
    never moving, and a moved checkout should fail loudly rather than reconcile
    whatever it happens to find."""
    assert "--workspace" in command()
    assert str(WORKSPACE) in command()


def test_paths_are_quoted_for_a_profile_name_with_spaces():
    quoted = installer.reconcile_arguments(Path(r"C:\Program Files\ws\worktree.py"), WORKSPACE)
    assert '"C:\\Program Files\\ws\\worktree.py"' in quoted


def test_the_static_checkouts_are_swept_by_default():
    """The whole reason a workspace with no boxes still wants this task installed: a
    merged PR has to advance the local default branch without anyone remembering."""
    assert "--checkouts" in command()
    assert "--no-checkouts" not in command()


def test_the_checkout_sweep_can_be_scheduled_off():
    assert "--no-checkouts" in command(checkouts=False)


def test_a_disk_floor_is_passed_only_when_set():
    assert "--min-free-gb" not in command()
    assert "--min-free-gb 40.0" in command(min_free_gb=40.0)


def test_the_interval_is_minutes_not_days():
    """The tier's promise is that a merged PR stops costing disk within minutes."""
    assert "<Interval>PT15M</Interval>" in installer.task_document(PY, "args", 15)


def test_the_scheduled_task_runs_on_battery_and_catches_up():
    """This task was found stopped for five days. `schtasks /SC MINUTE` cannot express
    any of these three, which is the whole reason it is registered from a document."""
    body = installer.task_document(PY, "args", 15)
    assert "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>" in body
    assert "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>" in body
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in body


def test_the_interpreter_is_the_action_not_part_of_the_arguments():
    """`<Exec>` splits the program from its arguments; folding them into one string
    registers a task whose program is a path with a space in it."""
    body = installer.task_document(PY, "worktree.py reconcile", 15)
    assert f"<Command>{PY}</Command>" in body
    assert "<Arguments>worktree.py reconcile</Arguments>" in body


def test_uninstall_names_the_task_and_does_not_prompt():
    argv = installer.uninstall_argv("devkit-worktree-reconcile")
    assert argv[:2] == ["schtasks", "/delete"]
    assert "devkit-worktree-reconcile" in argv
    assert "/f" in argv


def test_a_dry_run_never_calls_schtasks(monkeypatch, capsys):
    monkeypatch.setattr(installer.os, "name", "nt")
    monkeypatch.setattr(
        installer, "_run", lambda argv: (_ for _ in ()).throw(AssertionError("called schtasks"))
    )
    assert installer.main([]) == 0
    assert "Dry run" in capsys.readouterr().out


def test_a_non_windows_machine_is_a_no_op_not_a_failure(monkeypatch, capsys):
    monkeypatch.setattr(installer.os, "name", "posix")
    assert installer.main(["--yes"]) == 0
    assert "Windows-only" in capsys.readouterr().out


def test_the_scheduled_run_uses_the_windowless_interpreter(tmp_path):
    """A console window stealing focus every 15 minutes gets the task deleted, which
    silently removes the workspace's only automatic cleanup."""
    (tmp_path / "python.exe").write_bytes(b"")
    (tmp_path / "pythonw.exe").write_bytes(b"")
    assert installer.windowless(str(tmp_path / "python.exe")).endswith("pythonw.exe")


def test_a_missing_pythonw_falls_back_rather_than_breaking_the_task(tmp_path):
    """A visible window beats no scheduler at all."""
    lone = tmp_path / "python.exe"
    lone.write_bytes(b"")
    assert installer.windowless(str(lone)) == str(lone)
