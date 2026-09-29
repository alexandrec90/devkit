#!/usr/bin/env python3
"""Install the recurring `collectors.py maintain` run as a Windows Scheduled Task.

`collectors.py` explains the job. The installer's one decision worth stating is that it
registers the task on **every** workstation, the laptop included: whether a machine runs
a collector is `collectors.py run-here`, recorded on that machine alone, and a machine
assigned nothing makes each fire a no-op that spawns no `docker` at all. So moving a
collector between machines never needs an installer, and the daily `devkit-installers`
pass keeping this task current everywhere is harmless where it is idle.

Every 15 minutes, plus at logon: a reboot is exactly when a collector is certainly
down, and the logon fire starts it without waiting out an interval. Logon, not boot --
the job needs the interactive user's Docker Desktop, and a boot trigger is refused to the
unelevated `--yes` that keeps it current (`devkit_schtasks.logon_trigger`).

Windows-only by nature; elsewhere it says so and exits 0. Tested in
`tests/test_install_collectors.py`.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path, PureWindowsPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devkit_schtasks
import harness_state
import installer_cli
import sweep

REPO_ROOT = Path(__file__).resolve().parents[1]

TASK_NAME = "devkit-collectors"

# Machine maintenance: standing the branch-delivery tier down leaves ingestion running.
GROUP = "maintenance"

# `collectors.py` writes it on every exit path; `tests/test_scheduled_jobs.py` checks
# this against the runner's own constant.
ARTIFACT = "logs/collectors.log"

DEFAULT_INTERVAL_MINUTES = 15

# See `install-reconcile-task.py` for why this is resolved once at import.
WINDOWS = os.name == "nt"


def collectors_script(root: Path = REPO_ROOT) -> Path:
    return root / "scripts" / "collectors.py"


windowless = devkit_schtasks.windowless


def collectors_arguments(script: Path) -> str:
    """The task's arguments, the interpreter excluded.

    `maintain` is named because the default mode is `status`, read-only on purpose: a
    task that lost the word would fire every 15 minutes and start nothing.
    """
    return f'"{script}" maintain'


def task_document(python: str, arguments: str, minutes: int, script: Path) -> str:
    """The task XML, registered from a document for `devkit_schtasks`' reasons."""
    return devkit_schtasks.task_xml(
        python,
        arguments,
        devkit_schtasks.repeating_trigger(minutes) + devkit_schtasks.logon_trigger(),
        # `PureWindowsPath`: the document is Windows by construction, whatever builds it.
        working_dir=str(PureWindowsPath(script).parent.parent),
        enabled=TASK_NAME not in harness_state.stood_down(),
    )


def _run_argv(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """`devkit_schtasks.Runner` shape: a spawn failure is a returncode, not a traceback."""
    try:
        return subprocess.run(list(argv), capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(list(argv), 1, "", str(exc))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--uninstall", action="store_true")
    mode.add_argument("--status", action="store_true")
    mode.add_argument(
        "--check",
        action="store_true",
        help="report whether the registered task is the one this checkout would register",
    )
    parser.add_argument("--minutes", type=int, default=DEFAULT_INTERVAL_MINUTES)
    parser.add_argument("--yes", dest="apply", action="store_true", help="actually call schtasks")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)

    handled = installer_cli.answer(
        TASK_NAME,
        status=args.status,
        uninstall=args.uninstall,
        apply=args.apply,
        run=_run_argv,
        windows=WINDOWS,
    )
    if handled is not None:
        return handled
    if not WINDOWS:
        print("install-collectors: Windows-only; nothing to do here.")
        return 0
    if args.apply and sweep.source_checkout(REPO_ROOT) != REPO_ROOT:
        print(
            f"install-collectors: {REPO_ROOT} is a temporary checkout. Run this from the "
            f"static devkit checkout, which outlives the boxes.",
            file=sys.stderr,
        )
        return 2

    script = collectors_script()
    document = task_document(
        windowless(sys.executable), collectors_arguments(script), args.minutes, script
    )
    if args.check:
        code, message = devkit_schtasks.run_check(TASK_NAME, document, _run_argv)
        print(message, file=sys.stderr if code else sys.stdout)
        return code
    if not args.apply:
        print(
            f'Would run: "{windowless(sys.executable)}" {collectors_arguments(script)}\n\n'
            f"  every     {args.minutes} minutes, and at logon\n"
            f"  acts on   only the collectors `collectors.py run-here` / `stop-here` assigned\n"
            f"            to this machine; assigned none, each fire does nothing\n\n"
            f"Dry run -- re-run with --yes."
        )
        return 0
    ok, out = devkit_schtasks.register(TASK_NAME, document, _run_argv)
    print(out or f"installed {TASK_NAME} (every {args.minutes} minutes, and at logon)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
