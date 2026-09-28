"""`scripts/fix_budget.py`: whether a dispatch goes now, and escalation instead of a dead end.

Carried over from `test_fix_cycle.py`'s caps, with the one change that is the point of
the module: nothing here answers "needs a human". Where the old caps stopped, `budget`
files a finding the devkit session takes over, or backs off.
"""

from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fix_budget
import fix_findings
import fix_ledger
import fix_plan

NOW = _dt.datetime(2026, 9, 19, 9, 0, tzinfo=_dt.UTC)


def failure(**fields) -> fix_plan.Failure:
    base: dict[str, Any] = {
        "kind": fix_plan.PR,
        "project": "carameli",
        "number": 412,
        "title": "T",
        "url": "u",
        "head": "agent/x-0919",
        "base": "main",
        "sha": "abc",
        "reason": "1 check failing",
        "signature": ("tests/test_x.py::t",),
    }
    base.update(fields)
    return fix_plan.Failure(**base)


def decision(action, *failures) -> fix_plan.Decision:
    return fix_plan.Decision(action, "n", tuple(failures))


def ledger_after(*sent: fix_plan.Decision, when: _dt.datetime = NOW) -> dict[str, dict]:
    """The ledger `fix-pass.send_all` leaves after dispatching each of `sent` once."""
    return {
        fix_ledger.decision_key(d): {
            "when": when.isoformat(),
            "what": "n",
            "sent": 1,
            "problem": fix_ledger.problem_key(d),
        }
        for d in sent
    }


def go(verdict: fix_budget.Budget) -> bool:
    return verdict.go and verdict.finding is None


# --- the counting ------------------------------------------------------------------------


def test_a_decision_is_blind_when_no_failure_under_it_names_anything():
    assert fix_budget.is_blind(decision(fix_plan.DISPATCH, failure(signature=())))
    assert fix_budget.is_blind(decision(fix_plan.RESOLVE, failure(signature=(fix_plan.CONFLICT,))))
    assert not fix_budget.is_blind(decision(fix_plan.DISPATCH, failure()))
    mixed = decision(fix_plan.UPSTREAM, failure(signature=()), failure(number=2))
    assert not fix_budget.is_blind(mixed), "one failure with evidence is enough to start from"


# --- no daily caps ----------------------------------------------------


# --- the ladder ---------------------------------------------------------------------------


def test_the_same_failure_after_two_fixers_is_escalated_at_any_commit():
    """The retry is for a fix that did not take; a third fixer at the same failure is
    the loop. It used to read "needs a human"; it is now the devkit session's, filed
    with the problem as its key so the answer can be read back."""
    first = decision(fix_plan.DISPATCH, failure(sha="a1"))
    second = decision(fix_plan.DISPATCH, failure(sha="b2"))
    third = decision(fix_plan.DISPATCH, failure(sha="c3"))
    assert go(fix_budget.budget(second, ledger_after(first), NOW))
    verdict = fix_budget.budget(third, ledger_after(first, second), NOW)
    assert not verdict.go and "2 fixer session(s) left it red and unchanged" in verdict.why
    assert verdict.finding is not None
    assert verdict.finding.kind == "fixers-exhausted"
    assert verdict.finding.key == fix_ledger.problem_key(third)
    assert "carameli #412" in verdict.finding.detail and "needs a human" not in verdict.why


def test_an_escalated_problem_waits_while_open_and_starts_over_once_resolved():
    first = decision(fix_plan.DISPATCH, failure(sha="a1"))
    second = decision(fix_plan.DISPATCH, failure(sha="b2"))
    third = decision(fix_plan.DISPATCH, failure(sha="c3"))
    ledger = ledger_after(first, second)
    waiting = fix_budget.budget(third, ledger, NOW, fix_findings.Escalation(True, ""))
    assert not waiting.go and waiting.finding is None, "filed once, then waited on"
    assert waiting.why.startswith("escalated")
    resolved = (NOW + _dt.timedelta(hours=1)).isoformat()
    assert go(fix_budget.budget(third, ledger, NOW, fix_findings.Escalation(False, resolved)))


def test_a_changed_failure_is_progress_and_goes():
    first = decision(
        fix_plan.DISPATCH, failure(sha="a1", signature=("tests/a.py::t", "tests/b.py::t"))
    )
    second = decision(
        fix_plan.DISPATCH, failure(sha="b2", signature=("tests/a.py::t", "tests/b.py::t"))
    )
    narrower = decision(fix_plan.DISPATCH, failure(sha="c3", signature=("tests/b.py::t",)))
    assert go(fix_budget.budget(narrower, ledger_after(first, second), NOW))


def test_legacy_entries_count_against_their_problem():
    """Entries written before `problem` was kept are derived from their key."""
    first = decision(fix_plan.DISPATCH, failure(sha="a1"))
    second = decision(fix_plan.DISPATCH, failure(sha="b2"))
    legacy = {
        fix_ledger.decision_key(d): {"when": NOW.isoformat(), "what": "n"} for d in (first, second)
    }
    assert not fix_budget.budget(decision(fix_plan.DISPATCH, failure(sha="c3")), legacy, NOW).go


def test_a_blind_problem_gets_one_session_and_is_escalated_as_an_evidence_defect():
    """A session sent at "no artifact and no failed step named" is pure discovery. The
    second one is not a retry but a defect in reading the gate, and is filed as that."""
    blind = decision(fix_plan.DISPATCH, failure(sha="a1", signature=()))
    ledger = ledger_after(blind)
    verdict = fix_budget.budget(
        decision(fix_plan.DISPATCH, failure(sha="b2", signature=())), ledger, NOW
    )
    assert not verdict.go and verdict.finding and verdict.finding.kind == "blind-evidence"
    assert go(fix_budget.budget(decision(fix_plan.DISPATCH, failure()), ledger, NOW))


