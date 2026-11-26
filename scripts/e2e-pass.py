#!/usr/bin/env python3
"""Run every registered project's local end-to-end tests, once you are away from the PC.

The tests no hosted runner can run -- the live workspace file, VS Code itself, a Docker
stack, a browser, a credential -- run here, on the workstation that has all of them, and
what they find reaches the fix pass as a red default branch (`e2e_results.py` owns that
half and says why it is a result file rather than this job's exit code).

    e2e-pass.py run --due             # what `devkit-local-e2e` runs every half hour
    e2e-pass.py run [PROJECT ...]     # by hand: these, once the PC is idle
    e2e-pass.py run --no-wait devkit  # now, by hand, whoever is at the keyboard
    e2e-pass.py status                # each project's last result

**Only while the PC is idle, behind everything else on it.** The scheduled fire
(`install-e2e-schedule.py`) runs each project whose tip moved since its last verdict
(`is_due`), and only while the keyboard and mouse have been still for `--idle-minutes`,
or far longer for a tier that opens windows (`OPENS_WINDOWS`): a window under someone's
cursor is worse than a run an hour late. A PC in use is the next fire's. Once a night was
too slow for a tip that moves a hundred times a day: the fix pass holds a red whose commit
the tip has passed, so a night's red was moot by the time it was read. Everything runs at
below-normal priority (`low_priority.py`). By hand without `--due`, the pass waits for
that stillness itself and gives up at `--until`. The wait is ours rather than the scheduler's `RunOnlyIfIdle`: the scheduler's idle wait
is a setting no test can drive and that newer Windows versions document as ignored,
while `GetLastInputInfo` is one stdlib call a test can replace.

**What is tested is origin's tip, from the static checkout.** A box would be cut fresh
and could not answer the checks whose whole subject is the static checkout (the live
workspace file is rendered from it). So each checkout must be on its default branch with
no tracked edit, and is fast-forwarded to `origin/<default>` -- `merge --ff-only`, the
same move `sweep.py --sync` makes, which never rewrites anything. A checkout that is not
in that state is skipped with the reason written to its result: a person is working in
it, and it is theirs.

**Every project owes `scripts/e2e.py`** (`e2e_results.CONTRACT`). One whose
`origin/<default>` has none is recorded red as a missing tier (`record_missing`), which
sends its fixer to write one. It is asked of origin, not of the checkout: a checkout a
person is working in may lag, and the tier a fixer just merged must stop the red the
next night, whatever branch the static checkout is on. No idle wait comes first, since
nothing runs.

**One machine tests each project** (`SETTING`'s `hosts`). The workspace file is the same
on every PC and every PC registers the job, so with several of them each would test
every project, write its own result and send its own fixer at the same red. `hosts`
names the machine, in `devkit.remoteControl`'s shape (`rc_config.parse_hosts`): one
name for every project, or `{"*": "pc-a", "carameli": "pc-b"}` to spread them. Moving
the job is editing that value; each PC reads it at fire time, so nothing is re-registered.
Absent, every machine tests its own checkouts, which is right while there is one.

**With no `hosts` and a role, the machines claim each run instead** (`machine_role.py`):
`e2e-<project>-<sha12>` through `job_claims`, which a `worker` takes as soon as it is
idle and a `workstation` only once `job_claims.WORKER_FIRST` has passed with nobody
claiming it, so a worker that is off still leaves the tier running. A run that happened
marks its claim finished (`finished`), so the other PC never re-tests the commit at the
TTL. Either way, no run starts below the disk floor (`job_claims.disk_refusal`).

Exits 0 whenever every project reached a verdict, red included; non-zero only when the
pass itself broke, which `log-wrap.py` files on the ledger as a harness defect.

Tested in `tests/test_e2e_pass.py`.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as _dt
import os
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devkit_project
import e2e_results
import job_claims
import low_priority
import machine_role
import rc_config
import sweep
import task_branch as tb

REPO_ROOT = Path(__file__).resolve().parents[1]

# Set in the environment of every `scripts/e2e.py` the pass runs, so a project's script
# can tell the scheduled run from a person's (and, say, refuse to skip what it would
# skip by hand).
ENV_FLAG = "DEVKIT_E2E"

# What every run adds to the environment it inherits. A name, not an inline literal,
# because a fixer's prompt has to tell it what to set to reproduce a red run
# (`fix_prompts.LOCAL_ENV`, which a test holds to this).
RUN_ENV = {ENV_FLAG: "1", "PYTHONUTF8": "1"}

# The workspace setting that assigns each project's nightly run to one machine.
SETTING = rc_config.LOCAL_E2E_SETTING

DEFAULT_IDLE_MINUTES = 10
DEFAULT_UNTIL = "06:30"
# One project's ceiling. devkit's own tier drives every workspace task twice in a real
# VS Code, which is most of half an hour; a hung editor is the likely way to reach this,
# and the run is ended with its whole process tree and recorded red, naming the timeout.
DEFAULT_TIMEOUT_MINUTES = 60
# How often the idle wait looks again.
POLL_SECONDS = 120

Clock = Callable[[], _dt.datetime]
Idle = Callable[[], float]
Sleep = Callable[[float], None]
Git = sweep.Git


# --- idle ---------------------------------------------------------------------------------


def idle_seconds() -> float:
    """Seconds since the last keyboard or mouse input in this session.

    `GetLastInputInfo` answers for the interactive session the task runs in -- every
    devkit job runs with the interactive token (`devkit_schtasks.task_xml`). Elsewhere
    there is no session to disturb, so the answer is "idle forever".
    """
    if sys.platform != "win32":
        return float("inf")

    class LastInput(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    info = LastInput()
    info.cbSize = ctypes.sizeof(LastInput)
    windll = ctypes.windll  # win32 only: mypy narrows on the platform guard above
    if not windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    # Both are 32-bit millisecond tick counts, so the difference wraps every 49.7 days.
    elapsed = (windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF
    return elapsed / 1000.0


def deadline_at(until: str, now: _dt.datetime) -> _dt.datetime:
    """The next `until` (HH:MM, local) after `now`: tonight's 06:30 for a 01:00 start,
    and tomorrow's for a run started by hand at 07:00."""
    hour, minute = (int(part) for part in until.split(":", 1))
    at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return at if at > now else at + _dt.timedelta(days=1)


