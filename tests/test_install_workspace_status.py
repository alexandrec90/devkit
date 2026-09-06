"""Tests for the daily workspace-status installer.

Like `test_install_docker_prune.py`, the thing under test is the **command string**:
nothing re-reads it once `schtasks` has it, so a flag missing at install time is missing
every day until someone re-installs.

The job this registers exists because `workspace-status.py` was described as a
SessionStart line by every document in the repo and wired to no hook at all. So the two
assertions that matter most here are the ones about *reaching a person*: `--notify`, and
a trigger the health reporter will not read as a job that has never run.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from support import load_script

installer = load_script("scripts/install-workspace-status.py")
log_wrap = load_script("scripts/log-wrap.py")
schedule_health = load_script("scripts/schedule_health.py")

PY = r"C:\py\pythonw.exe"
ROOT = Path(r"C:\ws\devkit")


def command(**kwargs) -> str:
    return installer.status_arguments(PY, root=ROOT, **kwargs)


def test_the_scheduled_pass_notifies_rather_than_only_writing_a_log():
    """Without `--notify` this job reproduces the failure it was written to fix: it runs,
    it writes `logs/scheduled-workspace-status.log`, and nobody has a reason to open it.
    A missing VS Code extension would still be found by clicking a task and reading
    `command 'shellCommand.execute' not found`."""
    assert "--notify" in command()


def test_every_run_is_wrapped_so_it_leaves_an_account_of_itself():
    """`pythonw.exe` sends stdout nowhere. Without the wrapper the only trace of a daily
    run is an integer in the scheduler -- and the report *is* this job's output."""
    assert str(installer.wrapper_script(ROOT)) in command()
    assert "--always" in command()


def test_the_wrapper_comes_before_the_separator_and_the_job_after_it():
    """Order is the whole meaning of the argv: swap them and `log-wrap` becomes the
    thing being logged."""
    before, _, after = command().partition(" -- ")
    assert "log-wrap.py" in before
    assert "workspace-status.py" in after
    assert "workspace-status.py" not in before


def test_the_artifact_path_is_the_one_the_label_produces():
    """`ARTIFACT` is what `schedule_health` points a reader at; `LABEL` is what decides
    where the file actually lands. A comment claiming they agree is not enough."""
    assert installer.ARTIFACT == f"logs/{log_wrap.slug(installer.LABEL)}.log"


def test_the_health_reporter_points_at_that_same_file():
    """The pointer table is the reason a failing job is diagnosable at all, and this job
    is the one that renders it -- an entry it lacks for itself is the reporter unable to
    report on the reporter."""
    assert schedule_health.ARTIFACTS[installer.TASK_NAME] == installer.ARTIFACT


def test_the_inner_interpreter_is_a_console_one():
    """`log-wrap.py` spawns the wrapped command with `CREATE_NO_WINDOW`, which Windows
    **ignores for a GUI-subsystem child** -- so a `pythonw.exe` here would be left with
    no console at all, and every `git` this pass runs across six checkouts would be
    handed a fresh visible one."""
    _, _, after = command().partition(" -- ")
    assert after.startswith('"C:\\py\\python.exe"') or after.startswith(f'"{PY}"')


def test_a_console_interpreter_is_left_alone():
    """`console` is the identity for the interpreter a human installs from, so a machine
    whose Python has no `pythonw.exe` beside it is not silently rewritten."""
    assert installer.console(r"C:\py\python.exe") == r"C:\py\python.exe"


def test_paths_are_quoted_for_a_profile_name_with_spaces():
    """The property is that each path survives as *one* argv entry despite the space, so
    the assertion tokenises rather than matching a literal -- `Path` joins with the
    separator of the platform running the test, and a literal holds on Windows and fails
    on the Linux runner."""
    root = Path(r"C:\Program Files\ws\devkit")
    tokens = shlex.split(installer.status_arguments(PY, root=root), posix=False)
    assert f'"{installer.status_script(root)}"' in tokens
    assert f'"{installer.wrapper_script(root)}"' in tokens


def test_the_task_runs_in_the_checkout_so_its_log_is_findable():
    """`log-wrap` resolves `logs/` from the cwd, and a scheduled task's cwd is
    `system32`."""
    document = installer.task_document(PY, "args", "09:00", root=ROOT)
    assert f"<WorkingDirectory>{ROOT}</WorkingDirectory>" in document


def test_the_job_is_daily_at_the_hour_given():
    document = installer.task_document(PY, "args", "09:00", root=ROOT)
    assert "<StartBoundary>2020-01-01T09:00:00</StartBoundary>" in document
    assert "<ScheduleByDay>" in document


def test_the_job_inherits_the_laptop_settings_every_devkit_task_gets():
    """Registered through `devkit_schtasks`, which is where the three settings live that
    decide whether a job on a laptop runs at all. `StartWhenAvailable` is the one this
    job leans on hardest: a 09:00 fire on a machine that was shut is the ordinary case,
    not the exception."""
    document = installer.task_document(PY, "args", "09:00", root=ROOT)
    assert "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>" in document
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in document
    assert "<RunOnlyIfIdle>false</RunOnlyIfIdle>" in document


def test_the_uninstall_names_the_task_and_forces_it():
    """`/f` is what makes the removal answerable without a console: `schtasks /delete`
    prompts for confirmation otherwise, and this installer's parent may well be a
    scheduled task itself."""
    assert installer.uninstall_argv("devkit-workspace-status") == [
        "schtasks",
        "/delete",
        "/tn",
        "devkit-workspace-status",
        "/f",
    ]


def test_the_status_query_asks_about_this_task_only():
    """`schtasks /query` with no `/tn` dumps every task on the machine, which is a
    hundred lines of Windows' own jobs for a question about one of ours."""
    assert installer.query_argv("devkit-workspace-status") == [
        "schtasks",
        "/query",
        "/tn",
        "devkit-workspace-status",
    ]


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
    daily, in silence, forever."""
    monkeypatch.setattr(installer, "WINDOWS", True)
    monkeypatch.setattr(
        installer, "REPO_ROOT", Path(r"C:\ws") / installer.sweep.BOXES_DIR_NAME / "devkit--x"
    )
    assert installer.main(["--yes"]) == 2
    assert "temporary checkout" in capsys.readouterr().err


def test_installing_from_a_cli_worktree_is_refused_too(monkeypatch, capsys):
    """The refusal the five older installers miss. Their guard looks for `.worktrees/`
    and a `claude --worktree` checkout lives under `.claude/worktrees/`, so an agent sent
    to wire this up is standing in exactly the directory the narrow test waves through --
    and the task it would register dies with the branch. `sweep.source_checkout` resolves
    both shapes, which is why it is what this asks.
    """
    monkeypatch.setattr(installer, "WINDOWS", True)
    worktree = Path(r"C:\ws\devkit") / ".claude" / "worktrees" / "modular-watching-starfish"
    monkeypatch.setattr(installer, "REPO_ROOT", worktree)
    monkeypatch.setattr(installer.sweep, "source_checkout", lambda root: Path(r"C:\ws\devkit"))
    assert installer.main(["--yes"]) == 2
    assert "temporary checkout" in capsys.readouterr().err


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


def test_off_windows_it_says_so_and_does_nothing(monkeypatch, capsys):
    """A POSIX machine running devkit is supported, and there is no scheduler to install
    into. Exiting 0 keeps it out of the way of whatever ran it."""
    monkeypatch.setattr(installer, "WINDOWS", False)
    monkeypatch.setattr(installer, "_run_argv", _refuse_to_run)
    assert installer.main(["--yes"]) == 0
    assert "Windows-only" in capsys.readouterr().out
