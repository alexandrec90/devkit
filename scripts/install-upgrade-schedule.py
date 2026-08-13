#!/usr/bin/env python3
"""Register `upgrade-project.py --all --yes` as a recurring OS task.

The point of an upgrade that cannot refuse is that nobody has to run it. This is what
runs it: a Task Scheduler entry on Windows, a crontab line elsewhere, invoking

    <python> scripts/upgrade-project.py --all --yes

on a schedule. Each project that is behind gets a box, an adoption commit and a PR;
each project that is current costs a fetch and a `git show`. Nothing needs a human
until a PR wants reviewing.

**Read-only by default.** `--yes` installs, `--check` reports what is registered and
whether it still points at this checkout, and the bare invocation prints the plan.
Same three modes as `install-git-policy.py`, for the same reason: an installer whose
default is to install is one you cannot safely ask a question.

Why an OS task and not CI: a cross-repo GitHub Action would need a token with write
access to every consumer, and the whole workspace is reachable from one machine that
already holds `gh` credentials. The trade is that upgrades happen when the machine is
on, which for a workstation-hosted workspace is the honest place for them.

**It never merges anything.** The scheduled run opens PRs and stops. `worktree.py
reconcile` is the separate, already-scheduled pass that reaps a box once its PR has
merged -- merging stays a human act, which is the standing rule for this workspace.

Stdlib only, and every decision is an importable function tested in
`tests/test_install_upgrade_schedule.py`.
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
import sweep

REPO_ROOT = Path(__file__).resolve().parents[1]
UPGRADE_SCRIPT = REPO_ROOT / "scripts" / "upgrade-project.py"
# The directory ephemeral boxes live in. A schedule must never point inside it -- see
# `main`. Read from `sweep` rather than spelled again, so one rename moves both.
BOXES_DIR = sweep.BOXES_DIR_NAME

# The registered name, and the string `--check` looks the task up by. Stable because
# renaming it would orphan whatever a previous version registered -- the installer
# would report "nothing scheduled" while the old entry kept firing.
TASK_NAME = "devkit-upgrade-projects"

# Daily rather than hourly. A devkit release is a human act that happens a few times a
# month, so a tighter loop spends fetches to discover the same "already current" it
# discovered an hour ago; and the run opens PRs, which is not a thing to do more often
# than someone reviews them.
DEFAULT_TIME = "03:00"

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

    workspace: str = ""

    @property
    def command(self) -> list[str]:
        """The argv the scheduler runs. `--all --yes` is the whole point of it.

        `--workspace` is passed explicitly, the way the sibling reconcile task does.
        `upgrade-project.py` can resolve it from its own location, but a scheduled task
        has no cwd worth relying on and the registered command is the only record of
        what it operates on -- a reader of `schtasks /Query` should not have to know the
        default to know the blast radius.
        """
        argv = [self.python, self.script, "--all", "--yes"]
        if self.workspace:
            argv += ["--workspace", self.workspace]
        return argv


def windowless_python(executable: str = sys.executable) -> str:
    """`pythonw.exe` beside `executable` when it exists, else `executable` unchanged.

    **A scheduled task must not put a console on the desktop.** `python.exe` is a
    console application, so Windows allocates one every time the task fires -- a black
    window appearing and vanishing on its own, nightly, for a job whose entire output is
    a log file nobody is watching in real time. `pythonw.exe` is the same interpreter
    built as a GUI subsystem app, which is what the existing `devkit-worktree-reconcile`
    task uses, and matching it is the point: two scheduled devkit jobs should not differ
    in whether they interrupt you.

    Falls back rather than failing: a POSIX machine has no `pythonw`, and neither does
    every Windows Python layout. There the console question does not arise the same way.

    The cost is real and worth stating: `pythonw` has no stdout or stderr at all, so
    anything printed goes nowhere. That is survivable only because every failure this
    run can produce is written to `logs/upgrade.log` -- see `upgrade-project.py`'s
    `_finish`, which writes the artifact on *every* exit path including the ones that
    never reach a project.
    """
    candidate = Path(executable).with_name("pythonw.exe")
    return str(candidate) if candidate.is_file() else executable


def schedule_for(at: str = DEFAULT_TIME, root: Path = REPO_ROOT) -> Schedule:
    """Resolve the schedule against *this* interpreter and *this* checkout.

    Derived from `sys.executable` rather than a bare `python`: a scheduled task runs
    with no activated virtualenv and often a different PATH, and `upgrade-project.py`
    imports `sweep`, `worktree` and `task_branch` from beside it. Naming the interpreter
    that is running this installer is the only spelling that is right by construction.
    """
    workspace = sweep.default_workspace(root)
    return Schedule(
        name=TASK_NAME,
        python=windowless_python(),
        script=str((root / "scripts" / "upgrade-project.py").resolve()),
        at=at,
        workspace=str(workspace) if workspace else "",
    )


def valid_time(at: str) -> bool:
    """`HH:MM`, 24-hour. Both schedulers take it, and neither says so when it is wrong."""
    hours, _, minutes = at.partition(":")
    if not (hours.isdigit() and minutes.isdigit()) or len(hours) != 2 or len(minutes) != 2:
        return False
    return 0 <= int(hours) <= 23 and 0 <= int(minutes) <= 59


def schtasks_argv(schedule: Schedule) -> list[str]:
    """The Windows registration.

    `/F` overwrites an existing entry of the same name, which is what makes this
    installer re-runnable after the checkout moves -- the alternative is a stale task
    pointing at a path that no longer exists, failing silently every night.

    The command is one quoted string because `schtasks` takes `/TR` that way; the paths
    inside it are the reason it has to be quoted at all.
    """
    return [
        "schtasks",
        "/Create",
        "/TN",
        schedule.name,
        "/TR",
        subprocess.list2cmdline(schedule.command),
        "/SC",
        "DAILY",
        "/ST",
        schedule.at,
        "/F",
    ]


def crontab_line(schedule: Schedule) -> str:
    """The POSIX equivalent, for a machine that is not this one."""
    hours, _, minutes = schedule.at.partition(":")
    return f"{int(minutes)} {int(hours)} * * * {subprocess.list2cmdline(schedule.command)}"


def query_argv(name: str = TASK_NAME) -> list[str]:
    return ["schtasks", "/Query", "/TN", name, "/FO", "LIST", "/V"]


def registered_command(stdout: str) -> str:
    """The command line `schtasks /Query /V` reports, or "" when it reports none.

    Parsed rather than trusted wholesale because the answer that matters is not "is
    something scheduled" but "is the scheduled thing still *this* checkout". An
    installer that only checked existence would call a task pointing at a deleted
    directory healthy.
    """
    for line in stdout.splitlines():
        label, sep, value = line.partition(":")
        if sep and label.strip().lower() in {"task to run", "tâche à exécuter"}:
            return value.strip()
    return ""


def drifted(registered: str, schedule: Schedule) -> str:
    """Why the registered task no longer matches this checkout, or "".

    Compares the *script path*, not the whole command line: the interpreter may
    legitimately differ (a venv rebuilt, a Python upgraded in place) and rewriting the
    task over that would be noise. A different script path is the failure worth naming
    -- it means the checkout moved and the schedule is running something else, or
    nothing.
    """
    if not registered:
        return "nothing is scheduled"
    if schedule.script.lower() not in registered.lower():
        return f"the scheduled task runs `{registered}`, which is not this checkout"
    return ""


def render_plan(schedule: Schedule, windows: bool = os.name == "nt") -> str:
    """What `--yes` would do, in the words of whichever scheduler is going to do it."""
    lines = [
        f"schedule: {schedule.name} -- daily at {schedule.at}",
        f"  runs: {subprocess.list2cmdline(schedule.command)}",
        "",
        "Each project behind the newest devkit tag gets a box, an adoption commit and a",
        "PR. Projects already current cost a fetch. Nothing is merged.",
        "",
    ]
    if windows:
        lines.append(f"  via: {subprocess.list2cmdline(schtasks_argv(schedule))}")
    else:
        lines += [
            "  via crontab, which this installer does not edit for you:",
            f"    {crontab_line(schedule)}",
        ]
    return "\n".join(lines)


def install(schedule: Schedule, runner: Runner = run_command) -> tuple[bool, str]:
    """Register it. `(ok, message)`; POSIX is reported as unsupported rather than faked.

    Editing a user's crontab from a script is the kind of irreversible, hard-to-audit
    edit this workspace does not do unattended, so there the plan *is* the deliverable
    and the line is printed for pasting.
    """
    if os.name != "nt":
        return False, (
            "not a Windows machine -- add this crontab line yourself:\n  " + crontab_line(schedule)
        )
    result = runner(schtasks_argv(schedule))
    if result.returncode != 0:
        return False, (result.stderr or result.stdout or "schtasks failed").strip()
    return True, f"scheduled {schedule.name} daily at {schedule.at}"


def run_check(schedule: Schedule, runner: Runner = run_command) -> tuple[int, str]:
    """`(exit code, message)` for `--check`. 1 when the schedule needs attention."""
    if os.name != "nt":
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
    upgrade_script = root / "scripts" / "upgrade-project.py"
    if not upgrade_script.is_file():
        print(f"schedule: no upgrade script at {upgrade_script}", file=sys.stderr)
        return 2
    if args.yes and BOXES_DIR in root.parts:
        # Registering a box would look fine today and break silently on the next
        # `reconcile`. Refused rather than warned: the failure this mode's `--check`
        # exists for is a task nobody notices has stopped running.
        #
        # Only on `--yes`. Printing the plan from a box is how an agent reads what the
        # install would do before it has anywhere else to run, and refusing that would
        # make the read-only mode useless in the place it is most often invoked from.
        print(
            f"schedule: {root} is an ephemeral box. Point --devkit at the static "
            f"checkout, which outlives the boxes.",
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
