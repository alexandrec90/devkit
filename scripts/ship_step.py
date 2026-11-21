#!/usr/bin/env python3
"""Shipping a list of intents: the step the fix pass and the ship task share.

`ship_each` is what both do with an intent they have decided is theirs: every hold, the
toolchain, `ship_intent.ship_one` under a deadline, a line for the record. Which intents
are theirs is the one place they differ. `scripts/fix-pass.py` takes the gated ones and
reads back the direct ones (`left_to_ship_task`); `scripts/ship-pass.py` takes the direct
ones, and the gated ones too on a machine whose `devkit.fixPass` is `off`. Neither runs
beside the other: `SHIP_LOCK_NAME`.

Cut out of `fix-pass.py` so the ship task can run it without importing the pass: the task
fires every two minutes and has no use for anything else in there.

Tested through `tests/test_fix_pass.py` and `tests/test_ship_pass.py`, and in
`tests/test_ship_step.py`.
"""

from __future__ import annotations

import datetime as _dt
import sys
import time
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "precommit"))
import fix_cycle
import fix_loop
import fix_plan
import ship_intent
import sweep
import worktree
from _loader import load_by_path

REPO_ROOT = Path(__file__).resolve().parents[1]

# The interpreter resolver the push gate uses: the tree's venv, or the checkout's when
# the tree has none, so `ship.py --fix` runs with the project's own pre-commit.
push_gate = load_by_path("run_push_gate", REPO_ROOT / "scripts" / "precommit" / "run_push_gate.py")

Journal = fix_loop.fix_findings.Journal

# The job that ships what `left_to_ship_task` leaves: `install-ship-schedule.TASK_NAME`.
SHIP_TASK = "devkit-ship"

# Only one shipper on a machine at a time, beside the boxes: the ship task holds it for a
# fire and exits if it cannot have it, and a dispatching pass holds it around its ship
# step. Two shippers of one intent lose a push to each other (babfee68), and a look at
# who is running cannot stop that: one can start the moment after the look.
SHIP_LOCK_NAME = "ship-pass.lock"
# Past the longest holder, a pass's ship step, which ends by the pass's 20-minute send
# deadline. A holder that died is broken after this.
SHIP_LOCK_STALE = 21 * 60.0


def provision_for_ship(tree: Path) -> str:
    """Install the tree's toolchain when neither it nor its checkout has the project's own
    pre-commit: a line for the record, "" when nothing needed doing.

    `ship.py --fix` refuses without one ("provision first"), and that refusal went to a
    devkit session: sports_betting's worktree-harmonic-humming-kay, cut with no `.venv`
    from a checkout with none, was refused on every pass of the 2026-10-02 supervision.
    Provisioning is one command, and anything a script can do, no session does. A
    pre-commit on PATH is not the project's: the fixers it runs are the wrong versions.
    """
    own = ship_intent.ship.pre_commit_command(
        tree, sweep.source_checkout(tree), which=lambda _name: None, find_spec=lambda _name: None
    )
    if own is not None:
        return ""
    steps = worktree.plan_provision(tree, quiet=True)
    if not steps:
        return ""
    ok, notes = worktree.run_provision(tree, steps)
    if not ok:
        return "FAILED to provision: " + "; ".join(n for n in notes if n.startswith("FAILED"))
    return "provisioned its toolchain: " + ", ".join(step.label for step in steps)


def not_shipping(
    intent: ship_intent.Intent,
    busy: frozenset[str],
    now: _dt.datetime,
    mode: str,
    journal: Journal | None = None,
) -> str:
    """The record line for an intent this pass does not ship, or "" for one it does:
    held (`fix_loop.why_held`), unshippable (filed), or any intent of a dry run."""
    where = f"{intent.project} {intent.branch}"
    if held := fix_loop.why_held(busy, intent.tree, now):
        return f"{where} -- held: {held}"
    if intent.blocked:
        fix_loop.fix_findings.file(
            journal,
            "intent-unshippable",
            intent.project,
            f"{where}: {intent.blocked}",
            str(intent.tree),
        )
        return f"{where} -- NOT shipped: {intent.blocked}"
    if mode != fix_cycle.DISPATCH:
        spent = ship_intent.is_spent(intent)
        return f"{where} -- {'would set aside, already shipped' if spent else 'would ship'}: {intent.subject}"
    return ""


def shipped_line(outcome: ship_intent.Outcome, where: str) -> str:
    """One line: a refusal's detail is hook output, and its newlines broke the record
    into rows no reader of it could attribute. A refusal says why on the line
    `refusal_reason` picks -- its tail is the hook's boilerplate -- and the rest is
    evidence; for anything else the tail says why."""
    detail = outcome.detail
    if outcome.stage == ship_intent.REFUSED:
        state = ship_intent.read_state(outcome.intent.tree)
        if output := str(state.get("output", "")):
            detail = f"{state.get('step', 'commit')}: {ship_intent.refusal_reason(output)}"
    return f"{where} -- {outcome.stage}: {' '.join(detail.split())[-240:]}"


