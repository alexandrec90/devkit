#!/usr/bin/env python3
"""The fix pass's dispatch half: what goes, under the budget, and how it is sent.

Split out of `fix-pass.py`, which had reached its `file_lines` ceiling on three
changes in a row -- by the engineering rule a defect report, not a raise. The seam is
the one its imports already showed: everything here is `fix_budget`, `fix_ledger` and
`fix-prs.py`, and nothing in the rest of the pass touches those.

Tested through the pass in `tests/test_fix_pass.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "precommit"))
import agent_models
import fix_budget
import fix_findings
import fix_ledger
import fix_loop
import fix_plan
import ship_intent
import sweep
from _loader import load_by_path

REPO_ROOT = Path(__file__).resolve().parents[1]

# The dispatch half of the click task, loaded by path because the file is hyphenated.
# Its runner is the pass's window-less one, so a scheduled pass opens nothing visible.
fix_prs = load_by_path("fix_prs", REPO_ROOT / "scripts" / "fix-prs.py")

Journal = fix_findings.Journal

EXIT_OK = 0
EXIT_FAILED = 1


def update_branch(failure: fix_plan.Failure, root: Path) -> int:
    """An `UPDATE`: merge the base into the PR on GitHub, so its gate re-runs as-is now.

    No session and no worktree. A PR that comes back green is done; one still red at
    the new sha is a new ledger key and gets its session next pass; one GitHub cannot
    update (a conflict) reads `CONFLICTING` next pass and goes to the resolver.
    """
    done = sweep.gh_for(root / failure.project)("pr", "update-branch", str(failure.number))
    if done.returncode != 0:
        why = (done.stderr or done.stdout or "").strip().splitlines()
        print(
            f"  {failure.project} #{failure.number}: update-branch failed: {why[-1] if why else '?'}"
        )
        return EXIT_FAILED
    print(f"  {failure.project} #{failure.number}: branch updated; the gate re-runs")
    return EXIT_OK


def dispatch(
    decision: fix_plan.Decision, root: Path, launch: agent_models.Launch, problem: str = ""
) -> int:
    first = decision.failures[0]
    if decision.action == fix_plan.UPDATE:
        return update_branch(first, root)
    key = fix_ledger.decision_key(decision)
    on_branch = first.kind in (fix_plan.PR, fix_plan.COMMIT)
    if decision.action in (fix_plan.DISPATCH, fix_plan.RESOLVE) and on_branch:
        code = fix_prs.dispatch_pr(first, root, launch, ship_intent.run_quiet, key, problem)
        if code == EXIT_OK and first.kind == fix_plan.COMMIT and first.tree:
            # The refused intent is the fixer's to earn again: with it gone, the tree
            # is a session still working until the fixer ships, and the next pass does
            # not re-run the commit stage over its half-made edits.
            ship_intent.set_aside(Path(first.tree), ship_intent.REFUSED_FILE)
        return code
    return fix_prs.dispatch_fresh(decision, root, launch, ship_intent.run_quiet, key, problem)


def send_all(
    go: list[fix_plan.Decision],
    ctx: fix_loop.Context,
    launch: agent_models.Launch,
    journal: Journal | None = None,
    closed: fix_loop.Closed | None = None,
    items: list | None = None,
) -> tuple[list[str], list[tuple[fix_plan.Decision, str]], int]:
    """Steps 5 and 6: what the phase let through, each under `fix_budget.budget`.

    `(sent lines, waiting decisions with why, worst exit code)`. The ledger is written
    only for a dispatch that opened. `items` is the harness-defect ledger, for what
    became of each problem's escalation; `closed` says which devkit session is still
    working, which holds another, and which tree each problem's fixer worked in, which
    is what an escalation names.
    """
    closed = closed or fix_loop.Closed()
    ledger = fix_ledger.read_ledger(ctx.ledger_path)
    sent: list[str] = []
    capped: list[tuple[fix_plan.Decision, str]] = []
    worst = EXIT_OK
    for decision in go:
        names = ", ".join(f"{f.project} {fix_plan.name_of(f)}" for f in decision.failures)
        if why := _occupied(decision, closed):
            capped.append((decision, why))
            continue
        problem = fix_ledger.problem_key(decision)
        escalated = fix_loop.fix_findings.escalation(problem, items or [])
        verdict = fix_budget.budget(decision, ledger, ctx.now, escalated)
        if verdict.finding and journal is not None:
            journal.add(verdict.finding.at(closed.trees.get(problem, "")))
        if not verdict.go:
            capped.append((decision, verdict.why))
            continue
        line, code = _send_one(decision, ctx, launch, verdict.effort, journal)
        sent.append(f"{names} -- {line}")
        worst = max(worst, code)
        ledger = fix_ledger.read_ledger(ctx.ledger_path)
    return sent, capped, worst


def _occupied(decision: fix_plan.Decision, closed: fix_loop.Closed) -> str:
    """Why a session is already where this one would go, or "": the one devkit session
    at the harness, or any live session in the branch's own tree."""
    if closed.harness_busy and decision.action == fix_plan.UPSTREAM:
        return f"held until the devkit session in {closed.harness_busy} finishes"
    first = decision.failures[0]
    tree = (
        closed.busy.get((first.project, first.head))
        if first.kind in (fix_plan.PR, fix_plan.COMMIT)
        else None
    )
    return f"a session is working in {tree}" if tree else ""


def _send_one(
    decision: fix_plan.Decision,
    ctx: fix_loop.Context,
    launch: agent_models.Launch,
    effort: str,
    journal: Journal | None,
) -> tuple[str, int]:
    """Dispatch one decision the budget let through; `(record line, exit code)`.

    The ledger is written only for a dispatch that opened; one that did not is filed.
    """
    if not ctx.writes:
        would = "would update the branch" if decision.action == fix_plan.UPDATE else None
        return would or f"would send ({decision.action})", EXIT_OK
    how = agent_models.Launch(launch.agent, launch.model, effort) if effort else launch
    problem = fix_ledger.problem_key(decision)
    if dispatch(decision, ctx.root, how, problem) != EXIT_OK:
        first = decision.failures[0]
        names = ", ".join(f"{f.project} {fix_plan.name_of(f)}" for f in decision.failures)
        fix_findings.file(
            journal, f"{decision.action}-failed", first.project, f"{names}: {decision.note[:160]}"
        )
        return f"FAILED to {decision.action}", EXIT_FAILED
    key = fix_ledger.decision_key(decision)
    fix_ledger.record(ctx.ledger_path, key, decision.note, ctx.now, problem=problem)
    return decision.action + (f" at effort {effort}" if effort else ""), EXIT_OK
