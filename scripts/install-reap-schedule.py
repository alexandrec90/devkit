#!/usr/bin/env python3
"""Register `reap-stale.py maintain` as a recurring OS task.

`reap-stale.py` explains what the job reaps and what it never touches. This registers
it: a Task Scheduler entry on Windows, a crontab line elsewhere, invoking

    <python> scripts/reap-stale.py maintain --workspace <workspace>

every `--every` minutes. Each fire stops the Remote Control sessions nobody has used for
`sessionIdleMinutes`, the named servers `rc-servers.py` no longer owns once nothing live
is under them, and the dev servers whose agent has gone.

**Frequent rather than daily**, at `devkit-rc-servers`' interval and for a related
reason: what it reclaims is memory a working machine is short of *now*, and the cost of
a late reap is a desk that pages for the rest of the afternoon. The pass is cheap on a
tick that finds nothing -- one process listing and a `stat` per transcript.

**Read-only by default.** `--yes` installs, `--check` reports what is registered and
whether it still points at this checkout, and the bare invocation prints the plan. Same
three modes as `install-rc-schedule.py`, for the same reason.

Stdlib only, and every decision is an importable function tested in
`tests/test_install_reap_schedule.py`.
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
import installer_cli
import harness_state
import sweep

REPO_ROOT = Path(__file__).resolve().parents[1]
BOXES_DIR = sweep.BOXES_DIR_NAME

# Stable for `install-rc-schedule.TASK_NAME`'s reason: renaming it orphans whatever a
# previous version registered.
TASK_NAME = "devkit-reap-stale"

# Machine maintenance, not branch delivery: standing the agent tier down leaves this
# running. `tests/test_installer_contract.py` holds the switch's list to this word.
GROUP = "maintenance"

# `reap-stale.py` writes it on every exit path; `schedule_health.ARTIFACTS` sends a
# reader here when the scheduler reports a failure.
ARTIFACT = "logs/reap-stale.log"

WINDOWS = os.name == "nt"

DEFAULT_INTERVAL = 15

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def run_command(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), capture_output=True, text=True, check=False)


@dataclass(frozen=True)
class Schedule:
    """What is to be registered, resolved from this checkout."""

    name: str
    python: str
    script: str
    every: int
    workspace: str = ""

    @property
    def command(self) -> list[str]:
        """The argv the scheduler runs. `maintain` is named explicitly: the default mode
        is read-only on purpose, and a task that became a no-op because a default changed
        is the failure `tests/test_scheduled_jobs.py` is downstream of."""
        argv = [self.python, self.script, "maintain"]
        if self.workspace:
            argv += ["--workspace", self.workspace]
        return argv


def windowless_python(executable: str = sys.executable) -> str:
    """The windowless interpreter for `executable`, defaulting to this one. Survivable
    because `reap-stale.main` writes its artifact on every exit path."""
    return devkit_schtasks.windowless(executable)


def schedule_for(every: int = DEFAULT_INTERVAL, root: Path = REPO_ROOT) -> Schedule:
    """Resolve the schedule against *this* interpreter and *this* checkout."""
    workspace = sweep.default_workspace(root)
    return Schedule(
        name=TASK_NAME,
        python=windowless_python(),
        script=str((root / "scripts" / "reap-stale.py").resolve()),
        every=every,
        workspace=str(workspace) if workspace else "",
    )


def valid_interval(every: int) -> bool:
    """Minutes, positive, at most a day."""
    return isinstance(every, int) and not isinstance(every, bool) and 1 <= every <= 1440


def task_document(schedule: Schedule) -> str:
    """The Windows registration, as a task document, through `devkit_schtasks` for the
    settings a command-line registration cannot express.

    The time limit is shorter than the interval so a wedged fire cannot suppress the next
    one under `IgnoreNew`: the pass reads one process table and issues a handful of
    `taskkill`s, each with a five-second grace, and ten minutes is generous for that.
    """
    program, *arguments = schedule.command
    return devkit_schtasks.task_xml(
        program,
        subprocess.list2cmdline(arguments),
        # No boot trigger, unlike `devkit-rc-servers`: a reboot leaves nothing to reap,
        # and the first repetition is at most an interval away.
        devkit_schtasks.repeating_trigger(schedule.every),
        time_limit="PT10M",
        working_dir=str(PureWindowsPath(schedule.script).parent.parent),
        # Lands disabled when this job has been stood down by name (`harness-switch.py
        # --off --job`): the ledger is the standing instruction, and an installer that
        # ignored it would hand the operator back a running job they had switched off.
        enabled=TASK_NAME not in harness_state.stood_down(),
    )


def crontab_line(schedule: Schedule) -> str:
    """The POSIX equivalent, for a machine that is not this one."""
    return f"*/{schedule.every} * * * * {subprocess.list2cmdline(schedule.command)}"


def render_plan(schedule: Schedule, windows: bool = WINDOWS) -> str:
    """What `--yes` would do, in the words of whichever scheduler is going to do it."""
    lines = [
        f"schedule: {schedule.name} -- every {schedule.every} minute(s)",
        f"  runs: {subprocess.list2cmdline(schedule.command)}",
        "",
        "Each fire stops Remote Control sessions idle past `sessionIdleMinutes`, named",
        "servers rc-servers.py no longer owns once nothing live is under them, and dev",
        "servers whose agent has gone. Interactive sessions are never candidates.",
        "",
        f"Settings are `devkit.reapStale` in {Path(schedule.workspace).name}"
        if schedule.workspace
        else "Settings are `devkit.reapStale` in the workspace file.",
        "Without one the defaults apply and no stray server is ever named.",
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
    return True, f"scheduled {schedule.name} every {schedule.every} minute(s)"


def run_check(schedule: Schedule, runner: Runner = run_command) -> tuple[int, str]:
    """`(exit code, message)` for `--check`, per `devkit_schtasks.run_check`: the registered
    task against the document `--yes` would register, so the two cannot disagree."""
    if not WINDOWS:
        return (
            devkit_schtasks.CHECK_CURRENT,
            "not a Windows machine -- nothing this installer can query",
        )
    return devkit_schtasks.run_check(schedule.name, task_document(schedule), runner)


def build_parser() -> argparse.ArgumentParser:
    """The CLI. Its own function so `main` holds decisions rather than declarations --
    the shape `structure_check`'s `function_lines` limit asks for, and the one
    `install-reconcile-task.py` already had."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--uninstall",
        action="store_true",
        help="remove the registered task (dry run unless --yes)",
    )
    mode.add_argument(
        "--status", action="store_true", help="print what the scheduler currently holds"
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="apply: register the task, or confirm an --uninstall",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="report whether a task is registered and still points at this checkout",
    )
    parser.add_argument(
        "--every",
        type=int,
        default=DEFAULT_INTERVAL,
        help=f"minutes between fires (default: {DEFAULT_INTERVAL})",
    )
    parser.add_argument(
        "--devkit",
        type=Path,
        default=REPO_ROOT,
        help=(
            "the devkit checkout the task should run from (default: this one). Name the "
            "*static* checkout when installing from an ephemeral box"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    # Before every other check here: removing a task must not require the runner it points
    # at to still exist, which is exactly the state a moved or half-uninstalled checkout is
    # in. `installer_cli.answer` owns what the two verbs mean for all thirteen installers.
    handled = installer_cli.answer(
        TASK_NAME,
        status=args.status,
        uninstall=args.uninstall,
        apply=args.yes,
        run=run_command,
        windows=WINDOWS,
    )
    if handled is not None:
        return handled

    if not valid_interval(args.every):
        parser.error(
            f"--every must be a whole number of minutes from 1 to 1440, not {args.every!r}"
        )
    root = args.devkit.expanduser().resolve()
    script = root / "scripts" / "reap-stale.py"
    if not script.is_file():
        print(f"schedule: no runner at {script}", file=sys.stderr)
        return 2
    if args.yes and BOXES_DIR in root.parts:
        print(
            f"schedule: {root} is an ephemeral box. Point --devkit at the static "
            f"checkout, which outlives the boxes.",
            file=sys.stderr,
        )
        return 2

    schedule = schedule_for(args.every, root)
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
