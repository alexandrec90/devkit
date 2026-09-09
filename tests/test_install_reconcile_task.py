"""Tests for the scheduled-task installer.

What matters here is the *command string*, because nothing re-reads it: once
`schtasks` has it, it runs every 15 minutes with `--yes` for as long as the
workstation exists. A wrong flag baked in at install time is a wrong flag forever, and
the two that decide blast radius are `--merge` (does it touch PRs at all) and
`--workspace` (which set of boxes is it reconciling).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import pytest
from support import REPO_ROOT, load_script, sweep

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


def test_merging_is_label_gated_by_default():
    """`--merge` alone would merge ANY green PR, which turns every agent PR into a
    self-approving one. The default label keeps the aggressive mode scoped to PRs
    something explicitly marked routine (`automerge`), so the label is the review."""
    assert f"--merge-label {installer.sweep.AUTOMERGE_LABEL}" in command(automerge=True)


def test_the_label_gate_can_be_dropped_or_renamed():
    assert "--merge-label" not in command(automerge=True, merge_label="")
    assert "--merge-label trusted" in command(automerge=True, merge_label="trusted")


def test_no_merge_label_is_passed_when_merging_is_off():
    # `worktree.py reconcile --no-merge --merge-label x` would be a contradiction in
    # the one string `schtasks /query` shows a human debugging the task.
    assert "--merge-label" not in command()


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
    monkeypatch.setattr(installer, "WINDOWS", True)
    monkeypatch.setattr(
        installer, "_run", lambda argv: (_ for _ in ()).throw(AssertionError("called schtasks"))
    )
    assert installer.main([]) == 0
    assert "Dry run" in capsys.readouterr().out


def test_the_parser_is_read_only_and_merge_free_by_default():
    """The two knobs that decide blast radius both default to the safe side."""
    args = installer.build_parser().parse_args([])
    assert args.apply is False and args.automerge is False and args.checkouts is True


def test_status_and_uninstall_are_answered_before_any_document_is_built(monkeypatch, capsys):
    """`query_or_remove` owns the two modes that ask the scheduler about the task by
    name; neither should build a document, and neither exists when nobody asked."""
    monkeypatch.setattr(installer, "WINDOWS", True)
    monkeypatch.setattr(installer, "_run", lambda argv: (0, f"ran {argv[1]}"))
    assert installer.main(["--status"]) == 0
    assert "ran /query" in capsys.readouterr().out
    assert installer.main(["--uninstall"]) == 0
    assert "Dry run" in capsys.readouterr().out
    assert installer.query_or_remove(argparse.Namespace(status=False, uninstall=False)) is None


def _the_document_main_would_register() -> str:
    """Built the way `main` builds it: this interpreter, this checkout, the defaults."""
    python = installer.windowless(sys.executable)
    workspace = sweep.default_workspace(REPO_ROOT).resolve()
    arguments = installer.reconcile_arguments(installer.worktree_script(), workspace)
    return installer.task_document(python, arguments, installer.DEFAULT_INTERVAL_MINUTES)


def test_check_is_green_when_the_scheduler_holds_the_document_yes_would_register(
    monkeypatch, capsys
):
    monkeypatch.setattr(installer, "WINDOWS", True)
    document = _the_document_main_would_register()
    monkeypatch.setattr(
        installer,
        "_run_argv",
        lambda argv: subprocess.CompletedProcess(list(argv), 0, document, ""),
    )
    assert installer.main(["--check"]) == 0
    assert installer.TASK_NAME in capsys.readouterr().out


def test_check_sees_an_option_the_registered_task_carries_and_this_run_does_not(
    monkeypatch, capsys
):
    """`--merge` on the registered task and not on the command line is drift -- which is
    why `installers.py` passes `devkit.installers` options to `--check` and `--yes` alike."""
    monkeypatch.setattr(installer, "WINDOWS", True)
    document = _the_document_main_would_register()
    monkeypatch.setattr(
        installer,
        "_run_argv",
        lambda argv: subprocess.CompletedProcess(list(argv), 0, document, ""),
    )
    assert installer.main(["--check", "--merge"]) == 1
    assert "--merge" in capsys.readouterr().err


def test_check_is_red_when_nothing_is_registered(monkeypatch, capsys):
    monkeypatch.setattr(installer, "WINDOWS", True)
    monkeypatch.setattr(
        installer, "_run_argv", lambda argv: subprocess.CompletedProcess(list(argv), 1, "", "ERROR")
    )
    assert installer.main(["--check"]) == 1
    assert "nothing is scheduled" in capsys.readouterr().err


def _box_root(tmp_path: Path) -> Path:
    """A path shaped like an ephemeral box, on whichever OS is running the test.

    Built with `/` rather than written as a Windows literal. `Path(r"C:\\ws\\.worktrees\\b")`
    is a *single* path component on Linux -- backslash is an ordinary character there --
    so `BOXES_DIR_NAME in root.parts` was false and this suite passed on Windows while
    the same assertion failed in CI.
    """
    return tmp_path / installer.sweep.BOXES_DIR_NAME / "devkit--topic-0813"


def test_installing_from_an_ephemeral_box_is_refused(monkeypatch, capsys, tmp_path):
    """The task would carry the box's path verbatim, and the pass it schedules destroys
    boxes -- so it works until the next reconcile, then fails silently every fifteen
    minutes forever. The sibling installer already refused this; this one did not."""
    monkeypatch.setattr(installer, "WINDOWS", True)
    monkeypatch.setattr(installer, "REPO_ROOT", _box_root(tmp_path))
    monkeypatch.setattr(
        installer, "_run_argv", lambda argv: pytest.fail("registered a task from a box")
    )

    assert installer.main(["--yes"]) == 2
    assert "ephemeral box" in capsys.readouterr().err


def test_installing_from_a_cli_worktree_is_refused_too(monkeypatch, capsys, tmp_path):
    """`claude --worktree` cuts its checkout under `.claude/worktrees/`, not `.worktrees/`,
    and it is deleted the same way when the branch lands. The old `BOXES_DIR_NAME in
    REPO_ROOT.parts` test waved it through, so an agent standing in one registered a pass
    whose path died with the branch -- every fifteen minutes, in silence."""
    monkeypatch.setattr(installer, "WINDOWS", True)
    worktree = tmp_path / "devkit" / ".claude" / "worktrees" / "modular-watching-starfish"
    monkeypatch.setattr(installer, "REPO_ROOT", worktree)
    monkeypatch.setattr(
        installer, "_run_argv", lambda argv: pytest.fail("registered a task from a worktree")
    )

    assert installer.main(["--yes"]) == 2
    assert "temporary checkout" in capsys.readouterr().err


def test_reading_the_plan_from_a_box_still_works(monkeypatch, capsys, tmp_path):
    """The read-only mode is most often invoked from a box -- that is where an agent
    is, and refusing it would leave nothing to read before moving."""
    monkeypatch.setattr(installer, "WINDOWS", True)
    monkeypatch.setattr(installer, "REPO_ROOT", _box_root(tmp_path))
    assert installer.main([]) == 0
    assert "Dry run" in capsys.readouterr().out


def test_a_non_windows_machine_is_a_no_op_not_a_failure(monkeypatch, capsys):
    monkeypatch.setattr(installer, "WINDOWS", False)
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
