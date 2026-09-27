"""`scripts/fix_cycle.py`: harness first, project fixers held, every dispatch capped."""

from __future__ import annotations

import datetime as _dt
import json
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
PREFIXES = ("agent/auto/devkit-upgrade-", "agent/devkit-upgrade-")


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


def test_anything_in_devkit_but_a_pr_is_the_harness():
    for kind in (fix_plan.BRANCH, fix_plan.NIGHTLY, fix_plan.LEDGER):
        assert fix_cycle.classify(failure(kind=kind, project="devkit"), set()) == fix_cycle.HARNESS


def test_a_devkit_pr_red_on_its_own_diff_is_fixed_on_its_own_branch():
    """devkit #393 and #394 were red on the vendored ratchets -- a ceiling and the
    untested-symbol baseline, both judging the PR's own code -- while main was green.
    Classed as the harness, they were folded into the upstream session: a fresh branch
    off main, told to fix the harness, with nothing on main to fix and no way to land on
    either PR. A PR against a red base is already held by the plan, so a devkit PR that
    reaches here is red on its own diff, vendored test or not."""
    for signature in (
        VENDORED,
        ("scripts/hooks/tests/test_structure_check.py::test_nothing_is_new",),
        ("lint scripts/hooks/stop.py",),
        ("tests/test_x.py::t",),
    ):
        red = failure(project="devkit", number=393, signature=signature)
        assert fix_cycle.classify(red, set()) == fix_cycle.PROJECT
        classes = fix_cycle.classify_all([red])
        harness = fix_cycle.harness_state(classes, True, [])
        go, held = fix_cycle.phase([decision(fix_plan.DISPATCH, red)], classes, harness)
        assert harness.clean and held == []
        assert [(d.action, d.failures) for d in go] == [(fix_plan.DISPATCH, (red,))]


RATCHET = (
    "scripts/hooks/tests/test_structure_check.py::test_nothing_is_new_or_worse_than_the_baseline",
)


def test_a_consumer_pr_red_only_on_a_ratchet_is_fixed_on_its_own_branch():
    """carameli #395 grew its own `scripts/lint-all.py` past the ceiling its baseline
    records, and the vendored structure check said so. Classed as the harness, it went
    to a devkit session on a fresh branch off main -- nothing there to fix, and no way to
    reach the PR -- and stayed red. The test file is vendored; what it judges is not."""
    red = failure(head="agent/secrets-baseline-lf-0926", signature=RATCHET)
    assert fix_cycle.classify(red, set(), PREFIXES) == fix_cycle.PROJECT
    classes = fix_cycle.classify_all([red], PREFIXES)
    harness = fix_cycle.harness_state(classes, True, [])
    go, held = fix_cycle.phase([decision(fix_plan.DISPATCH, red)], classes, harness)
    assert harness.clean and held == []
    assert [(d.action, d.failures) for d in go] == [(fix_plan.DISPATCH, (red,))]


def test_a_ratchet_shared_adopted_or_beside_a_vendored_test_is_still_the_harness():
    """The v0.11.21 adoption was red in eight repos on the untested-symbols ratchet
    because the rule itself changed: across consumers, or on an adoption, what moved may
    be the ratchet, and that is devkit's."""
    both = [failure(signature=RATCHET), failure(project="ibkr_trader", number=7, signature=RATCHET)]
    shared = fix_cycle.shared_signatures(both)
    assert all(fix_cycle.classify(f, shared, PREFIXES) == fix_cycle.HARNESS for f in both)
    adopting = failure(head="agent/auto/devkit-upgrade-v0-11-25-0926", signature=RATCHET)
    assert fix_cycle.classify(adopting, set(), PREFIXES) == fix_cycle.HARNESS
    mixed = failure(signature=(*RATCHET, *VENDORED))
    assert fix_cycle.classify(mixed, set(), PREFIXES) == fix_cycle.HARNESS