def wait_for_idle(
    need: float,
    deadline: _dt.datetime,
    *,
    idle: Idle = idle_seconds,
    now: Clock = _dt.datetime.now,
    sleep: Sleep = time.sleep,
    poll: float = POLL_SECONDS,
) -> bool:
    """Block until input has been still for `need` seconds; False once `deadline` passes.

    Sleeps no longer than the stillness still owed, so a machine that went quiet a minute
    before a poll is not made to wait a whole extra poll.
    """
    while True:
        still = idle()
        if still >= need:
            return True
        left = (deadline - now()).total_seconds()
        if left <= 0:
            return False
        sleep(max(1.0, min(poll, need - still, left)))


# --- one project --------------------------------------------------------------------------


def prepare(git: Git, base: str) -> tuple[str, str]:
    """`(sha, "")` with the checkout at origin/<base>'s tip, or `("", why)` when it is not
    this pass's to move."""
    ok, out, said = e2e_results.ok, e2e_results.out, e2e_results.said
    fetched = git("fetch", "--quiet", "origin")
    if not ok(fetched):
        return "", f"git fetch failed: {said(fetched)}"
    branch = out(git("branch", "--show-current"))
    if branch != base:
        return "", f"checkout is on {branch or 'a detached HEAD'}, not {base}"
    dirty = git("status", "--porcelain", "--untracked-files=no")
    if not ok(dirty) or out(dirty):
        return "", "checkout has uncommitted changes to tracked files"
    merged = git("merge", "--ff-only", "--quiet", f"origin/{base}")
    if not ok(merged):
        return "", f"cannot fast-forward to origin/{base}: {said(merged)}"
    return out(git("rev-parse", "HEAD")), ""


def run_contract(project_dir: Path, timeout: float, runner=sweep.run_bounded) -> object:
    """`scripts/e2e.py` run in `project_dir` under the project's own interpreter."""
    python = e2e_results.venv_interpreter(project_dir, sweep.console_python())
    env = {**os.environ, **RUN_ENV}
    argv = [python, str(project_dir / e2e_results.CONTRACT)]
    return runner(argv, timeout, cwd=str(project_dir), env=env)


