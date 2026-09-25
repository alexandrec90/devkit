#!/usr/bin/env python3
"""Which red things get an agent, which get one agent between them, and which get none.

`fix-prs.py` used to ask the person: tick the checkouts, tick the PRs, pick the agent.
The ticking was the expensive part, and not in clicks. Eight consumers went red on the
same v0.11.21 adoption for one cause -- a vendored test that started demanding coverage
of a project-owned file -- and each ticked row was a fresh session rediscovering that
cause from nothing. The dropdown could not say "these six are one defect", so nobody did.

This module is the decision the dropdown could not make, as pure functions over the
shapes `gh` returns, so `tests/test_fix_plan.py` drives every branch without a network:

- **A failure signature** is what the gate actually said: the pytest ids in its
  `FAILED` lines, or the lint findings, or -- when no artifact came down -- the failed
  job and step names. A merge conflict is a signature of its own. Two PRs with the same
  signature are, until proven otherwise, one problem.
- **One signature of vendored tests across two or more projects goes to devkit,
  once.** `scripts/hooks/tests/` is byte-identical in every consumer, so a test there
  failing in several of them is a devkit defect by construction, and the fix -- the
  test, the vendored file, or the template behind the project-owned file it names --
  belongs upstream. The same fix made eight times in eight consumers is the cost this
  file exists to refuse.
- **A PR behind its base is updated, not fixed.** Its gate ran against a base that
  has moved, so what it says may already be fixed on the base; the pass updates the
  branch and reads the new run next time. No session is spent on it.
- **Three shapes are never dispatched.** A release PR is red by construction
  (`RELEASING.md`: `test_fallback_devkit_ref_tracks_the_newest_tag` fails until the
  tag exists, and `release-pipeline.py` judges exactly that red), so an agent sent at
  it can only fail or force -- and so is the default branch at the release commit,
  until the tag points at it. An adoption PR for a tag that is no longer the newest is
  superseded -- `upgrade-project.py` closes it on its next pass -- so fixing it would
  land a vendored copy the next sweep immediately replaces.
- **What the session is told** is `fix_prompts.py`, one function per shape.
- **A PR against a red default branch is held**, out loud, for the base's fixer: it
  inherits the base's failure, and one pass sent a base and two of its PRs three
  sessions in one second for one cause.
- **The ledger** (`fix_ledger.py`) is what makes a second click safe: every dispatch is
  recorded against the commit it was observed on, so clicking again sends nothing at a
  failure an agent is already on.

Stdlib only, and no `gh`: the evidence arrives as arguments. `scripts/gate_evidence.py`
is the half that asks GitHub.
"""

from __future__ import annotations

import functools
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "precommit"))
import task_branch as tb
from _loader import load_by_path

# The four sources of red this plans for. A `COMMIT` is a session's intent the fix pass
# could not commit: the commit stage refused it, and the branch is the worktree it sits in.
# A `BRANCH` is a default branch whose own gate is red: a push landed red, so nothing
# rebased onto it can be green and every PR against it inherits the failure.
PR = "pr"
NIGHTLY = "nightly"
COMMIT = "commit"
BRANCH = "branch"
# The harness-defect ledger's open backlog (`harness_triage.py`), as one failure for the
# devkit session: every entry on it is a devkit defect, whichever project filed it.
LEDGER = "ledger"

# The one test a release commit fails by construction, and the reason a red default
# branch is not always red: `release-pipeline.py` bumps `FALLBACK_DEVKIT_REF` to a tag
# that does not exist until the release workflow cuts it on the same commit. That
# workflow spells the same name as `EXPECTED_RED_TEST`; a test pins the two together.
RELEASE_TEST = "test_fallback_devkit_ref_tracks_the_newest_tag"

# What a decision does with its failures.
DISPATCH = "dispatch"  # one agent, in a worktree on the failure's own branch
RESOLVE = "resolve"  # the same worktree, a conflict-only prompt, and nothing about the gate
UPSTREAM = "upstream"  # one agent in devkit, for a signature shared across consumers
UPDATE = "update"  # no agent: the PR is behind its base, so update it and let the gate re-run
SKIP = "skip"  # nothing, and the note says why
HOLD = "hold"  # nothing this pass: its base is red, and the base's fixer goes first