def test_a_ratchet_is_the_projects_own_only_alone_and_off_an_adoption():
    own = failure(head="agent/secrets-baseline-lf-0926", signature=RATCHET)
    assert fix_cycle.is_own_ratchet(own, set(), PREFIXES)
    assert not fix_cycle.is_own_ratchet(own, {RATCHET}, PREFIXES)
    assert not fix_cycle.is_own_ratchet(
        failure(head="agent/auto/devkit-upgrade-v0-11-21-0917", signature=RATCHET), set(), PREFIXES
    )
    assert not fix_cycle.is_own_ratchet(failure(signature=("tests/t.py::a",)), set(), PREFIXES)
    for sig in ((), (*RATCHET, "tests/t.py::a"), VENDORED):
        mixed = failure(head="agent/secrets-baseline-lf-0926", signature=sig)
        assert not fix_cycle.is_own_ratchet(mixed, set(), PREFIXES)
    # Still vendored: across consumers it stays one devkit decision (the v0.11.21 fan-out).
    assert fix_plan.is_vendored(RATCHET)


def test_every_ratchet_names_a_live_vendored_test():
    """A renamed ratchet would silently send its failures back to the devkit session."""
    root = Path(__file__).resolve().parents[1]
    for entry in fix_cycle.RATCHETS:
        path, _, name = entry.partition("::")
        assert path in (fix_plan.vendored_paths() or ()), entry
        assert f"\ndef {name}(" in (root / path).read_text(encoding="utf-8"), entry


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
    red = [failure(number=1), failure(project="a", number=2, signature=VENDORED)]
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
    assert adopting.clean and adopting.adopting == ("carameli", "roguelike")


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
    vendored = failure(project="c", number=9, signature=VENDORED)
    shared = [failure(project="a", number=1), failure(project="b", number=2)]
    decisions = [
        decision(fix_plan.DISPATCH, vendored),
        decision(fix_plan.DISPATCH, shared[0]),
        decision(fix_plan.DISPATCH, shared[1]),
    ]
    classes = fix_cycle.classify_all([vendored, *shared])
    go, held = fix_cycle.phase(decisions, classes, fix_cycle.harness_state(classes, True, []))
    assert len(go) == 1 and go[0].action == fix_plan.UPSTREAM
    assert sorted(f.project for f in go[0].failures) == ["a", "b", "c"]
    assert "3 checkout(s)" in go[0].note
    assert held == []


def test_a_red_devkit_pr_on_a_green_default_goes_to_its_own_branch():
    """devkit #387 added a MANIFEST entry that took sync-devkit.py past its recorded
    `file_lines`, with devkit's own default branch green. The fold sent it upstream: a
    fresh branch off that green default, where the failure does not reproduce and
    nothing can land on the PR -- and the structure baseline must stay tight, so no
    change there could pre-grant the room either. Only the PR's own diff can fix it,
    so it goes as itself beside the folded session, not inside it."""
    devkit_pr = failure(project="devkit", number=387)
    shared = [failure(project="a", number=1), failure(project="b", number=2)]
    decisions = [
        decision(fix_plan.DISPATCH, devkit_pr),
        decision(fix_plan.DISPATCH, shared[0]),
        decision(fix_plan.DISPATCH, shared[1]),
    ]
    classes = fix_cycle.classify_all([devkit_pr, *shared])
    go, held = fix_cycle.phase(decisions, classes, fix_cycle.harness_state(classes, True, []))
    assert [d.action for d in go] == [fix_plan.UPSTREAM, fix_plan.DISPATCH]
    assert sorted(f.project for f in go[0].failures) == ["a", "b"]
    assert go[1].failures == (devkit_pr,)
    assert held == []


def test_a_conflicted_harness_pr_gets_its_resolver_rather_than_the_devkit_session():
    """devkit #381 was a conflict, and the fold sent an upstream session at it: a fresh
    branch off the default, told to fix the harness, with no way to land on the PR at
    all. An action that names a branch operation on one PR cannot be folded into a
    session that has no branch."""
    conflicted = failure(project="a", number=381, signature=VENDORED)
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
    behind = failure(project="a", number=9, behind=True, signature=VENDORED)
    red = failure(project="a", number=10, signature=VENDORED)
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
    mixed = decision(
        fix_plan.DISPATCH, failure(number=1), failure(project="a", number=2, signature=VENDORED)
    )
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


