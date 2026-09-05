"""Tests for scripts/schedule_health.py.

A scheduled job's failure mode is silence, so every test here is about detecting an
*absence*. The interesting ones are the two that must NOT fire: one missed run is a
laptop asleep at 04:00, and a healthy job must produce no line at all — a status check
that cries wolf is one that gets removed, and then the silence is back.
"""

from __future__ import annotations

import datetime as dt
import os

import pytest
from support import load_script

health = load_script("scripts/schedule_health.py")

NOW = dt.datetime(2026, 8, 13, 12, 0, 0)


def job(**overrides):
    base = {
        "name": "devkit-upgrade-projects",
        "enabled": True,
        "last_result": 0,
        "last_run": NOW - dt.timedelta(hours=9),
        "next_run": NOW + dt.timedelta(hours=15),
    }
    return health.Job(**{**base, **overrides})


# --- healthy is silent ----------------------------------------------------------


def test_a_healthy_job_produces_no_line():
    assert health.problems([job()], NOW) == []


def test_one_missed_run_is_not_reported():
    """A laptop asleep at 04:00 misses a daily run and catches the next one. That is
    the system working, and reporting it teaches everyone to ignore the line."""
    daily = job(last_run=NOW - dt.timedelta(hours=30), next_run=NOW + dt.timedelta(hours=-6))
    assert health.problems([daily], NOW) == []


# --- the four ways a scheduled job goes quiet -----------------------------------


def test_a_disabled_job_is_reported():
    """Lived: reconcile was disabled 26 minutes after it was created and stayed off for
    five days. 471 missed runs, nothing red anywhere, 26 leaked boxes and 5 GB."""
    lines = health.problems([job(enabled=False)], NOW)
    assert len(lines) == 1
    assert "disabled" in lines[0]


def test_a_job_that_has_never_run_is_reported():
    """Only once its moment has passed -- see the sentinel section below, where a job
    registered an hour ago and due tonight is correctly silent."""
    overdue = job(last_run=None, next_run=NOW - dt.timedelta(minutes=30))
    assert "never run" in health.problems([overdue], NOW)[0]


def test_a_failed_run_is_reported_with_its_exit_code():
    line = health.problems([job(last_result=1)], NOW)[0]
    assert "failed (exit 1)" in line


def written(tmp_path, name, body="=== something (exit 2) ===\nit broke\n", age=None):
    """Give `name`'s artifact a file, and point the module at that tree.

    Every pointer assertion below goes through here rather than through the real
    checkout: `logs/` is untracked, so in a fresh clone or an ephemeral box the same
    test would assert against whichever files happened to be on disk.

    **The default body is non-empty on purpose.** It used to be `""`, which made every
    pointer assertion here pass against the one file shape that cannot be read -- a
    zero-byte artifact -- and that is the shape the pointer was later found sending
    people to. `body=""` is now a case with its own tests rather than the fixture's
    default, and `age` back-dates the mtime so "was this rewritten after the run that
    failed" is something a test can set rather than something it inherits from the clock.
    """
    path = tmp_path / health.ARTIFACTS[name]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    if age is not None:
        stamp = age.timestamp()
        os.utime(path, (stamp, stamp))
    return path


def test_a_failed_run_says_where_to_read_about_it(tmp_path, monkeypatch):
    """The regression this closes: `devkit-docker-prune: last run failed (exit 1)` is a
    complete sentence and a dead end. The job runs windowless, so there is no terminal
    it scrolled off -- without the pointer the next move is to guess."""
    name = next(iter(health.ARTIFACTS))
    written(tmp_path, name)
    monkeypatch.setattr(health, "REPO_ROOT", tmp_path)
    line = health.problems([job(name=name, last_result=1)], NOW)[0]
    assert f"see {health.ARTIFACTS[name]}" in line