# A default branch's gate verdict beside True and False: a run at the tip that has not
# finished. Not a hold reason -- the verdict before it stands -- and not "unreadable".
RUNNING = "running"

# Where the vendored test tier lives in every consumer. A failing id under it is a
# devkit defect by construction, because the file is byte-identical everywhere.
VENDORED_TESTS = "scripts/hooks/tests/"

# `release.py`'s branch namespace. The PR gate on one of these is red by design.
RELEASE_PREFIX = "release/"

# The signature a conflicted PR carries, beside whatever its gate said.
CONFLICT = "merge conflict"

# Where the evidence lands inside the worktree the agent opens in. Under `logs/`
# because every project ignores that directory, so nothing here can be committed.
EVIDENCE_DIR = "logs/gate"

# pytest's short-summary line, `FAILED tests/x.py::test_y - AssertionError: ...`. The
# id alone is the signature: the message carries a line number or a value that changes
# between two runs of the same failure.
FAILED_LINE = re.compile(r"^FAILED (\S+)")
# ruff (`path:1:2: E501 ...`) and mypy (`path:1: error: ...`) both start with the file
# and a line; the file is the stable part.
LINT_LINE = re.compile(r"^(\S+?\.\w+):\d+(?::\d+)?: (?:error|[A-Z]{1,4}\d{3,4})\b")


@dataclass(frozen=True)
class Failure:
    """One red thing, with everything the plan and the prompt need to know about it."""

    kind: str  # PR, NIGHTLY, COMMIT or BRANCH
    project: str  # the checkout name in the workspace registry
    number: int  # PR number, or the tracker issue's number for a nightly; 0 otherwise
    title: str
    url: str
    head: str = ""  # PR head branch; empty for a nightly, which has no branch yet
    base: str = ""  # the branch a fix merges into
    sha: str = ""  # PR head sha the evidence was read at
    run_id: str = ""  # the workflow run the evidence came from
    workflow: str = ""  # nightly only: the workflow's name, from the issue title
    reason: str = ""  # `broken_pr_menu.broken_reason`, for a PR
    signature: tuple[str, ...] = ()
    evidence: str = ""  # the directory the run's artifacts were downloaded to
    behind: bool = False  # PR only: its head lacks the base's tip, so its gate is stale
    # PR only: the runs behind its failing checks, off the rollup, for when the gate
    # workflow's own run list has nothing at this sha -- a required check from another
    # workflow, a consumer whose gate is named differently.
    check_runs: tuple[str, ...] = ()
    # COMMIT only: the worktree the refused intent sits in, which is where the fixer opens.
    tree: str = ""


@dataclass(frozen=True)
class Decision:
    action: str
    note: str
    failures: tuple[Failure, ...]


# --- the signature ------------------------------------------------------------------


def signature_from_logs(texts: Iterable[str]) -> tuple[str, ...]:
    """The failing test ids and lint findings across every artifact, sorted, deduped."""
    found: set[str] = set()
    for text in texts:
        for line in str(text).splitlines():
            if failed := FAILED_LINE.match(line):
                found.add(failed.group(1))
            elif lint := LINT_LINE.match(line):
                found.add(f"lint {lint.group(1)}")
    return tuple(sorted(found))


def signature_from_jobs(jobs: Iterable[dict]) -> tuple[str, ...]:
    """`job / step` for every failed step, when no artifact says anything finer.

    A job that failed with no failed step named -- a cancelled one, a runner that
    never started -- contributes its own name, so a run that failed outside any step
    still has a signature rather than an empty one that would match every other
    artifact-less failure.
    """
    found: set[str] = set()
    for job in jobs:
        if not isinstance(job, dict) or str(job.get("conclusion", "")).lower() != "failure":
            continue
        name = str(job.get("name", "?"))
        steps = [
            s
            for s in job.get("steps", []) or []
            if isinstance(s, dict) and str(s.get("conclusion", "")).lower() == "failure"
        ]
        if steps:
            found.update(f"{name} / {s.get('name', '?')}" for s in steps)
        else:
            found.add(name)
    return tuple(sorted(found))


