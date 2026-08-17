#!/usr/bin/env python3
"""Whether this machine's scheduled devkit jobs are actually running.

**The failure a scheduled job has is silence.** Every other kind announces itself: a
failing test is red, a refused commit prints a hook id, a broken lint run leaves an
artifact. A scheduled job that stops running produces *nothing at all* -- and the
longer it is off, the more normal its absence looks.

That is not hypothetical here. `devkit-worktree-reconcile` was disabled 26 minutes
after it was created and stayed off for five days: 471 missed runs, no error anywhere,
and the only symptom was 26 ephemeral boxes and 5 GB of disk nobody could account for.
Nothing in `logs/` could have reported it, because the job that would have written the
log is the job that was not running.

So this reads the **scheduler's** view rather than the jobs' own output, which is the
only place three of the four failure modes are visible at all:

| what went wrong | where it shows |
| --- | --- |
| someone disabled it | `Scheduled Task State` |
| it ran and failed | `Last Result` |
| it has silently stopped firing | `Last Run Time` against its own cadence |
| it ran, and declined to do anything | the job's own artifact, not here |

The last row is the one this deliberately does not cover: "ran fine, did nothing" is a
statement about the work, and the job that did it owns that log. This answers the prior
question -- *did it run at all* -- which no artifact can answer, because an artifact
that was never written and one whose job never ran are the same empty file.

What it does do is **hand the reader over**. Every job's artifact is named in
`ARTIFACTS`, so a failure line ends in `see logs/<file>` rather than in an exit code
with nowhere to go; the two halves answer different questions and the report is only
useful when it carries both.

Cadence is derived rather than configured: a healthy job's `Next Run Time` minus its
`Last Run Time` **is** its interval, whatever the trigger says, so nothing here has to
know that reconcile is every 15 minutes and the upgrade is daily. Adding a fourth job
needs no edit.

Windows-only by nature (`schtasks`), and silent everywhere else -- same contract as
`workspace-status.py`, which is what surfaces this at session start.

Tested in `tests/test_schedule_health.py`.
"""

from __future__ import annotations

import csv
import datetime as _dt
import io
import subprocess
from dataclasses import dataclass
from pathlib import Path

# `ARTIFACTS` holds repo-relative paths, and the caller is `workspace-status.py`, whose
# cwd is the workspace rather than this checkout. Resolving against this file keeps the
# existence check answering "did the job write it", not "where was this run from".
REPO_ROOT = Path(__file__).resolve().parents[1]

# Every devkit job registers under this prefix, so the set maintains itself -- a job
# added tomorrow is checked without touching this file.
PREFIX = "devkit-"

# `schtasks` writes this when a task has never run. It is not a date to reason about.
NEVER = "N/A"

# The other spelling of "never ran", and the one that actually turned up: Windows fills
# both fields with sentinels rather than leaving them empty. `11/30/1999` is the epoch
# it uses for "no such time", and `0x00041303` is `SCHED_S_TASK_HAS_NOT_RUN`.
NEVER_RUN_DATE = _dt.date(1999, 11, 30)

# Codes in the `0x000413xx` range are *statuses*, not failures, and reading them as exit
# codes reports a healthy machine as broken. This was caught by running the check
# against the real scheduler: a task registered minutes earlier, due to fire that night,
# was reported as "last run failed (exit 267011)".
SCHED_S_TASK_HAS_NOT_RUN = 267011  # 0x00041303
SCHED_S_TASK_RUNNING = 267009  # 0x00041301
NOT_A_FAILURE = frozenset({0, SCHED_S_TASK_HAS_NOT_RUN, SCHED_S_TASK_RUNNING})

# How many intervals a job may miss before it is worth a line. One is noise: a laptop
# asleep at 04:00 misses a daily run and catches it the next night, which is the system
# working. Two consecutive misses is a job that has stopped.
STALE_INTERVALS = 2.0

# Where each job's own account of its last run lives, relative to the devkit checkout.
#
# **The exit code alone is a dead end.** "devkit-docker-prune: last run failed (exit 1)"
# is a true and complete sentence that tells a fresh agent nothing it can act on: the
# job runs windowless, so there is no terminal it scrolled off, and until this table
# existed the reader's only remaining move was to guess. One `see <path>` closes that,
# and it costs nothing at session start because the line is only printed when something
# is already wrong.
#
# Hand-maintained on purpose -- there is no rule mapping a task name to a file, since
# each job chose its own artifact for its own reasons. `tests/test_scheduled_jobs.py`
# requires an entry here for every job devkit knows how to install, so the drift this
# invites is caught in the suite rather than by the next person to read a bare exit code.
ARTIFACTS: dict[str, str] = {
    "devkit-worktree-reconcile": "logs/reconcile.log",
    "devkit-upgrade-projects": "logs/upgrade.log",
    "devkit-docker-prune": "logs/scheduled-docker-prune.log",
    "devkit-docker-stop-idle": "logs/scheduled-docker-stop-idle.log",
    "devkit-vanillaland-merge": "logs/scheduled-vanillaland-merge-develop.log",
}


