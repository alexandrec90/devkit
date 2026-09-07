#!/usr/bin/env python3
"""Install the daily `workspace-status.py` pass as a Windows Scheduled Task.

**The job this registers is the one that was supposed to already exist.**
`workspace-status.py` was written as a SessionStart line and every document in the repo
described it as one; nothing ever wired it. So for as long as it has existed it has
reported a missing `uv`, an unset git identity, a stood-down `reconcile`, a leaked box
and an uninstalled VS Code extension to nobody at all -- and the extension gap was
eventually found the only way left, as `command 'shellCommand.execute' not found` at the
moment a quick-pick task was clicked, which names a command rather than a package and so
cannot even be searched for.

**Why a scheduled job rather than the hook it was named after.** The full pass is nine
seconds on this workstation -- `sweep` 3.3s, the `schtasks` health query 3.1s, the box
survey 2.1s -- and a SessionStart hook is synchronous, so wiring it there would charge
every agent session nine seconds before its first turn. That is precisely the cost the
module's own docstring says "gets a hook disabled", and a check that gets disabled is
back where this started.

Daily rather than per-logon, and the reason is `schedule_health`, not taste: it derives a
job's cadence from `Next Run Time` minus `Last Run Time`, and a logon-only task has no
next run at all -- so between registration and the next logon it would be reported as
"registered but has never run", which is a false alarm raised by the very reporter this
job exists to deliver. A `TimeTrigger` always has a next run. `StartWhenAvailable` (which
`devkit_schtasks.task_xml` sets for every job) catches up a fire the machine slept
through, so a laptop shut at 09:00 gets the pass when it wakes.

Two things make the run legible, the same two `install-docker-prune.py` learned:

- `log-wrap.py --always` around the command, so the pass and the failure both land in
  `logs/scheduled-workspace-status.log`, capped and overwritten per run. Nobody is
  watching a scheduled job, so the passing run has to be kept too -- an empty artifact
  otherwise covers "it passed", "it had nothing to say" and "it stopped running".
- `<WorkingDirectory>` on the task, so `logs/` resolves to this checkout rather than to
  `system32`, a scheduled task's default cwd.

And `--notify` is what reaches a human between one log and the next. It toasts only when
there is something to report, which is why it is a flag on the runner rather than
`notify-wrap.py` around it: the wrapper toasts on every run and reads pass from fail off
the exit code it propagates, and for a scheduled task that code **is** its `Last Result`.
Findings would then be reported by `schedule_health` as a broken job, every day the
workspace had anything to say.

Everything else follows `install-docker-prune.py`, deliberately: same argv builders, same
`--status` / `--uninstall` / dry-run-unless-`--yes` shape, same refusal to install from an
ephemeral box whose path will not exist next week.

Windows-only by nature. On any other platform it says so and exits 0.

The builders are pure and tested in `tests/test_install_workspace_status.py`.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devkit_schtasks
import sweep

REPO_ROOT = Path(__file__).resolve().parents[1]

TASK_NAME = "devkit-workspace-status"

# 09:00: the report is about what a working day should start by knowing, and every other
# devkit job holds a small-hours slot (03:00 upgrade, 04:00 prune) precisely so they do
# not collide. A toast raised at 04:00 is one read from the Action Center hours later,
# next to no context about which morning it belongs to.
DEFAULT_AT = "09:00"

# The wrapper's title, and the artifact path it therefore writes. Kept as a pair here
# because the second is derived from the first by `log_wrap.slug` -- a test asserts they
# still agree rather than trusting this comment.
LABEL = "Scheduled: Workspace Status"
ARTIFACT = "logs/scheduled-workspace-status.log"

# See the module docstring. Without it the pass writes a log nobody opens: the whole
# failure being fixed here is a report with no reader.
STATUS_ARGS = ("--notify",)

# Same guard as `install-docker-prune.py`, for the same reason: `pathlib` reads `os.name`
# at call time, so a test that patches it breaks every later `Path(...)`.
WINDOWS = os.name == "nt"


def status_script(root: Path = REPO_ROOT) -> Path:
    return root / "scripts" / "workspace-status.py"


def wrapper_script(root: Path = REPO_ROOT) -> Path:
    return root / "scripts" / "log-wrap.py"


# The interpreter for the task's own `<Command>`; `devkit_schtasks.windowless` owns both
# the implementation and the failure that made it shared rather than copied per installer.
windowless = devkit_schtasks.windowless


def console(python: str) -> str:
    """`python.exe` beside `pythonw.exe`, for the command the wrapper actually runs.

    The inverse of `windowless`, and not an interchangeable preference: the task's own
    `<Command>` must be windowless, and the interpreter *inside* the wrapped argv must
    not be. `log-wrap.py` spawns it with `CREATE_NO_WINDOW`, which Windows **ignores for
    a GUI-subsystem child** -- so a `pythonw.exe` there is left with no console at all,
    and every `git` this pass runs across six checkouts is handed a fresh visible one.

    Falls back to the given interpreter when there is no `python.exe` beside it, and is
    the identity for the console interpreter a human installs from.
    """
    if os.path.basename(python).lower() != "pythonw.exe":
        return python
    candidate = os.path.join(os.path.dirname(python), "python.exe")
    return candidate if os.path.isfile(candidate) else python


def status_arguments(python: str, root: Path = REPO_ROOT) -> str:
    """The arguments the scheduled task runs, as one string -- interpreter excluded.

    Nested `log-wrap.py --always <label> -- <python> workspace-status.py --notify`, the
    same nesting a dispatched VS Code task gets from `devkit_project.plan_command`.

    Every path is quoted: this workspace lives under a user profile, and profile names
    contain spaces on most machines that are not this one.
    """
    return " ".join(
        [
            f'"{wrapper_script(root)}"',
            "--always",
            f'"{LABEL}"',
            "--",
            f'"{console(python)}"',
            f'"{status_script(root)}"',
            *STATUS_ARGS,
        ]
    )


def task_document(python: str, arguments: str, at: str, root: Path = REPO_ROOT) -> str:
    """The task XML registering (or replacing) the daily pass.

    `working_dir` is the whole reason this is not a one-liner: `log-wrap.py` resolves
    `logs/` from the cwd, and a scheduled task's cwd is `system32`.
    """
    return devkit_schtasks.task_xml(
        python,
        arguments,
        devkit_schtasks.daily_trigger(at),
        working_dir=str(root),
    )


def uninstall_argv(name: str) -> list[str]:
    return ["schtasks", "/delete", "/tn", name, "/f"]


def query_argv(name: str) -> list[str]:
    return ["schtasks", "/query", "/tn", name]


def _run_argv(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """`devkit_schtasks.Runner` shape: a spawn failure is a returncode, not a traceback."""
    try:
        return subprocess.run(list(argv), capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(list(argv), 1, "", str(exc))


def _run(argv: list[str]) -> tuple[int, str]:
    done = _run_argv(argv)
    return done.returncode, (done.stdout or done.stderr or "").strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--install", action="store_true", default=True)
    mode.add_argument("--uninstall", action="store_true")
    mode.add_argument("--status", action="store_true")
    parser.add_argument("--name", default=TASK_NAME)
    parser.add_argument("--at", default=DEFAULT_AT, help="daily start time, HH:MM (24-hour)")
    parser.add_argument("--yes", dest="apply", action="store_true", help="actually call schtasks")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if not WINDOWS:
        print("install-workspace-status: Windows-only; nothing to do here.")
        return 0

    if args.status:
        code, out = _run(query_argv(args.name))
        print(out or f"no scheduled task called {args.name}")
        return 0 if code == 0 else 1

    if args.uninstall:
        target = uninstall_argv(args.name)
        if not args.apply:
            print(f"Would run: {' '.join(target)}\n\nDry run -- re-run with --yes.")
            return 0
        code, out = _run(target)
        print(out or f"removed {args.name}")
        return code

    if args.apply and sweep.source_checkout(REPO_ROOT) != REPO_ROOT:
        # The registered command carries this checkout's path verbatim, and a temporary
        # one is deleted when its work lands -- so this would install a task that works
        # until then and fails daily, forever, in silence.
        #
        # `source_checkout` rather than a `BOXES_DIR_NAME in parts` test, and the
        # difference is not pedantry: that test looks for `.worktrees/` and so misses a
        # `.claude/worktrees/` checkout entirely -- the kind `claude --worktree` cuts,
        # which is where an agent asked to wire this up is actually standing.
        # `source_checkout` already resolves both, which is why `default_workspace`,
        # `workspace-status.py` and every other installer go through it.
        print(
            f"install-workspace-status: {REPO_ROOT} is a temporary checkout, which is "
            f"deleted when its work lands. Run this from the static devkit checkout.",
            file=sys.stderr,
        )
        return 2

    python = windowless(sys.executable)
    arguments = status_arguments(sys.executable)
    if not args.apply:
        print(
            f'Would run: "{python}" {arguments}\n\n'
            f"  daily at   {args.at}\n"
            f"  in         {REPO_ROOT}\n"
            f"  records    {ARTIFACT} (every run, pass or fail)\n"
            f"  toasts     only when there is something to report -- silent otherwise\n"
            f"  on battery runs anyway, and catches up a fire it slept through\n\n"
            f"Dry run -- re-run with --yes."
        )
        return 0
    ok, out = devkit_schtasks.register(
        args.name, task_document(python, arguments, args.at), _run_argv
    )
    print(out or f"installed {args.name} (daily at {args.at})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
