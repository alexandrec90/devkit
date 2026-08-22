#!/usr/bin/env python3
"""Install the nightly `docker-maint.py stop-idle` run as a Windows Scheduled Task.

Every stack in this workspace carries `restart: unless-stopped`, so whatever was
running at shutdown resurrects on every boot -- a stack someone brought up once runs
around the clock whether or not anyone touches that project again. On the machine this
was written for that was every project's stack at once, all day, on a laptop whose
`.wslconfig` was already tuned as far as it goes.

This job is the counterpart: nightly, `generic_stop_idle` stops each stack that has
opted in (`[docker] auto_stop = true` in the project's own `.devkit.toml`) and shows
no established connection to a published port, with a grace window for anything
recently started. `docker stop`, never `down` -- containers and named volumes survive,
and a manual stop is exactly the state `unless-stopped` respects across reboots, so a
stopped stack stays stopped until someone (or an agent's test run) wants it again.

Opt-in is the safety property, not a rollout convenience: a collector-style stack --
ibkr_trader's `serve` scheduler, sports_betting's collector -- does scheduled work
with no client connected, which no connection check can tell apart from idle. Those
projects simply never opt in.

03:30 rather than a shared slot: 03:00 belongs to the upgrade job and 04:00 to the
prune, and two `pythonw` jobs starting together on a laptop that just woke up is how
one of them times out. Landing *before* the prune also means a night where every
opted-in stack was idle hands the prune the container-free machine its `--idle-only`
guard is waiting for.

Everything else follows `install-docker-prune.py`, deliberately: same argv builders,
same `--status` / `--uninstall` / dry-run-unless-`--yes` shape, same refusal to
install from an ephemeral box whose path will not exist next week, same
`log-wrap.py --always` wrapper so the pass and the failure both land in the artifact.

Windows-only by nature. On any other platform it says so and exits 0.

The builders are pure and tested in `tests/test_install_docker_stop_idle.py`.
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

TASK_NAME = "devkit-docker-stop-idle"

# See the module docstring for why this slot: between the 03:00 upgrade and the 04:00
# prune, and before the prune on purpose.
DEFAULT_AT = "03:30"

# The wrapper's title, and the artifact path it therefore writes. Kept as a pair here
# because the second is derived from the first by `log_wrap.slug` -- a test asserts they
# still agree rather than trusting this comment.
LABEL = "Scheduled: Docker Stop Idle"
ARTIFACT = "logs/scheduled-docker-stop-idle.log"

# `--generic` is belt-and-braces: `stop-idle` ships no delegate slot at all, but the
# flag keeps the registered command meaning "devkit's own pass" even if that changes.
STOP_IDLE_ARGS = ("stop-idle", "--generic")

# Same guard as `install-docker-prune.py`, for the same reason: `pathlib` reads
# `os.name` at call time, so a test that patches it breaks every later `Path(...)`.
WINDOWS = os.name == "nt"


def maint_script(root: Path = REPO_ROOT) -> Path:
    return root / "scripts" / "docker-maint.py"


def wrapper_script(root: Path = REPO_ROOT) -> Path:
    return root / "scripts" / "log-wrap.py"


# The interpreter for the task's own `<Command>`; see `devkit_schtasks.windowless`, which
# owns this policy and the venv-stub failure that consolidated the installers' copies.
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


def stop_idle_arguments(python: str, root: Path = REPO_ROOT) -> str:
    """The arguments the scheduled task runs, as one string -- interpreter excluded.

    Nested `log-wrap.py --always <label> -- <python> docker-maint.py ...`, with the
    inner interpreter the **console** one (`console`, not `windowless`): the wrapper
    spawns it with `CREATE_NO_WINDOW`, which Windows ignores for a GUI-subsystem child,
    so a `pythonw.exe` here would be console-less and hand each `docker` below it a
    fresh visible console.

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
            *STOP_IDLE_ARGS,
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
        print("install-docker-stop-idle: Windows-only; nothing to do here.")
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
            f"install-docker-stop-idle: {REPO_ROOT} is an ephemeral box, which reconcile "
            f"destroys. Run this from the static devkit checkout.",
            file=sys.stderr,
        )
        return 2

    python = windowless(sys.executable)
    arguments = stop_idle_arguments(sys.executable)
    if not args.apply:
        print(
            f'Would run: "{python}" {arguments}\n\n'
            f"  daily at   {args.at}\n"
            f"  in         {REPO_ROOT}\n"
            f"  records    {ARTIFACT} (every run, pass or fail)\n"
            f"  stops      only stacks whose own .devkit.toml says [docker] auto_stop = true,\n"
            f"             with no established connection to a published port and nothing\n"
            f"             recently started -- every ambiguity keeps a stack up\n"
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
