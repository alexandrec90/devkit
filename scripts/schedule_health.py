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
only place all but one of these failure modes are visible at all:

| what went wrong | where it shows |
| --- | --- |
| someone disabled it | `Scheduled Task State`, minus what `stood_down` says was meant |
| it ran and failed | `Last Result` |
| it has silently stopped firing | `Last Run Time` against its own cadence |
| it runs, and opens a window every time | `Task To Run` -- see `virtualenv_interpreter` |
| it ran, and declined to do anything | the job's own artifact, not here |

`Disabled` is the one state the scheduler cannot interpret on its own, because an
operator standing the tier down with `harness-switch.py --off jobs` leaves the task in
exactly the state the incident above did. `stood_down` reads the intent off that switch's
ledger; everything not written there is still the fault it was.

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
import re
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devkit_schtasks
import harness_state

# `ARTIFACTS` holds repo-relative paths, and the caller is `workspace-status.py`, whose
# cwd is the workspace rather than this checkout. Resolving against this file keeps the
# existence check answering "did the job write it", not "where was this run from".
REPO_ROOT = Path(__file__).resolve().parents[1]

# Every devkit job registers under this prefix, so the set maintains itself -- a job
# added tomorrow is checked without touching this file.
PREFIX = "devkit-"

# This module's one spawn has to be console-less, and for a reason that only arrived
# with a second caller. Session start runs it from a terminal, where a flashed console
# is invisible among the output; `tray.py` runs it from a task registered under
# `pythonw.exe`, on a timer, forever -- and Windows gives a brand new console window to
# every console child of a console-less parent. Without this the tray would open a black
# window every poll, which is the exact regression `tests/test_scheduled_jobs.py`
# documents twice. Zero off Windows, where the flag does not exist.
NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)

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

# `0x800710E0` -- documented as "the operator or administrator has refused the request",
# and what Task Scheduler reports when `MultipleInstancesPolicy` is `IgnoreNew` and a
# fire comes due while the previous run is still going. It is the *scheduler's* verdict
# on starting a second instance, never the job's exit code, so reading it as one calls a
# healthy mid-pass job broken -- which is how `devkit-worktree-reconcile` came to be
# reported as `last run failed (exit -2147020576)` on 2026-08-20 while its 09:00 pass was
# still running, 50 minutes into a job scheduled every 15.
#
# Both spellings are carried because the two Windows front ends disagree: `schtasks`
# signs the HRESULT and `Get-ScheduledTaskInfo` reports the same bits unsigned, so which
# one arrives here depends only on who was asked.
SCHED_REFUSED_ALREADY_RUNNING = -2147020576  # 0x800710E0, as `schtasks` signs it
SCHED_REFUSED_ALREADY_RUNNING_UNSIGNED = 2147946720  # the same bits, unsigned
OVERLAPPING = frozenset({SCHED_REFUSED_ALREADY_RUNNING, SCHED_REFUSED_ALREADY_RUNNING_UNSIGNED})

NOT_A_FAILURE = frozenset({0, SCHED_S_TASK_HAS_NOT_RUN, SCHED_S_TASK_RUNNING}) | OVERLAPPING

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
    "devkit-release": "logs/scheduled-devkit-release.log",
    "devkit-upgrade-projects": "logs/upgrade.log",
    "devkit-docker-prune": "logs/scheduled-docker-prune.log",
    "devkit-docker-stop-idle": "logs/scheduled-docker-stop-idle.log",
    "devkit-global-tools": "logs/global-tools.log",
    "devkit-rc-servers": "logs/rc-servers.log",
    "devkit-tray": "logs/tray.log",
}


