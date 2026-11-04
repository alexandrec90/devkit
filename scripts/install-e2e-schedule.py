#!/usr/bin/env python3
"""Install the nightly local end-to-end pass (`scripts/e2e-pass.py`) as a Scheduled Task.

The tests no hosted runner can run -- the live workspace file, VS Code itself, a Docker
stack, a browser -- run on this workstation every night, and a red one reaches the fix
pass as that project's red default branch (`e2e_results.py`). Without this job they ran
only when a person ran the suite by hand on `main`.

**Night, then idle.** The task fires at `DEFAULT_AT`, and `e2e-pass.py run` waits before
each project until the keyboard and mouse have been still for its `--idle-minutes`,
giving up at its `--until`: a run may open a VS Code window, and one opened under
someone working late is the failure this ordering refuses. The wait is the runner's,
not the scheduler's `RunOnlyIfIdle` -- see that script's docstring for why -- so the
document is the shared one (`devkit_schtasks.task_xml`) with a long enough limit:
`TIME_LIMIT` covers the whole waiting window plus every project's own ceiling.

Everything else follows `install-workspace-status.py`: `log-wrap.py --always` around the
runner through `devkit_schtasks.logged`, so a run that crashes is on the ledger;
`<WorkingDirectory>` on the task; dry run unless `--yes`; refused from an ephemeral box.

Windows-only by nature. On any other platform it says so and exits 0.

The builders are pure and tested in `tests/test_install_e2e_schedule.py`.
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

TASK_NAME = "devkit-local-e2e"

# Machine maintenance: it delivers no branch -- what it finds, the fix pass delivers.
GROUP = "maintenance"

# 01:00, ahead of the release (02:00) and upgrade (03:00) passes, so a red found tonight
# is on the fix pass's desk before either moves anything. The runner then waits for idle.
DEFAULT_AT = "01:00"

# The waiting window (01:00 to the runner's 06:30) plus room for the runs themselves.
TIME_LIMIT = "PT8H"

# The wrapper's title and the artifact it therefore writes (`log_wrap.slug`); a test
# asserts they agree.
LABEL = "Scheduled: Local E2E"
ARTIFACT = "logs/scheduled-local-e2e.log"

RUN_ARGS = ("run",)

WINDOWS = os.name == "nt"

windowless = devkit_schtasks.windowless


def runner_script(root: Path = REPO_ROOT) -> Path:
    return root / "scripts" / "e2e-pass.py"


def pass_arguments(python: str, root: Path = REPO_ROOT) -> str:
    """The task's `<Arguments>`: the runner under `log-wrap.py --always`."""
    inner = " ".join([f'"{runner_script(root)}"', *RUN_ARGS])
    return devkit_schtasks.logged(LABEL, python, inner, root)


def task_document(python: str, arguments: str, at: str, root: Path = REPO_ROOT) -> str:
    return devkit_schtasks.task_xml(
        python,
        arguments,
        devkit_schtasks.daily_trigger(at),
        time_limit=TIME_LIMIT,
        working_dir=str(root),
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
    mode.add_argument("--install", action="store_true", default=True)
    mode.add_argument("--uninstall", action="store_true")
    mode.add_argument("--status", action="store_true")
    mode.add_argument(
        "--check",
        action="store_true",
        help="report whether the registered task is the one this checkout would register",
    )
    parser.add_argument("--name", default=TASK_NAME)
    parser.add_argument("--at", default=DEFAULT_AT, help="nightly start time, HH:MM (24-hour)")
    parser.add_argument("--yes", dest="apply", action="store_true", help="actually call schtasks")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if not WINDOWS:
        print("install-e2e-schedule: Windows-only; nothing to do here.")
        return 0

    handled = installer_cli.query_or_remove(
        args.name, status=args.status, uninstall=args.uninstall, apply=args.apply, run=_run_argv
    )
    if handled is not None:
        code, message = handled
        print(message)
        return code

    if args.apply and sweep.source_checkout(REPO_ROOT) != REPO_ROOT:
        print(
            f"install-e2e-schedule: {REPO_ROOT} is a temporary checkout, which is deleted "
            "when its work lands. Run this from the static devkit checkout.",
            file=sys.stderr,
        )
        return 2

    python = windowless(sys.executable)
    document = task_document(python, pass_arguments(python), args.at)
    if args.check:
        code, message = devkit_schtasks.run_check(args.name, document, _run_argv)
        print(message, file=sys.stderr if code else sys.stdout)
        return code
    if not args.apply:
        print(
            f'Would run: "{python}" {pass_arguments(python)}\n\n'
            f"  nightly at {args.at}, then waits for the PC to be idle\n"
            f"  in         {REPO_ROOT}\n"
            f"  records    {ARTIFACT}, and each project's result under logs/e2e/\n"
            f"  limit      {TIME_LIMIT}\n\n"
            f"Dry run -- re-run with --yes."
        )
        return 0
    ok, out = devkit_schtasks.register(args.name, document, _run_argv)
    print(out or f"installed {args.name} (nightly at {args.at})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
