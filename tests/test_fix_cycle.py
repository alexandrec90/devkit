"""`scripts/fix_cycle.py`: harness first, project fixers held, every dispatch capped."""

from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fix_cycle
import fix_ledger
import fix_plan

NOW = _dt.datetime(2026, 9, 19, 9, 0, tzinfo=_dt.UTC)
VENDORED = ("scripts/hooks/tests/test_untested_symbols.py::test_x",)


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


# --- the switch -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"settings": {"devkit.fixPass": "dispatch"}}', fix_cycle.DISPATCH),
        ('{"settings": {"devkit.fixPass": "plan"}}', fix_cycle.PLAN),
        ('{"settings": {"devkit.fixPass": "on"}}', fix_cycle.OFF),
        ('{"settings": {}}', fix_cycle.OFF),
        ("{not json", fix_cycle.OFF),
        ("[]", fix_cycle.OFF),
    ],
)
def test_the_switch_is_off_unless_the_workspace_file_says_plan_or_dispatch(text, expected):
    """A misspelt value must not turn dispatch on; `off` is the safe reading of everything."""
    assert fix_cycle.mode_from_workspace(text) == expected


# --- classification -------------------------------------------------------------------


def test_a_vendored_test_is_the_harness():
    assert fix_cycle.classify(failure(signature=VENDORED), set()) == fix_cycle.HARNESS


def test_anything_in_devkit_is_the_harness():
    assert fix_cycle.classify(failure(project="devkit"), set()) == fix_cycle.HARNESS


def test_a_signature_shared_by_two_projects_is_the_harness():
    red = [failure(project="a", number=1), failure(project="b", number=2)]
    shared = fix_cycle.shared_signatures(red)
    assert shared == {("tests/test_x.py::t",)}
    assert fix_cycle.classify(red[0], shared) == fix_cycle.HARNESS
    assert fix_cycle.classify(failure(project="a", number=1), set()) == fix_cycle.PROJECT


def test_a_lint_finding_in_a_vendored_path_is_the_harness():
    assert (
        fix_cycle.classify(failure(signature=("lint scripts/hooks/stop.py",)), set())
        == fix_cycle.HARNESS
    )
    assert fix_cycle.classify(failure(signature=("lint app/main.py",)), set()) == fix_cycle.PROJECT


def test_a_commit_refused_by_the_toolchain_is_the_harness_and_by_the_change_is_not():
    toolchain = failure(
        kind=fix_plan.COMMIT, signature=("fixers refused: Executable ruff not found",)
    )
    assert fix_cycle.classify(toolchain, set()) == fix_cycle.HARNESS
    secret = failure(kind=fix_plan.COMMIT, signature=("commit refused: detect secrets",))
    assert fix_cycle.classify(secret, set()) == fix_cycle.PROJECT


def test_no_evidence_is_unknown_and_a_conflict_alone_is_unknown():
    assert fix_cycle.classify(failure(signature=()), set()) == fix_cycle.UNKNOWN
    assert fix_cycle.classify(failure(signature=(fix_plan.CONFLICT,)), set()) == fix_cycle.UNKNOWN


def test_classify_all_is_keyed_like_the_ledger():
    red = [failure(number=1), failure(project="devkit", number=2, signature=("tests/t.py::d",))]
    classes = fix_cycle.classify_all(red)
    assert classes[fix_ledger.failure_key(red[1])] == fix_cycle.HARNESS
    assert classes[fix_ledger.failure_key(red[0])] == fix_cycle.PROJECT


# --- the phase gate -------------------------------------------------------------------


def test_the_harness_is_clean_only_when_nothing_harness_shaped_is_red():
    assert fix_cycle.harness_state({}, True, []).clean
    red = fix_cycle.harness_state({"k": fix_cycle.HARNESS}, True, [])
    assert not red.clean and "1 harness failure" in red.reasons[0]
    assert not fix_cycle.harness_state({}, False, []).clean
    unread = fix_cycle.harness_state({}, None, [])
    assert not unread.clean and "could not be read" in unread.reasons[0]
    adopting = fix_cycle.harness_state({}, True, ["roguelike", "carameli"])
    assert not adopting.clean and "carameli, roguelike" in adopting.reasons[0]


