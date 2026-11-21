#!/usr/bin/env python3
"""A database checkout's dev database brought to its merged head, so nobody has to remember.

Autogenerate is a diff against the live database (`db-revision.py` in each project), so a
database left behind its migrations makes the next revision re-propose what is already
written -- and every session that hit that told the operator to run `alembic upgrade
head` by hand, which is the step that kept being forgotten.

**The upgrade itself is the project's.** carameli runs Alembic inside its app container
to bypass PgBouncer for DDL; ibkr_trader runs it on the host against Postgres on 5433.
Neither is a case of the other -- the split `db-revision` already makes -- so what is
shared is the CLI of `scripts/db-upgrade.py`, in every checkout `devkit_project.DB_PROJECTS`
names, with `--check`'s exit codes the shape `installers.py` already reads:

- `--check`: 0 at head, 1 behind, 2 cannot tell (the database is not up). Writes nothing.
- bare: apply every pending revision; 0 applied, anything else failed. The script finds
  its own interpreter and environment: it is spawned with the pass's console Python.

**Only merged migrations, in a checkout nobody is working in.** The static checkout is on
whatever branch it was left on, and a revision applied from a task branch stamps the
database with an id that a rewrite or an abandoned PR removes from history -- after which
every Alembic command there fails with "Can't locate revision". So `ready` lets the
upgrade run only on the default branch, with nothing uncommitted (an unsaved file under
`versions/` would be applied too), and with HEAD already on `origin/<default>`. Anything
else is silence, not a finding: the database waits for the checkout to come home, and
`sweep.py`'s reconcile is what reports a checkout that has not.

A database that is not up is silence as well. There is nothing to migrate until it is,
and the next half-hourly pass after it starts does it.

Tested in `tests/test_fix_migrate.py`.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devkit_project
import fix_cycle
import sweep
import task_branch as tb

SCRIPT = "scripts/db-upgrade.py"
# `--check`'s answers.
CURRENT, BEHIND, UNKNOWN = 0, 1, 2
# A `docker compose exec` into a cold container is the slow case of the check.
CHECK_TIMEOUT = 120
# An upgrade ended here is rolled back by Postgres -- Alembic runs each migration in a
# transaction there -- so the bound costs a retry next pass, never a half-applied schema.
UPGRADE_TIMEOUT = 300

Runner = Callable[..., subprocess.CompletedProcess]


def _out(done: object) -> str:
    return str(getattr(done, "stdout", "") or "") if getattr(done, "returncode", 1) == 0 else ""


def ready(checkout: Path) -> str:
    """Why the checkout's migrations may not be applied; "" when they may."""
    git = sweep.git_for(checkout)
    base = tb.detect_default_branch(git, fallback="main")
    if _out(git("symbolic-ref", "--short", "HEAD")).strip() != base:
        return f"not on {base}"
    if _out(git("status", "--porcelain")).strip():
        return "uncommitted changes"
    if git("merge-base", "--is-ancestor", "HEAD", f"origin/{base}").returncode != 0:
        return f"HEAD is not on origin/{base}"
    return ""


def last_said(done: subprocess.CompletedProcess) -> str:
    """The last non-empty line the script printed, stderr first, or its exit code."""
    for stream in (done.stderr, done.stdout):
        lines = [line.strip() for line in str(stream or "").splitlines() if line.strip()]
        if lines:
            return lines[-1]
    return f"exit {done.returncode}"


def call(
    checkout: Path, args: list[str], timeout: int, runner: Runner = sweep.run_windowless
) -> subprocess.CompletedProcess:
    """`scripts/db-upgrade.py <args>` in `checkout`; a run that could not start or ran out
    of time comes back as a failed one, saying which."""
    try:
        return runner(
            [sweep.console_python(), SCRIPT, *args],
            cwd=checkout,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, -1, "", f"ran past {timeout}s")
    except OSError as exc:
        return subprocess.CompletedProcess(args, -1, "", f"could not start: {exc}")


def tend_one(project: str, checkout: Path, mode: str, runner: Runner = sweep.run_windowless) -> str:
    """The record line for one database checkout, "" when there is nothing to say."""
    if not (checkout / SCRIPT).is_file():
        return f"{project} -- not migrated: it has no {SCRIPT}"
    if ready(checkout):
        return ""
    checked = call(checkout, ["--check"], CHECK_TIMEOUT, runner)
    if checked.returncode in (CURRENT, UNKNOWN):
        return ""
    if checked.returncode != BEHIND:
        return f"{project} -- FAILED to check its database: {last_said(checked)}"
    if mode != fix_cycle.DISPATCH:
        return f"{project} -- would upgrade its database to head"
    done = call(checkout, [], UPGRADE_TIMEOUT, runner)
    if done.returncode != 0:
        return f"{project} -- FAILED to upgrade its database: {last_said(done)}"
    return f"{project} -- database upgraded to head"


def tend(
    workspace: Path, projects: list[str], mode: str, runner: Runner = sweep.run_windowless
) -> list[str]:
    """Every registered database checkout on this machine, a line each that has one."""
    lines = []
    for project in projects:
        checkout = workspace.parent / project
        if project not in devkit_project.DB_PROJECTS or not (checkout / ".git").exists():
            continue
        if line := tend_one(project, checkout, mode, runner):
            lines.append(line)
    return lines
