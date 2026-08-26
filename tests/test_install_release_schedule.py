"""Tests for scripts/install-release-schedule.py.

Two things here are worth more than the usual installer coverage, because this is the
one devkit job that merges a pull request.

The first is the **nesting**: `pythonw.exe log-wrap.py -- python.exe pipeline`. Get the
two interpreters the wrong way round and the job either puts a console on the desktop at
2am or -- the failure that actually happened to a sibling job -- puts dozens of them
there, one per `git`, because `CREATE_NO_WINDOW` is ignored for a GUI-subsystem child
and console-lessness propagates.

The second is that the registered argv still carries `--if-needed`. That flag is the
entire difference between "release what consumers cannot reach" and "tag every merge",
and it lives next to `--yes` in one tuple that a future edit could trim by half.
"""

import subprocess
import sys
from pathlib import Path

import pytest
from support import REPO_ROOT, load_script

sched = load_script("scripts/install-release-schedule.py")

WINDOWS_PYTHON = r"C:\py\python.exe"


def a_schedule(**overrides):
    base = {
        "name": sched.TASK_NAME,
        "python": WINDOWS_PYTHON,
        "root": Path(r"C:\ws\devkit"),
        "at": "02:00",
        "workspace": r"C:\ws\alex.code-workspace",
    }
    return sched.Schedule(**{**base, **overrides})


# --- what gets registered ------------------------------------------------------


def test_the_scheduled_command_releases_rather_than_planning_it():
    """`release-pipeline.py` defaults to a dry run, so a scheduled invocation without
    `--yes` would print the same plan into the same log every night forever."""
    assert "--yes" in a_schedule().arguments


def test_the_scheduled_command_keeps_the_predicate():
    """Without `--if-needed` this becomes "tag every merge to main": a release plus an
    adoption PR in every consumer for a night whose only change was a docstring."""
    assert "--if-needed" in a_schedule().arguments


def test_the_run_is_wrapped_so_the_night_leaves_a_record():
    """`pythonw.exe` has no stdout at all. Without the wrapper the entire account of a
    release that merged a PR and pushed a tag would be an integer in `schtasks`."""
    arguments = a_schedule().arguments
    wrapper, rest = arguments.split(" --always ", 1)
    assert wrapper.endswith('log-wrap.py"')
    assert rest.startswith(f'"{sched.LABEL}" -- ')


def test_the_artifact_is_the_one_the_label_slugs_to():
    log_wrap = load_script("scripts/log-wrap.py")
    assert sched.ARTIFACT == f"logs/{log_wrap.slug(sched.LABEL)}.log"


def test_the_workspace_is_named_rather_than_left_to_a_default():
    """The registered command is the only record of what the adoption pass will reach,
    and a scheduled task has no cwd worth relying on."""
    assert a_schedule().arguments.endswith('--workspace "C:\\ws\\alex.code-workspace"')


def test_a_checkout_with_no_workspace_beside_it_omits_the_flag():
    """An empty string would reach argparse as a stray positional, not as an absence."""
    assert "--workspace" not in a_schedule(workspace="").arguments


# --- the two interpreters ------------------------------------------------------


def test_the_task_itself_runs_the_windowless_interpreter(tmp_path):
    """The one Windows launches. A console `python.exe` here is a black window that
    appears and vanishes on its own at 2am, nightly."""
    (tmp_path / "pythonw.exe").write_text("", encoding="utf-8")
    console_exe = tmp_path / "python.exe"
    console_exe.write_text("", encoding="utf-8")
    assert sched.windowless(str(console_exe)) == str(tmp_path / "pythonw.exe")


def test_the_wrapped_interpreter_is_the_console_one(tmp_path):
    """The inverse, and not a matter of taste: `log-wrap.py` spawns this one with
    `CREATE_NO_WINDOW`, which Windows ignores for a GUI-subsystem child -- so a
    `pythonw.exe` here would be console-*less*, and every `gh` the pipeline runs would
    be handed a fresh visible console."""
    windowless_exe = tmp_path / "pythonw.exe"
    windowless_exe.write_text("", encoding="utf-8")
    (tmp_path / "python.exe").write_text("", encoding="utf-8")
    assert sched.console(str(windowless_exe)) == str(tmp_path / "python.exe")


