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
- **Three shapes are never dispatched.** A release PR is red by construction
  (`RELEASING.md`: `test_fallback_devkit_ref_tracks_the_newest_tag` fails until the
  tag exists, and `release-pipeline.py` judges exactly that red), so an agent sent at
  it can only fail or force -- and so is the default branch at the release commit,
  until the tag points at it. An adoption PR for a tag that is no longer the newest is
  superseded -- `upgrade-project.py` closes it on its next pass -- so fixing it would
  land a vendored copy the next sweep immediately replaces.
- **What the session is told** is `fix_prompts.py`, one function per shape.
- **The ledger** is what makes a second click safe. Every dispatch is recorded under a
  key that names the failure *and the commit it was observed on* (the PR head sha, or
  the run id for a scheduled workflow), so clicking again sends nothing at a failure an
  agent is already on, and a fix that pushed a new sha is a new key when it is still
  red. The user chose click-only over a scheduled dispatch precisely because a session
  costs real money and a loop that spent one silently would be the worst outcome here.

Stdlib only, and no `gh`: the evidence arrives as arguments. `scripts/gate_evidence.py`
is the half that asks GitHub.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

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
SKIP = "skip"  # nothing, and the note says why

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

# The ledger's file name; `fix-prs.py` puts it beside the box tier's lease file.
LEDGER_NAME = "dispatch.json"

# pytest's short-summary line, `FAILED tests/x.py::test_y - AssertionError: ...`. The
# id alone is the signature: the message carries a line number or a value that changes
# between two runs of the same failure.
FAILED_LINE = re.compile(r"^FAILED (\S+)")
# ruff (`path:1:2: E501 ...`) and mypy (`path:1: error: ...`) both start with the file
# and a line; the file is the stable part.
LINT_LINE = re.compile(r"^(\S+?\.\w+):\d+(?::\d+)?: (?:error|[A-Z]{1,4}\d{3,4})\b")

# Ledger keys are the sha256 of the signature, cut to this many hex digits: enough that
# two signatures on one machine cannot collide, short enough to read in a log line.
KEY_DIGEST = 12


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


def is_vendored(sig: tuple[str, ...]) -> bool:
    """Every id in the signature is a vendored test -- the shape that belongs upstream."""
    return bool(sig) and all(entry.startswith(VENDORED_TESTS) for entry in sig)


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

    `latest_tag` is already slugified the way branch names are, and empty means "cannot
    tell", in which case no adoption PR is called superseded: an unknown newest tag must
    not silently skip every adoption on the machine.
    """
    prefixes = tuple(adoption_prefixes)
    grouped: dict[tuple[str, ...], list[Failure]] = {}
    decisions: list[Decision] = []
    for failure in sorted(failures, key=lambda f: (f.kind, f.project, f.number)):
        if why := skip_reason(failure, latest_tag, prefixes):
            decisions.append(Decision(SKIP, why, (failure,)))
            continue
        if CONFLICT in failure.signature:
            # A conflict is resolved before anything else about the PR is knowable: its
            # gate has no merge ref to run against. The resolver is told nothing about
            # the failures; if it is still red afterwards, the next pass sees a plain one.
            decisions.append(Decision(RESOLVE, describe(failure), (failure,)))
            continue
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


# --- the ledger ---------------------------------------------------------------------


def failure_key(failure: Failure) -> str:
    """What one dispatch is remembered as: the failure, at the commit it was seen on."""
    digest = hashlib.sha256("\n".join(failure.signature).encode("utf-8")).hexdigest()
    at = failure.sha or failure.run_id or "?"
    return f"{failure.kind}:{failure.project}:{failure.number}:{at}:{digest[:KEY_DIGEST]}"


def decision_key(decision: Decision) -> str:
    """An upstream decision is one dispatch for the whole group, so one key.

    Any member re-observed at a new sha changes the key: a consumer whose PR was
    re-pushed and is still red under the same signature is a reason to look again.
    """
    keys = sorted(failure_key(f) for f in decision.failures)
    if len(keys) == 1:
        return keys[0]
    digest = hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()
    return f"{UPSTREAM}:{len(keys)}:{digest[:KEY_DIGEST]}"


def read_ledger(path: Path) -> dict[str, dict]:
    """The recorded dispatches. Unreadable is empty: a corrupt ledger must not block."""
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def record(path: Path, key: str, note: str, now: _dt.datetime | None = None) -> None:
    ledger = read_ledger(path)
    when = (now or _dt.datetime.now(_dt.UTC)).isoformat(timespec="seconds")
    ledger[key] = {"when": when, "what": note}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def already_sent(decision: Decision, ledger: dict[str, dict]) -> str:
    """When this exact dispatch was already made, or "" when it is new."""
    entry = ledger.get(decision_key(decision))
    return str(entry.get("when", "?")) if isinstance(entry, dict) else ""


# --- the report ---------------------------------------------------------------------


def render(decisions: Iterable[Decision], ledger: dict[str, dict]) -> str:
    """The plan, for the terminal: what will be sent, what was already, what is skipped."""
    lines = []
    for decision in decisions:
        names = ", ".join(f"{f.project} {name_of(f)}" for f in decision.failures)
        sent = already_sent(decision, ledger)
        if decision.action == SKIP:
            lines.append(f"skip     {names} -- {decision.note}")
        elif sent:
            lines.append(f"sent     {names} -- already dispatched at {sent} (--redo to send again)")
        else:
            lines.append(f"{decision.action:8} {names} -- {decision.note}")
    return "\n".join(lines) or "nothing is red"
