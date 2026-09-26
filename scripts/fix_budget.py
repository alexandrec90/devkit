#!/usr/bin/env python3
"""How much the fix pass may spend on one problem, and what happens when that runs out.

`fix_cycle.py` decides the order of what goes; this decides whether each may go now.
There is no "needs a human" answer anywhere in it. A problem the ledger shows fixers
could not move is escalated -- a finding on the harness-defect ledger, which the devkit
session takes over -- and gets fresh fixers once that finding is resolved. The devkit
session has nothing above it, so its own exhaustion backs off instead of stopping. The
fuses are what stops a pass whose reading has gone wrong, and a fuse that trips files
itself as a defect in the pass.

Tested in `tests/test_fix_budget.py`.
"""

from __future__ import annotations

import datetime as _dt
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fix_findings
import fix_ledger
import fix_plan

DEVKIT = "devkit"

# The fuses. The retry policy is `fix_ledger.ATTEMPTS` -- per problem, spent only by a
# fixer that left the same failure behind -- and these are only what stops a pass
# whose reading has gone wrong: a signature that changes on every run reads as
# progress forever, and nothing else would notice. Set above what a working day
# needs, so a fuse that trips is a defect to read about in the record, not a budget.
PER_TARGET_PER_DAY = 4
PER_DAY = 24


# --- the budget -------------------------------------------------------------------------


def target_of(key: str) -> str:
    """The PR, branch or checkout a ledger key names: the first three fields."""
    parts = key.split(":")
    if parts[0] == fix_plan.UPSTREAM:
        return DEVKIT
    return ":".join(parts[:3])


def sent_today(ledger: dict[str, dict], now: _dt.datetime) -> dict[str, int]:
    """Sessions per target on `now`'s date, from the ledger's own timestamps.

    An `UPDATE` is recorded for idempotence but is one `gh` call and no session, so it
    is not counted: after a release merge, a handful of behind PRs once spent the whole
    day's budget on free branch updates and the real fixers waited for tomorrow.
    """
    counts: dict[str, int] = {}
    day = now.date().isoformat()
    for key, entry in ledger.items():
        if key.endswith(f":{fix_plan.UPDATE}"):
            continue
        if isinstance(entry, dict) and str(entry.get("when", "")).startswith(day):
            target = target_of(key)
            counts[target] = counts.get(target, 0) + 1
    return counts


def is_blind(decision: fix_plan.Decision) -> bool:
    """No failure under it has any evidence: no test id, no lint line, no failed step."""
    return all(
        not [entry for entry in f.signature if entry != fix_plan.CONFLICT]
        for f in decision.failures
    )


@dataclass(frozen=True)
class Budget:
    """What `budget` allows one decision: go, or wait with why -- and what to file."""

    go: bool
    why: str = ""
    finding: fix_findings.Finding | None = None
    # The effort a session goes at when the ladder has climbed; "" leaves the launch be.
    effort: str = ""


# How long the devkit session waits before trying a problem it has already failed
# `ATTEMPTS` times, by how many rounds past that it is. It is the top of the ladder --
# nothing above it to escalate to -- so it never stops, it slows down, and each retry
# goes at `TOP_EFFORT`. A harness change it merges in between is a changed backlog, so a
# new problem with a fresh budget, not a retry.
BACKOFF = (
    _dt.timedelta(days=1),
    _dt.timedelta(days=2),
    _dt.timedelta(days=4),
    _dt.timedelta(days=7),
)
TOP_EFFORT = "max"


