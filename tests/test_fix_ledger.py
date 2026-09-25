"""`scripts/fix_ledger.py`: the keys, the file, and what the ledger says about a decision."""

from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fix_ledger
import fix_plan

NOW = _dt.datetime(2026, 9, 18, 12, 0, tzinfo=_dt.UTC)


def failure(**fields) -> fix_plan.Failure:
    base: dict[str, Any] = {
        "kind": fix_plan.PR,
        "project": "carameli",
        "number": 412,
        "title": "T",
        "url": "u",
        "head": "agent/x-0917",
        "base": "main",
        "sha": "abc123",
        "reason": "1 check failing",
        "signature": ("tests/test_x.py::t",),
    }
    base.update(fields)
    return fix_plan.Failure(**base)


# --- the keys -------------------------------------------------------------------------


def test_the_key_names_the_failure_at_the_commit_it_was_seen_on():
    """A fix that pushed a new sha and is still red is a new key: worth a second look.
    The same sha under the same signature is the same dispatch."""
    same = fix_ledger.failure_key(failure())
    assert same == fix_ledger.failure_key(failure(title="renamed"))
    assert same != fix_ledger.failure_key(failure(sha="def456"))
    assert same != fix_ledger.failure_key(failure(signature=("tests/t.py::other",)))
    assert same.startswith("pr:carameli:412:abc123:")


def test_a_nightly_is_keyed_by_its_run():
    nightly = failure(kind=fix_plan.NIGHTLY, sha="", run_id="4242", number=7)
    assert fix_ledger.failure_key(nightly).startswith("nightly:carameli:7:4242:")


def test_a_key_says_which_kind_of_failure_it_names():
    assert fix_ledger.key_kind(fix_ledger.failure_key(failure())) == fix_plan.PR
    assert fix_ledger.key_kind("ledger:devkit:0:abc:def") == fix_plan.LEDGER
    assert fix_ledger.key_kind("upstream:2:abc") == fix_plan.UPSTREAM


def test_an_upstream_decision_is_one_key_for_the_group():
    group = (failure(project="a", number=1), failure(project="b", number=2))
    decision = fix_plan.Decision(fix_plan.UPSTREAM, "n", group)
    key = fix_ledger.decision_key(decision)
    assert key.startswith("upstream:2:")
    reordered = fix_plan.Decision(fix_plan.UPSTREAM, "n", group[::-1])
    assert fix_ledger.decision_key(reordered) == key
    repushed = fix_plan.Decision(
        fix_plan.UPSTREAM, "n", (group[0], failure(project="b", number=2, sha="new"))
    )
    assert fix_ledger.decision_key(repushed) != key


def test_a_single_decision_is_keyed_as_its_failure_under_the_action_taken():
    """The action is in the key because the pass's decision can change while the failure
    does not: devkit #381 was dispatched at its head sha under the wrong action, and the
    corrected pass has to be able to send the resolver at that very sha."""
    decision = fix_plan.Decision(fix_plan.DISPATCH, "n", (failure(),))
    assert fix_ledger.decision_key(decision).startswith(fix_ledger.failure_key(failure()) + ":")
    assert fix_ledger.decision_key(decision).endswith(":" + fix_plan.DISPATCH)
    resolve = fix_plan.Decision(fix_plan.RESOLVE, "n", (failure(),))
    assert fix_ledger.decision_key(resolve) != fix_ledger.decision_key(decision)


# --- the file -------------------------------------------------------------------------


def test_the_ledger_records_and_answers_the_second_click(tmp_path):
    path = tmp_path / "boxes" / "dispatch.json"
    decision = fix_plan.Decision(fix_plan.DISPATCH, "1 check failing", (failure(),))
    assert fix_ledger.already_sent(decision, fix_ledger.read_ledger(path)) == ""
    fix_ledger.record(path, fix_ledger.decision_key(decision), decision.note, NOW)
    ledger = fix_ledger.read_ledger(path)
    assert fix_ledger.already_sent(decision, ledger) == "2026-09-18T12:00:00+00:00"
    assert ledger[fix_ledger.decision_key(decision)]["what"] == "1 check failing"