@dataclass(frozen=True)
class Seams:
    """Everything the pass reaches outside itself -- the clock, the input idle counter,
    git, the spawn, and where results land -- so a test replaces it in one argument."""

    root: Path = REPO_ROOT
    idle: Idle = idle_seconds
    now: Clock = _dt.datetime.now
    sleep: Sleep = time.sleep
    git_for: Callable[[Path], Git] = sweep.git_for
    runner: Callable[..., object] = sweep.run_bounded
    # This machine, spelled as `hosts` is matched against.
    host: str = field(default_factory=rc_config.harness_events.host_name)
    # Free disk on the checkouts' drive (`job_claims.disk_refusal`).
    disk: Callable[[], int | None] = job_claims.disk_here


def run_project(
    name: str, project_dir: Path, timeout: float, seams: Seams | None = None
) -> e2e_results.Result:
    """One project, prepared, run and recorded: its result file and its output."""
    seams = seams or Seams()
    clock = seams.now

    def stamp() -> str:
        return clock().isoformat(timespec="seconds")

    git = seams.git_for(project_dir)
    base = tb.detect_default_branch(git, fallback="main")
    result = e2e_results.Result(
        project=name, status=e2e_results.SKIPPED, base=base, started=stamp()
    )
    sha, why = prepare(git, base)
    if why:
        result = replace(result, reason=why, finished=stamp())
        last = e2e_results.read(name, seams.root)
        # A red stays on record until a run says otherwise: a person's dirty checkout is
        # not a pass, and this fires through the day they are working in it.
        if last is None or last.status != e2e_results.FAILED:
            e2e_results.write(result, seams.root)
        return result
    done = run_contract(project_dir, timeout, seams.runner)
    output = f"{getattr(done, 'stdout', '') or ''}\n{getattr(done, 'stderr', '') or ''}"
    code = int(getattr(done, "returncode", 1))
    why = "timed out" if code == sweep.TIMED_OUT else ""
    ended = replace(result, sha=sha, finished=stamp())
    result = e2e_results.verdict(ended, code, output, reason=why)
    e2e_results.record(result, output, seams.root)
    return result


def record_missing(
    name: str, project_dir: Path, seams: Seams | None = None
) -> e2e_results.Result | None:
    """A project with no tier on origin's tip, recorded; None when origin has one.

    Red when there is a tip to pin it to, so the fix pass sends this project's fixer to
    write the tier. With no `origin/<default>` there is nothing a fixer could branch
    from, so that is recorded as a skip naming why.
    """
    seams = seams or Seams()
    git = seams.git_for(project_dir)
    git("fetch", "--quiet", "origin")
    base = tb.detect_default_branch(git, fallback="main")
    if e2e_results.ok(git("cat-file", "-e", f"origin/{base}:{e2e_results.CONTRACT}")):
        return None
    stamp = seams.now().isoformat(timespec="seconds")
    tip = e2e_results.origin_tip(git, base)
    result = e2e_results.missing_result(name, base, tip, stamp)
    e2e_results.write(result, seams.root)
    return result


# --- the pass -----------------------------------------------------------------------------


def registered(workspace: Path) -> list[str]:
    return devkit_project.known_projects(workspace.read_text(encoding="utf-8"))


def assignment(text: str) -> rc_config.Config:
    """`SETTING`'s `hosts` off a workspace file; no hosts for anything absent or broken,
    which reads as "every machine tests its own"."""
    return rc_config.setting_hosts(text, SETTING)


def tested_here(workspace: Path, names: list[str], host: str) -> list[str]:
    """The names `SETTING` assigns this machine, saying where each other one is tested."""
    config = assignment(workspace.read_text(encoding="utf-8"))
    here, elsewhere = rc_config.assigned_here(config, names, host, verb="tested")
    for line in elsewhere:
        print(line, flush=True)
    return here


def opted_in(workspace: Path, names: Iterable[str]) -> tuple[list[str], list[str]]:
    """`(with an e2e tier, without)` among `names`, in order."""
    having: list[str] = []
    lacking: list[str] = []
    for name in names:
        contract = workspace.parent / name / e2e_results.CONTRACT
        (having if contract.is_file() else lacking).append(name)
    return having, lacking


def to_run_after_all(workspace: Path, lacking: Iterable[str], seams: Seams) -> list[str]:
    """Record each checkout lacking the script whose origin lacks it too; return the
    others, whose fast-forward will bring the script in, to be run with the rest."""
    late: list[str] = []
    for name in lacking:
        missing = record_missing(name, workspace.parent / name, seams)
        if missing is None:
            late.append(name)
        else:
            print(line_for(missing), flush=True)
    return late


