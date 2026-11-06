#!/usr/bin/env python3
"""Register `ship-pass.py` as a recurring Windows Scheduled Task, on every PC.

`ship-pass.py` explains what the job ships and when. This registers it: a task named
`devkit-ship`, firing every two minutes, invoking

    <python> -X utf8 scripts/ship-pass.py --workspace <workspace>

**In UTF-8 mode because it reads git's output as text**, and a `”` in a commit subject is a
byte cp1252 cannot decode (`fix-pass.in_utf8_mode` has the whole of it). A job that fires
every two minutes passes the flag rather than re-launching itself as the pass does.

**On every PC, whatever `devkit.fixPass` says.** That switch decides which PC sends
fixers; shipping a session's own work is every machine's, or the intents on a second PC
never go out. A fire on a machine with nothing to ship costs one `git worktree list` per
project and exits. Its stop is `harness-switch.py`, not that setting.

**The same two modes as every installer.** The bare invocation prints the plan, `--yes`
registers, `--check` compares the registered task with the document `--yes` would
register.

Windows-only by nature; any other platform says so and exits 0. Tested in
`tests/test_install_ship_schedule.py`.
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
import installer_cli
import sweep

REPO_ROOT = Path(__file__).resolve().parents[1]

# Stable for `install-rc-schedule.TASK_NAME`'s reason: renaming it orphans whatever a
# previous version registered. `ship_step.SHIP_TASK` names it to a reader of the record.
TASK_NAME = "devkit-ship"

# Branch delivery: this job moves agent branches along, so `harness-switch.py --off
# --group jobs` stands it down. `tests/test_installer_contract.py` holds the switch's
# list to the installers that say this.
GROUP = "delivery"

# `ship-pass.py` writes it whenever a fire had something to say; `schedule_health.ARTIFACTS`
# sends a reader here when the scheduler reports a failure.
ARTIFACT = "logs/ship-pass.log"

# The `log-wrap.py --always` label the task runs under (`devkit_schtasks.logged`), which
# files each failed run on the harness-events ledger; its files are
# `logs/scheduled-ship[.failed].log`, beside the runner's own `ARTIFACT`.
LABEL = "Scheduled: Ship"

WINDOWS = os.name == "nt"

DEFAULT_INTERVAL = 2

# A ship can run minutes on a machine short of memory, but a fire past this has stopped
# being one; `ship_step.SHIP_LOCK_STALE` is set past it.
TIME_LIMIT = "PT15M"

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]

windowless = devkit_schtasks.windowless


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
    def arguments(self) -> list[str]:
        """What follows the interpreter: named `--workspace` because a scheduled task
        starts in `system32`, and a moved checkout should fail loudly."""
        argv = ["-X", "utf8", self.script]
        return [*argv, "--workspace", self.workspace] if self.workspace else argv


def schedule_for(every: int = DEFAULT_INTERVAL, root: Path = REPO_ROOT) -> Schedule:
    """Resolve the schedule against *this* interpreter and *this* checkout."""
    workspace = sweep.default_workspace(root)
    return Schedule(
        name=TASK_NAME,
        python=windowless(sys.executable),
        script=str((root / "scripts" / "ship-pass.py").resolve()),
        every=every,
        workspace=str(workspace) if workspace else "",
    )


def valid_interval(every: int) -> bool:
    """Minutes, positive, at most a day."""
    return isinstance(every, int) and not isinstance(every, bool) and 1 <= every <= 1440


def task_document(schedule: Schedule) -> str:
    """The Windows registration, as a task document, through `devkit_schtasks` for the
    settings a command-line registration cannot express.

    The limit is longer than the interval, unlike a job that can wedge cheaply: a ship
    ended midway leaves a commit with no push, and `IgnoreNew` only skips fires, which
    the next one makes good.
    """
    root = str(PureWindowsPath(schedule.script).parent.parent)
    arguments = subprocess.list2cmdline(schedule.arguments)
    return devkit_schtasks.task_xml(
        schedule.python,
        devkit_schtasks.logged(LABEL, schedule.python, arguments, root),
        devkit_schtasks.repeating_trigger(schedule.every),
        time_limit=TIME_LIMIT,
        working_dir=root,
        # Lands disabled when the jobs tier is stood down; `harness-switch.py --off
        # jobs` records the whole group, not just the tasks that existed when it ran.
        enabled=TASK_NAME not in harness_state.stood_down(),
    )


def render_plan(schedule: Schedule) -> str:
    """What `--yes` would do."""
    return (
        f"schedule: {schedule.name} -- every {schedule.every} minute(s)\n"
        f'  runs: "{schedule.python}" {subprocess.list2cmdline(schedule.arguments)}\n\n'
        "Each fire ships every direct-flow intent on this machine, and the gated ones too\n"
        "when devkit.fixPass here is `off`. It sends no fixer, and a fire with nothing to\n"
        "ship exits within seconds. harness-switch.py is what stops it.\n"
    )


def build_parser() -> argparse.ArgumentParser:
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
    mode.add_argument(
        "--check",
        action="store_true",
        help="report whether the registered task is the one this checkout would register",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="apply: register the task, or confirm an --uninstall",
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
        help="the devkit checkout the task runs from (default: this one)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if not WINDOWS:
        print("install-ship-schedule: Windows-only; nothing to do here.")
        return 0

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
    if args.yes and sweep.source_checkout(root) != root:
        # The registered command carries this checkout's path verbatim, and a temporary
        # checkout is destroyed by the very passes being scheduled.
        print(
            f"install-ship-schedule: {root} is a temporary checkout. Run this from the "
            f"static devkit checkout.",
            file=sys.stderr,
        )
        return 2

    schedule = schedule_for(args.every, root)
    if args.check:
        code, message = devkit_schtasks.run_check(
            schedule.name, task_document(schedule), run_command
        )
        print(message, file=sys.stderr if code else sys.stdout)
        return code
    if not args.yes:
        print(render_plan(schedule))
        print("Nothing was registered. Re-run with --yes to install.")
        return 0

    ok, message = devkit_schtasks.register(schedule.name, task_document(schedule), run_command)
    print(message if not ok else f"scheduled {schedule.name} every {schedule.every} minute(s)")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
