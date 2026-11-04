#!/usr/bin/env python3
"""What the scheduled local end-to-end run found, in the one shape the fix pass reads.

**The gap this closes.** The fix pass sends a project's fixer at what a GitHub gate says
and at nothing else (`fix_red.collect_red`). A test that cannot run on a hosted runner --
it reads the live workspace file, drives VS Code, needs the Docker stack, a browser, a
credential, the static checkout itself -- was therefore skipped in CI, never run by a
session (`session-scope.md`), and skipped again in every fixer's box. devkit carried a
dozen such tests that ran only when a person happened to type `pytest` on `main`, and
every VS Code task regression that unit tests could not see -- an Escape that ran the
task anyway, a chained picker resolved in the wrong order, an extension nobody installed
-- was found by the person clicking it.

`scripts/e2e-pass.py` runs each registered project's `scripts/e2e.py` on this machine,
and this module turns each run into a result file and, when red, into a
`fix_plan.Failure` of kind `LOCAL`: the same shape as a red default branch, so the plan,
the ledger's per-commit dedupe and `ATTEMPTS`, the harness-first ordering and the
vendored-signature fold all apply unchanged.

**Why a result file and not the job's exit code.** A red end-to-end run is a finding
about a project, not a failure of the job: were the job to exit non-zero, `log-wrap.py`
would file a `scheduled-job-failed` row for it, and every ledger row goes to the one
devkit session, holding every project fixer behind it. So the job exits 0 whenever it
reached a verdict, and the verdict travels here.

**The CLI contract a project owes** is `scripts/e2e.py`: exit 0 green, anything else
red, run with the checkout as the cwd. Name each failing test the way pytest's short
summary does (`FAILED tests/x.py::test_y`) -- that is what the signature is read from,
and a run that names nothing is signed by its exit code alone. A project without the
script has no end-to-end tier, which is a skip by design (`engineering.md`). A run that
executed nothing must not exit 0: that is the script's to enforce, as devkit's own
`scripts/e2e.py` does.

Stdlib plus `fix_plan`. Tested in `tests/test_e2e_results.py`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, fields
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fix_plan

REPO_ROOT = Path(__file__).resolve().parents[1]

# Where every project's result lands: devkit's own `logs/`, one directory per project,
# because the fix pass runs from devkit and reads them all from one place.
RESULTS_DIR = Path("logs") / "e2e"
RESULT_FILE = "result.json"
OUTPUT_FILE = "output.log"

# The project-owned entry point, at the same path in every checkout.
CONTRACT = "scripts/e2e.py"

# How a `LOCAL` failure names its source in the plan and the prompt, where a gate's
# failure names its workflow.
WORKFLOW = "Local E2E"

PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"
STATUSES = frozenset({PASSED, FAILED, SKIPPED})


@dataclass(frozen=True)
class Result:
    """One project's run. `sha` is the commit the run tested; `base` the default branch.

    `reason` says why a run was skipped (or why a red run names no test); `signature` is
    what the fix pass groups and dedupes by.
    """

    project: str
    status: str
    sha: str = ""
    base: str = ""
    started: str = ""
    finished: str = ""
    exit_code: int = 0
    reason: str = ""
    signature: tuple[str, ...] = ()


# A checkout's own virtualenv interpreter, both spellings. `project_python.VENV_SUBPATHS`
# is the owner and a test pins the two together; it is not imported because that module
# spawns outside any `main()` without `NO_WINDOW` (`has_module`, `re_exec`), and nothing a
# scheduled job imports may (`tests/test_scheduled_jobs.py`).
VENV_INTERPRETERS = (Path(".venv") / "Scripts" / "python.exe", Path(".venv") / "bin" / "python")


def venv_interpreter(root: Path, fallback: str) -> str:
    """`root`'s own `.venv` interpreter, else `fallback`: what a contract script runs under."""
    found = next((root / rel for rel in VENV_INTERPRETERS if (root / rel).is_file()), None)
    return str(found) if found is not None else fallback


def result_dir(project: str, root: Path = REPO_ROOT) -> Path:
    return root / RESULTS_DIR / project


def signature_of(output: str, exit_code: int) -> tuple[str, ...]:
    """The failing ids the run printed, or its exit code when it named none.

    Never empty for a red run: an empty signature would match every other artifact-less
    failure, and `fix_plan.names_nothing` would hold it as unread forever.
    """
    found = fix_plan.signature_from_logs([output])
    return found or (f"{CONTRACT} exited {exit_code} naming no failing test",)


def write(result: Result, root: Path = REPO_ROOT) -> Path:
    """The result file, replacing the last run's. Returns its path."""
    where = result_dir(result.project, root)
    where.mkdir(parents=True, exist_ok=True)
    path = where / RESULT_FILE
    record = asdict(result)
    record["signature"] = list(result.signature)
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return path


def read(project: str, root: Path = REPO_ROOT) -> Result | None:
    """The project's last result; None when there is none or it cannot be read.

    Unknown keys are dropped and a status outside `STATUSES` reads as no result, so a
    file written by a newer runner never crashes an older pass.
    """
    path = result_dir(project, root) / RESULT_FILE
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("status") not in STATUSES:
        return None
    known = {f.name for f in fields(Result)}
    kept = {key: value for key, value in record.items() if key in known}
    kept["signature"] = tuple(str(entry) for entry in kept.get("signature") or ())
    try:
        kept["exit_code"] = int(kept.get("exit_code", 0) or 0)
        return Result(**kept)
    except (TypeError, ValueError):
        return None


def to_failure(result: Result, tip: str, root: Path = REPO_ROOT) -> fix_plan.Failure | None:
    """A red result as the fix pass's `Failure`; None for anything else.

    `tip` is origin/<base> as the pass reads it now, not when the run was made: a run at
    an older commit is held by the plan (`fix_plan._place`) until the next run re-tests
    the tip, because the commits between may already carry the fix -- and a dispatch at
    a stale red, re-sent each `RESEND_AFTER`, is what that hold refuses.
    """
    if result.status != FAILED:
        return None
    evidence = result_dir(result.project, root)
    return fix_plan.Failure(
        kind=fix_plan.LOCAL,
        project=result.project,
        number=0,
        title=f"{WORKFLOW} red on {result.base or 'the default branch'}",
        url="",
        base=result.base or "main",
        sha=result.sha,
        workflow=WORKFLOW,
        signature=result.signature or signature_of("", result.exit_code),
        evidence=str(evidence) if evidence.is_dir() else "",
        tip=tip,
    )


Git = Callable[..., "subprocess.CompletedProcess[str]"]


def origin_tip(git: Git, base: str) -> str:
    """origin/<base>'s sha, or "" when git cannot say."""
    done = git("rev-parse", "--verify", "--quiet", f"origin/{base}")
    return (done.stdout or "").strip() if done is not None and done.returncode == 0 else ""


def collect(
    workspace: Path,
    projects: Iterable[str],
    git_for: Callable[[Path], Git],
    root: Path | None = None,
) -> list[fix_plan.Failure]:
    """Every registered project's red local run, read at its base's current tip.

    `root` is the devkit checkout whose `logs/e2e/` holds the results -- the one beside
    the workspace file, which is where the scheduled job runs from.
    """
    devkit = root or workspace.parent / "devkit"
    found: list[fix_plan.Failure] = []
    for project in projects:
        result = read(project, devkit)
        if result is None or result.status != FAILED:
            continue
        project_dir = workspace.parent / project
        tip = origin_tip(git_for(project_dir), result.base) if project_dir.is_dir() else ""
        if failure := to_failure(result, tip, devkit):
            found.append(failure)
    return found
