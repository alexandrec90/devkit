#!/usr/bin/env python3
"""Run every registered project's local end-to-end tests, once you are away from the PC.

The tests no hosted runner can run -- the live workspace file, VS Code itself, a Docker
stack, a browser, a credential -- run here, on the workstation that has all of them, and
what they find reaches the fix pass as a red default branch (`e2e_results.py` owns that
half and says why it is a result file rather than this job's exit code).

    e2e-pass.py run [PROJECT ...]     # what `devkit-local-e2e` runs every night
    e2e-pass.py run --no-wait devkit  # now, by hand, whoever is at the keyboard
    e2e-pass.py status                # each project's last result

**Night, and idle.** The scheduled task fires at night (`install-e2e-schedule.py`), and
a run that opens a VS Code window under someone's cursor is worse than a run a night
late. So before each project the pass waits until the keyboard and mouse have been
still for `--idle-minutes`, and gives up at `--until`, leaving the last result in place.
The wait is ours rather than the scheduler's `RunOnlyIfIdle`: the scheduler's idle wait
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

Exits 0 whenever every project reached a verdict, red included; non-zero only when the
pass itself broke, which `log-wrap.py` files on the ledger as a harness defect.

Tested in `tests/test_e2e_pass.py`.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as _dt
import json
import os
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devkit_jsonc
import devkit_project
import e2e_results
import rc_config
import sweep
import task_branch as tb

REPO_ROOT = Path(__file__).resolve().parents[1]

# Set in the environment of every `scripts/e2e.py` the pass runs, so a project's script
# can tell the scheduled run from a person's (and, say, refuse to skip what it would
# skip by hand).
ENV_FLAG = "DEVKIT_E2E"

# The workspace setting that assigns each project's nightly run to one machine.
SETTING = "devkit.localE2e"

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


def _said(done: object) -> str:
    text = f"{getattr(done, 'stderr', '') or ''}\n{getattr(done, 'stdout', '') or ''}"
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else "no output"


def _ok(done: object) -> bool:
    return done is not None and getattr(done, "returncode", 1) == 0


def _out(done: object) -> str:
    return str(getattr(done, "stdout", "") or "").strip()


def prepare(git: Git, base: str) -> tuple[str, str]:
    """`(sha, "")` with the checkout at origin/<base>'s tip, or `("", why)` when it is not
    this pass's to move."""
    fetched = git("fetch", "--quiet", "origin")
    if not _ok(fetched):
        return "", f"git fetch failed: {_said(fetched)}"
    branch = _out(git("branch", "--show-current"))
    if branch != base:
        return "", f"checkout is on {branch or 'a detached HEAD'}, not {base}"
    dirty = git("status", "--porcelain", "--untracked-files=no")
    if not _ok(dirty) or _out(dirty):
        return "", "checkout has uncommitted changes to tracked files"
    merged = git("merge", "--ff-only", "--quiet", f"origin/{base}")
    if not _ok(merged):
        return "", f"cannot fast-forward to origin/{base}: {_said(merged)}"
    return _out(git("rev-parse", "HEAD")), ""


def run_contract(project_dir: Path, timeout: float, runner=sweep.run_bounded) -> object:
    """`scripts/e2e.py` run in `project_dir` under the project's own interpreter."""
    python = e2e_results.venv_interpreter(project_dir, sweep.console_python())
    env = {**os.environ, ENV_FLAG: "1", "PYTHONUTF8": "1"}
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
        e2e_results.write(result, seams.root)
        return result
    done = run_contract(project_dir, timeout, seams.runner)
    output = f"{getattr(done, 'stdout', '') or ''}\n{getattr(done, 'stderr', '') or ''}"
    code = int(getattr(done, "returncode", 1))
    where = e2e_results.result_dir(name, seams.root)
    where.mkdir(parents=True, exist_ok=True)
    (where / e2e_results.OUTPUT_FILE).write_text(output, encoding="utf-8")
    green = code == 0
    result = replace(
        result,
        status=e2e_results.PASSED if green else e2e_results.FAILED,
        sha=sha,
        exit_code=code,
        finished=stamp(),
        reason="" if green else ("timed out" if code == sweep.TIMED_OUT else ""),
        signature=() if green else e2e_results.signature_of(output, code),
    )
    e2e_results.write(result, seams.root)
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
    if _ok(git("cat-file", "-e", f"origin/{base}:{e2e_results.CONTRACT}")):
        return None
    stamp = seams.now().isoformat(timespec="seconds")
    tip = e2e_results.origin_tip(git, base)
    result = e2e_results.Result(
        project=name,
        status=e2e_results.FAILED if tip else e2e_results.SKIPPED,
        sha=tip,
        base=base,
        started=stamp,
        finished=stamp,
        reason=f"origin/{base} has no {e2e_results.CONTRACT}" if tip else f"no origin/{base}",
        signature=(e2e_results.MISSING,) if tip else (),
    )
    e2e_results.write(result, seams.root)
    return result


# --- the pass -----------------------------------------------------------------------------


def registered(workspace: Path) -> list[str]:
    return devkit_project.known_projects(workspace.read_text(encoding="utf-8"))


def assignment(text: str) -> rc_config.Config:
    """`SETTING`'s `hosts` off a workspace file; no hosts for anything absent or broken,
    which reads as "every machine tests its own"."""
    try:
        payload = devkit_jsonc.loads(text)
    except (json.JSONDecodeError, TypeError):
        return rc_config.Config()
    settings = payload.get("settings") if isinstance(payload, dict) else None
    raw = settings.get(SETTING) if isinstance(settings, dict) else None
    hosts = raw.get("hosts") if isinstance(raw, dict) else None
    return rc_config.Config(hosts=rc_config.parse_hosts(hosts))


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


def line_for(result: e2e_results.Result) -> str:
    if result.status == e2e_results.PASSED:
        return f"{result.project}: passed at {result.sha[:9]}"
    if result.status == e2e_results.SKIPPED:
        return f"{result.project}: skipped -- {result.reason}"
    shown = ", ".join(result.signature[:3])
    more = f" (+{len(result.signature) - 3} more)" if len(result.signature) > 3 else ""
    return f"{result.project}: FAILED at {result.sha[:9]} -- {shown}{more}"


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
    return run_pass(workspace, list(args.projects), args)


if __name__ == "__main__":
    sys.exit(main())