def signature(conflicted: bool, texts: Iterable[str], jobs: Iterable[dict]) -> tuple[str, ...]:
    """The whole signature: the conflict, then the finest evidence available."""
    parts = [CONFLICT] if conflicted else []
    fine = signature_from_logs(texts)
    parts.extend(fine or signature_from_jobs(jobs))
    return tuple(parts)


@functools.cache
def vendored_paths() -> frozenset[str] | None:
    """`sync-devkit.py`'s MANIFEST beside this file; None when it cannot be read.

    Read from the tool itself, as `new-project.py` does, so there is no second list to
    forget. None falls back to the directory prefix alone -- the answer before this
    existed -- rather than calling every path project-owned.
    """
    try:
        module = load_by_path(
            "_fix_plan_manifest", Path(__file__).resolve().parent / "sync-devkit.py"
        )
        return frozenset(module.MANIFEST) | frozenset(module.gated_source_paths())
    except (OSError, AttributeError, ImportError, SyntaxError):
        return None


def entry_path(entry: str) -> str:
    """The file a signature entry names: `lint a.py` and `a.py::test_b` are both `a.py`."""
    return entry.removeprefix("lint ").split("::", 1)[0]


def in_vendored_tier(entry: str, prefixes: tuple[str, ...] = (VENDORED_TESTS,)) -> bool:
    """The entry names a file under `prefixes` that devkit actually ships.

    A directory prefix is not enough: a consumer keeps its own tests beside the vendored
    ones -- carameli's `scripts/hooks/tests/test_codex_hooks_contract.py` is deliberately
    not in the MANIFEST -- and one of those red on an adoption is the project's to fix on
    its adoption branch, not a devkit session's on a fresh branch that cannot reach it.
    A prefix that is itself a file (`.pre-commit-config.yaml`) is taken as named.
    """
    path = entry_path(entry)
    if not path.startswith(prefixes):
        return False
    known = vendored_paths()
    return known is None or path in known or path in prefixes


def is_vendored(sig: tuple[str, ...]) -> bool:
    """Every id in the signature is a vendored test -- the shape that belongs upstream."""
    return bool(sig) and all(in_vendored_tier(entry) for entry in sig)


def is_release_red(sig: tuple[str, ...]) -> bool:
    """The signature is the newest-tag test and nothing else: a release commit's red."""
    return bool(sig) and all(entry.rsplit("::", 1)[-1] == RELEASE_TEST for entry in sig)


def name_of(failure: Failure) -> str:
    """How a failure is named in a record: `#412`, the branch it sits on, or `origin/main`."""
    if failure.kind == BRANCH:
        return f"origin/{failure.base}"
    if failure.kind == COMMIT:
        return failure.head
    if failure.kind == LEDGER:
        return LEDGER
    return f"#{failure.number}"


# --- the two shapes that never get an agent -------------------------------------------


def is_release(head: str) -> bool:
    return str(head).startswith(RELEASE_PREFIX)


def adoption_tag(head: str, prefixes: Iterable[str]) -> str:
    """The slugified tag an adoption branch carries (`v0-11-21`), or "" for any other.

    `upgrade-project.py` cuts `<prefix>v0-11-21-0917[-2]`; the tag is everything from
    the `v` to the first segment that is not part of a version number.
    """
    for prefix in prefixes:
        if str(head).startswith(prefix):
            rest = str(head)[len(prefix) :]
            found = re.match(r"(v\d+(?:-\d+)*?)(?:-\d{4}(?:-\d+)?)?$", rest)
            return found.group(1) if found else ""
    return ""


def is_adoption(decision: Decision, prefixes: Iterable[str]) -> bool:
    """Any failure under it is an adoption PR."""
    named = tuple(prefixes)
    return any(f.kind == PR and bool(adoption_tag(f.head, named)) for f in decision.failures)


def skip_reason(failure: Failure, latest_tag: str, prefixes: tuple[str, ...]) -> str:
    """Why this failure gets no agent, or "" when it gets one. Three shapes, each said."""
    on_branch = failure.kind in (PR, COMMIT)
    if on_branch and is_release(failure.head):
        return (
            "red by construction: a release PR fails the newest-tag test until the "
            "tag exists, and release-pipeline.py judges that red itself"
        )
    adopts = adoption_tag(failure.head, prefixes) if on_branch else ""
    if adopts and latest_tag and adopts != latest_tag:
        return (
            f"superseded: adopts {adopts} and the newest release is {latest_tag}; "
            "the upgrade sweep closes it on its next pass"
        )
    if failure.kind == BRANCH and is_release_red(failure.signature):
        return (
            "red by construction: the release commit fails the newest-tag test until "
            "its tag exists, and release-pipeline.py judges that itself"
        )
    return ""