line_for = e2e_results.line_for


def run_pass(
    workspace: Path,
    only: list[str],
    args: argparse.Namespace,
    seams: Seams | None = None,
    run_one: Callable[[str, Path, float, Seams | None], e2e_results.Result] = run_project,
) -> int:
    seams = seams or Seams()
    known = registered(workspace)
    unknown = [name for name in only if name not in known]
    if unknown:
        print(f"e2e-pass: not registered in the workspace: {', '.join(unknown)}", file=sys.stderr)
        return 2
    having, lacking = opted_in(workspace, tested_here(workspace, only or known, seams.host))
    having += to_run_after_all(workspace, lacking, seams)
    deadline = deadline_at(args.until, seams.now())
    need = args.idle_minutes * 60.0
    for name in having:
        idle = args.no_wait or wait_for_idle(
            need, deadline, idle=seams.idle, now=seams.now, sleep=seams.sleep
        )
        if not idle:
            left = ", ".join(having[having.index(name) :])
            print(f"e2e-pass: never idle before {args.until}; not run tonight: {left}")
            return 0
        print(f"{name}: running {e2e_results.CONTRACT}", flush=True)
        result = run_one(name, workspace.parent / name, args.timeout_minutes * 60.0, seams)
        print(line_for(result), flush=True)
    return 0


# --- the scheduled run ---------------------------------------------------------------------

# Projects whose tier opens windows on this desktop: VS Code, tkinter. A person back at
# the keyboard mid-run would find one under their cursor, so these wait for a PC that has
# been left for much longer, and run about once a day.
OPENS_WINDOWS = frozenset({"devkit", "radio"})
WINDOWED_IDLE_MINUTES = 60.0
WINDOWED_GAP = _dt.timedelta(hours=20)
# How long after a run the next may start, however often the tip moves.
GAP = _dt.timedelta(hours=3)
# After a red it is shorter: a fix that merged has moved the tip, and until a run at the
# new one the fix pass holds that red (`fix_plan.is_stale_local`).
RED_GAP = _dt.timedelta(hours=1)


def idle_minutes_for(name: str, asked: float) -> float:
    return max(asked, WINDOWED_IDLE_MINUTES) if name in OPENS_WINDOWS else asked


def is_due(name: str, result: e2e_results.Result | None, tip: str, now: _dt.datetime) -> bool:
    """Whether `name` is owed a run at `tip`: never judged there, and not run just now. A
    tip that could not be read is owed nothing."""
    if not tip:
        return False
    if result is None:
        return True
    if result.status != e2e_results.SKIPPED and result.sha == tip:
        return False
    gap = WINDOWED_GAP if name in OPENS_WINDOWS else GAP
    if result.status == e2e_results.FAILED:
        gap = RED_GAP
    try:
        return now - _dt.datetime.fromisoformat(result.finished) >= gap
    except (ValueError, TypeError):
        return True


def tip_of(project_dir: Path, seams: Seams) -> str:
    """origin's tip as it is now, or "" when origin cannot be read."""
    git = seams.git_for(project_dir)
    if not e2e_results.ok(git("fetch", "--quiet", "origin")):
        return ""
    return e2e_results.origin_tip(git, tb.detect_default_branch(git, fallback="main"))


def run_due(
    workspace: Path,
    args: argparse.Namespace,
    seams: Seams | None = None,
    run_one: Callable[[str, Path, float, Seams | None], e2e_results.Result] = run_project,
) -> int:
    """What the scheduled job runs, every half hour: each project owed a run, for as long
    as the PC stays idle.

    Nothing waits here. A PC in use is the next fire's, a half hour on, so a red is
    re-tested the first time its person steps away rather than the next night. Idle is
    asked again before each project, and first of all, so a fire at a PC in use fetches
    nothing.
    """
    seams = seams or Seams()
    if seams.idle() < args.idle_minutes * 60.0:
        print("e2e-pass: the PC is in use; the next fire looks again")
        return 0
    names = tested_here(workspace, registered(workspace), seams.host)
    role = shared(workspace, seams)
    due = []
    tips: dict[str, str] = {}
    for name in names:
        last = e2e_results.read(name, seams.root)
        tips[name] = tip_of(workspace.parent / name, seams)
        if is_due(name, last, tips[name], seams.now()):
            due.append(name)
    having, lacking = opted_in(workspace, due)
    having += to_run_after_all(workspace, lacking, seams)
    for name in having:
        if seams.idle() < idle_minutes_for(name, args.idle_minutes) * 60.0:
            print(f"{name}: not idle long enough; the next fire looks again", flush=True)
            continue
        if why := job_claims.disk_refusal(seams.disk()):
            print(f"{name}: {why}; the next fire looks again", flush=True)
            continue
        job = job_id(name, tips.get(name, ""))
        if role and (why := not_ours(name, workspace.parent / name, job, role, seams)):
            print(f"{name}: {why}", flush=True)
            continue
        print(f"{name}: running {e2e_results.CONTRACT}", flush=True)
        result = run_one(name, workspace.parent / name, args.timeout_minutes * 60.0, seams)
        print(line_for(result), flush=True)
        if role:
            finished(workspace.parent / name, job, seams.git_for, seams.host)
    return 0


