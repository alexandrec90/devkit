"""Tests for the daily VanillaLand trunk-merge installer.

Like its two siblings, the thing under test is the **command string**: nothing re-reads
it once `schtasks` has it, so a flag missing at install time is missing every morning
until someone re-installs.

Two properties here are specific to this job rather than inherited from the pattern, and
both are about the target being a *reference* checkout with a human's work in it:

- it addresses VanillaLand by its **registry name**, since `git-merge-default.py`
  resolves `--checkout` against the workspace file and refuses a name that is not in it;
- the base branch is **pinned**, because a detection that goes wrong here merges the
  wrong trunk into somebody's long-lived branch, while a pin that goes wrong merges
  nothing and says so.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

from support import load_script

installer = load_script("scripts/install-vanillaland-merge.py")
log_wrap = load_script("scripts/log-wrap.py")
merge = load_script("scripts/git-merge-default.py")

PY = r"C:\py\pythonw.exe"
ROOT = Path(r"C:\ws\devkit")


def command(**kwargs) -> str:
    return installer.merge_arguments(PY, root=ROOT, **kwargs)


def test_the_target_is_named_the_way_the_registry_spells_it():
    """`git-merge-default.py` resolves `--checkout` against the workspace file, so this
    is a registry name and not a path -- a mismatch is exit 2, every morning."""
    tokens = shlex.split(command(), posix=False)
    assert tokens[tokens.index("--checkout") + 1] == installer.CHECKOUT
    assert installer.CHECKOUT == "VanillaLand"


def test_the_base_branch_is_pinned_rather_than_auto_detected():
    """`auto` is right for the picker, which must work in seven checkouts. Unattended,
    the two failure modes are not symmetric: a wrong pin merges nothing and reports it,
    a wrong detection merges `main` into a human's long-lived branch."""
    tokens = shlex.split(command(), posix=False)
    assert tokens[tokens.index("--base") + 1] == "develop"
    assert installer.DEFAULT_BASE != merge.AUTO


def test_the_base_can_still_be_overridden_to_auto():
    """The pin is a default, not a wall -- a checkout whose trunk gets renamed should not
    need a code change to keep merging."""
    tokens = shlex.split(command(base=merge.AUTO), posix=False)
    assert tokens[tokens.index("--base") + 1] == merge.AUTO


def test_the_workspace_is_named_in_the_registered_command():
    """The registered command line is the only record of what a scheduled task operates
    on; a reader of `schtasks /Query` should not have to know a default to know that."""
    assert str(installer.sweep.default_workspace(ROOT)) in command()


def test_every_run_is_wrapped_so_it_leaves_an_account_of_itself():
    """`pythonw.exe` sends stdout nowhere. `--always` because the *quiet* outcome is the
    interesting one here: "already up to date" and "the job stopped running" are the same
    empty file without it."""
    assert str(installer.wrapper_script(ROOT)) in command()
    assert "--always" in command()


def test_the_wrapper_comes_before_the_separator_and_the_job_after_it():
    """Order is the whole meaning of the argv: swap them and `log-wrap` becomes the thing
    being logged."""
    before, _, after = command().partition(" -- ")
    assert "log-wrap.py" in before
    assert "git-merge-default.py" in after
    assert "git-merge-default.py" not in before


def test_the_artifact_path_is_the_one_the_label_produces():
    """`ARTIFACT` is what `schedule_health` points a reader at; `LABEL` is what decides
    where the file actually lands."""
    assert installer.ARTIFACT == f"logs/{log_wrap.slug(installer.LABEL)}.log"


def test_the_scheduled_label_is_not_the_vs_code_task_label():
    """They would otherwise share an artifact, and a click on any checkout would overwrite
    the only record of last night's unattended run."""
    assert (
        installer.ARTIFACT
        != f"logs/{log_wrap.slug('Git: Merge Origin Default into Current Branch')}.log"
    )


def test_the_inner_interpreter_is_windowless_too():
    """Windows allocates a console window for a console-subsystem child of a
    GUI-subsystem parent even with every handle redirected."""
    _, _, after = command().partition(" -- ")
    assert after.startswith(f'"{PY}"')


def test_paths_are_quoted_for_a_profile_name_with_spaces():
    """Each path must survive as *one* argv entry despite the space, so the assertion
    tokenises rather than matching a literal -- `Path` joins with the running platform's
    separator, and a literal would hold here and fail on the Linux runner."""
    root = Path(r"C:\Program Files\ws\devkit")
    tokens = shlex.split(installer.merge_arguments(PY, root=root), posix=False)
    assert f'"{installer.merge_script(root)}"' in tokens
    assert f'"{installer.wrapper_script(root)}"' in tokens


def test_the_task_runs_in_devkit_so_its_log_is_findable():
    """`log-wrap` resolves `logs/` from the cwd and a scheduled task's cwd is `system32`.
    The cwd is devkit rather than VanillaLand: the merge takes its target by name, and
    `schedule_health.ARTIFACTS` resolves its paths against this checkout."""
    document = installer.task_document(PY, "args", "05:00", root=ROOT)
    assert f"<WorkingDirectory>{ROOT}</WorkingDirectory>" in document