def test_a_blocked_report_is_escalated_rather_than_parked():
    one = decision(fix_plan.DISPATCH, failure())
    ledger = ledger_after(one)
    ledger[fix_ledger.decision_key(one)]["blocked"] = "the fixture needs a database"
    verdict = fix_budget.budget(one, ledger, NOW + _dt.timedelta(days=30))
    assert not verdict.go and verdict.finding and verdict.finding.kind == "fixer-blocked"
    assert "the fixture needs a database" in verdict.finding.detail
    resolved = (NOW + _dt.timedelta(hours=1)).isoformat()
    after = fix_budget.budget(
        one, ledger, NOW + _dt.timedelta(days=30), fix_findings.Escalation(False, resolved)
    )
    assert go(after), "once the devkit session resolved it, the block is history"


def test_the_devkit_session_backs_off_at_the_top_of_the_ladder_and_never_stops():
    """Nothing is above the devkit session to escalate to, so it slows down and climbs
    to the top effort rather than stopping: a backlog nobody fixes is the one outcome
    this loop must not settle into."""
    upstream = decision(fix_plan.UPSTREAM, failure(project="devkit"))
    ledger = ledger_after(upstream)
    ledger[fix_ledger.decision_key(upstream)]["sent"] = fix_ledger.ATTEMPTS
    # The same problem at a new commit: not "already dispatched", so the ladder decides.
    again = decision(fix_plan.UPSTREAM, failure(project="devkit", sha="new"))
    soon = fix_budget.budget(again, ledger, NOW + _dt.timedelta(minutes=30))
    assert not soon.go and soon.why.startswith("backing off") and soon.finding is None
    due = fix_budget.budget(again, ledger, NOW + fix_budget.BACKOFF[0])
    assert due.go and due.effort == fix_budget.TOP_EFFORT
    ledger[fix_ledger.decision_key(upstream)]["sent"] = fix_ledger.ATTEMPTS + 10
    far = fix_budget.budget(again, ledger, NOW + fix_budget.BACKOFF[-1])
    assert far.go, "the longest wait is a cap, not an end"
    assert fix_budget.BACKOFF[-1] <= _dt.timedelta(hours=8), "hours, not days"


# --- conflicts ----------------------------------------------------------------------------


def _conflict(sha: str) -> fix_plan.Decision:
    return decision(
        fix_plan.RESOLVE,
        failure(project="devkit", number=390, sha=sha, signature=(fix_plan.CONFLICT,)),
    )


def test_a_conflict_whose_head_did_not_move_is_escalated_after_its_one_blind_slot():
    ledger = ledger_after(_conflict("143b"))
    verdict = fix_budget.budget(_conflict("143b"), ledger, NOW + fix_ledger.RESEND_AFTER)
    assert not verdict.go and verdict.finding and verdict.finding.kind == "blind-evidence"


def test_no_daily_count_stops_a_problem_that_has_budget_left():
    """The daily fuses are gone: two dead launches once spent a target's four and held
    a 21-item backlog for a day. Fifty sessions today elsewhere do not stop a new one."""
    today = NOW.isoformat()
    busy = {f"pr:carameli:{i}:s:d{i}": {"when": today, "what": "n"} for i in range(50)}
    assert go(fix_budget.budget(decision(fix_plan.DISPATCH, failure(number=412)), busy, NOW))
    assert not hasattr(fix_budget, "PER_DAY") and not hasattr(fix_budget, "PER_TARGET_PER_DAY")


def test_an_update_goes_once_per_head():
    update = decision(fix_plan.UPDATE, failure(number=99, behind=True))
    assert go(fix_budget.budget(update, {}, NOW))
    again = fix_budget.budget(update, ledger_after(update), NOW)
    assert not again.go and again.why.startswith("already dispatched at")


def test_a_rerun_goes_once_per_tip_and_never_counts_against_the_fixers():
    nightly = {"kind": fix_plan.NIGHTLY, "number": 69, "sha": "old", "rerun_file": "n.yml"}
    rerun = decision(fix_plan.RERUN, failure(**nightly, tip="tip1"))
    assert go(fix_budget.budget(rerun, {}, NOW))
    ledger = ledger_after(rerun)
    again = fix_budget.budget(rerun, ledger, NOW)
    assert not again.go and again.why.startswith("already dispatched at")
    assert go(
        fix_budget.budget(decision(fix_plan.RERUN, failure(**nightly, tip="tip2")), ledger, NOW)
    )
    fixer = decision(fix_plan.DISPATCH, failure(**nightly, tip="tip1"))
    assert fix_ledger.attempts(fixer, ledger, "") == 0


def test_a_conflict_back_at_a_new_head_gets_its_resolver_again_however_often():
    """devkit #390: the resolver pushed its merge, main moved within the hour, and the
    new conflict read "needs a human". The head moving is what shows the one before took,
    so each new head gets a resolver -- there is no daily count to run out."""
    shas = [f"{i}a5" for i in range(6)]
    ledger = ledger_after(_conflict(shas[0]))
    for sha in shas[1:]:
        assert go(fix_budget.budget(_conflict(sha), ledger, NOW))
        ledger.update(ledger_after(_conflict(sha)))
