"""Tests for scripts/install-upgrade-schedule.py.

The interesting half is `--check`: an installer that only asked "is something
scheduled" would call a task pointing at a deleted checkout healthy, and a stale
schedule fails silently every night with nothing red anywhere.
"""

import subprocess

import pytest
from support import REPO_ROOT, load_script

sched = load_script("scripts/install-upgrade-schedule.py")


def a_schedule(**overrides):
    base = {
        "name": sched.TASK_NAME,
        "python": r"C:\py\python.exe",
        "script": r"C:\ws\devkit\scripts\upgrade-project.py",
        "at": "03:00",
    }
    return sched.Schedule(**{**base, **overrides})


# --- what gets registered ------------------------------------------------------


def test_the_scheduled_command_applies_the_upgrade_rather_than_planning_it():
    """A scheduled dry run would report the same thing every night and change nothing.
    The whole point of an upgrade that cannot refuse is that it runs unattended."""
    assert a_schedule().command[-2:] == ["--all", "--yes"]


def test_the_interpreter_is_this_one_not_a_bare_python():
    """A scheduled task runs with no activated virtualenv and often a different PATH,
    and the script imports `sweep`/`worktree` from beside it."""
    resolved = sched.schedule_for()
    assert resolved.python.endswith(("pythonw.exe", "python.exe", "python", "python3"))
    assert resolved.python != "python"


# --- it must not put a window on the desktop -----------------------------------


def test_the_task_runs_the_windowless_interpreter(tmp_path):
    """`python.exe` is a console app, so Windows allocates a console every time the task
    fires -- a black window appearing and vanishing on its own, nightly, for a job whose
    only output is a log file. The sibling `devkit-worktree-reconcile` task already uses
    `pythonw.exe`; two scheduled devkit jobs must not differ in whether they interrupt
    you."""
    real = tmp_path / "python.exe"
    real.write_text("", encoding="utf-8")
    (tmp_path / "pythonw.exe").write_text("", encoding="utf-8")
    assert sched.windowless_python(str(real)).endswith("pythonw.exe")


def test_a_layout_without_pythonw_falls_back_rather_than_failing(tmp_path):
    """POSIX has no `pythonw`, and neither does every Windows layout. There the console
    question does not arise the same way, so the honest answer is the interpreter given."""
    real = tmp_path / "python3"
    real.write_text("", encoding="utf-8")
    assert sched.windowless_python(str(real)) == str(real)


def test_the_schedule_names_the_workspace_it_operates_on():
    """The registered command is the only record of the blast radius; a reader of
    `schtasks /Query` should not have to know the script's default to know it."""
    resolved = sched.schedule_for()
    assert "--workspace" in resolved.command
    assert resolved.command.index("--workspace") == len(resolved.command) - 2


def test_a_schedule_with_no_workspace_resolved_still_runs():
    """`--workspace` is omitted rather than passed empty: an empty string reaches
    argparse as a stray positional, which is the failure the picker convention exists
    to avoid."""
    assert "--workspace" not in a_schedule(workspace="").command


def test_the_script_path_is_absolute_and_points_at_this_checkout():
    resolved = sched.schedule_for(root=REPO_ROOT)
    assert resolved.script.endswith("upgrade-project.py")
    assert str(REPO_ROOT) in resolved.script


def test_the_windows_registration_overwrites_its_own_previous_entry():
    """Without `/F` a moved checkout leaves a task pointing at a path that no longer
    exists, failing every night with nothing to notice it."""
    assert "/F" in sched.schtasks_argv(a_schedule())


def test_the_registration_is_daily_at_the_requested_time():
    argv = sched.schtasks_argv(a_schedule(at="04:30"))
    assert argv[argv.index("/SC") + 1] == "DAILY"
    assert argv[argv.index("/ST") + 1] == "04:30"


def test_the_crontab_line_puts_minutes_first():
    """`30 4 * * *`, not `4 30`. The two are both plausible and only one is right."""
    line = sched.crontab_line(a_schedule(at="04:30"))
    assert line.startswith("30 4 * * * ")
    assert "--all" in line


def test_a_midnight_schedule_is_not_rendered_with_leading_zeros():
    """cron takes `0 0`, and `00 00` is accepted by some implementations and not others."""
    assert sched.crontab_line(a_schedule(at="00:00")).startswith("0 0 * * * ")


# --- the time argument ---------------------------------------------------------


@pytest.mark.parametrize("value", ["03:00", "00:00", "23:59"])
def test_valid_times_are_accepted(value):
    assert sched.valid_time(value)


@pytest.mark.parametrize("value", ["3:00", "24:00", "03:60", "0300", "", "am", "03:0"])
def test_a_time_neither_scheduler_would_accept_is_rejected(value):
    """Both take `HH:MM` and neither says so when it is wrong -- `schtasks` registers a
    task that never fires, which is the worst of the available failures."""
    assert not sched.valid_time(value)


def test_a_bad_time_stops_the_install_rather_than_registering_it():
    with pytest.raises(SystemExit) as exit_info:
        sched.main(["--yes", "--at", "25:00"])
    assert exit_info.value.code == 2


# --- the checkout the task points at -------------------------------------------