def test_while_the_harness_is_red_one_devkit_session_goes_and_every_project_fixer_is_held():
    vendored = [
        failure(project="a", number=1, signature=VENDORED),
        failure(project="b", number=2, signature=VENDORED),
    ]
    project = failure(project="c", number=3)
    decisions = [decision(fix_plan.UPSTREAM, *vendored), decision(fix_plan.DISPATCH, project)]
    classes = fix_cycle.classify_all([*vendored, project])
    harness = fix_cycle.harness_state(classes, True, [])
    go, held = fix_cycle.phase(decisions, classes, harness)
    assert [d.action for d in go] == [fix_plan.UPSTREAM]
    assert [(d.failures[0].project, why[:34]) for d, why in held] == [
        ("c", "held until the harness is clean: 2")
    ]


def test_several_harness_decisions_fold_into_one_session():
    devkit_pr = failure(project="devkit", number=9)
    shared = [failure(project="a", number=1), failure(project="b", number=2)]
    decisions = [
        decision(fix_plan.DISPATCH, devkit_pr),
        decision(fix_plan.DISPATCH, shared[0]),
        decision(fix_plan.DISPATCH, shared[1]),
    ]
    classes = fix_cycle.classify_all([devkit_pr, *shared])
    go, held = fix_cycle.phase(decisions, classes, fix_cycle.harness_state(classes, True, []))
    assert len(go) == 1 and go[0].action == fix_plan.UPSTREAM
    assert sorted(f.project for f in go[0].failures) == ["a", "b", "devkit"]
    assert "3 checkout(s)" in go[0].note
    assert held == []


def test_a_conflicted_harness_pr_gets_its_resolver_rather_than_the_devkit_session():
    """devkit #381 was a conflict, and the fold sent an upstream session at it: a fresh
    branch off the default, told to fix the harness, with no way to land on the PR at
    all. An action that names a branch operation on one PR cannot be folded into a
    session that has no branch."""
    conflicted = failure(project="devkit", number=381, signature=(fix_plan.CONFLICT,))
    classes = fix_cycle.classify_all([conflicted])
    harness = fix_cycle.harness_state(classes, True, [])
    go, held = fix_cycle.phase([decision(fix_plan.RESOLVE, conflicted)], classes, harness)
    assert not harness.clean
    assert [d.action for d in go] == [fix_plan.RESOLVE]
    assert go[0].failures == (conflicted,)
    assert held == []


def test_a_behind_harness_pr_is_updated_rather_than_folded():
    """An `UPDATE` is a GitHub call against one PR, not a session; folding it spends an
    agent on what a merge would have done for nothing."""
    behind = failure(project="devkit", number=9, behind=True)
    red = failure(project="devkit", number=10)
    classes = fix_cycle.classify_all([behind, red])
    go, held = fix_cycle.phase(
        [decision(fix_plan.DISPATCH, red), decision(fix_plan.UPDATE, behind)],
        classes,
        fix_cycle.harness_state(classes, True, []),
    )
    assert [d.action for d in go] == [fix_plan.UPDATE, fix_plan.UPSTREAM]
    assert go[1].failures == (red,)
    assert held == []


def test_once_clean_project_fixers_go_updates_first_then_conflicts():
    """An update is free and may turn the PR green by itself; a conflict's gate cannot
    run at all; a plain red gets its session last."""
    conflict = failure(number=1, signature=(fix_plan.CONFLICT,))
    red = failure(number=2)
    behind = failure(number=3, behind=True)
    decisions = [
        decision(fix_plan.DISPATCH, red),
        decision(fix_plan.RESOLVE, conflict),
        decision(fix_plan.UPDATE, behind),
    ]
    classes = fix_cycle.classify_all([red, conflict, behind])
    go, held = fix_cycle.phase(decisions, classes, fix_cycle.harness_state({}, True, []))
    assert [d.action for d in go] == [fix_plan.UPDATE, fix_plan.RESOLVE, fix_plan.DISPATCH]
    assert held == []


