#!/usr/bin/env python3
"""Register `tray.py` to start at logon, so no scheduled job is ever fully invisible.

Unlike every other job here this one is not a *pass*: it starts once and stays running
for the whole session, drawing an icon. Two consequences follow, and both are settings
rather than code.

**No execution time limit.** `devkit_schtasks.DEFAULT_TIME_LIMIT` is an hour, which is
right for a pass that should finish in minutes and is exactly wrong here -- Task
Scheduler would kill the tray an hour after logon, every day, and the symptom would be
an icon that "sometimes isn't there". `PT0S` is Task Scheduler's spelling of no limit.

**A logon trigger, not a boot trigger.** A boot trigger fires before there is a desktop
to draw into. `logon_trigger` waits for a session.

**Read-only by default**, the same three modes as its siblings: `--yes` installs,
`--check` reports, and the bare invocation prints the plan.

Stdlib only, and every decision is an importable function tested in
`tests/test_install_tray.py`.
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
import sweep

REPO_ROOT = Path(__file__).resolve().parents[1]
BOXES_DIR = sweep.BOXES_DIR_NAME

TASK_NAME = "devkit-tray"

# Written only when the tray cannot start. A tray that is not running looks exactly like
# a tray reporting nothing wrong, so that one failure needs a file of its own.
ARTIFACT = "logs/tray.log"

WINDOWS = os.name == "nt"

# Task Scheduler's spelling of "no limit". Any real duration here is a scheduled kill.
NO_TIME_LIMIT = "PT0S"

DEFAULT_POLL_SECONDS = 120

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def run_command(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), capture_output=True, text=True, check=False)


@dataclass(frozen=True)
class Schedule:
    """What is to be registered, resolved from this checkout."""

    name: str
    python: str
    script: str
    poll_seconds: int

    @property
    def command(self) -> list[str]:
        return [self.python, self.script, "--poll-seconds", str(self.poll_seconds)]


def windowless_python(executable: str = sys.executable) -> str:
    """The windowless interpreter for `executable`, defaulting to this one.

    Load-bearing here in a way it is not for the passes: those would flash a console for
    a moment, whereas a console-subsystem tray would leave a black window open on the
    desktop for the entire session, next to the icon it drew.
    """
    return devkit_schtasks.windowless(executable)


def schedule_for(poll_seconds: int = DEFAULT_POLL_SECONDS, root: Path = REPO_ROOT) -> Schedule:
    """Resolve the schedule against *this* interpreter and *this* checkout."""
    return Schedule(
        name=TASK_NAME,
        python=windowless_python(),
        script=str((root / "scripts" / "tray.py").resolve()),
        poll_seconds=poll_seconds,
    )


def valid_poll(seconds: int) -> bool:
    """Seconds, positive, and not so frequent that the indicator costs more than it
    reports: every poll spawns a `schtasks`, and the fastest devkit job runs every
    fifteen minutes, so a sub-ten-second loop is asking a question that cannot have
    changed."""
    if isinstance(seconds, bool) or not isinstance(seconds, int):
        return False
    return 10 <= seconds <= 3600


def task_document(schedule: Schedule) -> str:
    """The Windows registration, as a task document."""
    program, *arguments = schedule.command
    return devkit_schtasks.task_xml(
        program,
        subprocess.list2cmdline(arguments),
        devkit_schtasks.logon_trigger(),
        # See the module docstring: an hour's limit would kill the tray every day.
        time_limit=NO_TIME_LIMIT,
        # `PureWindowsPath`, not `Path`: this document is Windows by construction, so the
        # separator it has to be split on is the backslash whatever host builds it. A
        # plain `Path` on a POSIX runner reads the whole path as one filename and yields
        # `.` -- the tests for this line ran there and caught it.
        working_dir=str(PureWindowsPath(schedule.script).parent.parent),
    )


def autostart_line(schedule: Schedule) -> str:
    """The POSIX equivalent, for a machine that is not this one.

    There is no tray to start off Windows -- `tray.py` says so and exits 0 -- so this is
    a desktop-autostart line rather than a crontab one, and it is printed for pasting
    rather than installed. Kept so `render_plan` has something true to say everywhere.
    """
    return subprocess.list2cmdline(schedule.command)


def query_argv(name: str = TASK_NAME) -> list[str]:
    return ["schtasks", "/Query", "/TN", name, "/FO", "LIST", "/V"]


def registered_command(stdout: str) -> str:
    """The command line `schtasks /Query /V` reports, or "" when it reports none."""
    for line in stdout.splitlines():
        label, sep, value = line.partition(":")
        if sep and label.strip().lower() in {"task to run", "tâche à exécuter"}:
            return value.strip()
    return ""


def drifted(registered: str, schedule: Schedule) -> str:
    """Why the registered task no longer matches this checkout, or ""."""
    if not registered:
        return "nothing is scheduled"
    if schedule.script.lower() not in registered.lower():
        return f"the scheduled task runs `{registered}`, which is not this checkout"
    return ""


def render_plan(schedule: Schedule, windows: bool = WINDOWS) -> str:
    """What `--yes` would do, in the words of whichever system is going to do it."""
    lines = [
        f"schedule: {schedule.name} -- at logon, then resident",
        f"  runs: {subprocess.list2cmdline(schedule.command)}",
        "",
        "One tray icon for every devkit scheduled job: green when they are all healthy,",
        "amber when one is late or has never run, red when one has failed or is",
        "disabled. Right-click lists them; clicking a job opens its log.",
        "",
    ]
    if windows:
        lines.append(
            "  via: a scheduled task registered from XML, with no execution time limit "
            "so the tray is not killed an hour after logon"
        )
    else:
        lines += [
            "  there is no notification area to draw into here. Add this to your",
            "  desktop session's autostart if you want it anyway:",
            f"    {autostart_line(schedule)}",
        ]
    return "\n".join(lines)


def install(schedule: Schedule, runner: Runner = run_command) -> tuple[bool, str]:
    """Register it. `(ok, message)`; POSIX is reported as unsupported rather than faked."""
    if not WINDOWS:
        return False, "not a Windows machine -- there is no notification area to draw into"
    ok, message = devkit_schtasks.register(schedule.name, task_document(schedule), runner)
    if not ok:
        return False, message
    return True, f"scheduled {schedule.name} at logon"


def run_check(schedule: Schedule, runner: Runner = run_command) -> tuple[int, str]:
    """`(exit code, message)` for `--check`. 1 when the schedule needs attention."""
    if not WINDOWS:
        return 0, "not a Windows machine -- nothing this installer can query"
    result = runner(query_argv(schedule.name))
    registered = registered_command(result.stdout) if result.returncode == 0 else ""
    reason = drifted(registered, schedule)
    if reason:
        return 1, f"schedule: {reason}. Re-run with --yes to (re)register it."
    return 0, f"schedule: {schedule.name} is registered and points at this checkout."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--yes", action="store_true", help="register the task")
    mode.add_argument("--check", action="store_true", help="report what is registered")
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=DEFAULT_POLL_SECONDS,
        help=f"seconds between checks (default: {DEFAULT_POLL_SECONDS})",
    )
    parser.add_argument(
        "--devkit",
        type=Path,
        default=REPO_ROOT,
        help=(
            "the devkit checkout the task should run from (default: this one). Name the "
            "*static* checkout when installing from an ephemeral box -- a task pointing "
            "into .worktrees/ dies the moment reconcile reaps it"
        ),
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if not valid_poll(args.poll_seconds):
        parser.error(
            f"--poll-seconds must be a whole number from 10 to 3600, not {args.poll_seconds!r}"
        )
    root = args.devkit.expanduser().resolve()
    script = root / "scripts" / "tray.py"
    if not script.is_file():
        print(f"schedule: no tray at {script}", file=sys.stderr)
        return 2
    if args.yes and BOXES_DIR in root.parts:
        print(
            f"schedule: {root} is an ephemeral box. Point --devkit at the static "
            f"checkout, which outlives the boxes.",
            file=sys.stderr,
        )
        return 2

    schedule = schedule_for(args.poll_seconds, root)
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