def test_registering_from_an_ephemeral_box_is_refused(tmp_path, capsys, monkeypatch):
    """A task pointing into `.worktrees/` looks fine today and dies silently the moment
    `reconcile` reaps that box -- which is exactly the failure `--check` exists to catch,
    so it is better not to create it. Agents install from boxes; this is not exotic."""
    box = tmp_path / sched.BOXES_DIR / "devkit--something-0812"
    (box / "scripts").mkdir(parents=True)
    (box / "scripts" / "upgrade-project.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(sched, "install", lambda *_a, **_kw: pytest.fail("registered a box"))

    assert sched.main(["--yes", "--devkit", str(box)]) == 2
    assert "ephemeral box" in capsys.readouterr().err


def test_a_missing_upgrade_script_stops_the_install(tmp_path, capsys):
    assert sched.main(["--yes", "--devkit", str(tmp_path)]) == 2
    assert "no upgrade script" in capsys.readouterr().err


def test_the_named_checkout_is_what_the_task_runs(tmp_path, capsys):
    """`--devkit` is how an install from a box still targets the static checkout."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "upgrade-project.py").write_text("", encoding="utf-8")
    assert sched.main(["--devkit", str(tmp_path)]) == 0
    assert str(tmp_path / "scripts" / "upgrade-project.py") in capsys.readouterr().out


# --- --check -------------------------------------------------------------------


QUERY_OUTPUT = """
Folder: \\
HostName:                             DESKTOP
TaskName:                             \\devkit-upgrade-projects
Task To Run:                          C:\\py\\python.exe C:\\ws\\devkit\\scripts\\upgrade-project.py --all --yes
Schedule:                             Scheduling data is not available
"""


def test_the_registered_command_is_read_out_of_the_query():
    assert "upgrade-project.py" in sched.registered_command(QUERY_OUTPUT)


def test_an_empty_query_reports_no_command():
    assert sched.registered_command("") == ""


def test_nothing_scheduled_is_reported_as_such():
    assert sched.drifted("", a_schedule()) == "nothing is scheduled"


def test_a_task_pointing_at_this_checkout_is_healthy():
    assert sched.drifted(sched.registered_command(QUERY_OUTPUT), a_schedule()) == ""


def test_a_task_pointing_at_a_different_checkout_is_drift():
    """The failure this mode exists for: the checkout moved, the task still fires, and
    it runs something else or nothing at all."""
    reason = sched.drifted(r"C:\old\devkit\scripts\upgrade-project.py --all --yes", a_schedule())
    assert "not this checkout" in reason


def test_a_rebuilt_interpreter_is_not_reported_as_drift():
    """The venv gets rebuilt and Python gets upgraded in place; rewriting the task over
    that would be noise, and noise is what makes a check get ignored."""
    registered = r"C:\other\python.exe C:\ws\devkit\scripts\upgrade-project.py --all --yes"
    assert sched.drifted(registered, a_schedule()) == ""


def test_the_check_is_case_insensitive_about_paths():
    """Windows hands back whatever case the task was registered with."""
    registered = r"c:\WS\DEVKIT\scripts\Upgrade-Project.py --all --yes"
    assert sched.drifted(registered, a_schedule()) == ""


# --- installing ----------------------------------------------------------------


class FakeRunner:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(list(argv), self.returncode, self.stdout, self.stderr)


def test_a_failed_registration_is_reported_rather_than_assumed(monkeypatch):
    monkeypatch.setattr(sched.os, "name", "nt")
    ok, message = sched.install(a_schedule(), FakeRunner(returncode=1, stderr="ERROR: denied"))
    assert not ok
    assert "denied" in message


def test_a_successful_registration_says_when_it_will_run(monkeypatch):
    monkeypatch.setattr(sched.os, "name", "nt")
    ok, message = sched.install(a_schedule(at="05:15"), FakeRunner())
    assert ok
    assert "05:15" in message


def test_a_posix_machine_is_told_the_line_rather_than_having_its_crontab_edited(monkeypatch):
    """Editing a user's crontab unattended is the kind of irreversible edit this
    workspace does not do; the line is the deliverable there."""
    monkeypatch.setattr(sched.os, "name", "posix")
    ok, message = sched.install(a_schedule(), FakeRunner())
    assert not ok
    assert "* * *" in message


# --- the plan is the default ---------------------------------------------------


def test_the_bare_invocation_registers_nothing(capsys, monkeypatch):
    monkeypatch.setattr(sched, "install", lambda *_a, **_kw: pytest.fail("installed without --yes"))
    assert sched.main([]) == 0
    assert "Nothing was registered" in capsys.readouterr().out


def test_the_plan_names_the_command_that_will_run(capsys):
    sched.main([])
    printed = capsys.readouterr().out
    assert "--all" in printed and "--yes" in printed


def test_the_plan_says_nothing_is_merged(capsys):
    """The standing rule for this workspace, and the one thing a reader of a scheduled
    job most needs to know before enabling it."""
    sched.main([])
    assert "Nothing is merged" in capsys.readouterr().out


def test_the_posix_plan_offers_a_crontab_line_rather_than_schtasks():
    rendered = sched.render_plan(a_schedule(), windows=False)
    assert "crontab" in rendered
    assert "schtasks" not in rendered
