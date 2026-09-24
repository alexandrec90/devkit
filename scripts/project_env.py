"""The new project's Python environment: `uv.lock`, `.venv`, and whether a commit can run.

Split out of `new-project.py`, which calls these between rendering the tree and seeding
its first commit. Every step is best-effort, because each resolves against PyPI and so
needs uv and a network; the one hard stop is `missing_commit_tooling`, asked before
anything is written.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

LOCK_COMMAND = ["uv", "lock"]
SYNC_COMMAND = ["uv", "sync", "--all-extras", "--all-groups"]


def _uv_step(command: list[str], root: Path, dry_run: bool, *, skip: str, fail: str) -> bool:
    """Run one uv command in `root`, printing the generator's status line. True on success."""
    if dry_run:
        print(f"  run     {' '.join(command)}    (in the new project)")
        return False
    if shutil.which("uv") is None:
        print(f"  skip    {skip}")
        return False
    result = subprocess.run(command, cwd=root, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  warn    {' '.join(command[:2])} failed -- {fail}")
        for line in (result.stderr or result.stdout).strip().splitlines()[-3:]:
            print(f"          {line}")
        return False
    return True


def lock_dependencies(root: Path, dry_run: bool) -> None:
    """Generate `uv.lock` so the project starts reproducible.

    When it cannot run, the project is still complete and valid -- every consumer of
    the lock (`uv sync`, the Dockerfile's `uv.lock*` glob, session-start.sh's
    detection) degrades to resolving fresh. So a failure prints what to run and
    continues rather than aborting a scaffold that is otherwise finished.

    Runs before `git_init` so the lock lands in the initial commit.
    """
    skip = (
        "uv lock -- uv is not installed\n"
        "          The project has no uv.lock; run `uv lock` in it to add one."
    )
    if _uv_step(LOCK_COMMAND, root, dry_run, skip=skip, fail="continuing without a lockfile"):
        print("  write   uv.lock")


def provision_environment(root: Path, dry_run: bool) -> None:
    """Build the project's `.venv`, so the initial commit's pre-commit gate can run.

    pre-commit is in the template's dev extra, hence `--all-extras`. A failure is not
    fatal: `pre-commit` may still be on `PATH`, so it warns and lets the commit give
    its own verdict.
    """
    skip = ".venv -- uv is not installed; the first commit uses pre-commit on PATH"
    fail = "the first commit needs pre-commit on PATH instead"
    if _uv_step(SYNC_COMMAND, root, dry_run, skip=skip, fail=fail):
        print("  write   .venv")


def missing_commit_tooling() -> str | None:
    """Why the initial commit cannot succeed on this machine, or None when it can.

    The generated project ships a `.pre-commit-config.yaml`, and devkit's branch policy
    (`core.hooksPath`) runs that gate on the generator's own first commit. The policy
    finds `pre-commit` in the project's `.venv` or on `PATH`, and a fresh project has
    no `.venv` until `provision_environment` builds one with uv. With neither tool on
    `PATH` the run used to write the whole tree and then die at `git commit`, leaving a
    half-generated directory that blocks the re-run.
    """
    if shutil.which("uv") or shutil.which("pre-commit"):
        return None
    return (
        "neither uv nor pre-commit is on PATH. The first commit runs the project's "
        "pre-commit gate, and the generator installs pre-commit into the project's "
        ".venv with uv.\n"
        "  install uv:  winget install astral-sh.uv   (or see docs.astral.sh/uv)\n"
        "  already installed? Restart VS Code or the terminal: a process started before "
        "the install keeps the PATH it had."
    )