def test_a_failure_whose_artifact_is_missing_says_that_instead(tmp_path, monkeypatch):
    """Lived, and the reason the check exists: `devkit-docker-prune` failed at 11:58
    under the hand-registered command, and the corrected task -- the one that wraps the
    run in `log-wrap.py --always` -- was registered at 16:18 the same day. Windows keeps
    a task's `Last Result` across the `/Create /F` that replaces it, so the failure
    outlived the command, and `see logs/scheduled-docker-prune.log` pointed every
    session start at a file that could not exist."""
    name = next(iter(health.ARTIFACTS))
    monkeypatch.setattr(health, "REPO_ROOT", tmp_path)
    line = health.problems([job(name=name, last_result=1)], NOW)[0]
    assert f"no {health.ARTIFACTS[name]}" in line
    assert "see" not in line


def test_an_artifact_emptied_after_the_failed_run_is_reported_as_history(tmp_path, monkeypatch):
    """Lived, and the reason `since` exists. `devkit-upgrade-projects` last *scheduled*
    fire failed at 08:51; a hand-run pass at 19:36 upgraded everything and, per
    `upgrade-project.artifact_body`, emptied the artifact because nothing needed a human.
    A hand run does not update the scheduler's `Last Result`, so session start went on
    reporting the morning's exit 2 and pointing at a zero-byte file -- no error in it,
    and no success either, which is exactly how it was read. The mtime settles it."""
    name = "devkit-upgrade-projects"
    written(tmp_path, name, body="", age=NOW - dt.timedelta(hours=1))
    monkeypatch.setattr(health, "REPO_ROOT", tmp_path)
    line = health.problems(
        [job(name=name, last_result=2, last_run=NOW - dt.timedelta(hours=9))], NOW
    )[0]
    assert "empty" in line
    assert "history" in line
    assert f"see {health.ARTIFACTS[name]}" not in line


def test_an_empty_artifact_no_newer_than_the_run_says_the_run_recorded_nothing(
    tmp_path, monkeypatch
):
    """The other reading of zero bytes, and the one that is still a dead end: the failing
    run is the last thing that touched the file, so it wrote nothing and there is no
    later pass to credit. Distinguished by mtime alone, which is why `see` cannot be the
    answer to either."""
    name = "devkit-upgrade-projects"
    written(tmp_path, name, body="", age=NOW - dt.timedelta(hours=10))
    monkeypatch.setattr(health, "REPO_ROOT", tmp_path)
    line = health.problems(
        [job(name=name, last_result=2, last_run=NOW - dt.timedelta(hours=9))], NOW
    )[0]
    assert "empty" in line
    assert "recorded nothing" in line
    assert "history" not in line


def test_a_job_with_no_artifact_is_not_sent_to_an_invented_one():
    """An absent file reads as "the job never ran", which is a different diagnosis --
    so a job outside the table gets no pointer rather than a plausible path."""
    line = health.problems([job(name="devkit-something-new", last_result=1)], NOW)[0]
    assert "see" not in line
    assert "logs/" not in line


def test_the_pointer_is_resolved_against_the_checkout_not_the_cwd(tmp_path, monkeypatch):
    """`workspace-status.py` calls this from the workspace root, one level above devkit.
    Resolving `logs/reconcile.log` from there would find nothing and report every job's
    artifact missing -- a cwd-shaped bug that reads exactly like a broken job."""
    name = next(iter(health.ARTIFACTS))
    written(tmp_path, name)
    monkeypatch.setattr(health, "REPO_ROOT", tmp_path)
    monkeypatch.chdir(tmp_path.parent)
    assert "see" in health.artifact_hint(name)


def test_an_explicit_root_overrides_the_checkout(tmp_path):
    """The seam the tests above use, exercised directly: `root` decides, and a table
    passed in is read instead of the module's."""
    name = next(iter(health.ARTIFACTS))
    written(tmp_path, name)
    assert health.artifact_hint(name, root=tmp_path).endswith(health.ARTIFACTS[name])
    assert health.artifact_hint(name, artifacts={}, root=tmp_path) == ""


def test_a_job_that_stopped_firing_is_reported():
    """Two intervals is the threshold: a 15-minute job silent for an hour has stopped,
    whatever the scheduler still claims about its next run."""
    stalled = job(
        last_run=NOW - dt.timedelta(hours=1),
        next_run=NOW - dt.timedelta(minutes=45),
    )
    line = health.problems([stalled], NOW)[0]
    assert "has not run since" in line


