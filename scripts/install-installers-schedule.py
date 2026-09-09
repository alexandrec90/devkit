#!/usr/bin/env python3
"""Register `installers.py maintain` as a recurring OS task.

`installers.py` explains what the pass does: every other installer's `--check`, then
`--yes` on each that needs it. This registers the pass, invoking

    <python> scripts/installers.py maintain

daily, and once shortly after logon. The logon trigger is the half that matters on a
laptop: a checkout moved or a devkit pulled during the day is caught at the next start
rather than tomorrow morning, and a fresh machine that has run this one installer has
every other job registered before the first coffee. Daily as well, because `schedule_health`
derives a job's cadence from its next run and a logon-only task has none.

**This is the one installer a machine runs by hand**, once. Everything the others
register is then kept current by the job this registers -- including this job itself,
since `installers.py` discovers this file like any other `install-*.py`.

**Read-only by default.** `--yes` installs, `--check` reports whether the registered task
is the one this checkout would register, and the bare invocation prints the plan. Same
three modes as `install-reap-schedule.py`, for the same reason.

Stdlib only, and every decision is an importable function tested in
`tests/test_install_installers_schedule.py`.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devkit_schtasks
import harness_state
import sweep

REPO_ROOT = Path(__file__).resolve().parents[1]

# Stable for `install-rc-schedule.TASK_NAME`'s reason: renaming it orphans whatever a
# previous version registered.
TASK_NAME = "devkit-installers"

# Machine maintenance, not branch delivery: standing the agent tier down must not stop
# the pass that keeps the machine's own jobs registered.
GROUP = "maintenance"

# `installers.py` writes it on every exit path; `schedule_health.ARTIFACTS` sends a
# reader here when the scheduler reports a failure.
ARTIFACT = "logs/installers.log"

WINDOWS = os.name == "nt"

# Before the 09:00 workspace-status pass, so the report a person reads describes a
# machine this pass has already put right; after the small-hours jobs, so it never
# re-registers one mid-fire.
DEFAULT_AT = "08:45"

# The logon delay is not decoration: at the instant a logon trigger would otherwise fire
# the scheduler service is still settling, and `schtasks` answers a query about a task
# it is mid-way through loading with an error the check reads as "not registered".
LOGON_DELAY = "PT2M"

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def run_command(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), capture_output=True, text=True, check=False)


@dataclass(frozen=True)
class Schedule:
    """What is to be registered, resolved from this checkout."""

    name: str
    python: str
    script: str
    at: str

    @property
    def command(self) -> list[str]:
        """The argv the scheduler runs. `maintain` is named explicitly: the default mode
        is read-only on purpose, and a task that became a no-op because a default changed
        is the failure `tests/test_scheduled_jobs.py` is downstream of."""
        return [self.python, self.script, "maintain"]


def windowless_python(executable: str = sys.executable) -> str:
    """The windowless interpreter for `executable`, defaulting to this one. Survivable
    because `installers.main` writes its artifact on every exit path."""
    return devkit_schtasks.windowless(executable)


def schedule_for(at: str = DEFAULT_AT, root: Path = REPO_ROOT) -> Schedule:
    """Resolve the schedule against *this* interpreter and *this* checkout."""
    return Schedule(
        name=TASK_NAME,
        python=windowless_python(),
        script=str((root / "scripts" / "installers.py").resolve()),
        at=at,
    )


def valid_time(at: str) -> bool:
    """`HH:MM`, 24-hour. Both schedulers take it, and neither says so when it is wrong."""
    hours, _, minutes = at.partition(":")
    if not (hours.isdigit() and minutes.isdigit()) or len(hours) != 2 or len(minutes) != 2:
        return False
    return 0 <= int(hours) <= 23 and 0 <= int(minutes) <= 59


def task_document(schedule: Schedule) -> str:
    """The Windows registration, as a task document, through `devkit_schtasks` for the
    settings a command-line registration cannot express.

    Half an hour is generous for ten `--check`s and however many `--yes`es, and finite:
    `IgnoreNew` means a wedged run suppresses every later fire until the limit expires.
    """
    program, *arguments = schedule.command
    return devkit_schtasks.task_xml(
        program,
        subprocess.list2cmdline(arguments),
        devkit_schtasks.daily_trigger(schedule.at) + devkit_schtasks.logon_trigger(LOGON_DELAY),
        time_limit="PT30M",
        # `PureWindowsPath`, not `Path`: the document is Windows by construction, so it
        # has to be split on backslashes whatever host builds it.
        working_dir=str(PureWindowsPath(schedule.script).parent.parent),
        # Lands disabled when this job has been stood down by name. The pass itself is
        # maintenance and no group stands it down, but `--off --job` can, and an
        # installer that ignored the ledger would hand the operator back a running job.
        enabled=TASK_NAME not in harness_state.stood_down(),
    )


def crontab_line(schedule: Schedule) -> str:
    """The POSIX equivalent, for a machine that is not this one."""
    hours, _, minutes = schedule.at.partition(":")
    return f"{int(minutes)} {int(hours)} * * * {subprocess.list2cmdline(schedule.command)}"


def render_plan(schedule: Schedule, windows: bool = WINDOWS) -> str:
    """What `--yes` would do, in the words of whichever scheduler is going to do it."""
    lines = [
        f"schedule: {schedule.name} -- daily at {schedule.at}, and two minutes after logon",
        f"  runs: {subprocess.list2cmdline(schedule.command)}",
        "",
        "Each fire runs every other installer's --check and re-registers whatever has",
        "drifted: a job never installed, a checkout that moved, an installer that gained a",
        "flag, a task disabled by nobody. A job stood down by harness-switch.py stays down.",
        "Options an installer should keep are `devkit.installers` in the workspace file.",
        "",
        f"  log: {ARTIFACT}, rewritten on every pass",
        "",
    ]
    if windows:
        lines.append(
            "  via: a scheduled task registered from XML, so it runs on battery "
            "and catches up a run it slept through"
        )
    else:
        lines += [
            "  via crontab, which this installer does not edit for you:",
            f"    {crontab_line(schedule)}",
        ]
    return "\n".join(lines)


def install(schedule: Schedule, runner: Runner = run_command) -> tuple[bool, str]:
    """Register it. `(ok, message)`; POSIX is reported as unsupported rather than faked."""
    if not WINDOWS:
        return False, (
            "not a Windows machine -- add this crontab line yourself:\n  " + crontab_line(schedule)
        )
    ok, message = devkit_schtasks.register(schedule.name, task_document(schedule), runner)
    if not ok:
        return False, message
    return True, f"scheduled {schedule.name} daily at {schedule.at} and at logon"


def run_check(schedule: Schedule, runner: Runner = run_command) -> tuple[int, str]:
    """`(exit code, message)` for `--check`, per `devkit_schtasks.run_check`."""
    if not WINDOWS:
        return (
            devkit_schtasks.CHECK_CURRENT,
            "not a Windows machine -- nothing this installer can query",
        )
    return devkit_schtasks.run_check(schedule.name, task_document(schedule), runner)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--yes", action="store_true", help="register the task")
    mode.add_argument(
        "--check",
        action="store_true",
        help="report whether the registered task is the one this checkout would register",
    )
    parser.add_argument("--at", default=DEFAULT_AT, help="daily start time, HH:MM (24-hour)")
    parser.add_argument(
        "--devkit",
        type=Path,
        default=REPO_ROOT,
        help=(
            "the devkit checkout the task should run from (default: this one). Name the "
            "*static* checkout when installing from a temporary one"
        ),
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if not valid_time(args.at):
        parser.error(f"--at must be HH:MM in 24-hour time, not {args.at!r}")
    root = args.devkit.expanduser().resolve()
    script = root / "scripts" / "installers.py"
    if not script.is_file():
        print(f"schedule: no runner at {script}", file=sys.stderr)
        return 2
    if args.yes and sweep.source_checkout(root) != root:
        # A task pointing into a temporary checkout works until that checkout is deleted
        # and then fails at every logon, in silence. Refused on `--yes` only, so the plan
        # and the check still read from the place an agent is standing.
        print(
            f"schedule: {root} is a temporary checkout -- an ephemeral box or a claude "
            f"--worktree worktree. Point --devkit at the static checkout, which outlives both.",
            file=sys.stderr,
        )
        return 2

    schedule = schedule_for(args.at, root)
    if args.check:
        code, message = run_check(schedule)
        print(message, file=sys.stderr if code else sys.stdout)
        return code
    if not args.yes:
        print(render_plan(schedule))
        print("\nNothing was registered. Re-run with --yes to install.")
        return 0

    ok, message = install(schedule)
    print(message, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
