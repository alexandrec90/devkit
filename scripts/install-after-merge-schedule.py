#!/usr/bin/env python3
"""Install the after-merge queue's runner (`scripts/after-merge-pass.py`) as a Scheduled Task.

Every project's `.devkit-queue/` is read every five minutes, on every PC, so what a session
left for later happens without anyone remembering it: the queue is in git, so each PC sees
it, and `job_claims.py` decides which one takes a task. Five minutes because a task is
usually "right after this merges", and a pass with nothing due is one fetch per project.

**Windowless and unelevated, like every job here.** A task needing elevation goes through
the elevated worker's spool, never through this job's token.

Delivery group: it opens sessions and PRs, so `harness-switch.py --off --group jobs` stands
it down with the others. Everything else follows `install-e2e-schedule.py`: `log-wrap.py
--always` around the runner through `devkit_schtasks.logged`, `<WorkingDirectory>` on the
task, dry run unless `--yes`, refused from an ephemeral box.

Windows-only by nature. On any other platform it says so and exits 0.

The builders are pure and tested in `tests/test_install_after_merge_schedule.py`.
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

TASK_NAME = "devkit-after-merge"

GROUP = "delivery"

DEFAULT_INTERVAL_MINUTES = 5

# The wrapper's title and the artifact it therefore writes (`log_wrap.slug`); a test
# asserts they agree. Each task's own account is under `logs/after-merge/`.
LABEL = "Scheduled: After Merge"
ARTIFACT = "logs/scheduled-after-merge.log"

RUN_ARGS = ("run",)

WINDOWS = os.name == "nt"

windowless = devkit_schtasks.windowless


def runner_script(root: Path = REPO_ROOT) -> Path:
    return root / "scripts" / "after-merge-pass.py"


def pass_arguments(python: str, root: Path = REPO_ROOT) -> str:
    """The task's `<Arguments>`: the runner under `log-wrap.py --always`."""
    inner = " ".join([f'"{runner_script(root)}"', *RUN_ARGS])
    return devkit_schtasks.logged(LABEL, python, inner, root)


def task_document(python: str, arguments: str, minutes: int, root: Path = REPO_ROOT) -> str:
    return devkit_schtasks.task_xml(
        python,
        arguments,
        devkit_schtasks.repeating_trigger(minutes),
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
    parser.add_argument("--minutes", type=int, default=DEFAULT_INTERVAL_MINUTES)
    parser.add_argument("--yes", dest="apply", action="store_true", help="actually call schtasks")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if not WINDOWS:
        print("install-after-merge-schedule: Windows-only; nothing to do here.")
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
            f"install-after-merge-schedule: {REPO_ROOT} is a temporary checkout, which is "
            "deleted when its work lands. Run this from the static devkit checkout.",
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
            f"  every {args.minutes} min\n"
            f"  in     {REPO_ROOT}\n"
            f"  records {ARTIFACT}, and each task under logs/after-merge/\n\n"
            f"Dry run -- re-run with --yes."
        )
        return 0
    ok, out = devkit_schtasks.register(args.name, document, _run_argv)
    print(out or f"installed {args.name} (every {args.minutes} min)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