def test_cadence_is_derived_from_the_job_rather_than_configured():
    """`next_run - last_run` *is* the interval, so a 15-minute job and a daily one are
    judged on their own terms and adding a third needs no edit here."""
    quarter_hourly = job(
        name="devkit-worktree-reconcile",
        last_run=NOW - dt.timedelta(minutes=50),
        next_run=NOW - dt.timedelta(minutes=35),
    )
    assert health.problems([quarter_hourly], NOW), "50 minutes is over three intervals"
    daily = job(last_run=NOW - dt.timedelta(minutes=50), next_run=NOW + dt.timedelta(hours=23))
    assert health.problems([daily], NOW) == [], "the same 50 minutes is nothing for a daily job"


# --- one line per job -----------------------------------------------------------


def test_a_disabled_job_is_not_also_reported_as_stale():
    """It is all three at once, and the other two are consequences. Reporting them
    would bury the one fact that explains the rest."""
    dead = job(enabled=False, last_result=1, last_run=NOW - dt.timedelta(days=40), next_run=None)
    assert len(health.problems([dead], NOW)) == 1


def test_jobs_are_reported_in_name_order():
    lines = health.problems(
        [job(name="devkit-z", enabled=False), job(name="devkit-a", enabled=False)], NOW
    )
    assert [line.split(":")[0] for line in lines] == ["devkit-a", "devkit-z"]


# --- parsing what schtasks actually prints --------------------------------------


CSV = (
    '"HostName","TaskName","Next Run Time","Status","Last Run Time","Last Result",'
    '"Scheduled Task State"\n'
    '"HOST","\\devkit-upgrade-projects","8/14/2026 3:00:00 AM","Ready",'
    '"8/13/2026 3:00:00 AM","0","Enabled"\n'
    '"HOST","\\devkit-worktree-reconcile","N/A","Disabled","8/8/2026 4:29:01 PM","0","Disabled"\n'
    '"HOST","\\SomeVendorUpdate","8/14/2026 1:00:00 AM","Ready","8/13/2026 1:00:00 AM","0","Enabled"\n'
)


def test_only_devkit_jobs_are_read():
    """The prefix is what makes the set maintain itself -- and what keeps every vendor
    updater on the machine out of a devkit status line."""
    names = [item.name for item in health.parse_tasks(CSV)]
    assert names == ["devkit-upgrade-projects", "devkit-worktree-reconcile"]


def test_the_leading_backslash_is_stripped():
    assert health.parse_tasks(CSV)[0].name == "devkit-upgrade-projects"


def test_a_disabled_task_parses_with_no_next_run():
    reconcile = health.parse_tasks(CSV)[1]
    assert not reconcile.enabled
    assert reconcile.next_run is None
    assert reconcile.interval is None


def test_a_repeated_header_row_is_not_read_as_a_job():
    """Some Windows builds repeat the header between tasks. Without dropping those, one
    phantom job per task appears and the whole report becomes noise."""
    doubled = (
        CSV + '"HostName","TaskName","Next Run Time","Status","Last Run Time",'
        '"Last Result","Scheduled Task State"\n'
    )
    assert len(health.parse_tasks(doubled)) == 2


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("8/14/2026 3:00:00 AM", dt.datetime(2026, 8, 14, 3, 0)),
        ("8/14/2026 15:00:00", dt.datetime(2026, 8, 14, 15, 0)),
        ("2026-08-14 03:00:00", dt.datetime(2026, 8, 14, 3, 0)),
        ("2026-08-14 3:00:00 AM", dt.datetime(2026, 8, 14, 3, 0)),
        ("2026-08-14 3:00:00 PM", dt.datetime(2026, 8, 14, 15, 0)),
    ],
)
def test_the_timestamp_formats_schtasks_emits(raw, expected):
    assert health.parse_time(raw) == expected