# --- the budget leaks, and what closes them ------------------------------------------


def _conflict(sha: str) -> fix_plan.Decision:
    return decision(
        fix_plan.RESOLVE,
        failure(project="devkit", number=390, sha=sha, signature=(fix_plan.CONFLICT,)),
    )


def test_moved_on_counts_every_day_but_only_this_problem():
    now = _conflict("62a5")
    yesterday = (NOW - _dt.timedelta(days=1)).isoformat()
    assert not fix_ledger.moved_on(now, {}), "nothing sent is not movement"
    earlier = fix_ledger.decision_key(_conflict("143b"))
    assert fix_ledger.moved_on(now, {earlier: {"when": yesterday, "what": "n"}}), (
        "a conflict resolved yesterday and back today is the base moving"
    )
    update = earlier.replace(":resolve", ":update")
    assert not fix_ledger.moved_on(now, {update: {"when": NOW.isoformat(), "what": "n"}})
    folded = decision(
        fix_plan.RESOLVE,
        failure(project="devkit", number=390, sha="62a5", signature=(fix_plan.CONFLICT,)),
        failure(project="devkit", number=391, sha="62a5", signature=(fix_plan.CONFLICT,)),
    )
    assert not fix_ledger.moved_on(folded, {earlier: {"when": NOW.isoformat(), "what": "n"}})


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
    assert adopting.clean and adopting.adopting == ("carameli",)
    go, held = fix_cycle.phase([own, other], classes, adopting, prefixes)
    assert go == [own] and [d for d, _why in held] == [other]
    assert held[0][1] == "held until the newest release is adopted in carameli"

    red_too = fix_cycle.harness_state(classes, True, ["carameli"])
    go, held = fix_cycle.phase([own, vendored, other], classes, red_too, prefixes)
    assert [d.action for d in go] == [fix_plan.UPSTREAM]
    assert [d for d, _why in held] == [own, other]


def test_an_open_adoption_holds_only_its_own_projects_prs():
    """carameli #389 sat red for a day and held sports_betting, ibkr_trader, data-lake and
    devkit's own PRs with it, though no carameli adoption can fix any of them."""
    prefixes = ("agent/auto/devkit-upgrade-",)
    carameli = decision(fix_plan.DISPATCH, failure(number=390))
    elsewhere = decision(
        fix_plan.DISPATCH,
        failure(project="sports_betting", number=45, signature=("tests/test_s.py::t",)),
    )
    own_pr = decision(
        fix_plan.DISPATCH, failure(project="devkit", number=394, signature=("tests/test_d.py::t",))
    )
    listed = [*carameli.failures, *elsewhere.failures, *own_pr.failures]
    classes = fix_cycle.classify_all(listed)
    harness = fix_cycle.harness_state(classes, True, ["carameli"])
    go, held = fix_cycle.phase([carameli, elsewhere, own_pr], classes, harness, prefixes)
    assert go == [elsewhere, own_pr]
    assert [d for d, _why in held] == [carameli]


def test_a_devkit_pr_is_never_held_behind_the_harness():
    """A devkit PR is red on its own diff, and is often the harness fix in flight: held
    until the harness is clean, it is the one thing that could make it clean."""
    own_pr = decision(
        fix_plan.DISPATCH, failure(project="devkit", number=394, signature=("tests/test_d.py::t",))
    )
    project = decision(fix_plan.DISPATCH, failure(number=3))
    classes = fix_cycle.classify_all([*own_pr.failures, *project.failures])
    red = fix_cycle.harness_state({"k": fix_cycle.HARNESS}, True, [])
    go, held = fix_cycle.phase([own_pr, project], classes, red)
    assert go == [own_pr] and [d for d, _why in held] == [project]