def test_a_venv_interpreter_resolves_to_the_base_install(tmp_path):
    """The half that is not "find the file next door": inside a virtualenv,
    `pythonw.exe` is a stub deferring to the base named in `pyvenv.cfg`, and uv's spawns
    that base as a *child* -- which is exactly what Windows hands a new console to. So
    this delegates to `devkit_schtasks.windowless` rather than resolving beside the
    interpreter it was given."""
    base = tmp_path / "base"
    base.mkdir()
    (base / "pythonw.exe").write_text("", encoding="utf-8")
    scripts = tmp_path / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "pythonw.exe").write_text("", encoding="utf-8")
    (scripts / "python.exe").write_text("", encoding="utf-8")
    (tmp_path / "venv" / "pyvenv.cfg").write_text(f"home = {base}\n", encoding="utf-8")
    assert sched.windowless(str(scripts / "python.exe")) == str(base / "pythonw.exe")


def test_each_interpreter_helper_falls_back_rather_than_failing():
    """A POSIX machine has neither name, and not every Windows layout ships both."""
    assert sched.windowless("/usr/bin/python3") == "/usr/bin/python3"
    assert sched.console("/usr/bin/python3") == "/usr/bin/python3"


def test_the_wrapped_interpreter_is_console_even_when_the_task_is_not(tmp_path):
    """Both halves at once, on one schedule: this is the pairing the flicker came from."""
    (tmp_path / "pythonw.exe").write_text("", encoding="utf-8")
    (tmp_path / "python.exe").write_text("", encoding="utf-8")
    resolved = a_schedule(python=str(tmp_path / "python.exe"))
    assert resolved.command.startswith(f'"{tmp_path / "pythonw.exe"}" ')
    assert f'"{tmp_path / "python.exe"}"' in resolved.arguments
    assert "pythonw.exe" not in resolved.arguments


def test_the_interpreter_is_this_one_not_a_bare_python():
    """A scheduled task runs with no activated virtualenv and often a different PATH,
    and `release-pipeline.py` imports `release`, `sweep` and `task_branch` from beside
    it.

    Lowercased first, because `sys.executable` keeps whatever casing the launcher used
    and Windows does not care which: a run started through PATHEXT reports
    `C:\\Python314\\python.EXE`, which this read as a bare name and failed. The claim is
    about the *file*, not its spelling — and it failed only under the Stop hook, where
    a red suite reads as the change under test having broken something."""
    resolved = sched.schedule_for(root=REPO_ROOT).python.lower()
    assert resolved.endswith(("pythonw.exe", "python.exe", "python", "python3"))


# --- the task document ---------------------------------------------------------


def test_the_document_sets_a_working_directory():
    """`log-wrap.py` resolves `logs/` from the cwd, and a scheduled task's cwd is
    `system32`. Without this the job's only record is written where nobody looks."""
    document = sched.task_document(a_schedule())
    assert r"<WorkingDirectory>C:\ws\devkit</WorkingDirectory>" in document


def test_the_document_allows_for_two_gates_but_is_still_finite():
    """Most of this job's wall clock is spent waiting on GitHub. `IgnoreNew` means a
    wedged run suppresses every later one, so "no limit" turns one bad night into a
    permanently dead job."""
    assert "<ExecutionTimeLimit>PT3H</ExecutionTimeLimit>" in sched.task_document(a_schedule())


def test_the_document_fires_at_the_hour_it_was_given():
    assert "02:00:00" in sched.task_document(a_schedule())


def test_it_runs_an_hour_before_the_adoption_pass():
    """The pipeline ends by opening every consumer's adoption PR, so a release cut after
    the upgrade pass would sit undelivered for a day. Read off the sibling installer
    rather than restated, so moving either time fails here instead of drifting."""
    upgrade = load_script("scripts/install-upgrade-schedule.py")
    assert sched.DEFAULT_AT < upgrade.DEFAULT_TIME


def test_the_crontab_line_is_the_same_command_at_the_same_time():
    line = sched.crontab_line(a_schedule())
    assert line.startswith("0 2 * * * ")
    assert "release-pipeline.py" in line
    assert "--if-needed" in line


@pytest.mark.parametrize("at", ["02:00", "00:00", "23:59"])
def test_a_valid_time_is_accepted(at):
    assert sched.valid_time(at)


@pytest.mark.parametrize("at", ["2:00", "24:00", "02:60", "0200", "", "noon"])
def test_an_invalid_time_is_rejected(at):
    """Neither scheduler says so when it is wrong; it just never fires."""
    assert not sched.valid_time(at)


# --- --check -------------------------------------------------------------------


