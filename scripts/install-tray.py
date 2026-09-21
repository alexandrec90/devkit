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

**`--restart` is the fourth, and only this installer has one.** A pass picks up an edit
on its next run; the tray imported `tray.py` once at logon and holds it for the session,
so a change to the icon is invisible -- with no error anywhere -- until the process is
replaced. The logon trigger's own answer is "log out", which is why this flag exists.

**So `--check` asks whether the resident process is current, not only the
registration.** Re-registering the same command line says nothing about the code the
running tray imported, and the two questions came apart the first time they could: a
tray started 2026-09-17 15:19 went on drawing an `Exit` row that had been deleted from
`tray.py` at 16:46 the same day, while this installer's check reported "current" every
morning until someone noticed on the 19th. It was answering a different question from
the one being asked of it. It now also compares the scheduler's `Last Run Time` against
every module in `TRAY_MODULES`, and `--yes` restarts a tray older than its own source --
which is the half that makes the new answer something `installers.py maintain` repairs
rather than repeats.

Stdlib only, and every decision is an importable function tested in
`tests/test_install_tray.py`.
"""

from __future__ import annotations

import argparse
import datetime as _dt
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
import schedule_health
import sweep

REPO_ROOT = Path(__file__).resolve().parents[1]
BOXES_DIR = sweep.BOXES_DIR_NAME

TASK_NAME = "devkit-tray"

# Machine maintenance, not branch delivery: standing the agent tier down leaves this
# running. `tests/test_installer_contract.py` holds the switch's list to this word.
GROUP = "maintenance"

# Written only when the tray cannot start. A tray that is not running looks exactly like
# a tray reporting nothing wrong, so that one failure needs a file of its own.
ARTIFACT = "logs/tray.log"

WINDOWS = os.name == "nt"

# Task Scheduler's spelling of "no limit". Any real duration here is a scheduled kill.
NO_TIME_LIMIT = "PT0S"

DEFAULT_POLL_SECONDS = 120

# Every module the resident tray holds from logon to logout: `tray.py` and the siblings
# it imports, transitively. All of them, not `tray.py` alone -- the icon's pixels are in
# `tray_icon` and what counts as a problem is in `schedule_health`, so watching only the
# file the task names would miss most of what a person edits when they change what the
# tray shows. `tests/test_install_tray.py` walks the real import graph and holds this
# tuple to it, because a sibling import nobody added here would reintroduce the exact
# silence this mechanism exists to end.
#
# Module names, not filenames, and that is not cosmetic: `tests/test_scheduled_jobs.py`
# reads every `*.py` literal in an installer as a script that installer *launches* and
# demands console suppression of it. These are imported by the tray, launched by nobody.
TRAY_MODULES = (
    "tray",
    "tray_icon",
    "tray_state",
    "schedule_health",
    "devkit_schtasks",
    "harness_state",
)

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
        # Lands disabled when this job has been stood down by name (`harness-switch.py
        # --off --job`): the ledger is the standing instruction, and an installer that
        # ignored it would hand the operator back a running job they had switched off.
        enabled=TASK_NAME not in harness_state.stood_down(),
    )


def autostart_line(schedule: Schedule) -> str:
    """The POSIX equivalent, for a machine that is not this one.

    There is no tray to start off Windows -- `tray.py` says so and exits 0 -- so this is
    a desktop-autostart line rather than a crontab one, and it is printed for pasting
    rather than installed. Kept so `render_plan` has something true to say everywhere.
    """
    return subprocess.list2cmdline(schedule.command)


def end_argv(name: str = TASK_NAME) -> list[str]:
    return ["schtasks", "/End", "/TN", name]


def start_argv(name: str = TASK_NAME) -> list[str]:
    return ["schtasks", "/Run", "/TN", name]


def restart(schedule: Schedule, runner: Runner = run_command) -> tuple[bool, str]:
    """Stop the resident tray and start it again, so it re-imports. `(ok, message)`.

    Addressed by task *name*, never by `schedule.script`: the icon on the desktop was
    drawn by whichever checkout is registered, so restarting from a worktree has to
    restart that one rather than a tray this checkout would have installed. It follows
    that `--restart` needs no `--devkit` and cannot be pointed at the wrong tray.

    **`/End` is allowed to fail.** It reports non-zero when nothing is running, which is
    the very state a restart produces anyway; treating it as fatal would make the flag
    refuse exactly when the tray has died and needs starting most. Only `/Run` failing
    means the restart did not happen.
    """
    if not WINDOWS:
        return False, "not a Windows machine -- there is no notification area to draw into"
    runner(end_argv(schedule.name))
    result = runner(start_argv(schedule.name))
    if result.returncode != 0:
        return False, (result.stderr or result.stdout or "schtasks failed").strip()
    return True, f"restarted {schedule.name}; it is running the tray as it is on disk now"


def last_run_argv(name: str = TASK_NAME) -> list[str]:
    """`schtasks`' verbose CSV for one task -- the row `last_started` reads."""
    return ["schtasks", "/Query", "/TN", name, "/FO", "CSV", "/V"]