def artifact_hint(
    name: str,
    artifacts: dict[str, str] | None = None,
    root: Path | None = None,
    since: _dt.datetime | None = None,
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

    **An artifact that is present but empty is the third spelling of that dead end**, and
    the one this reported as `see logs/upgrade.log` for months. `upgrade-project.py` --
    and every other job whose artifact is a *failure* artifact -- writes an empty file on
    a clean run deliberately, so a fixed failure cannot go on sending readers after
    itself. That makes a zero-byte artifact the strongest evidence available that the
    scheduler's verdict is history: `devkit-upgrade-projects` was found reporting exit 2
    from 08:51 beside an artifact a clean 19:36 run had emptied, and reading the file
    answered neither question -- no error, and no success either, which is exactly how it
    was reported.

    So `since` (the failed run's own timestamp) turns the mtime into the answer. Newer
    means a later run finished clean and the scheduler is simply repeating its last
    *scheduled* result, which a hand-run pass never updates. Not newer -- or unknown --
    means the run itself recorded nothing, and the job's own console output is the only
    remaining place to look.
    """
    path = (ARTIFACTS if artifacts is None else artifacts).get(name, "")
    if not path:
        return ""
    base = REPO_ROOT if root is None else root
    target = base / path
    try:
        size = target.stat().st_size
        written = _dt.datetime.fromtimestamp(target.stat().st_mtime)
    except OSError:
        return f" -- no {path}: the run predates it, or died before writing one"
    if size:
        return f" -- see {path}"
    if since is not None and written > since:
        return (
            f" -- {path} is empty and was rewritten {written:%Y-%m-%d %H:%M}, after this "
            f"run: a later pass finished clean and the scheduler is reporting history"
        )
    return f" -- {path} is empty: this run recorded nothing, so it left no diagnosis"


@dataclass(frozen=True)
class Job:
    """One scheduled task, as the scheduler describes it."""

    name: str
    enabled: bool
    last_result: int
    last_run: _dt.datetime | None = None
    next_run: _dt.datetime | None = None
    command: str = ""

    @property
    def interpreter(self) -> str:
        """The executable out of the whole registered command line, or `""`.

        `Task To Run` is `<Command>` and `<Arguments>` joined back together with a space,
        and `schtasks` leaves the command unquoted however the arguments are quoted -- so
        the first whitespace token is wrong for any path containing a space, which every
        one of these does on a machine whose profile name is two words. Everything up to
        the first `.exe` is right for both, and `schtasks` truncates the tail of a long
        command line, which is another reason not to depend on anything but the head.
        """
        text = self.command.strip().lstrip('"')
        match = re.match(r"(?i)(.*?\.exe)", text)
        if match:
            return match.group(1)
        return text.split(" ", 1)[0]

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

    **The date and the clock vary independently**, which is the trap: `schtasks` builds
    a stamp from the machine's short-date setting and its long-time setting, and neither
    constrains the other. So an ISO short date beside a 12-hour clock --
    `2026-09-02 9:02:42 AM` -- is an ordinary machine, and it was not covered here. Every
    stamp on it parsed as None, and a job with neither a last run nor a next run is
    indistinguishable from one that has never run: four healthy jobs were reported as
    never having run at every session start, which is this module's own failure mode
    turned inside out. Cover the product of the two, not the spellings seen so far.
    """
    text = raw.strip()
    if not text or text.startswith(NEVER):
        return None
    for fmt in (
        "%m/%d/%Y %I:%M:%S %p",
        "%m/%d/%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %I:%M:%S %p",
    ):
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

    **`schtasks` emits one row per *trigger*, not per task**, so a task with more than
    one appears more than once. That was theoretical until `devkit-rc-servers` gained a
    boot trigger beside its repetition, and then it was not: the tray listed the job
    twice and `problems` would have reported any fault with it twice.

    The rows are merged rather than deduplicated by taking the first, because they
    disagree about exactly one field that matters. Every trigger reports the same last
    run and the same result, but its own **next** run -- and `Job.interval` is
    `next_run - last_run`, so keeping a boot trigger's row (which has no next run at all
    until the next boot) would either lose the cadence or invent a wrong one. The
    earliest next run is both the true answer to "when does this fire next" and the one
    that yields the real interval.
    """
    merged: dict[str, Job] = {}
    for row in csv.DictReader(io.StringIO(stdout)):
        name = (row.get("TaskName") or "").lstrip("\\")
        if not name.startswith(prefix) or name == "TaskName":
            continue
        raw_result = (row.get("Last Result") or "").strip()
        try:
            result = int(raw_result)
        except ValueError:
            continue
        job = Job(
            name=name,
            enabled=(row.get("Scheduled Task State") or "").strip().lower() == "enabled",
            last_result=result,
            last_run=parse_time(row.get("Last Run Time") or ""),
            next_run=parse_time(row.get("Next Run Time") or ""),
            command=(row.get("Task To Run") or "").strip(),
        )
        seen = merged.get(name)
        if seen is None:
            merged[name] = job
            continue
        if job.next_run is not None and (seen.next_run is None or job.next_run < seen.next_run):
            merged[name] = replace(seen, next_run=job.next_run)
    return list(merged.values())


def virtualenv_interpreter(job: Job) -> Path | None:
    """The base install behind a job registered against a venv interpreter, else None.

    **This is the one check here that reads the registration rather than the run**, and
    it exists because the failure it catches is invisible to every other row of this
    module's table: the job fires, exits 0, writes its artifact, and puts a console
    window on the desktop anyway.

    `pythonw.exe` inside a virtualenv is not an interpreter. It is a stub deferring to the
    base install named in `pyvenv.cfg`, and **uv builds that stub as a trampoline that
    spawns the base as a child process**. A scheduled task running it is correctly
    console-less, so Windows hands its console-*less* child a brand new visible one --
    which is how `devkit-global-tools`, the only job whose interpreter came from a
    `.venv`, went on flashing a window through two rounds of fixing exactly this bug.
    Both rounds were reported by a human watching windows flash, because every guard was
    a source-level scan of devkit's own files and no scan can tell that a file *named*
    `pythonw.exe` is a trampoline rather than an interpreter. This one asks the machine.

    A venv `<Command>` is worth a line even where the stub is CPython's in-process copy:
    `install-global-tools.interpreter` prefers a checkout's `.venv`, and a box's `.venv`
    is deleted the moment its PR merges, taking the task's command with it.

    None for a job with no command recorded, so a scheduler front end that stops
    reporting one degrades to silence rather than to a false alarm.
    """
    executable = job.interpreter
    if not executable:
        return None
    return devkit_schtasks.venv_home(executable)


def stood_down(path: Path | None = None) -> frozenset[str]:
    """The jobs an operator switched off **on purpose**, per `harness-switch.py`.

    `--off jobs` disables the branch-delivery tasks with `schtasks /Change /DISABLE` and
    records the group in `harness_state`'s ledger. From the scheduler's side that is
    byte-for-byte the accident this module exists to catch -- `Scheduled Task State` says
    `Disabled` either way -- so the scheduler alone cannot tell the two apart, and
    reporting both as faults is what makes the real one skippable.

    **The ledger is the only place the intent is written down**, which is what makes it
    the right thing to consult rather than a suppression list of this module's own: it is
    already the record `--status` prints and `--on` restores from, so a job stops being
    reported exactly when someone stood it down and starts again the moment they put it
    back. A hand-disabled job is in no ledger and is still a fault -- which is the whole
    of the 471-missed-runs incident in the header, and stays reported.

    An alias, kept because this module's readers ask the scheduler's question and should
    not have to know which module owns the switch. `harness_state` owns the ledger, so it
    owns what the ledger means; reading the file a second way here is how the two answers
    would eventually disagree.
    """
    return harness_state.stood_down(path)


def problems(
    jobs: list[Job],
    now: _dt.datetime | None = None,
    deliberate: frozenset[str] = frozenset(),
) -> list[str]:
    """One line per job that needs attention; [] when they are all healthy.

    Ordered so the most actionable comes first, and **at most one line per job** -- a
    disabled job is also stale and also has a stale result, and reporting all three
    would bury the one fact that explains the other two.

    The registration check goes last for that reason and not because it matters least: a
    misregistered command is permanent, so it is still there to report next session,
    while a failing run is the thing that may not be.
    """
    moment = now or _dt.datetime.now()
    found: list[str] = []
    for job in sorted(jobs, key=lambda item: item.name):
        if not job.enabled:
            if job.name in deliberate:
                # Stood down through `harness-switch.py --off jobs`, which wrote the
                # name to the ledger `deliberate` came from. A state someone chose is
                # not a fault, and `--status` is where it is reported; saying it again
                # here as a problem is what trains a reader to skim this whole block.
                continue
            found.append(f"{job.name}: disabled -- nothing is running it")
            continue
        if job.last_run is None:
            # A job registered an hour ago and due tonight has never run and is
            # perfectly healthy. Only a *missed* first run is worth a line, and the
            # scheduler says which that is: its next run is already in the past.
            if job.next_run is None or job.next_run < moment:
                found.append(f"{job.name}: registered but has never run")
            continue
        if job.last_result in OVERLAPPING:
            # Not a failure, but not silence either: a job whose pass outlives its own
            # interval is running essentially continuously, and that background cost is
            # invisible everywhere else.
            found.append(
                f"{job.name}: a run was still going at "
                f"{job.last_run:%Y-%m-%d %H:%M}, so the scheduled fire was skipped -- "
                f"its runs are overlapping"
                f"{artifact_hint(job.name, since=job.last_run)}"
            )
            continue
        if job.last_result not in NOT_A_FAILURE:
            found.append(
                f"{job.name}: last run failed (exit {job.last_result}) at "
                f"{job.last_run:%Y-%m-%d %H:%M}"
                f"{artifact_hint(job.name, since=job.last_run)}"
            )
            continue
        interval = job.interval
        if interval and moment - job.last_run > interval * STALE_INTERVALS:
            missed = (moment - job.last_run) / interval
            found.append(
                f"{job.name}: has not run since {job.last_run:%Y-%m-%d %H:%M} "
                f"({missed:.0f} intervals ago)"
            )
            continue
        base = virtualenv_interpreter(job)
        if base is not None:
            found.append(
                f"{job.name}: runs {job.interpreter}, a virtualenv stub that defers to "
                f"{base} -- under uv it spawns that as a child, and Windows gives the "
                f"child of a console-less task a visible console. Re-run the job's "
                f"installer with --yes to re-register it against the base interpreter"
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
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return parse_tasks(result.stdout, prefix)


def report(prefix: str = PREFIX, now: _dt.datetime | None = None) -> list[str]:
    """The lines a caller should print. Empty means every scheduled job is healthy.

    "Healthy" includes a job deliberately stood down: `problems` is pure and takes the
    intent as an argument, and this is the one place that reads it off the machine.
    """
    return problems(query(prefix), now, stood_down())