# --- the plan -----------------------------------------------------------------------


def plan(
    failures: Iterable[Failure],
    latest_tag: str,
    adoption_prefixes: Iterable[str],
) -> list[Decision]:
    """Every failure placed under exactly one decision, in a stable order.

    `latest_tag` is devkit's newest release as written (`v0.11.21`) or already as a
    branch slug; it is compared to adoption branch names, so it is slugified here.
    Empty means "cannot tell", in which case no adoption PR is called superseded: an
    unknown newest tag must not silently skip every adoption on the machine.
    """
    prefixes = tuple(adoption_prefixes)
    latest = tb.slugify(latest_tag) if latest_tag else ""
    grouped: dict[tuple[str, ...], list[Failure]] = {}
    decisions: list[Decision] = []
    listed = list(failures)
    red_bases = {
        (f.project, f.base)
        for f in listed
        if f.kind == BRANCH and not skip_reason(f, latest, prefixes)
    }
    for failure in sorted(listed, key=lambda f: (f.kind, f.project, f.number)):
        if why := skip_reason(failure, latest, prefixes):
            decisions.append(Decision(SKIP, why, (failure,)))
        elif placed := _place(failure, red_bases):
            decisions.append(placed)
        else:
            grouped.setdefault(failure.signature, []).append(failure)

    for sig, group in grouped.items():
        projects = sorted({f.project for f in group})
        if is_vendored(sig) and len(projects) >= 2:
            decisions.append(
                Decision(
                    UPSTREAM,
                    f"one vendored failure in {len(projects)} projects "
                    f"({', '.join(projects)}); fixing it in devkit once",
                    tuple(group),
                )
            )
            continue
        decisions.extend(Decision(DISPATCH, describe(f), (f,)) for f in group)
    return decisions


def _place(failure: Failure, red_bases: set[tuple[str, str]]) -> Decision | None:
    """The decision a failure gets on its own, before any grouping; None to group it.

    A conflict is decided first: its gate has no merge ref to run against, GitHub
    cannot update the branch, and the resolver is told nothing about the failures. A
    behind PR is a free update, and goes even against a red base: the update is what
    lands the base's fix on the PR once that fix is in (#379 was red on a pip-audit
    finding master had already fixed, and the session sent at it did nothing but merge
    master in). Anything else against a red base is held: every PR against a red base
    inherits its failure, a nightly runs on the same commit, and a resolver is a session
    whose merge the base's fix may move. The base's fixer goes alone.
    """
    against_red = failure.kind in (PR, NIGHTLY) and (failure.project, failure.base) in red_bases
    if CONFLICT in failure.signature and not against_red:
        return Decision(RESOLVE, describe(failure), (failure,))
    if failure.behind and CONFLICT not in failure.signature:
        return Decision(UPDATE, f"{describe(failure)}; behind origin/{failure.base}", (failure,))
    if against_red:
        return Decision(HOLD, held_note(failure), (failure,))
    return None


def held_note(failure: Failure) -> str:
    return (
        f"held: origin/{failure.base} is red in {failure.project}, and its fixer goes "
        "first; re-read once it is green"
    )


def describe(failure: Failure) -> str:
    """The one-line reason a row is red, for the report and the prompt."""
    if failure.kind == LEDGER:
        head = f"{len(failure.signature)} open group(s) on the harness-defect ledger"
    elif failure.kind in (NIGHTLY, BRANCH):
        head = f"{failure.workflow} workflow failing on origin/{failure.base}"
    else:
        head = failure.reason or "red"
    if failure.signature:
        shown = ", ".join(failure.signature[:4])
        more = f" (+{len(failure.signature) - 4} more)" if len(failure.signature) > 4 else ""
        return f"{head}: {shown}{more}"
    return f"{head}: no artifact and no failed step named -- read the run at {failure.url}"