def budget(
    decision: fix_plan.Decision,
    ledger: dict[str, dict],
    now: _dt.datetime,
    escalated: fix_findings.Escalation = fix_findings.NOT_ESCALATED,
    per_target: int = PER_TARGET_PER_DAY,
    per_day: int = PER_DAY,
) -> Budget:
    """Whether this dispatch goes now; when it does not, why, and whether to file that.

    There is no "needs a human" answer. An update is free and always goes. A problem
    the ledger has escalated waits for the devkit session that has it (`escalated.open`)
    and, once that is resolved, starts over with fresh fixers (`resolved_at` is the
    `since` every ledger read takes). A fixer's blocked report is escalated the same way.
    A problem `fix_ledger.ATTEMPTS` fixers left unchanged -- one, when there is no
    evidence to tell progress by -- is escalated too, except a conflict whose head has
    moved since (`fix_ledger.moved_on`): a resolver pushes only a merge that resolved,
    so a new conflict at a new commit is the base moving again (devkit #390). The
    devkit session's own exhaustion backs off instead (`BACKOFF`). A fuse that trips is
    a pass reading wrong, and files itself.
    """
    if decision.action == fix_plan.UPDATE:
        # Free, so never rationed -- but made once per head, like everything else.
        when = fix_ledger.already_sent(decision, ledger, now)
        return Budget(False, f"already dispatched at {when}") if when else Budget(True)
    if escalated.open:
        return Budget(False, "escalated: the devkit session has it on the harness ledger")
    since = escalated.resolved_at
    if reason := fix_ledger.blocked_reason(decision, ledger, since):
        return _escalate(decision, "fixer-blocked", f"a fixer reported it blocked: {reason}")
    if when := fix_ledger.already_sent(decision, ledger, now, since):
        return Budget(False, f"already dispatched at {when}")
    made = fix_ledger.attempts(decision, ledger, since)
    limit = fix_ledger.BLIND_ATTEMPTS if is_blind(decision) else fix_ledger.ATTEMPTS
    rebased = decision.action == fix_plan.RESOLVE and fix_ledger.moved_on(decision, ledger)
    if made >= limit and not rebased:
        if decision.action == fix_plan.UPSTREAM:
            return _back_off(decision, ledger, now, made - limit)
        return _exhausted(decision, made)
    return _fuses(decision, ledger, now, per_target, per_day)


def _exhausted(decision: fix_plan.Decision, made: int) -> Budget:
    if is_blind(decision):
        # A blind failure is the evidence reader's defect before it is anyone's: the
        # gate failed and `gate_evidence` could not say where.
        why = f"{made} session(s) sent with no failing id to tell progress by -- the gate "
        return _escalate(decision, "blind-evidence", why + "evidence names nothing")
    why = f"{made} fixer session(s) left it red and unchanged"
    return _escalate(decision, "fixers-exhausted", why)


def _escalate(decision: fix_plan.Decision, kind: str, why: str) -> Budget:
    first = decision.failures[0]
    finding = fix_findings.Finding(
        kind,
        first.project,
        f"{_names(decision)}: {why}; {fix_plan.describe(first)}",
        key=fix_ledger.problem_key(decision),
        evidence=first.url or first.tree,
    )
    return Budget(False, f"escalated to the devkit session: {why}", finding)


def _back_off(
    decision: fix_plan.Decision, ledger: dict[str, dict], now: _dt.datetime, rounds: int
) -> Budget:
    wait = BACKOFF[min(rounds, len(BACKOFF) - 1)]
    last = fix_ledger.last_sent(decision, ledger)
    try:
        due = _dt.datetime.fromisoformat(last) + wait
    except ValueError:
        due = now
    if due.tzinfo is None:
        due = due.replace(tzinfo=_dt.UTC)
    if now < due:
        return Budget(
            False, f"backing off: retried at {TOP_EFFORT} effort after {due:%Y-%m-%d %H:%M}"
        )
    return Budget(True, effort=TOP_EFFORT)


def _fuses(
    decision: fix_plan.Decision,
    ledger: dict[str, dict],
    now: _dt.datetime,
    per_target: int,
    per_day: int,
) -> Budget:
    counts = sent_today(ledger, now)
    target = target_of(fix_ledger.decision_key(decision))
    if sum(counts.values()) >= per_day:
        why = f"the pass has sent {per_day} sessions today"
    elif counts.get(target, 0) >= per_target:
        why = f"{target} has had {per_target} sessions today"
    else:
        return Budget(True)
    # A fuse only trips when the pass is reading something wrong -- a signature that
    # changes every run reads as progress forever. That is a defect in the pass, so it
    # is filed against devkit rather than left for someone to notice in the record.
    finding = fix_findings.Finding("fuse-tripped", DEVKIT, f"{why} (sessions per day is a fuse)")
    return Budget(False, f"fuse: {why}", finding)


def _names(decision: fix_plan.Decision) -> str:
    return ", ".join(f"{f.project} {fix_plan.name_of(f)}" for f in decision.failures)