def test_skips_are_in_neither_list():
    skipped = decision(fix_plan.SKIP, failure(head="release/v1"))
    go, held = fix_cycle.phase([skipped], {}, fix_cycle.harness_state({}, True, []))
    assert go == [] and held == []


def test_unknown_goes_to_the_project_bucket():
    """Guessing the other way sends a devkit session at a project bug."""
    unknown = failure(signature=())
    classes = fix_cycle.classify_all([unknown])
    go, held = fix_cycle.phase(
        [decision(fix_plan.DISPATCH, unknown)], classes, fix_cycle.harness_state({}, False, [])
    )
    assert go == [] and len(held) == 1


# --- the caps -------------------------------------------------------------------------


def test_a_decision_is_harness_if_any_failure_under_it_is():
    mixed = decision(fix_plan.DISPATCH, failure(number=1), failure(project="devkit", number=2))
    classes = fix_cycle.classify_all(mixed.failures)
    assert fix_cycle.decision_class(mixed, classes) == fix_cycle.HARNESS
    plain = decision(fix_plan.DISPATCH, failure(number=1))
    assert (
        fix_cycle.decision_class(plain, fix_cycle.classify_all(plain.failures)) == fix_cycle.PROJECT
    )
    assert fix_cycle.decision_class(decision(fix_plan.UPSTREAM, failure()), {}) == fix_cycle.HARNESS


def test_folding_one_upstream_decision_keeps_it_as_it_is():
    one = decision(fix_plan.UPSTREAM, failure())
    assert fix_cycle.fold_harness([one]) is one
    two = fix_cycle.fold_harness([one, decision(fix_plan.DISPATCH, failure(number=2))])
    assert two.action == fix_plan.UPSTREAM and len(two.failures) == 2


def test_sent_today_counts_per_target_on_the_ledgers_own_dates():
    ledger = {
        "pr:a:1:s:d": {"when": NOW.isoformat()},
        "pr:a:1:t:e": {"when": NOW.isoformat()},
        "upstream:2:d": {"when": NOW.isoformat()},
        "pr:b:2:s:d": {"when": "2020-01-01T00:00:00+00:00"},
        "junk": "not an entry",
    }
    assert fix_cycle.sent_today(ledger, NOW) == {"pr:a:1": 2, "devkit": 1}


def test_the_target_is_the_pr_the_branch_or_devkit():
    assert fix_cycle.target_of("pr:carameli:412:abc:deadbeef") == "pr:carameli:412"
    assert fix_cycle.target_of("commit:carameli:0:abc:deadbeef") == "commit:carameli:0"
    assert fix_cycle.target_of("upstream:3:deadbeef") == fix_cycle.DEVKIT
    # The action the key ends in is a suffix, so two dispatches about one PR still
    # draw on the same target's daily budget.
    assert fix_cycle.target_of("pr:carameli:412:abc:deadbeef:resolve") == "pr:carameli:412"


def test_a_target_past_its_daily_count_needs_a_human():
    one = decision(fix_plan.DISPATCH, failure())
    key = fix_ledger.decision_key(one)
    today = NOW.isoformat()
    ledger = {
        key + "1": {"when": today, "what": "n"},
        "pr:carameli:412:other:digest": {"when": today, "what": "n"},
    }
    ok, why = fix_cycle.within_caps(one, ledger, NOW)
    assert not ok and "needs a human" in why
    assert fix_cycle.within_caps(decision(fix_plan.DISPATCH, failure(number=9)), ledger, NOW) == (
        True,
        "",
    )


def test_yesterdays_dispatches_do_not_count():
    one = decision(fix_plan.DISPATCH, failure())
    yesterday = (NOW - _dt.timedelta(days=1)).isoformat()
    ledger = {f"k{i}": {"when": yesterday, "what": "n"} for i in range(10)}
    assert fix_cycle.within_caps(one, ledger, NOW) == (True, "")


