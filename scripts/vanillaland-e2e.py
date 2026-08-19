#!/usr/bin/env python3
"""Start VanillaLand's local E2E runtime -- SQL, the SOAP stand-ins, and IIS Express.

The recipe belongs to that checkout: `.local/carameli-e2e/start.ps1` starts the
persisted `vs-sql` container, the local SOAP stand-ins, and two 64-bit IIS Express
sites (the VoipApi and the classic web UI), then waits for both to answer before it
exits. This is a launcher for it, not a second copy of it -- the ports, the sites and
the health checks stay in the one file that can be edited alongside the code it serves.

**The second workspace task to reach a REFERENCE checkout, and for the same reason as
the first.** `devkit_project.py` resolves through `known_projects`, which subtracts
`NOT_PROJECTS`, because every action it dispatches needs a harness VanillaLand does not
have. Running a start script the checkout itself owns needs PowerShell and nothing
else, so `--checkout` resolves against the **raw** registry the way
`git-merge-default.py` does, and the dispatcher's contract still does not move to allow
it. Both are workspace tasks rather than `ACTIONS` entries for that one reason.

**A missing start script is the expected failure, not a broken task.** `.local/` is
excluded through VanillaLand's `.git/info/exclude`, so the runtime exists on a machine
somebody has seeded and on no other -- a fresh clone has the checkout and none of this.
`missing_script` therefore names the checkout's own README and the seeding script
rather than letting PowerShell report a path that was never going to be there.

Nothing here stops the stack: `start.ps1` detaches every process it starts and is
idempotent -- each site is started only if its port is not already listening -- so the
task is safe to click twice, and stopping is `docker stop vs-sql` plus killing
`iisexpress.exe`, which no task owns.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devkit_project
import sweep

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKSPACE = sweep.default_workspace(REPO_ROOT)

# The checkout that owns the stack, and the path within it. Both are constants rather
# than arguments with defaults: there is exactly one legacy monolith in this workspace,
# and a `--script` flag would be a way to point a one-click task at a file nobody
# reviewed. `--checkout` stays a flag only because the picker feeds it.
DEFAULT_CHECKOUT = "VanillaLand"
E2E_SCRIPT = Path(".local/carameli-e2e/start.ps1")

# PowerShell 7 first, Windows PowerShell as the fallback. The script runs under either
# -- it uses nothing newer than `Get-NetTCPConnection` -- but only one of them is
# guaranteed to be installed, and it is not the same one on every machine.
SHELLS = ("pwsh", "powershell")

Runner = Callable[[list[str], Path], int]


class StartError(ValueError):
    """The stack cannot be started as asked, before anything has been launched."""


# --- pure helpers -----------------------------------------------------------


def every_checkout(workspace_text: str) -> list[str]:
    """Every folder in the registry, reference checkouts included.

    `devkit_project.known_projects` subtracts `NOT_PROJECTS` and `sweep` subtracts its
    own `DEFAULT_EXCLUDE`, both because the actions they dispatch need a harness. This
    task runs a script the checkout ships, so applying either exclusion would remove
    the only checkout it was written for.
    """
    return sweep.parse_workspace(workspace_text, frozenset())


def target_repo(checkout: str, workspace: Path) -> Path:
    """The checkout directory, validated against the raw registry.

    Validated against the registry rather than by `is_dir()`, so a stale picker entry
    fails naming the real checkouts instead of running somewhere that happens to exist.
    """
    try:
        text = workspace.read_text(encoding="utf-8")
    except OSError as exc:
        raise StartError(f"cannot read the workspace registry at {workspace}: {exc}") from exc
    return devkit_project.resolve_project(
        checkout, every_checkout(text), workspace.parent, noun="checkout"
    )


def missing_script(repo: Path, script: Path) -> str:
    """What to say when the checkout has no start script.

    This is the common failure and it has nothing to do with the task: the runtime is
    untracked, so it is present on a seeded machine and absent everywhere else. The
    message names the two tracked things that rebuild it, because the checkout's README
    is where the rest of the procedure lives and repeating it here would be a copy that
    goes stale the first time the stack changes.
    """
    return (
        f"{script} does not exist in {repo}.\n"
        "That directory is untracked (VanillaLand's .git/info/exclude), so a checkout "
        "that has never\nhad the local E2E runtime set up does not carry it. Seed the "
        "machine first:\n"
        "  scripts/seed-vs-sql.ps1 restores the SQL volume from .local/carameli-e2e/"
        "VanillaSoft.bak\n"
        "  .local/carameli-e2e/README.md carries the rest of the procedure\n"
        "Both live in that checkout; nothing in devkit can recreate them."
    )


def script_in(repo: Path) -> Path:
    """The start script inside a resolved checkout."""
    script = repo / E2E_SCRIPT
    if not script.is_file():
        raise StartError(missing_script(repo, E2E_SCRIPT))
    return script


def powershell(which: Callable[[str], str | None] = shutil.which) -> str:
    """The PowerShell to run the script with, preferring 7."""
    for shell in SHELLS:
        found = which(shell)
        if found:
            return found
    raise StartError(
        f"no PowerShell on PATH (looked for {', '.join(SHELLS)}); the stack is IIS "
        "Express and Windows-only."
    )


def launch_command(shell: str, script: Path) -> list[str]:
    """How the start script is invoked.

    `-NoProfile` because a profile is someone's shell configuration and this is a task:
    the script sets `$ErrorActionPreference` itself and a profile that sets it back
    would turn a failed health check into a silent success. `-ExecutionPolicy Bypass`
    because the file is untracked and therefore unsigned -- it is the spelling the
    checkout's own README gives.
    """
    return [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]


# --- running ----------------------------------------------------------------


def run_attached(argv: list[str], cwd: Path) -> int:
    """Run the start script, streaming its output to the task terminal."""
    return subprocess.run(argv, cwd=cwd, check=False).returncode


# --- entrypoint -------------------------------------------------------------


def main(argv: list[str] | None = None, run: Runner = run_attached) -> int:
    parser = argparse.ArgumentParser(
        description="Start VanillaLand's local Carameli E2E runtime (SQL, SOAP stubs, IIS Express)."
    )
    parser.add_argument(
        "--checkout",
        default=DEFAULT_CHECKOUT,
        help=f"a checkout name from the workspace registry (default: {DEFAULT_CHECKOUT})",
    )
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    args = parser.parse_args(argv)

    try:
        repo = target_repo(args.checkout, args.workspace)
        script = script_in(repo)
        command = launch_command(powershell(), script)
    except (StartError, devkit_project.ProjectError) as exc:
        print(f"vanillaland-e2e: {exc}", file=sys.stderr)
        return 2

    print(f"Starting {script} ...", flush=True)
    return run(command, repo)


if __name__ == "__main__":
    raise SystemExit(main())
