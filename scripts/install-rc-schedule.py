#!/usr/bin/env python3
"""Register `rc-servers.py maintain` as a recurring OS task.

`rc-servers.py` explains what the job does and why it has to be a job at all. This
registers it: a Task Scheduler entry on Windows, a crontab line elsewhere, invoking

    <python> scripts/rc-servers.py maintain --workspace <workspace>

every `--every` minutes. Each fire restarts any Remote Control server that has died and,
once a day, updates the Claude CLI in the gap between stopping the servers and starting
them again.

**Frequent rather than daily**, unlike its two nightly siblings, because the thing it
repairs is a *process that exited* and the cost of the repair being late is that the
phone shows an offline session for as long as it takes. Fifteen minutes matches
`devkit-worktree-reconcile`, which is the other job whose subject is machine state
rather than a piece of work.

The pass is cheap on the ticks that find nothing to do -- one `tasklist` per served
project and one `stat` per transcript -- which is what makes that interval affordable.

**Read-only by default.** `--yes` installs, `--check` reports what is registered and
whether it still points at this checkout, and the bare invocation prints the plan. Same
three modes as `install-upgrade-schedule.py`, for the same reason: an installer whose
default is to install is one you cannot safely ask a question.

Stdlib only, and every decision is an importable function tested in
`tests/test_install_rc_schedule.py`.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devkit_schtasks
import sweep

REPO_ROOT = Path(__file__).resolve().parents[1]
BOXES_DIR = sweep.BOXES_DIR_NAME

# The registered name, and the string `--check` looks the task up by. Stable because
# renaming it would orphan whatever a previous version registered -- the installer would
# report "nothing scheduled" while the old entry kept firing.
TASK_NAME = "devkit-rc-servers"

# Where this job's account of itself lives. `rc-servers.py` writes it on every exit path;
# `schedule_health.ARTIFACTS` sends a reader here when the scheduler reports a failure.
ARTIFACT = "logs/rc-servers.log"

# See `install-upgrade-schedule.WINDOWS` for why this is resolved once at import rather
# than read from `os.name` at each call site.
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
        """The argv the scheduler runs.

        `maintain` is named explicitly rather than left to the default mode. The default
        is `status`, which is read-only on purpose -- a scheduled task that silently
        became a no-op because someone changed a default is the failure this whole file
        is downstream of.

        `--workspace` is passed explicitly, the way the reconcile and upgrade tasks do:
        a scheduled task has no cwd worth relying on, and the registered command is the
        only record of what it operates on.
        """
        argv = [self.python, self.script, "maintain"]
        if self.workspace:
            argv += ["--workspace", self.workspace]
        return argv


def windowless_python(executable: str = sys.executable) -> str:
    """The windowless interpreter for `executable`, defaulting to this one.

    A wrapper rather than an alias for `devkit_schtasks.windowless`, for
    `install-upgrade-schedule.windowless_python`'s reason: the default argument is the
    whole call site, and an alias would drop it.

    The cost `pythonw.exe` carries -- no stdout, no stderr, anywhere -- is survivable
    here because `rc-servers.main` writes `logs/rc-servers.log` on every exit path
    including the ones that never reach a project.
    """
    return devkit_schtasks.windowless(executable)


def schedule_for(every: int = DEFAULT_INTERVAL, root: Path = REPO_ROOT) -> Schedule:
    """Resolve the schedule against *this* interpreter and *this* checkout.

    Derived from `sys.executable` rather than a bare `python`: a scheduled task runs
    with no activated virtualenv and often a different PATH, and `rc-servers.py` imports
    `agent_clis` and `sweep` from beside it.
    """
    workspace = sweep.default_workspace(root)
    return Schedule(
        name=TASK_NAME,
        python=windowless_python(),
        script=str((root / "scripts" / "rc-servers.py").resolve()),
        every=every,
        workspace=str(workspace) if workspace else "",
    )


def valid_interval(every: int) -> bool:
    """Minutes, positive, and not so long the repair is worse than the fault.

    The upper bound is a day: past that the "restart what died" half has stopped being a
    repair and the daily update predicate can no longer fire on the day it is due.
    """
    return isinstance(every, int) and not isinstance(every, bool) and 1 <= every <= 1440


def task_document(schedule: Schedule) -> str:
    """The Windows registration, as a task document.

    Not `schtasks /SC MINUTE /MO`, for `devkit_schtasks`' reason: that spelling cannot
    express `StartWhenAvailable`, and the settings it silently inherits are the ones
    that decide whether the job runs at all.

    The time limit is deliberately shorter than either nightly job's. This pass should
    take seconds; the one thing that can make it take longer is `claude update`, and a
    fire still running when the next one is due would -- under `IgnoreNew` -- suppress
    every later fire until the limit expired. Fifteen minutes of a wedged update is a
    delayed restart, an hour of one is a job that looks dead.
    """
    program, *arguments = schedule.command
    return devkit_schtasks.task_xml(
        program,
        subprocess.list2cmdline(arguments),
        # Two triggers. The repetition is the job; the boot trigger only closes the gap
        # after a restart, where the servers are certainly down (a reboot kills them) and
        # the next repetition could be a full interval away. Fifteen minutes of an
        # unreachable phone is exactly the failure this job exists to prevent.
        devkit_schtasks.repeating_trigger(schedule.every) + devkit_schtasks.boot_trigger(),
        time_limit="PT15M",
        working_dir=str(Path(schedule.script).parent.parent),
    )


def crontab_line(schedule: Schedule) -> str:
    """The POSIX equivalent, for a machine that is not this one."""
    return f"*/{schedule.every} * * * * {subprocess.list2cmdline(schedule.command)}"


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
    """Why the registered task no longer matches this checkout, or "".

    Compares the script path rather than the whole command line, for
    `install-upgrade-schedule.drifted`'s reason: the interpreter may legitimately differ
    after a venv rebuild, and rewriting the task over that would be noise. A different
    script path means the checkout moved.
    """
    if not registered:
        return "nothing is scheduled"
    if schedule.script.lower() not in registered.lower():
        return f"the scheduled task runs `{registered}`, which is not this checkout"
    return ""


def render_plan(schedule: Schedule, windows: bool = WINDOWS) -> str:
    """What `--yes` would do, in the words of whichever scheduler is going to do it."""
    lines = [
        f"schedule: {schedule.name} -- every {schedule.every} minute(s)",
        f"  runs: {subprocess.list2cmdline(schedule.command)}",
        "",
        "Each fire restarts any Remote Control server that has exited. Once a day, when",
        "every served project has been quiet long enough, it stops the servers, updates",
        "the Claude CLI, and starts them again.",
        "",
        f"Projects are opted in by `devkit.remoteControl` in {Path(schedule.workspace).name}"
        if schedule.workspace
        else "Projects are opted in by `devkit.remoteControl` in the workspace file.",
        "Nothing is served until that setting names one.",
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
            "*static* checkout when installing from an ephemeral box -- a task pointing "
            "into .worktrees/ dies the moment reconcile reaps it"
        ),
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if not valid_interval(args.every):
        parser.error(
            f"--every must be a whole number of minutes from 1 to 1440, not {args.every!r}"
        )
    root = args.devkit.expanduser().resolve()
    script = root / "scripts" / "rc-servers.py"
    if not script.is_file():
        print(f"schedule: no runner at {script}", file=sys.stderr)
        return 2
    if args.yes and BOXES_DIR in root.parts:
        # Registering a box would look fine today and break silently on the next
        # `reconcile`. Refused rather than warned, and only on `--yes`, for the reasons
        # `install-upgrade-schedule.main` spells out.
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