def test_the_pass_as_a_whole_has_a_daily_budget():
    one = decision(fix_plan.DISPATCH, failure(number=99))
    ledger = {
        f"pr:p{i}:{i}:s:d": {"when": NOW.isoformat(), "what": "n"} for i in range(fix_cycle.PER_DAY)
    }
    ok, why = fix_cycle.within_caps(one, ledger, NOW)
    assert not ok and "today" in why


# --- the account and the merge --------------------------------------------------------


def test_the_record_says_what_shipped_what_went_what_was_held_and_why():
    harness = fix_cycle.harness_state({"k": fix_cycle.HARNESS}, True, [])
    go = [decision(fix_plan.UPSTREAM, failure(project="devkit", number=9))]
    held = [(decision(fix_plan.DISPATCH, failure(number=3)), "held until the harness is clean")]
    account = fix_cycle.Account(
        fix_cycle.PLAN,
        harness,
        ("carameli agent/x -- would ship: T",),
        tuple(go),
        tuple(held),
        (),
        ("devkit #9 -- would send",),
        (),
        (decision(fix_plan.SKIP, failure(kind=fix_plan.BRANCH, base="main", number=0)),),
    )
    text = fix_cycle.render(account)
    assert isinstance(harness, fix_cycle.Harness)
    lines = text.splitlines()
    assert lines[0] == "fix-pass: mode=plan"
    assert lines[1].startswith("shipped  carameli agent/x")
    assert lines[2].startswith("harness  RED -- 1 harness failure")
    assert lines[3].startswith("upstream devkit #9")
    assert lines[4].startswith("held     carameli #3 -- held until")
    assert lines[5].startswith("skip     carameli origin/main -- ")
    assert lines[6].startswith("sent     devkit #9")


def test_only_a_green_labelled_adoption_is_mergeable_unattended():
    rows = [
        {
            "number": 1,
            "headRefName": "agent/auto/devkit-upgrade-v0-11-22-0919",
            "labels": [{"name": "automerge"}],
            "statusCheckRollup": [{"conclusion": "SUCCESS"}],
            "mergeable": "MERGEABLE",
        },
        {
            "number": 2,
            "headRefName": "agent/auto/devkit-upgrade-v0-11-22-0919",
            "labels": [],
            "statusCheckRollup": [{"conclusion": "SUCCESS"}],
        },
        {
            "number": 3,
            "headRefName": "agent/auto/devkit-upgrade-v0-11-22-0919",
            "labels": [{"name": "automerge"}],
            "statusCheckRollup": [{"conclusion": "FAILURE"}],
        },
        {
            "number": 4,
            "headRefName": "agent/auto/devkit-upgrade-v0-11-22-0919",
            "labels": [{"name": "automerge"}],
            "statusCheckRollup": [],
        },
        {
            "number": 5,
            "headRefName": "agent/auto/devkit-upgrade-v0-11-22-0919",
            "labels": [{"name": "automerge"}],
            "statusCheckRollup": [{"conclusion": "SUCCESS"}],
            "mergeable": "CONFLICTING",
        },
        {
            "number": 6,
            "headRefName": "agent/feature-0919",
            "labels": [{"name": "automerge"}],
            "statusCheckRollup": [{"conclusion": "SUCCESS"}],
        },
        {
            "number": 7,
            "headRefName": "agent/auto/devkit-upgrade-v0-11-22-0919",
            "labels": [{"name": "automerge"}],
            "statusCheckRollup": [{"conclusion": "SUCCESS"}],
            "isDraft": True,
        },
    ]
    prefixes = ("agent/auto/devkit-upgrade-", "agent/devkit-upgrade-")
    assert [r["number"] for r in fix_cycle.green_adoptions(rows, prefixes, "automerge")] == [1]


# --- the budget leaks, and what closes them ------------------------------------------


def test_an_update_never_draws_on_the_session_budget():
    """After a release merge, a handful of behind PRs burnt the eight daily slots on
    free `gh pr update-branch` calls and the real fixers read "sent 8 sessions today"."""
    today = NOW.isoformat()
    ledger = {f"pr:p{i}:{i}:s:d:update": {"when": today, "what": "n"} for i in range(10)}
    assert fix_cycle.sent_today(ledger, NOW) == {}
    update = decision(fix_plan.UPDATE, failure(number=99, behind=True))
    full = {f"pr:p{i}:{i}:s:d": {"when": today, "what": "n"} for i in range(fix_cycle.PER_DAY)}
    assert fix_cycle.within_caps(update, full, NOW) == (True, "")


