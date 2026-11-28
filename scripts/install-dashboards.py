#!/usr/bin/env python3
"""Install the dashboard gateway, `dashboards.py serve`, as a Windows Scheduled Task.

`dashboards.py` explains the gateway. It is **resident**: it starts at logon
and runs until sign-out, so the task carries no execution time limit (`PT0S`, the
scheduler's spelling of none). An hour's limit would close every dashboard an hour after
logon, each day.

**A 15-minute repetition beside the logon trigger** is what restarts a gateway that died
mid-session. While one runs, `MultipleInstancesPolicy` `IgnoreNew` turns each fire into
nothing (`schedule_health.OVERLAP_EXPECTED` knows to read the refusal as the gateway
working). Without it, a gateway that crashed would leave every dashboard port closed until
the next logon.

Not run under `log-wrap.py`, as no resident job is: the wrapper would hold everything a
resident process prints for the whole session. The gateway writes `logs/dashboards.log`
itself, appending as it goes.

Registered on every workstation: a PC with no checkout of a project skips its dashboard,
and one with none at all serves nothing and exits. Windows-only by nature; elsewhere it
says so and exits 0. Tested in `tests/test_install_dashboards.py`.
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

TASK_NAME = "devkit-dashboards"

# Machine maintenance: standing the branch-delivery tier down leaves the dashboards up.
GROUP = "maintenance"

# Written by the gateway itself (`dashboards.ARTIFACT`); `tests/test_scheduled_jobs.py`
# holds the two equal.
ARTIFACT = "logs/dashboards.log"

NO_TIME_LIMIT = "PT0S"
RESTART_MINUTES = 15

# See `install-reconcile-task.py` for why this is resolved once at import.
WINDOWS = os.name == "nt"

windowless = devkit_schtasks.windowless


def dashboards_script(root: Path = REPO_ROOT) -> Path:
    return root / "scripts" / "dashboards.py"


def dashboards_arguments(script: Path) -> str:
    """The task's arguments, the interpreter excluded. `serve` is named because the
    default verb is `status`, which prints and exits: a task that lost the word would
    leave every dashboard port closed."""
    return f'"{script}" serve'


def task_document(python: str, arguments: str, script: Path) -> str:
    """The task XML, started in the checkout whose `logs/` the gateway writes."""
    root = str(PureWindowsPath(script).parent.parent)
    return devkit_schtasks.task_xml(
        python,
        arguments,
        devkit_schtasks.logon_trigger() + devkit_schtasks.repeating_trigger(RESTART_MINUTES),
        time_limit=NO_TIME_LIMIT,
        working_dir=root,
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
        print("install-dashboards: Windows-only; nothing to do here.")
        return 0
    if args.apply and sweep.source_checkout(REPO_ROOT) != REPO_ROOT:
        print(
            f"install-dashboards: {REPO_ROOT} is a temporary checkout. Run this from the "
            f"static devkit checkout, which outlives the boxes.",
            file=sys.stderr,
        )
        return 2

    script = dashboards_script()
    python = windowless(sys.executable)
    document = task_document(python, dashboards_arguments(script), script)
    if args.check:
        code, message = devkit_schtasks.run_check(TASK_NAME, document, _run_argv)
        print(message, file=sys.stderr if code else sys.stdout)
        return code
    if not args.apply:
        print(
            f'Would run: "{python}" {dashboards_arguments(script)}\n\n'
            f"  at        logon, resident until sign-out; restarted within\n"
            f"            {RESTART_MINUTES} minutes if it dies\n"
            f"  serves    every `devkit.dashboards` entry with a checkout on this PC\n\n"
            f"Dry run -- re-run with --yes."
        )
        return 0
    ok, out = devkit_schtasks.register(TASK_NAME, document, _run_argv)
    print(out or f"installed {TASK_NAME} (at logon, resident)")
    if ok:
        # Registering does not start it; the next logon would. Start it now so the
        # dashboards answer today, as `--yes` promises.
        started = _run_argv(["schtasks", "/Run", "/TN", TASK_NAME])
        if started.returncode:
            print(f"install-dashboards: registered, but could not start it: {started.stderr}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