def last_started(name: str, runner: Runner = run_command) -> _dt.datetime | None:
    """When the scheduler last started this task, or None when it cannot say.

    `Last Run Time`, not the process table. The tray is started by this task and by
    nothing else, so the scheduler already holds the answer to the second -- it matched
    `Get-Process`'s `StartTime` exactly on the machine this was written for -- and
    reading it costs one more `schtasks`, where asking Windows for a process start time
    means WMI: a subprocess and a parse of its own, for a worse answer (several
    `pythonw.exe` are running, and which one is the tray is only knowable from the
    command line the scheduler already knows).

    The parse is `schedule_health`'s, locale traps and all, rather than a second reader
    of the same CSV -- `parse_time` covers the product of Windows' short-date and
    long-time settings, which is four formats and was a bug there before it was a
    docstring.
    """
    result = runner(last_run_argv(name))
    if result.returncode != 0:
        return None
    jobs = schedule_health.parse_tasks(result.stdout or "", prefix=name)
    return jobs[0].last_run if jobs else None


def stale_sources(
    started: _dt.datetime | None,
    scripts_dir: Path,
    names: Sequence[str] = TRAY_MODULES,
) -> list[str]:
    """Which of the tray's modules have changed since the run now drawing the icon.

    Takes module names and reports filenames, because the answer is read by a person in
    a check message and `tray_icon.py` is what they will go and look at.

    Both sides are naive local time: `schedule_health.parse_time` reads a local stamp
    off `schtasks`, and `fromtimestamp` without a timezone returns one, so they compare
    directly. Do not "fix" either into UTC alone.

    **Empty when the scheduler cannot say when it started.** A task that has never run
    is the logon trigger's business, and calling that stale would have `maintain` repair
    it on every pass forever rather than once -- a check whose repair cannot clear it is
    worse than the silence it replaced. A module that has gone missing is skipped for
    the same reason: it is a broken checkout, which `checkout_refusal` reports, and no
    restart fixes it.
    """
    if started is None:
        return []
    changed = []
    for name in names:
        source = scripts_dir / f"{name}.py"
        try:
            edited = _dt.datetime.fromtimestamp(source.stat().st_mtime)
        except OSError:
            continue
        if edited > started:
            changed.append(source.name)
    return changed