def test_a_task_pointing_at_another_checkout_is_drift():
    """The failure `--check` exists for: the checkout moved, and the schedule has been
    running something else -- or nothing -- ever since."""
    reason = sched.drifted(
        r"C:\py\pythonw.exe C:\old\devkit\scripts\release-pipeline.py", a_schedule()
    )
    assert "not this checkout" in reason


def test_a_task_pointing_here_is_not_drift():
    registered = f"{WINDOWS_PYTHON} {a_schedule().script} --if-needed --yes"
    assert sched.drifted(registered, a_schedule()) == ""


def test_nothing_registered_is_reported_as_such():
    assert sched.drifted("", a_schedule()) == "nothing is scheduled"


@pytest.mark.parametrize("label", ["Task To Run", "TÂCHE À EXÉCUTER"])
def test_the_command_is_read_out_of_either_locale(label):
    """This machine's `schtasks` answers in French often enough to matter."""
    stdout = f"Folder: \\\n{label}: C:\\py\\pythonw.exe run.py\nStatus: Ready\n"
    assert sched.registered_command(stdout) == r"C:\py\pythonw.exe run.py"


def test_a_query_that_reports_nothing_yields_no_command():
    assert sched.registered_command("Status: Ready\n") == ""


def test_check_fails_when_the_query_fails(monkeypatch):
    monkeypatch.setattr(sched, "WINDOWS", True)
    code, message = sched.run_check(
        a_schedule(),
        runner=lambda argv: subprocess.CompletedProcess(list(argv), 1, "", "not found"),
    )
    assert code == 1
    assert "--yes" in message


def test_check_passes_when_the_registered_task_is_this_one(monkeypatch):
    monkeypatch.setattr(sched, "WINDOWS", True)
    stdout = f"Task To Run: {WINDOWS_PYTHON} {a_schedule().script} --if-needed --yes\n"
    code, message = sched.run_check(
        a_schedule(),
        runner=lambda argv: subprocess.CompletedProcess(list(argv), 0, stdout, ""),
    )
    assert code == 0
    assert sched.TASK_NAME in message


# --- the plan, and the refusals ------------------------------------------------


def test_the_plan_says_it_usually_does_nothing():
    """The property a reader most needs before installing a nightly job that merges."""
    plan = sched.render_plan(a_schedule(), windows=True)
    assert "nothing" in plan.lower()
    assert sched.ARTIFACT in plan


def test_the_posix_plan_hands_over_a_line_rather_than_editing_a_crontab():
    plan = sched.render_plan(a_schedule(), windows=False)
    assert "crontab" in plan
    assert "0 2 * * *" in plan


def test_the_bare_invocation_registers_nothing(capsys):
    assert sched.main(["--devkit", str(REPO_ROOT)]) == 0
    assert "Nothing was registered" in capsys.readouterr().out


def test_an_installation_aimed_at_a_box_is_refused(tmp_path, capsys):
    """A task pointing into `.worktrees/` looks fine today and dies the moment
    `reconcile` reaps the box -- silently, which is what `--check` exists to catch."""
    box = tmp_path / sched.BOXES_DIR / "devkit--something-0821"
    (box / "scripts").mkdir(parents=True)
    (box / "scripts" / "release-pipeline.py").write_text("", encoding="utf-8")
    assert sched.main(["--yes", "--devkit", str(box)]) == 2
    assert "ephemeral box" in capsys.readouterr().err


def test_the_plan_may_still_be_read_from_a_box(tmp_path, capsys):
    """Refusing the read-only mode too would make it useless in the place an agent is
    most often invoked from."""
    box = tmp_path / sched.BOXES_DIR / "devkit--something-0821"
    (box / "scripts").mkdir(parents=True)
    (box / "scripts" / "release-pipeline.py").write_text("", encoding="utf-8")
    assert sched.main(["--devkit", str(box)]) == 0
    assert sched.TASK_NAME in capsys.readouterr().out


def test_a_checkout_with_no_pipeline_is_refused(tmp_path, capsys):
    assert sched.main(["--devkit", str(tmp_path)]) == 2
    assert "no release pipeline" in capsys.readouterr().err


def test_a_malformed_time_is_refused_before_anything_is_registered(capsys):
    with pytest.raises(SystemExit):
        sched.main(["--yes", "--at", "2am", "--devkit", str(REPO_ROOT)])