def test_an_iso_date_beside_a_twelve_hour_clock_is_a_real_run():
    """The false alarm this fixes, and the worst kind: silence inverted into noise.

    `schtasks` formats its stamps from the machine's *short date* and *long time*
    settings independently, so a machine set to an ISO short date and a 12-hour clock
    emits `2026-09-02 9:02:42 AM` -- a spelling no format here covered. Both stamps then
    parsed as None, and a job with no last run and no next run takes the `never run`
    branch: four healthy jobs, every one of them green in the scheduler, reported at
    every session start as never having run.

    A check whose failure mode is crying wolf is worse than no check, because this one
    exists to be believed when it says a job stopped.
    """
    assert health.parse_time("2026-09-02 9:02:42 AM") == dt.datetime(2026, 9, 2, 9, 2, 42)
    healthy = job(
        name="devkit-docker-prune",
        last_run=health.parse_time("2026-09-02 9:02:42 AM"),
        next_run=health.parse_time("2026-09-03 4:00:00 AM"),
    )
    assert health.problems([healthy], now=dt.datetime(2026, 9, 2, 13, 0)) == []


@pytest.mark.parametrize("raw", ["N/A", "", "   ", "never", "30/30/2026 99:99:99"])
def test_an_unreadable_timestamp_is_none_rather_than_an_exception(raw):
    """This runs at session start. A status line that can raise is one that gets
    deleted the first time it does."""
    assert health.parse_time(raw) is None


# --- it cannot break a session start --------------------------------------------


def test_a_machine_with_no_schtasks_reports_nothing(monkeypatch):
    def missing(*_a, **_kw):
        raise FileNotFoundError("schtasks")

    monkeypatch.setattr(health.subprocess, "run", missing)
    assert health.query() == []


def test_a_failing_query_reports_nothing(monkeypatch):
    monkeypatch.setattr(
        health.subprocess,
        "run",
        lambda *_a, **_kw: __import__("subprocess").CompletedProcess([], 1, "", "denied"),
    )
    assert health.report() == []


# --- Windows' sentinels for "never ran" -----------------------------------------
# Caught by running the check against the real scheduler rather than by unit tests: a
# task registered minutes earlier, due to fire that night, was reported as
# `last run failed (exit 267011)`. Windows fills both fields with sentinels instead of
# leaving them empty, and `0x000413xx` is a *status* range, not an exit code.


def test_the_never_ran_date_sentinel_is_read_as_never():
    """`11/30/1999` is the epoch Windows uses for "no such time", not a run in 1999."""
    assert health.parse_time("11/30/1999 00:00:00") is None
    assert health.parse_time("11/30/1999 12:00:00 AM") is None


def test_a_brand_new_job_due_tonight_is_not_a_problem():
    """The false positive this fixes. Registered an hour ago, first run at 04:00: it
    has never run, and that is exactly right."""
    fresh = job(
        last_run=None,
        last_result=health.SCHED_S_TASK_HAS_NOT_RUN,
        next_run=NOW + dt.timedelta(hours=16),
    )
    assert health.problems([fresh], NOW) == []


def test_a_first_run_that_was_missed_is_reported():
    """The same never-ran state, but its moment has passed -- which is the scheduler
    saying it should have fired and did not."""
    missed = fresh_but_overdue = job(
        last_run=None,
        last_result=health.SCHED_S_TASK_HAS_NOT_RUN,
        next_run=NOW - dt.timedelta(hours=1),
    )
    assert "never run" in health.problems([missed], NOW)[0]
    assert fresh_but_overdue.interval is None


def test_a_running_task_is_not_a_failure():
    """`SCHED_S_TASK_RUNNING` is what a task in flight reports. Reading it as an exit
    code makes every check that lands mid-run report a failure."""
    running = job(last_result=health.SCHED_S_TASK_RUNNING)
    assert health.problems([running], NOW) == []


def test_a_real_failure_is_still_reported():
    """Reversion check: widen `NOT_A_FAILURE` too far and nothing is ever reported."""
    assert "failed (exit 1)" in health.problems([job(last_result=1)], NOW)[0]
    assert "failed (exit 2147942401)" in health.problems([job(last_result=2147942401)], NOW)[0]


