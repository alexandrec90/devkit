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
and a run that names nothing is signed by its exit code alone. A run that executed
nothing must not exit 0: that is the script's to enforce, as devkit's own
`scripts/e2e.py` does.

**Every project owes the script, so its absence is red too** (`MISSING`). A missing tier
used to be a skip by design, which is the state the tier exists to end: every project
has tests only this machine can run, and a skip reads as green. So a project whose
`origin/<default>` carries no `scripts/e2e.py` is recorded red with the one signature
`MISSING`, and the fix pass sends its fixer to write the tier (`fix_prompts.local_prompt`).
One fixer per project however many share the line: neither `fix_plan.plan` nor
`fix_cycle.classify` may read it as one vendored cause.

**A second tier reads the same way** (`MEDIUM`, `scripts/tier-pass.py`): the project's
own tests, on this Windows machine, through the day. It keeps its own folder beside
`logs/e2e/`, since either tier's run would otherwise replace the other's result, and
everything here takes the `Tier` it is reading. The two differ in one thing, which is
`carried`: this tier tests only the tip, every few minutes on a busy repo, so its run
history is what says where a failure began and that a tip has had its second run.

Stdlib plus `fix_plan`. Tested in `tests/test_e2e_results.py`.
"""

from __future__ import annotations

import datetime as _dt
import json
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, fields, replace
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

# The whole signature of a project with no tier: one stable line, so the ledger dedupes
# it per commit like any red, and the prompt can tell "write the tier" from "fix it".
MISSING = f"{CONTRACT} is missing: no local end-to-end tier"


@dataclass(frozen=True)
class Tier:
    """One scheduled local tier: where its results land, and how a red names itself."""

    results_dir: Path
    contract: str
    workflow: str
    missing: str


E2E = Tier(RESULTS_DIR, CONTRACT, WORKFLOW, MISSING)

# `scripts/run-tests.py`, the entry point every project already has for its own suite.
MEDIUM_CONTRACT = "scripts/run-tests.py"
MEDIUM = Tier(
    Path("logs") / "medium-tier",
    MEDIUM_CONTRACT,
    fix_plan.MEDIUM_TIER,
    f"{MEDIUM_CONTRACT} is missing: no medium test tier",
)

PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"
STATUSES = frozenset({PASSED, FAILED, SKIPPED})

# How long a red at an unmoved tip waits for its one second run (`retest_due`): long
# enough for whatever a neighbour was doing to the machine to have stopped.
RETEST_AFTER = _dt.timedelta(hours=1)


@dataclass(frozen=True)
class Result:
    """One project's run. `sha` is the commit the run tested; `base` the default branch.

    `reason` says why a run was skipped (or why a red run names no test); `signature` is
    what the fix pass groups and dedupes by. `last_green` and `first_red` are `carried`
    from the run before, and stay empty for a tier that does not ask.
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
    last_green: str = ""
    first_red: str = ""
    retested: bool = False


# A checkout's own virtualenv interpreter, both spellings. `project_python.VENV_SUBPATHS`
# is the owner and a test pins the two together; it is not imported because that module
# spawns outside any `main()` without `NO_WINDOW` (`has_module`, `re_exec`), and nothing a
# scheduled job imports may (`tests/test_scheduled_jobs.py`).
VENV_INTERPRETERS = (Path(".venv") / "Scripts" / "python.exe", Path(".venv") / "bin" / "python")


def venv_interpreter(root: Path, fallback: str) -> str:
    """`root`'s own `.venv` interpreter, else `fallback`: what a contract script runs under."""
    found = next((root / rel for rel in VENV_INTERPRETERS if (root / rel).is_file()), None)
    return str(found) if found is not None else fallback


def is_missing(signature: Iterable[str]) -> bool:
    """True when `signature` is a missing tier's rather than a red run's, in either tier."""
    return tuple(signature) in ((MISSING,), (MEDIUM.missing,))


def result_dir(project: str, root: Path = REPO_ROOT, tier: Tier = E2E) -> Path:
    return root / tier.results_dir / project


def signature_of(output: str, exit_code: int, project: str, tier: Tier = E2E) -> tuple[str, ...]:
    """The failing ids the run printed, or its exit code when it named none.

    Never empty for a red run: an empty signature would match every other artifact-less
    failure, and `fix_plan.names_nothing` would hold it as unread forever. And never the
    same in two projects: `fix_cycle.shared_signatures` reads one line in two places as
    one cause, so a run in each that named nothing folded into a devkit session that
    could fix neither. So the line names the project.
    """
    found = fix_plan.signature_from_logs([output])
    return found or (f"{project}: {tier.contract} exited {exit_code} naming no failing test",)


def ok(done: object) -> bool:
    """Whether a spawn ran and exited 0: the shape every tier's git call is read through."""
    return done is not None and getattr(done, "returncode", 1) == 0


def out(done: object) -> str:
    return str(getattr(done, "stdout", "") or "").strip()


def said(done: object) -> str:
    """A failed spawn's last line, for a reason written to a result."""
    text = f"{getattr(done, 'stderr', '') or ''}\n{getattr(done, 'stdout', '') or ''}"
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else "no output"


def verdict(result: Result, code: int, output: str, tier: Tier = E2E, reason: str = "") -> Result:
    """`result`, a finished run, as what its exit code and output say. `reason` is for a
    red its exit code alone explains, a timeout."""
    green = code == 0
    return replace(
        result,
        status=PASSED if green else FAILED,
        exit_code=code,
        reason="" if green else reason,
        signature=() if green else signature_of(output, code, result.project, tier),
    )