def test_the_job_is_daily_at_the_hour_given():
    document = installer.task_document(PY, "args", "05:00", root=ROOT)
    assert "<StartBoundary>2020-01-01T05:00:00</StartBoundary>" in document
    assert "<ScheduleByDay>" in document


def test_it_runs_after_the_other_two_nightly_jobs():
    """Three `pythonw` jobs starting together on a laptop that has just woken is how one
    of them times out, and this is the one that touches a tree a human then opens."""
    prune = load_script("scripts/install-docker-prune.py")
    upgrade = load_script("scripts/install-upgrade-schedule.py")
    assert installer.DEFAULT_AT > prune.DEFAULT_AT > upgrade.DEFAULT_TIME


def test_the_job_inherits_the_laptop_settings_every_devkit_task_gets():
    document = installer.task_document(PY, "args", "05:00", root=ROOT)
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
    # The plan names the artifact -- "where does this report to" is the question a reader
    # has about an unattended job -- and states the two things about this one that would
    # otherwise surprise someone: it commits, and a conflict is left in the tree.
    assert installer.ARTIFACT in printed
    assert "NEVER pushes" in printed
    assert "IN PROGRESS" in printed


def test_installing_from_an_ephemeral_box_is_refused(monkeypatch, capsys):
    """The command carries the checkout path verbatim, and `reconcile` destroys boxes --
    so this would install a task that works until the next reconcile pass and then fails
    every morning, in silence, forever."""
    monkeypatch.setattr(installer, "WINDOWS", True)
    monkeypatch.setattr(
        installer, "REPO_ROOT", Path(r"C:\ws") / installer.sweep.BOXES_DIR_NAME / "devkit--x"
    )
    assert installer.main(["--yes"]) == 2
    assert "ephemeral box" in capsys.readouterr().err


def test_devkit_points_the_registered_command_at_another_checkout(monkeypatch, capsys):
    """The escape from the refusal above. Every path in the registered command belongs to
    the *named* checkout, not to the one the installer is being run from -- otherwise the
    flag would look like it worked and register the box's paths anyway."""
    monkeypatch.setattr(installer, "WINDOWS", True)
    monkeypatch.setattr(installer, "_run_argv", _refuse_to_run)
    monkeypatch.setattr(
        installer, "REPO_ROOT", Path(r"C:\ws") / installer.sweep.BOXES_DIR_NAME / "devkit--x"
    )
    # A checkout that is genuinely not the running one. Naming the real `ROOT` made the
    # test contradict itself the moment the suite ran from a box -- which is now the
    # normal way this repo is worked on: the first assertion demanded a path containing
    # `.worktrees` and the second forbade one.
    static = Path(r"C:\ws\devkit")
    assert installer.main(["--devkit", str(static)]) == 0
    printed = capsys.readouterr().out
    assert str(installer.merge_script(static)) in printed
    assert installer.sweep.BOXES_DIR_NAME not in printed


def test_the_interpreter_comes_from_the_named_checkout(tmp_path):
    """The half `--devkit` missed. Every *script* path moved to the named checkout while
    the interpreter stayed `sys.executable`, so installing from a box registered the
    box's own `.venv\\Scripts\\pythonw.exe` -- which reconcile deletes when the PR merges.
    The task then fails every morning, in silence, which is exactly what `--devkit`
    exists to avoid."""
    venv = tmp_path / ".venv" / ("Scripts" if installer.WINDOWS else "bin")
    venv.mkdir(parents=True)
    named = venv / ("python.exe" if installer.WINDOWS else "python")
    named.write_text("", encoding="utf-8")
    assert installer.interpreter(tmp_path) == str(named)


def test_a_named_checkout_without_a_virtualenv_never_yields_a_box_interpreter(monkeypatch):
    """The remaining hole: `--devkit` names a checkout whose tools are on PATH, so there
    is no virtualenv to point at, and falling back to the running one would re-register
    the box's python after all. `sys._base_executable` is the one interpreter in reach
    that outlives every box."""
    box_python = Path(r"C:\ws") / installer.sweep.BOXES_DIR_NAME / "d--x" / ".venv/python.exe"
    monkeypatch.setattr(sys, "executable", str(box_python))
    monkeypatch.setattr(sys, "_base_executable", r"C:\Python314\python.exe", raising=False)
    assert installer.interpreter(Path(r"C:\ws\devkit")) == r"C:\Python314\python.exe"


def test_a_checkout_without_a_virtualenv_keeps_the_running_interpreter(tmp_path, monkeypatch):
    """Outside a box there is nothing wrong with the running interpreter, and a task
    pointing at a python that does not exist is worse than one pointing at this one."""
    monkeypatch.setattr(sys, "executable", r"C:\Python314\python.exe")
    assert installer.interpreter(tmp_path) == r"C:\Python314\python.exe"


def test_installing_against_a_checkout_with_no_merge_script_is_refused(monkeypatch, capsys):
    """A mistyped `--devkit` would otherwise register a task that runs nothing, every
    morning, and reports it only as an exit code."""
    monkeypatch.setattr(installer, "WINDOWS", True)
    monkeypatch.setattr(installer, "_run_argv", _refuse_to_run)
    assert installer.main(["--devkit", str(ROOT), "--yes"]) == 2
    assert "no merge script" in capsys.readouterr().err


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