def artifact_hint(
    name: str,
    artifacts: dict[str, str] | None = None,
    root: Path | None = None,
) -> str:
    """`" -- see logs/x.log"` for a job that kept one, and the truth when it did not.

    A job with no entry gets no pointer rather than a guessed path: sending a reader to
    a file that was never written is worse than sending them nowhere, because an absent
    artifact reads as "the job never ran" and that is a different diagnosis.

    **An entry whose file is not there is that same dead end**, which the paragraph above
    asserted and the code did not check. `devkit-docker-prune` produced exactly it: the
    job failed at 11:58 under the hand-registered command, the corrected task -- the one
    that wraps the run in `log-wrap.py --always` -- was registered at 16:18 the same day,
    and Windows carries a task's `Last Result` across the `/Create /F` that replaces it.
    So the scheduler went on reporting a failure from a command that no longer existed,
    and every session start sent its reader to a file that *could not* exist, because the
    run it describes predates the wrapper that would have written one.

    Naming the absence costs one `stat` and turns that into the two things it can mean:
    the failing run predates the artifact, or the job died before writing it. Both point
    at the same next move -- compare the registered command against its installer, which
    `install-<job>.py --status` prints and `--yes` makes true again idempotently.
    """
    path = (ARTIFACTS if artifacts is None else artifacts).get(name, "")
    if not path:
        return ""
    base = REPO_ROOT if root is None else root
    if (base / path).is_file():
        return f" -- see {path}"
    return f" -- no {path}: the run predates it, or died before writing one"


@dataclass(frozen=True)
class Job:
    """One scheduled task, as the scheduler describes it."""

    name: str
    enabled: bool
    last_result: int
    last_run: _dt.datetime | None = None
    next_run: _dt.datetime | None = None

    @property
    def interval(self) -> _dt.timedelta | None:
        """This job's own cadence, or None when it cannot be derived.

        `next_run - last_run` for a job that has run and is still scheduled. A disabled
        job has no next run, and a job that has never run has no last one -- both are
        already reported by a rule above, so returning None here costs nothing.
        """
        if self.last_run is None or self.next_run is None:
            return None
        gap = self.next_run - self.last_run
        return gap if gap > _dt.timedelta(0) else None


def parse_time(raw: str) -> _dt.datetime | None:
    """`schtasks`' local-time stamp, or None for `N/A` and anything unparseable.

    Several formats reach this depending on locale, so it tries the common ones and
    gives up quietly rather than raising: a status line that can crash a session start
    is a status line that gets deleted.
    """
    text = raw.strip()
    if not text or text.startswith(NEVER):
        return None
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = _dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
        return None if parsed.date() == NEVER_RUN_DATE else parsed
    return None


def parse_tasks(stdout: str, prefix: str = PREFIX) -> list[Job]:
    """Every `prefix` job in `schtasks /Query /FO CSV /V` output.

    The header repeats between tasks in some Windows builds, so rows whose values equal
    the column names are dropped -- without that, one phantom job per task appears with
    a `Last Result` of `"Last Result"` and the whole report becomes noise.
    """
    jobs: list[Job] = []
    for row in csv.DictReader(io.StringIO(stdout)):
        name = (row.get("TaskName") or "").lstrip("\\")
        if not name.startswith(prefix) or name == "TaskName":
            continue
        raw_result = (row.get("Last Result") or "").strip()
        try:
            result = int(raw_result)
        except ValueError:
            continue
        jobs.append(
            Job(
                name=name,
                enabled=(row.get("Scheduled Task State") or "").strip().lower() == "enabled",
                last_result=result,
                last_run=parse_time(row.get("Last Run Time") or ""),
                next_run=parse_time(row.get("Next Run Time") or ""),
            )
        )
    return jobs


def problems(jobs: list[Job], now: _dt.datetime | None = None) -> list[str]:
    """One line per job that needs attention; [] when they are all healthy.

    Ordered so the most actionable comes first, and **at most one line per job** -- a
    disabled job is also stale and also has a stale result, and reporting all three
    would bury the one fact that explains the other two.
    """
    moment = now or _dt.datetime.now()
    found: list[str] = []
    for job in sorted(jobs, key=lambda item: item.name):
        if not job.enabled:
            found.append(f"{job.name}: disabled -- nothing is running it")
            continue
        if job.last_run is None:
            # A job registered an hour ago and due tonight has never run and is
            # perfectly healthy. Only a *missed* first run is worth a line, and the
            # scheduler says which that is: its next run is already in the past.
            if job.next_run is None or job.next_run < moment:
                found.append(f"{job.name}: registered but has never run")
            continue
        if job.last_result not in NOT_A_FAILURE:
            found.append(
                f"{job.name}: last run failed (exit {job.last_result}) at "
                f"{job.last_run:%Y-%m-%d %H:%M}{artifact_hint(job.name)}"
            )
            continue
        interval = job.interval
        if interval and moment - job.last_run > interval * STALE_INTERVALS:
            missed = (moment - job.last_run) / interval
            found.append(
                f"{job.name}: has not run since {job.last_run:%Y-%m-%d %H:%M} "
                f"({missed:.0f} intervals ago)"
            )
    return found


def query(prefix: str = PREFIX) -> list[Job]:
    """Ask the scheduler. [] on any failure, including not being Windows at all."""
    try:
        result = subprocess.run(
            ["schtasks", "/Query", "/FO", "CSV", "/V"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return parse_tasks(result.stdout, prefix)


def report(prefix: str = PREFIX, now: _dt.datetime | None = None) -> list[str]:
    """The lines a caller should print. Empty means every scheduled job is healthy."""
    return problems(query(prefix), now)