def test_a_corrupt_ledger_is_empty_rather_than_a_traceback(tmp_path):
    path = tmp_path / "dispatch.json"
    path.write_text("{not json", encoding="utf-8")
    assert fix_ledger.read_ledger(path) == {}
    path.write_text("[1, 2]", encoding="utf-8")
    assert fix_ledger.read_ledger(path) == {}


def test_the_problem_key_is_the_decision_key_without_its_commit():
    at_a = fix_plan.Decision(fix_plan.DISPATCH, "n", (failure(sha="a1"),))
    at_b = fix_plan.Decision(fix_plan.DISPATCH, "n", (failure(sha="b2"),))
    other = fix_plan.Decision(fix_plan.DISPATCH, "n", (failure(sha="b2", signature=("t::u",)),))
    assert fix_ledger.decision_key(at_a) != fix_ledger.decision_key(at_b)
    assert fix_ledger.problem_key(at_a) == fix_ledger.problem_key(at_b)
    assert fix_ledger.problem_key(at_a) != fix_ledger.problem_key(other)
    key = fix_ledger.decision_key(at_a)
    assert fix_ledger.problem_of(key, {}) == fix_ledger.problem_key(at_a), "derived when unrecorded"
    assert fix_ledger.problem_of("upstream:2:abc", {}) == "", "a group cannot be derived"
    group = (failure(project="a", sha="1"), failure(project="b", sha="2"))
    moved = (failure(project="a", sha="3"), failure(project="b", sha="2"))
    assert fix_ledger.problem_key(
        fix_plan.Decision(fix_plan.UPSTREAM, "n", group)
    ) == fix_ledger.problem_key(fix_plan.Decision(fix_plan.UPSTREAM, "n", moved))


def test_attempts_count_every_commit_of_one_problem(tmp_path):
    path = tmp_path / "dispatch.json"
    for sha in ("a1", "b2"):
        decision = fix_plan.Decision(fix_plan.DISPATCH, "n", (failure(sha=sha),))
        fix_ledger.record(
            path, fix_ledger.decision_key(decision), "n", NOW, fix_ledger.problem_key(decision)
        )
    ledger = fix_ledger.read_ledger(path)
    later = fix_plan.Decision(fix_plan.DISPATCH, "n", (failure(sha="c3"),))
    assert fix_ledger.attempts(later, ledger) == 2
    unrelated = fix_plan.Decision(fix_plan.DISPATCH, "n", (failure(number=9),))
    assert fix_ledger.attempts(unrelated, ledger) == 0


def test_a_blocked_report_stands_after_the_sha_moves(tmp_path):
    """The blocker a fixer named does not go away because somebody pushed."""
    path = tmp_path / "dispatch.json"
    first = fix_plan.Decision(fix_plan.DISPATCH, "n", (failure(sha="a1"),))
    key = fix_ledger.decision_key(first)
    fix_ledger.record(path, key, "n", NOW, fix_ledger.problem_key(first))
    fix_ledger.mark_blocked(path, key, "needs a database")
    moved = fix_plan.Decision(fix_plan.DISPATCH, "n", (failure(sha="b2"),))
    assert fix_ledger.blocked_reason(moved, fix_ledger.read_ledger(path)) == "needs a database"


def test_a_dispatch_older_than_the_resend_window_is_sent_again(tmp_path):
    """A background session that died on a permission prompt left a ledger entry that
    blocked its key forever, and the record said "already dispatched" for a week."""
    path = tmp_path / "dispatch.json"
    decision = fix_plan.Decision(fix_plan.DISPATCH, "n", (failure(),))
    fix_ledger.record(path, fix_ledger.decision_key(decision), "n", NOW)
    ledger = fix_ledger.read_ledger(path)
    assert fix_ledger.already_sent(decision, ledger, NOW + _dt.timedelta(hours=5))
    assert fix_ledger.already_sent(decision, ledger, NOW + fix_ledger.RESEND_AFTER) == ""
    assert fix_ledger.already_sent(decision, ledger), "with no clock, the entry stands"