def missing_result(project: str, base: str, tip: str, stamp: str, tier: Tier = E2E) -> Result:
    """A project with no tier: red at origin's tip, so the fix pass sends its fixer to
    write one. With no tip there is nothing a fixer could branch from, so that is a skip
    naming why."""
    return Result(
        project=project,
        status=FAILED if tip else SKIPPED,
        sha=tip,
        base=base,
        started=stamp,
        finished=stamp,
        reason=f"origin/{base} has no {tier.contract}" if tip else f"no origin/{base}",
        signature=(tier.missing,) if tip else (),
    )


def carried(result: Result, previous: Result | None) -> Result:
    """`result` with what the run before it says about it, for a tier testing the tip.

    `first_red` is where a failure that came back was first seen: the bad end of the range
    the fixer bisects, and what keeps the ledger's key still while the tip moves.
    `last_green` is the good end. An unrelated failure starts a new `first_red`, since
    the first one's commit says nothing about it. `retested` marks the one second run a
    red gets at a tip that has not moved (`retest_due`).
    """
    if previous is None or result.status != FAILED:
        return result
    green = previous.sha if previous.status == PASSED else previous.last_green
    again = previous.status == FAILED and previous.sha == result.sha
    if previous.status != FAILED or not set(previous.signature) & set(result.signature):
        return replace(result, last_green=green, retested=again)
    first = previous.first_red or previous.sha
    return replace(
        result, last_green=green, first_red="" if first == result.sha else first, retested=again
    )


def retest_due(result: Result, now: _dt.datetime) -> bool:
    """Whether a red at a tip that has not moved is owed its one second run.

    With no merge nothing else ever re-tests it: a flake stayed red, and a fixer who found
    nothing could not clear it. Once, because a red that held twice is not a flake, and
    every 15 minutes it would be a suite's worth of this machine for nothing new. A missing
    file is no flake. A time that cannot be read is never due.
    """
    if result.status != FAILED or result.retested or is_missing(result.signature):
        return False
    try:
        return now - _dt.datetime.fromisoformat(result.finished) >= RETEST_AFTER
    except ValueError:
        return False


def write(result: Result, root: Path = REPO_ROOT, tier: Tier = E2E) -> Path:
    """The result file, replacing the last run's. Returns its path."""
    where = result_dir(result.project, root, tier)
    where.mkdir(parents=True, exist_ok=True)
    path = where / RESULT_FILE
    record = asdict(result)
    record["signature"] = list(result.signature)
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return path


def record(result: Result, output: str, root: Path = REPO_ROOT, tier: Tier = E2E) -> Path:
    """A finished run's output, then its result file, which is what makes it the last run."""
    where = result_dir(result.project, root, tier)
    where.mkdir(parents=True, exist_ok=True)
    (where / OUTPUT_FILE).write_text(output, encoding="utf-8")
    return write(result, root, tier)


def line_for(result: Result) -> str:
    """One result as the line a pass prints and `status` lists."""
    if result.status == PASSED:
        return f"{result.project}: passed at {result.sha[:9]}"
    if result.status == SKIPPED:
        return f"{result.project}: skipped -- {result.reason}"
    shown = ", ".join(result.signature[:3])
    more = f" (+{len(result.signature) - 3} more)" if len(result.signature) > 3 else ""
    return f"{result.project}: FAILED at {result.sha[:9]} -- {shown}{more}"


def read(project: str, root: Path = REPO_ROOT, tier: Tier = E2E) -> Result | None:
    """The project's last result; None when there is none or it cannot be read.

    Unknown keys are dropped and a status outside `STATUSES` reads as no result, so a
    file written by a newer runner never crashes an older pass.
    """
    path = result_dir(project, root, tier) / RESULT_FILE
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


def to_failure(
    result: Result, tip: str, root: Path = REPO_ROOT, tier: Tier = E2E
) -> fix_plan.Failure | None:
    """A red result as the fix pass's `Failure`; None for anything else.

    `tip` is origin/<base> as the pass reads it now, not when the run was made: a run at
    an older commit is held by the plan (`fix_plan._place`) until the next run re-tests
    the tip, because the commits between may already carry the fix -- and a dispatch at
    a stale red, re-sent each `RESEND_AFTER`, is what that hold refuses.

    That holds for a red the tier saw come back too (`Result.first_red`): a merged fix
    moves the tip whatever the failure's history. So `first_seen` carries that commit for
    the ledger's key and the fixer's range, and `sha` and `tip` stay what the hold compares.
    """
    if result.status != FAILED:
        return None
    evidence = result_dir(result.project, root, tier)
    return fix_plan.Failure(
        kind=fix_plan.LOCAL,
        project=result.project,
        number=0,
        title=f"{tier.workflow} red on {result.base or 'the default branch'}",
        url="",
        base=result.base or "main",
        sha=result.sha,
        workflow=tier.workflow,
        signature=result.signature or signature_of("", result.exit_code, result.project, tier),
        evidence=str(evidence) if evidence.is_dir() else "",
        tip=tip,
        green_sha=result.last_green,
        first_seen=result.first_red,
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
    tier: Tier = E2E,
) -> list[fix_plan.Failure]:
    """Every registered project's red local run, read at its base's current tip.

    `root` is the devkit checkout whose `logs/` holds the results -- the one beside the
    workspace file, which is where the scheduled job runs from.
    """
    devkit = root or workspace.parent / "devkit"
    found: list[fix_plan.Failure] = []
    for project in projects:
        result = read(project, devkit, tier)
        if result is None or result.status != FAILED:
            continue
        project_dir = workspace.parent / project
        tip = origin_tip(git_for(project_dir), result.base) if project_dir.is_dir() else ""
        if failure := to_failure(result, tip, devkit, tier):
            found.append(failure)
    return found
