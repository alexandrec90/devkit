#!/usr/bin/env python3
"""What each dispatched session is told, as one function per shape of red.

Split out of `fix_plan.py`, which decides *what* gets a session; this is the words.
Every prompt names the failure the way the record does (`fix_plan.name_of`), the
evidence directory, the branch the worktree is on and the finish line -- which is the
ship skill, that is, `logs/ship-intent.md`, never a commit or a push. A fixer fixes
and stops; everything a script can do -- merging the base in, committing, pushing,
opening or updating the PR, reading the gate -- the pass does, because a session's
turn spent on any of it is the expensive way to run a command. What a fixer cannot do
it says in `logs/fix-blocked.md` (`fix_reports.BLOCKED_FILE`), the one channel back.
The conflict prompt names no failure on purpose: the gate cannot have run against an
unmergeable head, and a resolver told "also fix the tests" fixes the wrong thing.

Stdlib only, no `gh`. Tested in `tests/test_fix_prompts.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fix_plan import COMMIT, CONFLICT, EVIDENCE_DIR, LEDGER, Failure, describe, name_of
from fix_reports import BLOCKED_FILE, REFUSED_FILE

# How every prompt ends. The ship skill is the finish line and the blocked file is the
# only other way out; both are files, so the pass reads the outcome without a session.
FINISH = (
    "When it is done, run the targeted tests and the linter, then ship it with the ship "
    "skill and stop: the fix pass commits, pushes, opens or updates the PR and reads "
    "what the gate says. If it cannot be done, write "
    f"{BLOCKED_FILE.as_posix()} saying what is in the way, in a sentence or two, and stop."
)

# What the devkit session is told about the harness-defect ledger, when the backlog is
# among its failures. No quotes or backticks: the sentence crosses a `wt` command line.
LEDGER_STEPS = (
    " The ledger groups are in that directory's harness-triage.log: work them as "
    ".claude/skills/triage-harness/SKILL.md says -- verify each against current code "
    "before believing it, fix what is real, and retire each group with "
    "python scripts/harness_triage.py --resolve-like ID --note WHAT-FIXED-IT once the "
    "fix is in your intent."
)


def _ids(sig: tuple[str, ...]) -> str:
    return ", ".join(entry for entry in sig if entry != CONFLICT) or "see the run"


def _logs(failure: Failure) -> str:
    """Where the evidence is -- and, when none came down, that none did.

    Two sessions were told the logs were under `logs/gate/` and spent turns finding the
    directory absent: the run had aged out, or uploaded nothing. Saying so is cheaper.
    """
    if failure.evidence:
        return f"The gate's own logs are in {EVIDENCE_DIR}/ in this worktree -- read them first."
    where = failure.url or "the run"
    return f"No artifact came down from the run; read it at {where} first."


def pr_prompt(failure: Failure) -> str:
    """One branch, in its own worktree: a conflict to resolve, a refused commit, or a red PR.

    Three shapes, one function, because the worktree and the finish line are the same
    and only the middle differs. The conflict prompt names no failure on purpose: the
    gate cannot have run, and a resolver told "also fix the tests" fixes the wrong thing.
    """
    if CONFLICT in failure.signature:
        return (
            f"PR #{failure.number} in {failure.project} has a merge conflict with "
            f"origin/{failure.base}. This worktree is checked out on its head branch "
            f"{failure.head}. Merge origin/{failure.base} in and resolve the conflicts so "
            "that both sides' intent survives, and leave the merge uncommitted: the fix "
            "pass concludes it with the hooks running, and whatever the gate says after "
            f"that is the next pass's business, not this session's. {FINISH}"
        )
    if failure.kind == COMMIT:
        return (
            f"The commit stage refused the change on {failure.head} in {failure.project}: "
            f"{_ids(failure.signature)}. The pre-commit output is in {EVIDENCE_DIR}/ in this "
            "worktree, which is the worktree the change was made in, and the message it "
            f"was being shipped with is in {REFUSED_FILE.as_posix()} -- reuse it when it "
            f"still fits. Fix what the output reports. {FINISH}"
        )
    return (
        f"PR #{failure.number} in {failure.project} against origin/{failure.base} is stuck: "
        f"{failure.reason}. Failing: {_ids(failure.signature)}. {_logs(failure)} "
        f"This worktree is checked out on the PR head branch {failure.head}, already up "
        "to date with its base. Fix what the gate is failing on, and nothing else about "
        f"the PR. {FINISH}"
    )


def upstream_prompt(failures: tuple[Failure, ...], branch: str) -> str:
    """One devkit session for everything harness-shaped this pass found.

    A vendored failure several consumers share, devkit's own red default branch, a
    refused commit the toolchain caused: one session, every failure named with what
    its gate said, so the session reads the whole set before deciding what one fix
    covers it.
    """
    ordered = sorted(failures, key=lambda f: (f.project, f.kind, f.number))
    projects = sorted({f.project for f in ordered})
    rows = "; ".join(f"{f.project} {name_of(f)} -- {describe(f)}" for f in ordered)
    urls = ", ".join(f"{f.project} {name_of(f)} {f.url}".rstrip() for f in ordered)
    return (
        f"The harness is red in {len(projects)} checkout(s) ({', '.join(projects)}): "
        f"{rows}. The fix belongs here in devkit, once -- in the vendored file, the "
        "test, or the template that generates the project-owned file it names -- not in "
        f"each consumer. Each failure's gate logs are under {EVIDENCE_DIR}/ in this "
        "worktree, one directory per failure that uploaded any; for the rest, read the "
        "run at its URL."
        + (LEDGER_STEPS if any(f.kind == LEDGER for f in ordered) else "")
        + f" This worktree is on the fresh branch {branch} off the default branch. Say in "
        f"the intent which of these the fix unblocks: {urls}. {FINISH}"
    )


def branch_prompt(failure: Failure, branch: str) -> str:
    """A default branch whose own gate is red: fix on a fresh branch, off that red base."""
    return (
        f"The {failure.workflow} workflow in {failure.project} is red on "
        f"origin/{failure.base} itself, at {failure.sha[:12] or 'its head'} ({failure.url}). "
        f"Failing: {_ids(failure.signature)}. {_logs(failure)} "
        f"This worktree is on the fresh branch {branch} off origin/{failure.base}, "
        "so the failure reproduces here. Fix it; every PR against this base is red until "
        f"the fix lands. {FINISH}"
    )


def nightly_prompt(failure: Failure, branch: str) -> str:
    """A scheduled workflow that failed on the default branch: fix on a fresh branch."""
    return (
        f"The {failure.workflow} workflow in {failure.project} is failing on "
        f"origin/{failure.base}; issue #{failure.number} ({failure.url}) tracks it. "
        f"Failing: {_ids(failure.signature)}. {_logs(failure)} "
        f"This worktree is on the fresh branch {branch} off "
        f"origin/{failure.base}. Fix it; the issue closes itself when the workflow next "
        f"passes. {FINISH}"
    )