def carried_where(intent: ship_intent.Intent, outcome: ship_intent.Outcome, root: Path) -> str:
    """The record's name for a ship carried off a retired branch, re-pointing the
    resolutions that named it.

    The record names what went out, or it reads as a merged branch shipping again. The
    session's resolutions named the retired name too, whose PR predates them, so they
    follow the fix to its new one. A hand-named branch never headed a PR, so every
    resolution naming it does.
    """
    if intent.adopt:
        since = ship_intent.EVER
    else:
        since = ship_intent.retired_at(intent.tree, intent.branch)
    moved = fix_loop.triage.repoint(
        intent.branch, outcome.intent.branch, since, root / fix_cycle.DEVKIT
    )
    return (
        f"{intent.project} {outcome.intent.branch} (carried off {intent.branch}; "
        f"{len(moved)} resolution(s) re-pointed)"
    )


def deadline_of(journal: Journal | None) -> float | None:
    """The `time.monotonic()` the journal's deadline falls at (`Journal.left`), or None
    for a journal with none: what `ship_intent.bounded` ends a ship by."""
    if journal is None or journal.left is None:
        return None
    return time.monotonic() + journal.left()


def ship_each(
    intents: list[ship_intent.Intent],
    root: Path,
    mode: str,
    journal: Journal | None = None,
    budget: float = 0.0,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[list[str], list[fix_plan.Failure], bool]:
    """Ship `intents`: `(lines for the record, refused commits as failures, any ship failed)`.

    A push or a PR that failed is retried next pass and filed now (GitHub's own failure
    only once it lasts: `ship_intent.DEFERRED`); the third element turns the task red
    over a branch that did not go out. One no PR can be opened from is filed for the
    devkit session to move. One whose fixer is still busy in its tree waits, and so does
    one a live session has edited past (`fix_loop.why_held`), and so does every one left
    once the step has spent `budget` seconds: the steps after it are what send anyone
    anywhere. The budget bounds when a ship may start; the journal's deadline
    (`Journal.left`) bounds the one under way, which is ended there and held for the
    next pass.
    """
    lines: list[str] = []
    refused: list[fix_plan.Failure] = []
    failed = False
    started = clock()
    busy = fix_loop.fixers_working() if mode == fix_cycle.DISPATCH else frozenset()
    now = _dt.datetime.now(_dt.UTC)
    for intent in intents:
        where = f"{intent.project} {intent.branch}"
        if line := not_shipping(intent, busy, now, mode, journal):
            lines.append(line)
            continue
        if (shipping := clock() - started) >= budget:
            lines.append(
                f"{where} -- held: this pass spent {int(shipping // 60)} min shipping; "
                f"the next pass ships it"
            )
            continue
        # Flushed as it starts: the record is written only at the end, so a pass stopped
        # mid-ship otherwise leaves the watchdog no line saying where it was.
        print(f"fix-pass: shipping {where}", flush=True)
        base = intent.base or "main"
        # A direct-flow commit runs no framework, so a tree with no toolchain ships as it is.
        if not intent.direct and (provisioned := provision_for_ship(intent.tree)):
            lines.append(f"{where} -- {provisioned}")
        # Read after provisioning, so the commit stage runs with the `.venv` just made.
        python = push_gate.interpreter(intent.tree)
        try:
            with ship_intent.bounded(deadline_of(journal)):
                outcome = ship_intent.ship_one(intent, python, base)
        except ship_intent.OutOfTime as stopped:
            # Ended, not refused: nothing about the change said no, so no fixer is sent.
            cleared = stopped.ended and ship_intent.clear_index_lock(intent.tree)
            lines.append(
                f"{where} -- held: {stopped}{' (its index.lock removed)' if cleared else ''}"
                f"; the next pass ships it"
            )
            continue
        if outcome.intent.branch != intent.branch:
            where = carried_where(intent, outcome, root)
        lines.append(shipped_line(outcome, where))
        if outcome.stage == ship_intent.REFUSED:
            refused.append(ship_intent.refusal_failure(outcome, base))
        if outcome.stage == ship_intent.FAILED:
            failed = True
            fix_loop.fix_findings.file(
                journal,
                "ship-failed",
                intent.project,
                f"{where}: {outcome.detail[:200]}",
                str(intent.tree),
            )
    return lines, refused, failed


def left_to_ship_task(
    intents: list[ship_intent.Intent], mode: str, journal: Journal | None = None
) -> tuple[list[str], list[fix_plan.Failure]]:
    """What the fix pass does with a direct-flow intent, which is not to ship it: the ship
    task has it. `(lines for the record, stored refusals as failures)`.

    The ship task sends no one, so a refusal it recorded is the pass's: read from the
    state file, no git step in the tree, and placed as any refused commit is, which is
    how the fixer goes.
    """
    lines: list[str] = []
    refused: list[fix_plan.Failure] = []
    busy = fix_loop.fixers_working() if intents and mode == fix_cycle.DISPATCH else frozenset()
    now = _dt.datetime.now(_dt.UTC)
    for intent in intents:
        where = f"{intent.project} {intent.branch}"
        if line := not_shipping(intent, busy, now, mode, journal):
            lines.append(line)
        elif outcome := ship_intent.stored_refusal(intent):
            lines.append(shipped_line(outcome, where))
            refused.append(ship_intent.refusal_failure(outcome, intent.base or "main"))
        else:
            lines.append(f"{where} -- the ship task has it ({SHIP_TASK})")
    return lines, refused