# --- a fire refused because the previous run had not finished --------------------
# Lived on 2026-08-20: `devkit-worktree-reconcile` fires every 15 minutes, a pass took
# over 50, and the 09:00 fire was refused. Session start reported
# `last run failed (exit -2147020576)` -- a healthy job, mid-pass, described as broken.


def test_a_fire_refused_because_a_run_was_still_going_is_not_a_failure():
    """`0x800710E0` is what `IgnoreNew` reports when it declines to start a second
    instance. It is a scheduler status, not the job's exit code."""
    for code in (
        health.SCHED_REFUSED_ALREADY_RUNNING,
        health.SCHED_REFUSED_ALREADY_RUNNING_UNSIGNED,
    ):
        assert "failed" not in "".join(health.problems([job(last_result=code)], NOW))


def test_overlapping_runs_are_reported_as_an_overrun_not_a_failure():
    """Still worth a line: a job that cannot finish inside its own interval runs
    essentially continuously, which is a real background cost."""
    line = health.problems([job(last_result=health.SCHED_REFUSED_ALREADY_RUNNING)], NOW)[0]
    assert "overlapping" in line
    assert "devkit-upgrade-projects" in line


def test_both_spellings_of_the_refusal_code_are_recognised():
    """`schtasks` signs the HRESULT and `Get-ScheduledTaskInfo` does not, so the same
    refusal reaches this module as either value depending on which one was read."""
    assert health.SCHED_REFUSED_ALREADY_RUNNING == -2147020576
    assert health.SCHED_REFUSED_ALREADY_RUNNING_UNSIGNED == 2147946720
    assert (
        health.SCHED_REFUSED_ALREADY_RUNNING_UNSIGNED - health.SCHED_REFUSED_ALREADY_RUNNING
        == 2**32
    )


# --- the interpreter the task is registered against -----------------------------
#
# The only check here that reads the registration rather than the run, and the reason it
# exists is that the run looks perfect: the job fires, exits 0, writes its artifact, and
# puts a console window on the desktop anyway. Two rounds of fixing that bug were guarded
# by source scans of devkit's own files, which cannot see that a file *named*
# `pythonw.exe` is a uv trampoline spawning the base interpreter as a child.

VENV_CSV = (
    '"HostName","TaskName","Next Run Time","Status","Last Run Time","Last Result",'
    '"Scheduled Task State","Task To Run"\n'
    r'"HOST","\devkit-global-tools","8/14/2026 3:00:00 AM","Ready",'
    '"8/13/2026 3:00:00 AM","0","Enabled",'
    r'"C:\vs_code\devkit\.venv\Scripts\pythonw.exe C:\devkit\scripts\g.py --yes"'
    "\n"
)

VENV_COMMAND = r"C:\vs_code\devkit\.venv\Scripts\pythonw.exe"


def venv_layout(tmp_path):
    """A virtualenv `Scripts/` beside the base install its `pyvenv.cfg` names."""
    base = tmp_path / "base"
    scripts = tmp_path / "venv" / "Scripts"
    base.mkdir()
    scripts.mkdir(parents=True)
    for directory in (base, scripts):
        for stem in ("python.exe", "pythonw.exe"):
            (directory / stem).write_text("", encoding="utf-8")
    (tmp_path / "venv" / "pyvenv.cfg").write_text(
        f"home = {base}\nuv = 0.11.29\n", encoding="utf-8"
    )
    return scripts / "pythonw.exe", base


def test_the_registered_command_is_read_off_the_scheduler():
    parsed = health.parse_tasks(VENV_CSV)[0]
    assert parsed.command.endswith("--yes")
    assert parsed.interpreter == VENV_COMMAND


def test_the_interpreter_is_found_even_when_its_path_contains_spaces():
    """`schtasks` leaves the command unquoted however the arguments are quoted, so the
    first whitespace token is wrong on any machine whose profile name is two words --
    which is most of them, and all of the ones this has to work on."""
    parsed = job(command=r"C:\Program Files\Python\pythonw.exe C:\a b\script.py --all")
    assert parsed.interpreter == r"C:\Program Files\Python\pythonw.exe"