def stale_resident(schedule: Schedule, runner: Runner = run_command) -> list[str]:
    """The tray's modules newer than the process drawing the icon. `[]` means current.

    Read against the *registered* script's directory, which is the checkout the running
    tray imported from -- by the time this is asked, `devkit_schtasks.run_check` has
    already established that the registration is the one this checkout would write, so
    the two are the same directory or the caller never got here.

    **A stood-down tray is never stale.** `--off --job devkit-tray` is a standing
    instruction not to be running one, and restarting it to pick up an edit would hand
    the operator back the job they switched off -- the same reading `task_document`
    gives the ledger when it registers the task disabled.
    """
    if schedule.name in harness_state.stood_down():
        return []
    return stale_sources(last_started(schedule.name, runner), Path(schedule.script).parent)


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
    """Register it, and replace a tray older than the modules on disk. `(ok, message)`;
    POSIX is reported as unsupported rather than faked.

    The restart is what makes `run_check`'s second answer repairable. `installers.py
    maintain` repairs a stale check by running `--yes`, and registering alone would
    leave the same process drawing the same stale icon -- so the check would report the
    identical thing tomorrow, and every morning after, which is a pass that has learnt
    to complain rather than to fix.
    """
    if not WINDOWS:
        return False, "not a Windows machine -- there is no notification area to draw into"
    ok, message = devkit_schtasks.register(schedule.name, task_document(schedule), runner)
    if not ok:
        return False, message
    scheduled = f"scheduled {schedule.name} at logon"
    if not stale_resident(schedule, runner):
        return True, scheduled
    restarted, note = restart(schedule, runner)
    return restarted, f"{scheduled}; {note}"


def run_check(schedule: Schedule, runner: Runner = run_command) -> tuple[int, str]:
    """`(exit code, message)` for `--check`: the registration, and then the process.

    The first question is `devkit_schtasks.run_check`'s -- the registered task against
    the document `--yes` would register, so the two cannot disagree. The second is this
    installer's alone, because only this job is resident: a registration that is exactly
    right says nothing about the code the process started from it is still holding.
    Drift in the registration wins when both are wrong, since re-registering is what
    would fix it and the restart comes with that anyway.
    """
    if not WINDOWS:
        return (
            devkit_schtasks.CHECK_CURRENT,
            "not a Windows machine -- nothing this installer can query",
        )
    code, message = devkit_schtasks.run_check(schedule.name, task_document(schedule), runner)
    if code != devkit_schtasks.CHECK_CURRENT:
        return code, message
    stale = stale_resident(schedule, runner)
    if not stale:
        return code, message
    return (
        devkit_schtasks.CHECK_STALE,
        f"schedule: {schedule.name} is registered as this checkout would register it, "
        f"but the tray drawing the icon started before {', '.join(stale)} changed, and a "
        f"resident process holds the modules it imported at logon. Re-run with --yes to "
        f"restart it.",
    )


def checkout_refusal(root: Path, apply: bool) -> str:
    """Why `root` cannot be the checkout this task runs from, or "" when it can.

    Both refusals are about a path that will outlive the command registering it: one
    checks the runner is there at all, the other that it is not inside an ephemeral box --
    `reconcile` deletes those when their PR merges, taking the task's `<Command>` with it,
    silently and days later.
    """
    script = root / "scripts" / "tray.py"
    if not script.is_file():
        return f"schedule: no tray at {script}"
    if apply and BOXES_DIR in root.parts:
        return (
            f"schedule: {root} is an ephemeral box. Point --devkit at the static "
            f"checkout, which outlives the boxes."
        )
    return ""


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
    mode.add_argument("--check", action="store_true", help="report what is registered")
    mode.add_argument(
        "--restart",
        action="store_true",
        help="stop and restart the running tray, so it picks up an edited tray.py",
    )
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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    # `--yes` is the apply flag for two verbs now rather than a verb itself, so it can no
    # longer sit in the mutually-exclusive group -- `--uninstall --yes` has to be
    # expressible. Restarting is still not something one applies, so the exclusion this
    # installer alone needs is stated here instead of inferred from the group.
    if args.restart and args.yes:
        parser.error("--restart acts on the registered task; it takes no --yes")

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

    if not valid_poll(args.poll_seconds):
        parser.error(
            f"--poll-seconds must be a whole number from 10 to 3600, not {args.poll_seconds!r}"
        )
    # Before the checkout checks below, all of which ask about a tray this invocation
    # might install. A restart acts on the registered task, so none of them apply.
    if args.restart:
        ok, message = restart(schedule_for(args.poll_seconds))
        print(message, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 2

    root = args.devkit.expanduser().resolve()
    refusal = checkout_refusal(root, args.yes)
    if refusal:
        print(refusal, file=sys.stderr)
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