def test_a_dispatch_with_no_evidence_gets_one_session_a_day_not_two():
    """A session sent at "no artifact and no failed step named" is pure discovery, the
    most expensive kind; the retry a second slot exists for is a fix that did not take,
    which a blind session cannot be told from."""
    blind = decision(fix_plan.DISPATCH, failure(signature=()))
    ledger = {"pr:carameli:412:other:digest": {"when": NOW.isoformat(), "what": "n"}}
    ok, why = fix_cycle.within_caps(blind, ledger, NOW)
    assert not ok and "no evidence" in why and "needs a human" in why
    assert fix_cycle.within_caps(decision(fix_plan.DISPATCH, failure()), ledger, NOW) == (True, "")


def _conflict(sha: str) -> fix_plan.Decision:
    return decision(
        fix_plan.RESOLVE,
        failure(project="devkit", number=390, sha=sha, signature=(fix_plan.CONFLICT,)),
    )


def test_a_conflict_back_at_a_new_head_gets_its_resolver_again():
    """devkit #390: the resolver pushed its merge, main moved within the hour, and the
    new conflict read "needs a human" -- so the harness stayed red and every project
    PR stayed held behind it. The head moving is what shows the first one took."""
    first = fix_ledger.decision_key(_conflict("143b"))
    ledger = {first: {"when": NOW.isoformat(), "what": "merge conflict"}}
    assert fix_cycle.within_caps(_conflict("62a5"), ledger, NOW) == (True, "")
    second = fix_ledger.decision_key(_conflict("62a5"))
    ledger[second] = {"when": NOW.isoformat(), "what": "merge conflict"}
    ok, why = fix_cycle.within_caps(_conflict("77ff"), ledger, NOW)
    assert not ok and f"{fix_cycle.PER_TARGET_PER_DAY} sessions today" in why, (
        "the per-target cap still bounds a conflict that keeps coming back"
    )


def test_a_conflict_whose_head_did_not_move_keeps_its_one_blind_slot():
    same = fix_ledger.decision_key(_conflict("143b"))
    # Older than a day would be a re-send; today at the same commit is the session
    # that did nothing, re-keyed by a different signature digest.
    ledger = {same.replace(":resolve", "x:resolve"): {"when": NOW.isoformat(), "what": "n"}}
    ok, why = fix_cycle.within_caps(_conflict("143b"), ledger, NOW)
    assert not ok and "no evidence" in why


def test_moved_on_ignores_updates_other_days_and_folded_keys():
    key = fix_ledger.decision_key(_conflict("62a5"))
    yesterday = (NOW - _dt.timedelta(days=1)).isoformat()
    assert not fix_cycle.moved_on(key, {}, NOW), "nothing sent today is not movement"
    assert not fix_cycle.moved_on(key, {"pr:devkit:390:143b:d:resolve": {"when": yesterday}}, NOW)
    assert not fix_cycle.moved_on(
        key, {"pr:devkit:390:143b:d:update": {"when": NOW.isoformat()}}, NOW
    )
    assert fix_cycle.moved_on(key, {"pr:devkit:390:143b:d:resolve": {"when": NOW.isoformat()}}, NOW)
    assert not fix_cycle.moved_on("upstream:2:abcd", {}, NOW)


