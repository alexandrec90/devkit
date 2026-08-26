#!/usr/bin/env python3
"""Register the nightly `global-tools.py --yes` pass as a Windows Scheduled Task.

The machine-wide half of the same idea `install-upgrade-schedule.py` covers for
repositories: something has to move the versions nobody is watching. Four of this
workspace's MCP servers, and every linter reachable without a project venv, run from a
**globally installed** npm package -- a version pinned by whoever typed `npm i -g` once
and moved by nothing since. The two binaries this workspace runs *most*, `claude` and
`codex`, are not even npm packages here, so nothing was moving them at all; the same
pass now runs their own updaters first. `global-tools.py`'s docstring has the full
argument, and `agent_clis.py`'s the agent half.

That second stage is deliberately not a flag on the command below. The argv registered
in a task document is a fact about the machine, not about the repo: an installer that
had to grow an argument would leave the already-registered task doing half the job until
somebody re-ran this script, and `drifted` compares the script path, so nothing would
report the gap. Widening what the runner does, rather than what the schedule asks for,
makes the merge the whole rollout.

Why devkit owns this at all, rather than a hand-registered `schtasks /Create`: the
hand-registered job is a failure this repo has already had. `devkit-docker-prune` was
created that way, and so it skipped every fire on battery, never caught up a run it
slept through, and wrote nothing anywhere -- it had been exiting 1 for a day before
anyone could tell. Every property that job lacked is a property `devkit_schtasks`
supplies and `schtasks.exe`'s flags cannot express, and `tests/test_scheduled_jobs.py`
now fails a job that skips any of them. A global-package updater installed by hand
would be that job again, with a worse blast radius: it changes the versions every
session's tooling runs on.

None of this ships into consuming projects. Installers are not in `sync-devkit.py`'s
`MANIFEST` -- they configure *this* machine, which is also why `devkit-docker-prune`,
`devkit-docker-stop-idle` and `devkit-vanillaland-merge` live here without being
anything a generated project inherits.

04:30 rather than a shared slot: 03:00 is the devkit upgrade, 03:30 the Docker stop-idle
pass and 04:00 the prune. Landing last means a night that updated npm's global tree is
not also competing with a `docker system prune` for the same laptop's IO.

Daily rather than weekly, deliberately. The cost of a pass with nothing to do is one
registry query, and updating one package the day it releases makes a breakage
attributable to that package -- a weekly pass bumps seven at once and leaves the
morning's broken session with seven suspects.

Read-only by default -- `--check` reports what is registered and whether it still points
at this checkout, `--yes` installs, the bare invocation prints the plan. Same three modes
as its siblings, for the same reason: an installer whose default is to install is one you
cannot safely ask a question.

Windows-only by nature. Elsewhere it prints the crontab line for pasting and changes
nothing.

The builders are pure and tested in `tests/test_install_global_tools.py`.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devkit_schtasks
import sweep

REPO_ROOT = Path(__file__).resolve().parents[1]

# The registered name, and the string `--check` looks the task up by. Stable: renaming
# it orphans whatever a previous version registered, so the installer would report
# "nothing scheduled" while the old entry kept firing.
TASK_NAME = "devkit-global-tools"

# See the module docstring for why this slot: after the 04:00 prune.
DEFAULT_TIME = "04:30"

# The artifact `global-tools.py` writes, named here because the installer is the one
# place that knows this job exists at all. `tests/test_scheduled_jobs.py` checks it
# against the runner's own constant and against `schedule_health.ARTIFACTS`.
ARTIFACT = "logs/global-tools.log"

# The directory ephemeral boxes live in. A schedule must never point inside one --
# `reconcile` deletes it when the PR merges. Read from `sweep` so one rename moves both.
BOXES_DIR = sweep.BOXES_DIR_NAME

# Resolved once, at import, so a test can force the Windows path without patching
# `os.name` itself -- `pathlib` reads that at call time, and patching it makes every
# later bare `Path(...)` raise on a POSIX runner.
WINDOWS = os.name == "nt"

Runner = devkit_schtasks.Runner


def run_command(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """`devkit_schtasks.Runner` shape: a spawn failure is a returncode, not a traceback."""
    try:
        return subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        return subprocess.CompletedProcess(list(argv), 1, "", str(exc))


def runner_script(root: Path = REPO_ROOT) -> Path:
    return root / "scripts" / "global-tools.py"


def interpreter(root: Path = REPO_ROOT) -> str:
    """The interpreter the scheduled task should run, belonging to `root`.

    A copy of `install-vanillaland-merge.interpreter`, and for the reason its docstring
    gives: `--devkit` exists so an agent in a box can register the job against the static
    checkout, and using `sys.executable` there registers the *box's* `.venv` -- which
    `reconcile` deletes the moment the PR merges, silently, days later.

    `global-tools.py` imports nothing but the standard library, so any Python 3 would
    run it. The trap is about the path outliving the box, not about what is installed.
    """
    venv = (
        root / ".venv" / ("Scripts" if WINDOWS else "bin") / ("python.exe" if WINDOWS else "python")
    )
    if venv.is_file():
        return str(venv)
    if BOXES_DIR in Path(sys.executable).parts:
        return getattr(sys, "_base_executable", "") or sys.executable
    return sys.executable


# The interpreter for the task's own `<Command>`. This job is the reason
# `devkit_schtasks.windowless` is shared rather than copied: `interpreter` above prefers
# the checkout's `.venv`, whose `pythonw.exe` under uv is a trampoline that spawns the
# real interpreter as a child -- and a console child of a console-less scheduled task is
# what put a window back on this desktop. That docstring owns the whole account.
windowless = devkit_schtasks.windowless


@dataclass(frozen=True)
class Schedule:
    """What is to be registered, resolved from one checkout."""

    name: str
    python: str
    script: str
    at: str

    @property
    def command(self) -> list[str]:
        """The argv the scheduler runs. `--yes` is the whole point of it: without it
        `global-tools.py` reports and installs nothing."""
        return [self.python, self.script, "--yes"]


def schedule_for(at: str = DEFAULT_TIME, root: Path = REPO_ROOT) -> Schedule:
    return Schedule(
        name=TASK_NAME,
        python=windowless(interpreter(root)),
        script=str(runner_script(root).resolve()),
        at=at,
    )


def valid_time(at: str) -> bool:
    """`HH:MM`, 24-hour. Both schedulers take it, and neither says so when it is wrong."""
    hours, _, minutes = at.partition(":")
    if not (hours.isdigit() and minutes.isdigit()) or len(hours) != 2 or len(minutes) != 2:
        return False
    return 0 <= int(hours) <= 23 and 0 <= int(minutes) <= 59


def task_document(schedule: Schedule, root: Path = REPO_ROOT) -> str:
    """The task XML registering (or replacing) the nightly pass.

    `working_dir` matters here for the same reason it does for the wrapper-based jobs:
    a scheduled task's cwd is `system32`, and while `global-tools.py` resolves its
    artifact from its own location, an npm install run from `system32` is a surprise
    nobody needs.

    Not `schtasks /SC DAILY /ST`: at 04:30 a laptop is asleep or unplugged more often
    than not, and that spelling cannot say "run on battery" or "catch up a missed run".
    See `devkit_schtasks`.
    """
    program, *arguments = schedule.command
    return devkit_schtasks.task_xml(
        program,
        subprocess.list2cmdline(arguments),
        devkit_schtasks.daily_trigger(schedule.at),
        working_dir=str(root),
    )


def crontab_line(schedule: Schedule) -> str:
    """The POSIX equivalent, for a machine that is not this one."""
    hours, _, minutes = schedule.at.partition(":")
    return f"{int(minutes)} {int(hours)} * * * {subprocess.list2cmdline(schedule.command)}"


def query_argv(name: str = TASK_NAME) -> list[str]:
    return ["schtasks", "/Query", "/TN", name, "/FO", "LIST", "/V"]


def uninstall_argv(name: str = TASK_NAME) -> list[str]:
    return ["schtasks", "/Delete", "/TN", name, "/F"]


def registered_command(stdout: str) -> str:
    """The command line `schtasks /Query /V` reports, or "" when it reports none.

    Parsed rather than trusted wholesale: the question that matters is not "is something
    scheduled" but "is the scheduled thing still *this* checkout". An installer that
    only checked existence would call a task pointing into a reaped box healthy.
    """
    for line in stdout.splitlines():
        label, sep, value = line.partition(":")
        if sep and label.strip().lower() in {"task to run", "tâche à exécuter"}:
            return value.strip()
    return ""


def drifted(registered: str, schedule: Schedule) -> str:
    """Why the registered task no longer matches this checkout, or "".

    Compares the *script path*, not the whole command line: an interpreter may
    legitimately differ (a venv rebuilt, a Python upgraded in place) and rewriting the
    task over that would be noise.
    """
    if not registered:
        return "nothing is scheduled"
    if schedule.script.lower() not in registered.lower():
        return f"the scheduled task runs `{registered}`, which is not this checkout"
    return ""


def render_plan(schedule: Schedule, windows: bool = WINDOWS) -> str:
    """What `--yes` would do, in the words of whichever scheduler is going to do it."""
    lines = [
        f"schedule: {schedule.name} -- daily at {schedule.at}",
        f"  runs: {subprocess.list2cmdline(schedule.command)}",
        "",
        "The agent CLIs (claude, codex) are updated through their own updaters, skipping",
        "any that is running. Then every globally-installed npm package that is behind is",
        "updated to its newest release, except npm and Claude Code. Each move is recorded",
        f"in {ARTIFACT} with the command that undoes it.",
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


def install(
    schedule: Schedule, root: Path = REPO_ROOT, runner: Runner = run_command
) -> tuple[bool, str]:
    """Register it. `(ok, message)`; POSIX is reported as unsupported rather than faked."""
    if not WINDOWS:
        return False, "not a Windows machine -- add this crontab line yourself:\n  " + crontab_line(
            schedule
        )
    ok, message = devkit_schtasks.register(schedule.name, task_document(schedule, root), runner)
    if not ok:
        return False, message
    return True, f"scheduled {schedule.name} daily at {schedule.at}"


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


def main(argv: list[str] | None = None, runner: Runner = run_command) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--yes", action="store_true", help="register the task")
    mode.add_argument(
        "--check",
        action="store_true",
        help="report whether a task is registered and still points at this checkout",
    )
    mode.add_argument("--uninstall", action="store_true", help="remove the task")
    parser.add_argument("--at", default=DEFAULT_TIME, help="daily start time, HH:MM (24-hour)")
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

    if not valid_time(args.at):
        parser.error(f"--at must be HH:MM in 24-hour time, not {args.at!r}")
    root = args.devkit.expanduser().resolve()
    if not runner_script(root).is_file():
        print(f"schedule: no runner at {runner_script(root)}", file=sys.stderr)
        return 2

    schedule = schedule_for(args.at, root)

    if args.uninstall:
        result = runner(uninstall_argv(schedule.name))
        ok = result.returncode == 0
        print(
            f"removed {schedule.name}"
            if ok
            else f"could not remove {schedule.name}: {result.stderr.strip()}",
            file=sys.stdout if ok else sys.stderr,
        )
        return 0 if ok else 2
    if args.check:
        code, message = run_check(schedule, runner)
        print(message, file=sys.stderr if code else sys.stdout)
        return code
    if not args.yes:
        print(render_plan(schedule))
        print("\nNothing was registered. Re-run with --yes to install.")
        return 0
    if BOXES_DIR in root.parts:
        # Registering a box would look fine today and break silently on the next
        # `reconcile`. Refused on `--yes` only: printing the plan from a box is how an
        # agent reads what the install would do before it has anywhere else to run.
        print(
            f"schedule: {root} is an ephemeral box. Point --devkit at the static "
            f"checkout, which outlives the boxes.",
            file=sys.stderr,
        )
        return 2

    ok, message = install(schedule, root, runner)
    print(message, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
