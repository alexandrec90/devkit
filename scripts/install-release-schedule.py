#!/usr/bin/env python3
"""Register `release-pipeline.py --if-needed --yes` as a nightly OS task.

`Devkit: Cut Release` made a release one click. This makes it none: a task that fires
at 02:00, asks whether `origin/main` carries anything a consumer cannot reach, and cuts
the release when the answer is yes.

**The predicate is the whole design.** "Tag every merge" would cost a release and five
adoption PRs for a doc fix; `release_needed` restricts the question to the two tiers a
consumer actually receives -- the vendored `MANIFEST` and the published pre-commit
channel -- so a night with only devkit-internal changes on main ends with a log line
saying so and nothing else. That predicate is not a judgement anybody was making by
hand; it is the same diff `upgrade-project.py` already computes to *warn* that a
release is owed, which put the nightly pass in the position of announcing a problem it
had everything it needed to fix.

**02:00, an hour ahead of `devkit-upgrade-projects`.** The pipeline ends by running the
adoption pass itself, so on a night it fires the 03:00 job finds every consumer current
and costs a fetch each. That ordering is the point: adoption an hour later than the
release rather than a day.

**This one merges, and its sibling installer says it never does.** That difference is
deliberate and is the single thing to weigh before installing this. `install-upgrade-
schedule.py` opens PRs and stops, because a green gate plus a label is what authorises
a merge in this workspace and the scheduled pass has no business pre-empting it. The
release PR cannot use that route at all: it is red **by construction** on
`test_fallback_devkit_ref_tracks_the_newest_tag`, since `FALLBACK_DEVKIT_REF` must name
a tag that does not exist until the PR merges. No label-driven auto-merge can ever fire
on it, so either a human reads a red gate at 2am or something encodes what the red is
allowed to be. `release_pipeline.gate_verdict` is that encoding, and unlike a human at
2am it refuses on a *second* failing test, on a failure in another job, and on an
artifact naming no tests at all.

Why an OS task and not a GitHub Action, same answer as the upgrade job's: the tag is
pushed by `release.yml`, but the PR has to be opened by a real account -- a PR authored
by `GITHUB_TOKEN` triggers no workflow run, so it arrives with no PR Gate, and the gate
is exactly what step 5 reads. This machine holds those credentials; CI does not.

**Read-only by default.** `--yes` installs, `--check` reports what is registered and
whether it still points at this checkout, and the bare invocation prints the plan.

Stdlib only, and every decision is an importable function tested in
`tests/test_install_release_schedule.py`.
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
import harness_state
import sweep

REPO_ROOT = Path(__file__).resolve().parents[1]

# The directory ephemeral boxes live in. A schedule must never point inside it -- see
# `main`. Read from `sweep` rather than spelled again, so one rename moves both.
BOXES_DIR = sweep.BOXES_DIR_NAME

# The registered name, and the string `--check` looks the task up by. Stable because
# renaming it would orphan whatever a previous version registered.
TASK_NAME = "devkit-release"

# The wrapper's title, and the artifact path it therefore writes. `release-pipeline.py`
# writes no artifact of its own -- it has two callers and most of its runs are clicks --
# so the wrapper is what gives the scheduled one a record. A test asserts the pair still
# agrees rather than trusting this comment.
#
# The label differs from the clicked task's ("Devkit: Cut Release") on purpose: they
# would otherwise slug to the same file, and a click the next morning would overwrite
# the only account of what the unattended run did.
LABEL = "Scheduled: Devkit Release"
ARTIFACT = "logs/scheduled-devkit-release.log"

# Same guard as the sibling installers, for the same reason: `pathlib` reads `os.name`
# at call time, so a test that patches it breaks every later `Path(...)`.
WINDOWS = os.name == "nt"

# An hour before `devkit-upgrade-projects` at 03:00. See the module docstring.
DEFAULT_AT = "02:00"

# `--if-needed` is what makes a nightly cadence affordable, and `--yes` is what makes it
# a release rather than a plan. Neither is optional, so both live here rather than in an
# argument a future edit could drop one half of.
PIPELINE_ARGS = ("--if-needed", "--yes")

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def run_command(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), capture_output=True, text=True, check=False)


def pipeline_script(root: Path = REPO_ROOT) -> Path:
    return root / "scripts" / "release-pipeline.py"


def wrapper_script(root: Path = REPO_ROOT) -> Path:
    return root / "scripts" / "log-wrap.py"


def windowless(python: str) -> str:
    """The interpreter the task's `<Command>` names, so the nightly run opens no console.

    A wrapper rather than an alias for `devkit_schtasks.windowless`, for the reason its
    sibling `install-upgrade-schedule.windowless_python` gives: the shared function owns
    *why* -- including that a venv's `pythonw.exe` is a stub that must be resolved
    through `pyvenv.cfg`, since uv's spawns the base interpreter as a child and Windows
    hands the child of a console-less task a brand new window. What belongs here is the
    call site. See `console` for the other half.
    """
    return devkit_schtasks.windowless(python)


def console(python: str) -> str:
    """`python.exe` beside `pythonw.exe`, for the command the wrapper actually runs.

    The inverse of `windowless`, and the two are not interchangeable halves of a
    preference: `log-wrap.py` spawns this one with `CREATE_NO_WINDOW`, which Windows
    **ignores for a GUI-subsystem child** -- so a `pythonw.exe` here would be left with
    no console at all, and every `gh` and `git` the pipeline runs would be handed a
    fresh visible one. Dozens of them, at 2am.

    Resolved beside the interpreter it is handed rather than through `pyvenv.cfg`: this
    one is spawned *by* the wrapper, so a venv stub is only a stub, and running the
    checkout's own `python.exe` is the behaviour a venv is for.
    """
    if os.path.basename(python).lower() != "pythonw.exe":
        return python
    candidate = os.path.join(os.path.dirname(python), "python.exe")
    return candidate if os.path.isfile(candidate) else python


def release_arguments(python: str, root: Path = REPO_ROOT, workspace: str = "") -> str:
    """The arguments the scheduled task runs, as one string -- interpreter excluded.

    Nested `log-wrap.py --always <label> -- <python> release-pipeline.py ...`, the same
    nesting a dispatched VS Code task gets from `devkit_project.plan_command`. The inner
    interpreter is the **console** one, and that is the half `tests/test_scheduled_jobs.py`
    checks across every wrapped job at once: the wrapper spawns it with
    `CREATE_NO_WINDOW`, which Windows ignores for a GUI-subsystem child, so a
    `pythonw.exe` here would leave every `gh` and `git` the pipeline runs with a fresh
    visible console.

    `--workspace` is passed explicitly, as the sibling jobs do: a scheduled task has no
    cwd worth relying on, and the registered command is the only record of what the
    job's adoption pass will reach.

    Every path is quoted: this workspace lives under a user profile, and profile names
    contain spaces on most machines that are not this one.
    """
    parts = [
        f'"{wrapper_script(root).resolve()}"',
        "--always",
        f'"{LABEL}"',
        "--",
        f'"{console(python)}"',
        f'"{pipeline_script(root).resolve()}"',
        *PIPELINE_ARGS,
    ]
    if workspace:
        parts += ["--workspace", f'"{workspace}"']
    return " ".join(parts)


@dataclass(frozen=True)
class Schedule:
    """What is to be registered, resolved from this checkout."""

    name: str
    python: str
    root: Path
    at: str
    workspace: str = ""

    @property
    def script(self) -> str:
        """The path `--check` compares by; see `drifted`."""
        return str(pipeline_script(self.root).resolve())

    @property
    def arguments(self) -> str:
        return release_arguments(self.python, self.root, self.workspace)

    @property
    def command(self) -> str:
        """The whole registered line, for the plan and the crontab equivalent."""
        return f'"{windowless(self.python)}" {self.arguments}'


def schedule_for(at: str = DEFAULT_AT, root: Path = REPO_ROOT, python: str = "") -> Schedule:
    """Resolve the schedule against *this* interpreter and *this* checkout.

    `python` is a parameter rather than a read of `sys.executable` inside the function
    so a test can resolve a schedule for a Windows layout it is not running on. The
    default is the interpreter running the installer, which is the only spelling that is
    right by construction: a scheduled task has no activated virtualenv, and
    `release-pipeline.py` imports `release`, `sweep` and `task_branch` from beside it.
    """
    workspace = sweep.default_workspace(root)
    return Schedule(
        name=TASK_NAME,
        python=python or sys.executable,
        root=root,
        at=at,
        workspace=str(workspace) if workspace else "",
    )


def valid_time(at: str) -> bool:
    """`HH:MM`, 24-hour. Both schedulers take it, and neither says so when it is wrong."""
    hours, _, minutes = at.partition(":")
    if not (hours.isdigit() and minutes.isdigit()) or len(hours) != 2 or len(minutes) != 2:
        return False
    return 0 <= int(hours) <= 23 and 0 <= int(minutes) <= 59


def task_document(schedule: Schedule) -> str:
    """The Windows registration, as a task document.

    `working_dir` is not decoration: `log-wrap.py` resolves `logs/` from the cwd, and a
    scheduled task's cwd is `system32`. Without it the job's only record would be
    written somewhere nobody looks, which is the exact failure the artifact exists for.

    `time_limit` is generous because most of this job's wall clock is spent waiting on
    two GitHub gates it does not control -- the prepare PR's, then the tag workflow's.
    Still finite: `IgnoreNew` means a wedged run suppresses every later one until it
    expires, so "no limit" would turn one bad night into a permanently dead job.
    """
    return devkit_schtasks.task_xml(
        windowless(schedule.python),
        schedule.arguments,
        devkit_schtasks.daily_trigger(schedule.at),
        working_dir=str(schedule.root),
        time_limit="PT3H",
        # Lands disabled when the jobs tier is stood down. `harness-switch.py --off
        # jobs` records the whole group, not just the tasks that existed when it
        # ran, so an installer executed afterwards must not hand the operator back
        # a running job they had switched off.
        enabled=TASK_NAME not in harness_state.stood_down(),
    )


def crontab_line(schedule: Schedule) -> str:
    """The POSIX equivalent, for a machine that is not this one."""
    hours, _, minutes = schedule.at.partition(":")
    return f"{int(minutes)} {int(hours)} * * * {schedule.command}"


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

    Compares the *script path*, not the whole command line: the interpreter may
    legitimately differ (a venv rebuilt, a Python upgraded in place) and rewriting the
    task over that would be noise. A different script path means the checkout moved and
    the schedule is running something else, or nothing.
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
        f"  runs: {schedule.command}",
        "",
        "Most nights this exits having done nothing: it releases only when main carries",
        "a vendored or published-channel change no tag delivers. On the nights it does",
        "fire it opens the prepare PR, merges it once the gate's only failure is the",
        "expected one, dispatches the tag workflow, and opens every consumer's adoption",
        f"PR. It is the one devkit job that merges -- see {Path(__file__).name}'s docstring.",
        "",
        f"  log: {ARTIFACT}, written on every exit path including the quiet ones",
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--yes", action="store_true", help="register the task")
    mode.add_argument(
        "--check",
        action="store_true",
        help="report whether a task is registered and still points at this checkout",
    )
    parser.add_argument("--at", default=DEFAULT_AT, help="daily start time, HH:MM (24-hour)")
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
    if not pipeline_script(root).is_file():
        print(f"schedule: no release pipeline at {pipeline_script(root)}", file=sys.stderr)
        return 2
    if args.yes and BOXES_DIR in root.parts:
        # Registering a box would look fine today and break silently on the next
        # `reconcile`. Refused rather than warned. Only on `--yes`: printing the plan
        # from a box is how an agent reads what the install would do before it has
        # anywhere else to run.
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