def test_a_key_is_resent_once_and_then_needs_a_human(tmp_path):
    """The entry counts its sessions: a failure whose session dies every day would
    otherwise buy a new one every day, under the daily caps but with no end."""
    path = tmp_path / "dispatch.json"
    decision = fix_plan.Decision(fix_plan.DISPATCH, "n", (failure(),))
    key = fix_ledger.decision_key(decision)
    fix_ledger.record(path, key, "n", NOW)
    assert fix_ledger.read_ledger(path)[key]["sent"] == 1
    later = NOW + fix_ledger.RESEND_AFTER
    assert fix_ledger.already_sent(decision, fix_ledger.read_ledger(path), later) == ""
    fix_ledger.record(path, key, "n", later)
    ledger = fix_ledger.read_ledger(path)
    assert ledger[key]["sent"] == fix_ledger.MAX_SENDS
    spent = fix_ledger.already_sent(decision, ledger, later + 2 * fix_ledger.RESEND_AFTER)
    assert spent.startswith(later.isoformat(timespec="seconds")) and "needs a human" in spent
    assert "needs a human" in fix_ledger.already_sent(decision, ledger), "with no clock too"
    assert fix_ledger.sends({"when": "2026-09-01"}) == 1, "an entry from before the count"
    assert fix_ledger.sends({"sent": "junk"}) == 1 and fix_ledger.sends(None) == 0


def test_a_blocked_dispatch_is_remembered_with_its_reason_and_never_resent(tmp_path):
    path = tmp_path / "dispatch.json"
    decision = fix_plan.Decision(fix_plan.DISPATCH, "n", (failure(),))
    key = fix_ledger.decision_key(decision)
    assert not fix_ledger.mark_blocked(path, key, "before any dispatch"), "nothing to mark yet"
    fix_ledger.record(path, key, "n", NOW)
    assert fix_ledger.blocked_reason(decision, fix_ledger.read_ledger(path)) == ""
    assert fix_ledger.mark_blocked(path, key, "the fixture needs a database the runner lacks")
    ledger = fix_ledger.read_ledger(path)
    assert (
        fix_ledger.blocked_reason(decision, ledger)
        == "the fixture needs a database the runner lacks"
    )
    assert fix_ledger.already_sent(decision, ledger, NOW + _dt.timedelta(days=30)) == NOW.isoformat(
        timespec="seconds"
    )
    assert not fix_ledger.mark_blocked(path, "unknown:key", "ignored")
    assert "unknown:key" not in fix_ledger.read_ledger(path), "a stamp no dispatch made is noise"


# --- the report -----------------------------------------------------------------------


def test_the_report_says_what_will_be_sent_what_was_held_and_what_is_skipped(tmp_path):
    sent = fix_plan.Decision(fix_plan.DISPATCH, "1 check failing: t", (failure(number=1),))
    fresh = fix_plan.Decision(fix_plan.UPSTREAM, "one vendored failure", (failure(number=2),))
    skipped = fix_plan.Decision(fix_plan.SKIP, "red by construction", (failure(number=3),))
    held = fix_plan.Decision(fix_plan.HOLD, "held: origin/main is red", (failure(number=4),))
    path = tmp_path / "dispatch.json"
    fix_ledger.record(path, fix_ledger.decision_key(sent), sent.note, NOW)
    text = fix_ledger.render([sent, fresh, skipped, held], fix_ledger.read_ledger(path))
    lines = text.splitlines()
    assert lines[0].startswith("sent     carameli #1 -- already dispatched at 2026-09-18")
    assert lines[1].startswith("upstream carameli #2 -- one vendored failure")
    assert lines[2].startswith("skip     carameli #3 -- red by construction")
    assert lines[3] == "held     carameli #4 -- held: origin/main is red"


def test_an_empty_plan_says_so():
    assert fix_ledger.render([], {}) == "nothing is red"