def test_a_quoted_command_loses_its_quote():
    parsed = job(command=r'"C:\py\pythonw.exe" script.py')
    assert parsed.interpreter == r"C:\py\pythonw.exe"


def test_a_command_naming_no_exe_falls_back_to_the_first_token():
    """POSIX, and anything this has no model of. A wrong guess here would be a false
    alarm at every session start, which is how a status check gets deleted."""
    assert job(command="/usr/bin/python3 /opt/devkit/x.py --all").interpreter == "/usr/bin/python3"


def test_a_job_registered_against_a_virtualenv_stub_is_reported(tmp_path):
    stub, base = venv_layout(tmp_path)
    lines = health.problems([job(command=f"{stub} script.py --yes")], NOW)
    assert len(lines) == 1
    assert str(base) in lines[0]
    assert "visible console" in lines[0]
    assert "--yes" in lines[0].rpartition("installer")[2]


def test_a_healthy_job_on_a_real_interpreter_says_nothing(tmp_path):
    """The reversion check, and the property that matters more than the rule: a check
    that fires on the correct registration is one that gets removed."""
    real = tmp_path / "pythonw.exe"
    real.write_text("", encoding="utf-8")
    assert health.problems([job(command=f"{real} script.py --yes")], NOW) == []


def test_a_job_with_no_command_recorded_is_not_reported(tmp_path):
    """A scheduler front end that stops reporting `Task To Run` should cost this check
    its coverage, not the reader their trust in every other line."""
    assert health.problems([job(command="")], NOW) == []


def test_a_disabled_job_on_a_stub_is_reported_once(tmp_path):
    """One line per job: the misregistration is real, and irrelevant next to a job
    nothing is running at all."""
    stub, _base = venv_layout(tmp_path)
    lines = health.problems([job(enabled=False, command=f"{stub} script.py")], NOW)
    assert lines == ["devkit-upgrade-projects: disabled -- nothing is running it"]


def test_a_failing_job_on_a_stub_reports_the_failure_first(tmp_path):
    """A misregistered command is permanent, so it is still there to report next
    session. The failing run is the thing that may not be."""
    stub, _base = venv_layout(tmp_path)
    lines = health.problems([job(last_result=2, command=f"{stub} script.py")], NOW)
    assert len(lines) == 1
    assert "exit 2" in lines[0]


# --- one task, several triggers ----------------------------------------------
#
# `schtasks` emits a row per *trigger*, not per task. It was theoretical until
# `devkit-rc-servers` gained a boot trigger beside its repetition, and then the tray
# listed the job twice -- which is what found it.

TWO_TRIGGER_CSV = (
    '"HostName","TaskName","Next Run Time","Status","Last Run Time","Last Result",'
    '"Scheduled Task State"\n'
    '"HOST","\\devkit-rc-servers","9/2/2026 8:45:00 PM","Ready",'
    '"9/2/2026 8:30:00 PM","0","Enabled"\n'
    '"HOST","\\devkit-rc-servers","N/A","Ready","9/2/2026 8:30:00 PM","0","Enabled"\n'
)


def test_a_task_with_two_triggers_is_one_job():
    jobs = health.parse_tasks(TWO_TRIGGER_CSV)
    assert [item.name for item in jobs] == ["devkit-rc-servers"]


def test_the_earliest_next_run_survives_the_merge():
    """The boot trigger's row has no next run until the next boot. Keeping it would lose
    the cadence, because `Job.interval` is `next_run - last_run`."""
    job = health.parse_tasks(TWO_TRIGGER_CSV)[0]
    assert job.next_run == dt.datetime(2026, 9, 2, 20, 45)
    assert job.interval == dt.timedelta(minutes=15)


