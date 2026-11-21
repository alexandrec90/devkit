#!/usr/bin/env python3
"""Install the medium test tier (`scripts/tier-pass.py`) as a Scheduled Task.

Every project's own tests, on this PC, within minutes of a merge: CI is Linux, so without
this job nothing checks a change on Windows. A red one reaches the fix pass as that
project's red default branch (`e2e_results.py`).

**Registered on every PC, and it tests only where `devkit.fixPass` is `dispatch`.** A
result stays on the PC that made it, so anywhere else a red would reach no fixer: the runner
says so in one line and exits 0, and nothing is re-registered when the switch moves.

**Every 15 minutes, whatever the person is doing.** Unlike
`devkit-local-e2e` there is no idle wait and no host assignment: what it runs is ordinary
tests in a tree of their own, windowless, so a person at the keyboard does not see it, and
a machine that is busy says so through `host_memory`. `TIME_LIMIT` covers the runner's whole
pass, and fires during one are dropped by the shared document's `IgnoreNew`, which loses
nothing since the pass reads every tip as it gets to it.

Everything else follows `install-e2e-schedule.py`: `log-wrap.py --always` around the
runner through `devkit_schtasks.logged`, so a run that crashes is on the ledger;
`<WorkingDirectory>` on the task; dry run unless `--yes`; refused from an ephemeral box.

Windows-only by nature. On any other platform it says so and exits 0.

The builders are pure and tested in `tests/test_install_tier_schedule.py`.
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

TASK_NAME = "devkit-medium-tier"

# Machine maintenance: it delivers no branch -- what it finds, the fix pass delivers.
GROUP = "maintenance"

EVERY_MINUTES = 15

# The runner's `DEFAULT_PASS_MINUTES`, then the one run it may have just started
# (`DEFAULT_TIMEOUT_MINUTES`) and that run's tree and toolchain; a test holds them together.
TIME_LIMIT = "PT190M"

# The wrapper's title and the artifact it therefore writes (`log_wrap.slug`); a test
# asserts they agree.
LABEL = "Scheduled: Medium Tier"
ARTIFACT = "logs/scheduled-medium-tier.log"

RUN_ARGS = ("run",)

WINDOWS = os.name == "nt"

windowless = devkit_schtasks.windowless


def runner_script(root: Path = REPO_ROOT) -> Path:
    return root / "scripts" / "tier-pass.py"


def pass_arguments(python: str, root: Path = REPO_ROOT) -> str:
    """The task's `<Arguments>`: the runner under `log-wrap.py --always`."""
    inner = " ".join([f'"{runner_script(root)}"', *RUN_ARGS])
    return devkit_schtasks.logged(LABEL, python, inner, root)


def task_document(python: str, arguments: str, minutes: int, root: Path = REPO_ROOT) -> str:
    return devkit_schtasks.task_xml(
        python,
        arguments,
        devkit_schtasks.repeating_trigger(minutes),
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
    parser.add_argument("--minutes", type=int, default=EVERY_MINUTES, help="minutes between fires")
    parser.add_argument("--yes", dest="apply", action="store_true", help="actually call schtasks")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if not WINDOWS:
        print("install-tier-schedule: Windows-only; nothing to do here.")
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
            f"install-tier-schedule: {REPO_ROOT} is a temporary checkout, which is deleted "
            "when its work lands. Run this from the static devkit checkout.",
            file=sys.stderr,
        )
        return 2

    python = windowless(sys.executable)
    document = task_document(python, pass_arguments(python), args.minutes)
    if args.check:
        code, message = devkit_schtasks.run_check(args.name, document, _run_argv)
        print(message, file=sys.stderr if code else sys.stdout)
        return code
    if not args.apply:
        print(
            f'Would run: "{python}" {pass_arguments(python)}\n\n'
            f"  every {args.minutes} minutes, whatever the person is doing, "
            "where devkit.fixPass is dispatch\n"
            f"  in        {REPO_ROOT}\n"
            f"  records   {ARTIFACT}, and each project's result under logs/medium-tier/\n"
            f"  limit     {TIME_LIMIT}\n\n"
            f"Dry run -- re-run with --yes."
        )
        return 0
    ok, out = devkit_schtasks.register(args.name, document, _run_argv)
    print(out or f"installed {args.name} (every {args.minutes} minutes)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