def test_an_adoption_pr_is_classified_by_what_fails_and_its_own_release_never_holds_it():
    """An adoption red for a project-shaped reason -- the upgrade broke the project's
    own lint -- was held because the release was still being adopted, and the release
    was still being adopted because it was red: a deadlock only a person broke. It goes
    now, on its own branch, while that release is the only hold; red on a vendored test
    it is the harness and folds into the devkit session like any other."""
    prefixes = ("agent/auto/devkit-upgrade-",)
    head = "agent/auto/devkit-upgrade-v0-11-21-0917"
    own = decision(fix_plan.DISPATCH, failure(number=1, head=head, signature=("lint src/a.py",)))
    vendored = decision(fix_plan.DISPATCH, failure(number=2, head=head, signature=VENDORED))
    other = decision(fix_plan.DISPATCH, failure(number=3))
    assert fix_plan.is_adoption(own, prefixes) and not fix_plan.is_adoption(other, prefixes)
    classes = fix_cycle.classify_all([*own.failures, *vendored.failures, *other.failures])
    assert classes[fix_ledger.failure_key(own.failures[0])] == fix_cycle.PROJECT
    assert classes[fix_ledger.failure_key(vendored.failures[0])] == fix_cycle.HARNESS

    adopting = fix_cycle.harness_state({}, True, ["carameli"])
    assert not adopting.clean and adopting.only_adopting
    go, held = fix_cycle.phase([own, other], classes, adopting, prefixes)
    assert go == [own] and [d for d, _why in held] == [other]

    red_too = fix_cycle.harness_state(classes, True, ["carameli"])
    assert not red_too.only_adopting
    go, held = fix_cycle.phase([own, vendored, other], classes, red_too, prefixes)
    assert [d.action for d in go] == [fix_plan.UPSTREAM]
    assert [d for d, _why in held] == [own, other]


def test_the_ledger_backlog_rides_along_without_holding_anyone():
    """One unresolved hook event anywhere held every project fixer; the backlog goes to
    the devkit session when one is sent and is not by itself a reason to send one."""
    backlog = failure(kind=fix_plan.LEDGER, project="devkit", number=0, head="")
    classes = fix_cycle.classify_all([backlog])
    assert classes[fix_ledger.failure_key(backlog)] == fix_cycle.HARNESS
    assert fix_cycle.harness_state(classes, True, []).clean


def test_a_gate_running_at_the_tip_is_not_a_reason_to_hold():
    """Every pass in the minutes after a merge to devkit main read "could not be read"
    and held every project fixer behind a gate that was merely running."""
    assert fix_cycle.harness_state({}, fix_plan.RUNNING, []).clean
    assert not fix_cycle.harness_state({}, None, []).clean


def test_a_held_decision_is_held_with_its_own_note_clean_or_not():
    held_one = fix_plan.Decision(
        fix_plan.HOLD, "held: origin/main is red in carameli", (failure(),)
    )
    clean = fix_cycle.harness_state({}, True, [])
    go, held = fix_cycle.phase([held_one], {}, clean)
    assert go == [] and held == [(held_one, "held: origin/main is red in carameli")]
    red = fix_cycle.harness_state({"k": fix_cycle.HARNESS}, True, [])
    go, held = fix_cycle.phase([held_one], {}, red)
    assert go == [] and held == [(held_one, "held: origin/main is red in carameli")]


def test_a_decision_is_blind_when_no_failure_under_it_names_anything():
    assert fix_cycle.is_blind(decision(fix_plan.DISPATCH, failure(signature=())))
    assert fix_cycle.is_blind(decision(fix_plan.RESOLVE, failure(signature=(fix_plan.CONFLICT,))))
    assert not fix_cycle.is_blind(decision(fix_plan.DISPATCH, failure()))
    mixed = decision(fix_plan.UPSTREAM, failure(signature=()), failure(number=2))
    assert not fix_cycle.is_blind(mixed), "one failure with evidence is enough to start from"


def test_the_record_names_the_regated_branches_and_the_blocked_reports():
    account = fix_cycle.Account(
        fix_cycle.PLAN,
        fix_cycle.harness_state({}, True, []),
        blocked=("carameli agent/x -- needs a database the runner lacks",),
        regated=("devkit main -- no verdict at the tip; gate re-run",),
    )
    lines = fix_cycle.render(account).splitlines()
    assert lines[1] == "regate   devkit main -- no verdict at the tip; gate re-run"
    assert lines[2] == "harness  clean"
    assert lines[3] == "blocked  carameli agent/x -- needs a database the runner lacks"
