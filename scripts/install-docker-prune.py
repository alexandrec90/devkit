#!/usr/bin/env python3
"""Install the nightly `docker-maint.py prune` run as a Windows Scheduled Task.

**This job already existed on this machine and was the only one nothing here created.**
It was registered by hand with `schtasks /Create /SC DAILY`, which is exactly the
spelling `scripts/devkit_schtasks.py` exists to replace, and it had every consequence
that module's docstring predicts plus one it does not:

| what the hand-registered task did | why |
| --- | --- |
| skipped every fire while unplugged | `DisallowStartIfOnBatteries`, no flag to turn it off |
| waited for 10 minutes of idle, retrying for two hours | `/SC DAILY` inherits `RunOnlyIfIdle` |
| never caught up a fire it slept through | `StartWhenAvailable` defaults false |
| **left no trace of any run, passing or failing** | `pythonw.exe` + a script with no artifact |

The last row is the one that made it worth writing a file about. It ran, it exited 1,
and the only evidence anywhere on the machine was a `Last Result` integer in the
scheduler -- no message, no log, nothing naming which of `generic_prune`'s two failure
exits it took. `scripts/schedule_health.py` surfaced the integer at session start, which
is what it is for, and a fresh agent reading that line had nowhere to go next.

So this installer's job is not only to register the task with settings a laptop can
honour. It is to make the run **legible afterwards**, which takes three things the hand
spelling had none of:

- `log-wrap.py --always` around the command, so the pass and the failure both land in
  `logs/scheduled-docker-prune.log`, capped and overwritten per run.
- `<WorkingDirectory>` on the task, so `logs/` resolves to this checkout rather than to
  `system32` -- a scheduled task's default cwd.
- `--idle-only`, so the unattended run *declines* when containers are up instead of
  stopping them at 04:00 for disk. `generic_prune`'s docstring already called this "the
  scheduled caller's" spelling; the registered task did not pass it. That alone is the
  likeliest source of the exit 1, since the non-idle path starts Docker and fails when
  the engine does not come back.

Everything else follows `install-reconcile-task.py`, deliberately: same argv builders,
same `--status` / `--uninstall` / dry-run-unless-`--yes` shape, same refusal to install
from an ephemeral box whose path will not exist next week.

Windows-only by nature. On any other platform it says so and exits 0.

The builders are pure and tested in `tests/test_install_docker_prune.py`.
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

TASK_NAME = "devkit-docker-prune"

# 04:00 rather than 03:00: the upgrade job holds that slot, and two `pythonw` jobs
# starting together on a laptop that just woke up is how one of them times out.
DEFAULT_AT = "04:00"

# The wrapper's title, and the artifact path it therefore writes. Kept as a pair here
# because the second is derived from the first by `log_wrap.slug` -- a test asserts they
# still agree rather than trusting this comment.
LABEL = "Scheduled: Docker Prune"
ARTIFACT = "logs/scheduled-docker-prune.log"

# See the module docstring. `--generic` matches the hand-registered command and is
# belt-and-braces given the working directory is devkit, which ships no delegate.
PRUNE_ARGS = ("prune", "--generic", "--idle-only")

# Same guard as `install-reconcile-task.py`, for the same reason: `pathlib` reads
# `os.name` at call time, so a test that patches it breaks every later `Path(...)`.
WINDOWS = os.name == "nt"


def maint_script(root: Path = REPO_ROOT) -> Path:
    return root / "scripts" / "docker-maint.py"


def wrapper_script(root: Path = REPO_ROOT) -> Path:
    return root / "scripts" / "log-wrap.py"


# The interpreter for the task's own `<Command>`. Every installer used to carry its own
# copy of this, on the reasoning that six lines are cheaper to repeat than to couple --
# and all six were then wrong in the same way for as long as it took one job's
# interpreter to come from a uv `.venv`. `devkit_schtasks.windowless` owns both the
# implementation and that failure.
windowless = devkit_schtasks.windowless


def console(python: str) -> str:
    """`python.exe` beside `pythonw.exe`, for the command the wrapper actually runs.

    The inverse of `windowless`, and the two are not interchangeable halves of a
    preference: the task's own `<Command>` must be windowless, and the interpreter
    *inside* the wrapped argv must not be. `log-wrap.py` spawns it with
    `CREATE_NO_WINDOW`, which Windows **ignores for a GUI-subsystem child** -- so a
    `pythonw.exe` there is left with no console at all, and every process it goes on to
    spawn is handed a fresh visible one. A console child with the flag gets a hidden
    console instead, and passes it down.

    Falls back to the given interpreter when there is no `python.exe` beside it, and is
    the identity for the console interpreter a human installs from.
    """
    if os.path.basename(python).lower() != "pythonw.exe":
        return python
    candidate = os.path.join(os.path.dirname(python), "python.exe")
    return candidate if os.path.isfile(candidate) else python


def prune_arguments(python: str, root: Path = REPO_ROOT) -> str:
    """The arguments the scheduled task runs, as one string -- interpreter excluded.

    Nested `log-wrap.py --always <label> -- <python> docker-maint.py ...`, which is the
    same nesting a dispatched VS Code task gets from `devkit_project.plan_command`. The
    inner interpreter is the **console** one (`console`, not `windowless`): the wrapper
    spawns it with `CREATE_NO_WINDOW`, a flag Windows ignores for a GUI-subsystem child,
    so a `pythonw.exe` here would be console-less and every `docker` under it would open
    a window of its own at 04:00 -- the exact complaint that made these jobs windowless.

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
            f'"{maint_script(root)}"',
            *PRUNE_ARGS,
        ]
    )


def task_document(python: str, arguments: str, at: str, root: Path = REPO_ROOT) -> str:
    """The task XML registering (or replacing) the nightly pass.

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
        print("install-docker-prune: Windows-only; nothing to do here.")
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

    if args.apply and sweep.BOXES_DIR_NAME in REPO_ROOT.parts:
        # The registered command carries this checkout's path verbatim, and a box is
        # destroyed by `reconcile` -- so this would install a task that works until the
        # next reconcile pass and then fails nightly, forever, in silence. Same refusal
        # both other installers make, on `--yes` only so a dry run still reads.
        print(
            f"install-docker-prune: {REPO_ROOT} is an ephemeral box, which reconcile "
            f"destroys. Run this from the static devkit checkout.",
            file=sys.stderr,
        )
        return 2

    python = windowless(sys.executable)
    arguments = prune_arguments(sys.executable)
    if not args.apply:
        print(
            f'Would run: "{python}" {arguments}\n\n'
            f"  daily at   {args.at}\n"
            f"  in         {REPO_ROOT}\n"
            f"  records    {ARTIFACT} (every run, pass or fail)\n"
            f"  skips      whenever containers are up -- it never stops a running stack\n"
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