# --- the pieces the registered command is assembled from -----------------------
#
# Reached until now only through `Schedule`, which is the shape a reader checks and the
# shape a rename survives: every one of these could return the wrong path and the
# assertions above would still pass, because they read the string this file builds
# rather than the file it points at.


def test_the_pipeline_the_schedule_runs_is_the_one_in_the_checkout():
    """A release job that points at a script the checkout does not have is the failure
    `--check` exists for, and it is invisible until 2am."""
    assert sched.pipeline_script(REPO_ROOT) == REPO_ROOT / "scripts" / "release-pipeline.py"
    assert sched.pipeline_script(REPO_ROOT).is_file()


def test_the_wrapper_the_schedule_runs_is_the_one_in_the_checkout():
    assert sched.wrapper_script(REPO_ROOT) == REPO_ROOT / "scripts" / "log-wrap.py"
    assert sched.wrapper_script(REPO_ROOT).is_file()


def test_every_path_in_the_arguments_is_quoted(tmp_path):
    """This workspace lives under a user profile, and profile names contain spaces on
    most machines that are not this one: an unquoted path registers a task whose first
    argument is half a directory name. Written against a directory with a space in it
    rather than a Windows literal, so it is the quoting being asserted on either OS."""
    root = tmp_path / "Program Files" / "devkit"
    arguments = sched.release_arguments(WINDOWS_PYTHON, root)
    assert f'"{sched.wrapper_script(root).resolve()}"' in arguments
    assert f'"{sched.pipeline_script(root).resolve()}"' in arguments
    assert " Files" not in arguments.replace(
        f'"{sched.wrapper_script(root).resolve()}"', ""
    ).replace(f'"{sched.pipeline_script(root).resolve()}"', "")


def test_a_checkout_with_no_workspace_beside_it_passes_no_flag():
    """`--workspace ""` would reach the adoption pass as an empty path rather than as
    the absence of one."""
    assert "--workspace" not in sched.release_arguments(WINDOWS_PYTHON, REPO_ROOT)
    assert "--workspace" in sched.release_arguments(
        WINDOWS_PYTHON, REPO_ROOT, "somewhere/alex.code-workspace"
    )


def test_the_query_asks_for_the_verbose_list_the_parser_reads():
    """`registered_command` reads a `Task To Run:` line, which only `/V` prints and only
    `/FO LIST` prints one-per-line. Drop either and the query still succeeds, reports no
    command, and `--check` calls a correctly registered task drift."""
    argv = sched.query_argv("devkit-release")
    assert argv[:4] == ["schtasks", "/Query", "/TN", "devkit-release"]
    assert "/V" in argv and argv[argv.index("/FO") + 1] == "LIST"


def test_the_runner_captures_output_and_leaves_a_failure_to_the_caller():
    """Every caller here reads `stdout` and branches on `returncode`; a `check=True`
    would turn a task that is merely not registered yet into a traceback."""
    result = sched.run_command([sys.executable, "-c", "print('out'); raise SystemExit(3)"])
    assert result.returncode == 3
    assert result.stdout.strip() == "out"


# --- registering it ------------------------------------------------------------


def test_registering_hands_schtasks_the_document_and_reports_the_time(monkeypatch):
    monkeypatch.setattr(sched, "WINDOWS", True)
    seen = []

    def runner(argv):
        seen.append(list(argv))
        return subprocess.CompletedProcess(list(argv), 0, "SUCCESS", "")

    ok, message = sched.install(a_schedule(), runner=runner)
    assert ok, message
    assert seen[0][:4] == ["schtasks", "/Create", "/TN", sched.TASK_NAME]
    assert "02:00" in message


def test_a_failed_registration_is_reported_rather_than_claimed(monkeypatch):
    monkeypatch.setattr(sched, "WINDOWS", True)
    ok, message = sched.install(
        a_schedule(),
        runner=lambda argv: subprocess.CompletedProcess(list(argv), 1, "", "Access is denied."),
    )
    assert not ok
    assert "denied" in message


def test_posix_is_told_what_to_add_rather_than_told_it_worked(monkeypatch):
    """The installer cannot register anything here, and the one useful thing it can do
    is hand over the line -- which is also what `render_plan` prints."""
    monkeypatch.setattr(sched, "WINDOWS", False)
    ok, message = sched.install(a_schedule(), runner=_no_runner)
    assert not ok
    assert "0 2 * * *" in message


def _no_runner(argv):
    raise AssertionError(f"nothing should be run on a machine that cannot register: {argv}")
