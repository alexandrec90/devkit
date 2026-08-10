#!/usr/bin/env python3
"""Install the recurring `worktree.py reconcile` run as a Windows Scheduled Task.

Reconcile is the half of the ephemeral tier that costs no attention — but only if
something runs it. Nothing in this workspace could: `CronCreate` lives in one agent
session and expires with it, a VS Code task needs a click, and a Stop hook would have
every parallel session racing on `leases.json`, which has no locking. So the runner is
the operating system's, which is the only scheduler here that outlives a session, a
reboot, and a closed editor.

**What the scheduled run is allowed to do, and what it is not.** It runs with `--yes`,
so it genuinely destroys boxes — that is the point, and `reconcile_action` is what
makes it safe: a box holding work is never touched, at any age, under any disk
pressure. It runs with `--no-merge`, so merging a PR stays a human decision. Flip that
with `--merge` here when you would rather it merged anything green.

Every knob it passes is visible in `schtasks /query /tn <name> /xml`, and the task is
removed by `--uninstall`, so nothing about it is hidden state.

Windows-only by nature. On any other platform it says so and exits 0 rather than
failing: this is a workstation convenience, not part of any gate.

The argv builders are pure and tested in `tests/test_install_reconcile_task.py`; the
`schtasks` calls are the thin shell.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sweep

REPO_ROOT = Path(__file__).resolve().parents[1]

TASK_NAME = "devkit-worktree-reconcile"
DEFAULT_INTERVAL_MINUTES = 15


def worktree_script(root: Path = REPO_ROOT) -> Path:
    return root / "scripts" / "worktree.py"


def windowless(python: str) -> str:
    """`pythonw.exe` beside `python.exe`, so the scheduled run opens no console.

    A task running `python.exe` pops a console window into the foreground on every
    fire. At a fifteen-minute interval on a desktop that is not a cosmetic
    complaint — it steals focus while you are typing, and the natural response is to
    delete the task, which silently removes the workspace's only automatic cleanup.

    `pythonw.exe` is the same interpreter with no console allocated. Its stdout goes
    nowhere, which is why `worktree.write_reconcile_log` exists: the pass has to leave
    a record somewhere a person can read it.

    Falls back to the given interpreter when there is no `pythonw` beside it (a
    virtualenv without one, a non-CPython build). A visible window beats no scheduler.
    """
    candidate = os.path.join(os.path.dirname(python), "pythonw.exe")
    return candidate if os.path.isfile(candidate) else python


def reconcile_command(
    python: str,
    script: Path,
    workspace: Path,
    automerge: bool = False,
    min_free_gb: float = 0.0,
) -> str:
    """The command line the scheduled task runs, as one string.

    `schtasks /tr` takes a single string, not argv, so quoting is this function's
    problem. Both paths are absolute and this workstation's contain no quotes; they are
    wrapped in double quotes because `Desktop\\vs_code` sits under a user profile whose
    name can, and on other machines does, contain spaces.

    `--workspace` is passed explicitly rather than left to the default. A scheduled
    task starts in `system32`, and `worktree.py` resolves its default workspace
    relative to its own location — which works, but silently depends on the checkout
    never moving. Naming it makes a moved checkout fail loudly instead.
    """
    parts = [
        f'"{python}"',
        f'"{script}"',
        "reconcile",
        "--workspace",
        f'"{workspace}"',
        "--merge" if automerge else "--no-merge",
        "--yes",
    ]
    if min_free_gb > 0:
        parts += ["--min-free-gb", str(min_free_gb)]
    return " ".join(parts)


def install_argv(name: str, command: str, minutes: int) -> list[str]:
    """`schtasks` argv creating (or replacing) the recurring task.

    `/f` so re-running this is an update rather than an error — the natural thing to do
    after changing the interval or turning merging on.

    `/sc minute /mo N` rather than a daily schedule: the tier's whole promise is that a
    merged PR stops costing disk within minutes, and an hourly job would leave a box's
    containers and volumes up for most of an hour after they became garbage.
    """
    return [
        "schtasks",
        "/create",
        "/tn",
        name,
        "/tr",
        command,
        "/sc",
        "minute",
        "/mo",
        str(minutes),
        "/f",
    ]


def uninstall_argv(name: str) -> list[str]:
    return ["schtasks", "/delete", "/tn", name, "/f"]


def query_argv(name: str) -> list[str]:
    return ["schtasks", "/query", "/tn", name]


def _run(argv: list[str]) -> tuple[int, str]:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return done.returncode, (done.stdout or done.stderr or "").strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--install", action="store_true", default=True)
    mode.add_argument("--uninstall", action="store_true")
    mode.add_argument("--status", action="store_true")
    parser.add_argument("--name", default=TASK_NAME)
    parser.add_argument("--minutes", type=int, default=DEFAULT_INTERVAL_MINUTES)
    parser.add_argument(
        "--merge",
        dest="automerge",
        action="store_true",
        help="let the scheduled run squash-merge PRs whose gate is green (off by default)",
    )
    parser.add_argument("--min-free-gb", type=float, default=0.0)
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument("--yes", dest="apply", action="store_true", help="actually call schtasks")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if os.name != "nt":
        print("install-reconcile-task: Windows-only; nothing to do here.")
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

    # `sys.executable` is whichever interpreter installed this, which is the one that
    # will still be there in an hour. A bare `python` would resolve against a PATH the
    # scheduler does not necessarily share.
    # Box-aware (see `sweep.default_workspace`): the installed task carries this path
    # verbatim, so installing from a box would have scheduled a reconcile pass pointed
    # at a registry that does not exist -- every fifteen minutes, silently.
    workspace = (args.workspace or sweep.default_workspace(REPO_ROOT)).resolve()
    command = reconcile_command(
        windowless(sys.executable), worktree_script(), workspace, args.automerge, args.min_free_gb
    )
    target = install_argv(args.name, command, args.minutes)
    if not args.apply:
        print(
            f"Would run: {' '.join(target)}\n\n"
            f"  every    {args.minutes} minutes\n"
            f"  merging  {'ON -- green PRs are squash-merged' if args.automerge else 'off'}\n\n"
            f"Dry run -- re-run with --yes."
        )
        return 0
    code, out = _run(target)
    print(out or f"installed {args.name} (every {args.minutes} minutes)")
    return code


if __name__ == "__main__":
    sys.exit(main())