def test_the_merge_does_not_depend_on_which_trigger_schtasks_lists_first():
    """`schtasks` gives no ordering guarantee across triggers, so a fix that kept the
    first row would work only while the repetition happened to come first."""
    lines = TWO_TRIGGER_CSV.strip().splitlines()
    reversed_csv = "\n".join([lines[0], lines[2], lines[1]]) + "\n"
    job = health.parse_tasks(reversed_csv)[0]
    assert job.next_run == dt.datetime(2026, 9, 2, 20, 45)
    assert job.interval == dt.timedelta(minutes=15)


def test_a_problem_with_a_multi_trigger_job_is_reported_once():
    """The consequence that reached a user: the tray drew the same job twice, and
    `problems` would have said the same thing about it twice."""
    failing = TWO_TRIGGER_CSV.replace('"0","Enabled"', '"1","Enabled"')
    found = health.problems(health.parse_tasks(failing), dt.datetime(2026, 9, 2, 20, 40))
    assert len(found) == 1
    assert found[0].startswith("devkit-rc-servers: last run failed")


def test_every_trigger_reporting_no_next_run_still_leaves_one_job():
    never = TWO_TRIGGER_CSV.replace('"9/2/2026 8:45:00 PM"', '"N/A"')
    jobs = health.parse_tasks(never)
    assert len(jobs) == 1
    assert jobs[0].next_run is None


# --- deliberate is not broken ---------------------------------------------------
#
# `Disabled` is the one scheduler state with two opposite meanings. The incident in this
# module's header is one of them; an operator running `harness-switch.py --off jobs` is
# the other, and the scheduler records them identically. Reporting the deliberate one as
# a fault is how the real one becomes a line people skim past.


def test_a_deliberately_stood_down_job_is_silent():
    off = job(name="devkit-worktree-reconcile", enabled=False, next_run=None)
    deliberate = frozenset({"devkit-worktree-reconcile"})
    assert health.problems([off], NOW, deliberate) == []


def test_a_hand_disabled_job_is_still_reported():
    """The whole of the 471-missed-runs incident. Only a name in the ledger is intent;
    a job disabled by hand is in none, and must stay a fault."""
    off = job(name="devkit-worktree-reconcile", enabled=False, next_run=None)
    assert health.problems([off], NOW, frozenset({"devkit-release"})) != []


def test_standing_one_job_down_does_not_quieten_its_neighbours():
    off = job(name="devkit-worktree-reconcile", enabled=False, next_run=None)
    broken = job(name="devkit-release", last_result=1)
    lines = health.problems([off, broken], NOW, frozenset({"devkit-worktree-reconcile"}))
    assert len(lines) == 1
    assert lines[0].startswith("devkit-release:")


def test_a_stood_down_job_that_is_running_again_is_judged_normally():
    """The ledger goes stale the moment someone re-enables a task by hand. Intent only
    ever suppresses the `disabled` line, so a job that is actually running is checked
    like any other -- here, still reporting its failed run."""
    back = job(name="devkit-worktree-reconcile", enabled=True, last_result=1)
    lines = health.problems([back], NOW, frozenset({"devkit-worktree-reconcile"}))
    assert len(lines) == 1
    assert "last run failed" in lines[0]


def test_problems_defaults_to_treating_nothing_as_deliberate():
    """The safe direction: a caller that has not read the ledger must not lose a fault."""
    assert health.problems([job(enabled=False)], NOW) != []


def test_stood_down_reads_the_switch_ledger(tmp_path):
    ledger = tmp_path / "ledger.json"
    ledger.write_text(
        '{"hooks": false, "jobs": ["devkit-worktree-reconcile"], "instructions": []}',
        encoding="utf-8",
    )
    assert health.stood_down(ledger) == frozenset({"devkit-worktree-reconcile"})


def test_stood_down_is_empty_when_no_ledger_exists(tmp_path):
    """A fresh clone, CI, anyone else's machine. Empty is the direction that keeps a
    genuinely disabled job reported."""
    assert health.stood_down(tmp_path / "nope.json") == frozenset()


def test_a_corrupt_ledger_reads_as_nothing_deliberate(tmp_path):
    ledger = tmp_path / "ledger.json"
    ledger.write_text("{not json", encoding="utf-8")
    assert health.stood_down(ledger) == frozenset()
