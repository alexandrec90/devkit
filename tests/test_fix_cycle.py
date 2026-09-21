"""`scripts/fix_cycle.py`: harness first, project fixers held, every dispatch capped."""

from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fix_cycle
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
    assert classes[fix_plan.failure_key(red[1])] == fix_cycle.HARNESS
    assert classes[fix_plan.failure_key(red[0])] == fix_cycle.PROJECT


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
    key = fix_plan.decision_key(one)
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
