"""Tests for scripts/schedule_health.py.

A scheduled job's failure mode is silence, so every test here is about detecting an
*absence*. The interesting ones are the two that must NOT fire: one missed run is a
laptop asleep at 04:00, and a healthy job must produce no line at all — a status check
that cries wolf is one that gets removed, and then the silence is back.
"""

from __future__ import annotations

import datetime as dt

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


def written(tmp_path, name):
    """Give `name`'s artifact a file, and point the module at that tree.

    Every pointer assertion below goes through here rather than through the real
    checkout: `logs/` is untracked, so in a fresh clone or an ephemeral box the same
    test would assert against whichever files happened to be on disk.
    """
    path = tmp_path / health.ARTIFACTS[name]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
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
    ],
)
def test_the_timestamp_formats_schtasks_emits(raw, expected):
    assert health.parse_time(raw) == expected


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