def test_a_devkit_pr_is_one_whose_every_failure_is_a_devkit_pr():
    assert fix_cycle.is_devkit_pr(decision(fix_plan.DISPATCH, failure(project="devkit")))
    assert not fix_cycle.is_devkit_pr(decision(fix_plan.DISPATCH, failure()))
    main = failure(project="devkit", kind=fix_plan.BRANCH, number=0)
    assert not fix_cycle.is_devkit_pr(decision(fix_plan.DISPATCH, main)), "main is the harness"


def test_the_history_line_says_why_nothing_went():
    capped = (decision(fix_plan.DISPATCH, failure()), "2 session(s) sent -- needs a human")
    account = fix_cycle.Account(
        fix_cycle.DISPATCH,
        fix_cycle.harness_state({"k": fix_cycle.HARNESS}, True, ["carameli"]),
        capped=(capped,),
    )
    line = json.loads(fix_cycle.history_line(account, NOW))
    assert line["when"] == NOW.isoformat(timespec="seconds")
    assert line["harness"] == ["1 harness failure(s) open"]
    assert line["adopting"] == ["carameli"] and line["sent"] == []
    assert line["capped"] == ["carameli #412 -- 2 session(s) sent -- needs a human"]


def test_the_record_names_the_adoptions_still_open():
    harness = fix_cycle.harness_state({}, True, ["carameli"])
    text = fix_cycle.render(fix_cycle.Account(fix_cycle.DISPATCH, harness))
    assert "harness  clean" in text
    assert "adopting carameli -- the newest release" in text


def test_the_record_and_the_history_carry_the_release_line_only_when_there_is_one():
    harness = fix_cycle.harness_state({}, True, [])
    quiet = fix_cycle.Account(fix_cycle.DISPATCH, harness)
    assert "release" not in fix_cycle.render(quiet)
    assert json.loads(fix_cycle.history_line(quiet, NOW))["release"] == ""

    started = fix_cycle.Account(fix_cycle.DISPATCH, harness, release="started -- 5 change(s)")
    assert fix_cycle.render(started).splitlines()[-1] == "release  started -- 5 change(s)"
    assert json.loads(fix_cycle.history_line(started, NOW))["release"] == "started -- 5 change(s)"


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


def test_a_projects_own_test_under_scripts_hooks_is_the_projects():
    own = failure(
        project="carameli",
        signature=("scripts/hooks/tests/test_codex_hooks_contract.py::test_drop",),
    )
    assert fix_cycle.classify(own, set()) == fix_cycle.PROJECT


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


def test_when_planning_raises_only_the_backlog_goes_to_the_devkit_session():
    """The crash is on that backlog by then, so the devkit session is what fixes the plan."""
    backlog = failure(kind=fix_plan.LEDGER, project="devkit", number=0, head="", signature=("x",))
    harness, go, held, skipped = fix_cycle.only_the_harness(backlog)
    assert not harness.clean and "the plan step raised" in harness.reasons[0]
    assert [d.action for d in go] == [fix_plan.UPSTREAM] and go[0].failures == (backlog,)
    assert held == [] and skipped == []
    assert fix_cycle.only_the_harness(None)[1] == []


def test_a_devkit_commit_the_commit_stage_refused_is_fixed_in_its_own_tree():
    """The first supervised run folded a refused devkit commit into the upstream session,
    whose fresh branch cannot reach the tree; it spent seven calls repairing it from
    outside. It is the same shape as a devkit PR, and goes, and is not held."""
    refused = failure(
        project="devkit",
        kind=fix_plan.COMMIT,
        number=0,
        head="agent/x",
        signature=("commit refused: x",),
    )
    assert fix_cycle.classify(refused, set()) == fix_cycle.PROJECT
    assert fix_cycle.is_devkit_pr(decision(fix_plan.DISPATCH, refused))
    red = fix_cycle.harness_state({"k": fix_cycle.HARNESS}, True, [])
    go, held = fix_cycle.phase([decision(fix_plan.DISPATCH, refused)], {}, red)
    assert [d.action for d in go] == [fix_plan.DISPATCH] and held == []
