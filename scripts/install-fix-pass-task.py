#!/usr/bin/env python3
"""Install the recurring `fix-pass.py --scheduled` run as a Windows Scheduled Task.

The fix pass is the half of shipping that no session does any more: it commits and
pushes what a session left an intent for, opens the PR, reads what every gate said, and
sends fixers -- harness first, then projects -- under a ledger and a daily cap. All of
that is worth nothing if a person has to click it, so the runner is the operating
system's scheduler, the one thing here that outlives a session, a reboot and a closed
editor.

**Registered wired, and off.** The task runs every half hour and reads the switch --
`"devkit.fixPass"` in the workspace file, `off` by default -- on every fire. Off, it
writes one line to its artifact and exits; `plan` writes what it would do; `dispatch`
does it. So the wiring is complete from the day it is installed, and turning it on is
one setting rather than a change to any file here. That is deliberate: the first week is
manual passes through the VS Code task, read one at a time, before anything runs unseen.

A scheduled pass always dispatches through `claude-bg`: a tab is a window, and the
scheduler has no desktop to open one on.

Windows-only by nature. On any other platform it says so and exits 0 rather than
failing: this is a workstation convenience, not part of any gate.

The argv builders are pure and tested in `tests/test_install_fix_pass_task.py`; the
`schtasks` calls are the thin shell, shared with every other installer.
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
import harness_state
import installer_cli
import sweep

REPO_ROOT = Path(__file__).resolve().parents[1]

TASK_NAME = "devkit-fix-pass"

# Branch delivery: this job moves agent branches along, so `harness-switch.py --off
# --group jobs` stands it down. `tests/test_installer_contract.py` holds the switch's
# list to the installers that say this.
GROUP = "delivery"

# Half an hour: long enough that a fixer session sent on one pass has usually pushed
# before the next reads its PR, short enough that an intent is shipped while the
# session that wrote it is still a fresh memory.
DEFAULT_INTERVAL_MINUTES = 30

# Where this job's account of itself lives -- `fix-pass.py` writes it on every pass,
# including the passes where the switch is off. `tests/test_scheduled_jobs.py` checks
# it against the script's own constant rather than trusting the copy.
ARTIFACT = "logs/fix-pass.log"

# Resolved once, at import, so a test can force the Windows path without touching
# `os.name` itself -- see `install-reconcile-task.py` for why patching the global breaks
# `pathlib` on a POSIX runner.
WINDOWS = os.name == "nt"


def pass_script(root: Path = REPO_ROOT) -> Path:
    return root / "scripts" / "fix-pass.py"


windowless = devkit_schtasks.windowless


def pass_arguments(script: Path, workspace: Path) -> str:
    """The arguments the scheduled task runs, as one string -- the interpreter excluded.

    `--scheduled` is what makes the pass read its mode from the workspace file and force
    the background agent; `--workspace` is named rather than left to the default for the
    reason every installer names it -- a scheduled task starts in `system32`, and a moved
    checkout should fail loudly rather than reconcile whatever it happens to find.
    """
    return f'"{script}" --scheduled --workspace "{workspace}"'


def task_document(python: str, arguments: str, minutes: int) -> str:
    """The task XML registering (or replacing) the recurring pass.

    Registered from a document rather than `schtasks /SC MINUTE` because the flags that
    spelling supports do not include the ones that decide whether this runs at all on a
    laptop -- see `devkit_schtasks`.
    """
    return devkit_schtasks.task_xml(
        python,
        arguments,
        devkit_schtasks.repeating_trigger(minutes),
        # Lands disabled when the jobs tier is stood down; `harness-switch.py --off
        # jobs` records the whole group, not just the tasks that existed when it ran.
        enabled=TASK_NAME not in harness_state.stood_down(),
    )


def uninstall_argv(name: str) -> list[str]:
    return installer_cli.uninstall_argv(name)


def query_argv(name: str) -> list[str]:
    return installer_cli.query_argv(name)


def _run_argv(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """`devkit_schtasks.Runner` shape: a spawn failure is a returncode, not a traceback."""
    try:
        return subprocess.run(list(argv), capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(list(argv), 1, "", str(exc))


def query_or_remove(args: argparse.Namespace) -> int | None:
    """The `--status` and `--uninstall` modes; None when neither was asked for."""
    handled = installer_cli.query_or_remove(
        args.name,
        status=args.status,
        uninstall=args.uninstall,
        apply=args.apply,
        run=_run_argv,
    )
    if handled is None:
        return None
    code, message = handled
    print(message)
    return code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--install", action="store_true", default=True)
    mode.add_argument("--uninstall", action="store_true")
    mode.add_argument("--status", action="store_true")
    mode.add_argument(
        "--check",
        action="store_true",
        help="report whether the registered task is the one this checkout would register",
    )
    parser.add_argument("--name", default=TASK_NAME)
    parser.add_argument("--minutes", type=int, default=DEFAULT_INTERVAL_MINUTES)
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument("--yes", dest="apply", action="store_true", help="actually call schtasks")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)

    if not WINDOWS:
        print("install-fix-pass-task: Windows-only; nothing to do here.")
        return 0

    handled = query_or_remove(args)
    if handled is not None:
        return handled

    if args.apply and sweep.source_checkout(REPO_ROOT) != REPO_ROOT:
        # The registered command carries this checkout's path verbatim, and a temporary
        # checkout is destroyed by the very passes being scheduled.
        print(
            f"install-fix-pass-task: {REPO_ROOT} is a temporary checkout (an ephemeral box "
            f"or an agent CLI's --worktree checkout). Run this from the static devkit checkout.",
            file=sys.stderr,
        )
        return 2

    workspace = (args.workspace or sweep.default_workspace(REPO_ROOT)).resolve()
    python = windowless(sys.executable)
    arguments = pass_arguments(pass_script(), workspace)
    if args.check:
        code, message = devkit_schtasks.run_check(
            args.name, task_document(python, arguments, args.minutes), _run_argv
        )
        print(message, file=sys.stderr if code else sys.stdout)
        return code
    if not args.apply:
        print(
            f'Would run: "{python}" {arguments}\n\n'
            f"  every     {args.minutes} minutes\n"
            f"  switch    devkit.fixPass in the workspace file (off, plan, dispatch); off does nothing\n"
            f"  on battery runs anyway, and catches up a fire it slept through\n\n"
            f"Dry run -- re-run with --yes."
        )
        return 0
    ok, out = devkit_schtasks.register(
        args.name, task_document(python, arguments, args.minutes), _run_argv
    )
    print(out or f"installed {args.name} (every {args.minutes} minutes)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