# --- across machines, with no `hosts` ---------------------------------------------------------

# The file a workstation keeps its first sight of each run in (`job_claims.Sightings`).
SIGHTINGS = "e2e"


def job_id(project: str, tip: str) -> str:
    return f"e2e-{project}-{tip[:12]}"


def shared(workspace: Path, seams: Seams) -> str:
    """This machine's role when it claims each run, else "": it has one
    (`machine_role.py`) and `SETTING` names no `hosts`. Named hosts are a person's
    assignment and stand; no role is today's behaviour, every machine testing its own."""
    try:
        text = workspace.read_text(encoding="utf-8")
    except OSError:
        return ""
    return "" if assignment(text).hosts else machine_role.role_of(text)


def not_ours(name: str, project_dir: Path, job: str, role: str, seams: Seams) -> str:
    """Why another machine runs `job`, or "" with it claimed here: a workstation's window
    for a worker, then the claim. A claim that could not be made fails open, as the medium
    tier's does: a commit tested twice costs a run, one tested nowhere hides a red."""
    if why := job_claims.Sightings(seams.root, SIGHTINGS).hold(role, job):
        return why
    attempt = job_claims.claim(seams.git_for(project_dir), job, job_claims.E2E, seams.host)
    if attempt.outcome == job_claims.LOST:
        return f"claimed by {attempt.holder or 'another machine'}"
    if attempt.outcome == job_claims.FAILED:
        print(f"{name}: claim failed open -- {attempt.said[:120]}", flush=True)
    return ""


def finished(project_dir: Path, job: str, git_for=sweep.git_for, host: str = "") -> None:
    """Mark `job`'s claim as a run that happened, so no PC breaks it at the TTL to test
    the same commit again (`job_claims.finish`). A failure is said, not raised: it costs
    the other PC a duplicate run, never a lost one."""
    host = host or rc_config.harness_events.host_name()
    if not job_claims.finish(git_for(project_dir), job, job_claims.E2E, host):
        print(f"{job}: could not mark the claim finished", flush=True)


def status(workspace: Path) -> int:
    for name in registered(workspace):
        result = e2e_results.read(name)
        if result is None:
            continue
        print(f"{line_for(result)} ({result.finished or result.started})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    verbs = parser.add_subparsers(dest="verb", required=True)
    run = verbs.add_parser("run", help="run each project's scripts/e2e.py")
    run.add_argument("projects", nargs="*", help="registered checkouts; none means all")
    run.add_argument(
        "--due", action="store_true", help="what origin has moved past, while the PC is idle"
    )
    run.add_argument("--no-wait", action="store_true", help="run now, idle or not")
    run.add_argument("--idle-minutes", type=float, default=DEFAULT_IDLE_MINUTES)
    run.add_argument("--until", default=DEFAULT_UNTIL, help="give up waiting at HH:MM")
    run.add_argument("--timeout-minutes", type=float, default=DEFAULT_TIMEOUT_MINUTES)
    verbs.add_parser("status", help="each project's last result")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    workspace = sweep.default_workspace(REPO_ROOT)
    if not workspace.is_file():
        print(f"e2e-pass: no workspace file at {workspace}", file=sys.stderr)
        return 2
    if args.verb == "status":
        return status(workspace)
    low_priority.lower()
    if args.due:
        return run_due(workspace, args)
    return run_pass(workspace, list(args.projects), args)


if __name__ == "__main__":
    sys.exit(main())
