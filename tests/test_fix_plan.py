"""`scripts/fix_plan.py`: the decision behind the one-question fix task.

Every function is pure over the shapes `gh` returns, so nothing here touches a network
or a subprocess. The plan's four rules -- one PR gets one session, a vendored failure
shared across consumers goes to devkit once, a release PR and a superseded adoption get
nothing -- each have the case that motivated them, and the ledger has the second click.
"""

from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path
from typing import Any

import pytest
from support import load_script

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fix_plan

PREFIXES = ("agent/auto/devkit-upgrade-", "agent/devkit-upgrade-")
VENDORED_SIG = (
    "scripts/hooks/tests/test_untested_symbols.py::test_every_public_symbol_is_named_by_a_test",
)
NOW = _dt.datetime(2026, 9, 18, 12, 0, tzinfo=_dt.UTC)

SUMMARY = """\
=========================== short test summary info ============================
FAILED scripts/hooks/tests/test_untested_symbols.py::test_every_public_symbol_is_named_by_a_test - AssertionError: write a test naming these
FAILED tests/test_x.py::test_y - assert 1 == 2
FAILED tests/test_x.py::test_y - assert 1 == 2
"""

LINT = """\
scripts/lint-all.py:12:1: F401 `os` imported but unused
scripts/other.py:40: error: Incompatible return value type
"""


def failure(**fields) -> fix_plan.Failure:
    base: dict[str, Any] = {
        "kind": fix_plan.PR,
        "project": "carameli",
        "number": 412,
        "title": "Adopt devkit v0.11.21 (10 vendored file(s))",
        "url": "https://github.com/x/carameli/pull/412",
        "head": "agent/auto/devkit-upgrade-v0-11-21-0917",
        "base": "main",
        "sha": "abc123",
        "reason": "1 check failing",
        "signature": (
            "scripts/hooks/tests/test_untested_symbols.py::test_every_public_symbol_is_named_by_a_test",
        ),
    }
    base.update(fields)
    return fix_plan.Failure(**base)


# --- the signature ------------------------------------------------------------------


def test_the_signature_is_the_failed_ids_sorted_and_deduped():
    assert fix_plan.signature_from_logs([SUMMARY]) == (
        "scripts/hooks/tests/test_untested_symbols.py::test_every_public_symbol_is_named_by_a_test",
        "tests/test_x.py::test_y",
    )


def test_a_lint_finding_is_keyed_by_file_not_by_line():
    """The line moves with every edit; the file is what two runs share."""
    assert fix_plan.signature_from_logs([LINT]) == (
        "lint scripts/lint-all.py",
        "lint scripts/other.py",
    )


def test_the_message_after_the_id_is_not_part_of_the_signature():
    """It carries a value or a line number that differs between two runs of the same
    failure, and a signature that changed every run would never match its own PR."""
    one = fix_plan.signature_from_logs(["FAILED tests/t.py::a - assert 1 == 2"])
    two = fix_plan.signature_from_logs(["FAILED tests/t.py::a - assert 3 == 4"])
    assert one == two == ("tests/t.py::a",)


def test_without_an_artifact_the_failed_steps_are_the_signature():
    jobs = [
        {
            "name": "Tests",
            "conclusion": "failure",
            "steps": [
                {"name": "Set up", "conclusion": "success"},
                {"name": "Application suite", "conclusion": "failure"},
            ],
        },
        {"name": "Lint", "conclusion": "success", "steps": []},
        {"name": "Drift", "conclusion": "failure", "steps": []},
    ]
    assert fix_plan.signature_from_jobs(jobs) == ("Drift", "Tests / Application suite")


def test_the_artifact_wins_over_the_steps_and_a_conflict_comes_first():
    jobs = [{"name": "Tests", "conclusion": "failure", "steps": []}]
    assert fix_plan.signature(True, [SUMMARY], jobs) == (
        fix_plan.CONFLICT,
        "scripts/hooks/tests/test_untested_symbols.py::test_every_public_symbol_is_named_by_a_test",
        "tests/test_x.py::test_y",
    )
    assert fix_plan.signature(False, [], jobs) == ("Tests",)
    assert fix_plan.signature(True, [], []) == (fix_plan.CONFLICT,)


def test_a_signature_is_vendored_only_when_every_id_is():
    assert fix_plan.is_vendored(("scripts/hooks/tests/test_a.py::t",))
    assert not fix_plan.is_vendored(("scripts/hooks/tests/test_a.py::t", "tests/test_b.py::u"))
    assert not fix_plan.is_vendored(())


# --- the two shapes that never get an agent -------------------------------------------


def test_a_release_branch_is_recognised():
    assert fix_plan.is_release("release/v0.11.22")
    assert not fix_plan.is_release("agent/release-notes-0918")


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        ("agent/auto/devkit-upgrade-v0-11-21-0917", "v0-11-21"),
        ("agent/auto/devkit-upgrade-v0-11-21-0917-2", "v0-11-21"),
        ("agent/devkit-upgrade-v0-10-2-0816", "v0-10-2"),
        ("agent/sweep-labels-0904", ""),
        ("agent/auto/devkit-upgrade-", ""),
    ],
)
def test_the_adopted_tag_is_read_off_the_branch(head, expected):
    """The date suffix and the same-day counter are `worktree.plan_new`'s, and neither
    may be mistaken for part of the version."""
    assert fix_plan.adoption_tag(head, PREFIXES) == expected


# --- the plan -----------------------------------------------------------------------


def actions(decisions):
    return [(d.action, [f"{f.project}#{f.number}" for f in d.failures]) for d in decisions]


def test_one_vendored_failure_across_consumers_is_one_decision_for_devkit():
    """The v0.11.21 fan-out: three consumers red on the same vendored test are one
    devkit defect, and the eight sessions the per-PR dropdown cost are what this rule
    refuses to spend again."""
    red = [
        failure(project="carameli", number=412),
        failure(project="roguelike", number=16),
        failure(project="ibkr_trader", number=88),
    ]
    decisions = fix_plan.plan(red, "v0-11-21", PREFIXES)
    assert actions(decisions) == [
        (fix_plan.UPSTREAM, ["carameli#412", "ibkr_trader#88", "roguelike#16"])
    ]
    assert "3 projects" in decisions[0].note


def test_the_same_vendored_failure_in_one_project_is_that_projects_own():
    """One consumer red on a vendored test may be that consumer's baseline, and sending
    it to devkit would be guessing. Two is the evidence."""
    decisions = fix_plan.plan([failure()], "v0-11-21", PREFIXES)
    assert actions(decisions) == [(fix_plan.DISPATCH, ["carameli#412"])]


def test_a_shared_signature_that_is_not_vendored_is_still_one_per_project():
    """Two projects failing their own `tests/` on the same id are two projects."""
    red = [
        failure(project="a", number=1, signature=("tests/test_x.py::t",)),
        failure(project="b", number=2, signature=("tests/test_x.py::t",)),
    ]
    assert actions(fix_plan.plan(red, "v0-11-21", PREFIXES)) == [
        (fix_plan.DISPATCH, ["a#1"]),
        (fix_plan.DISPATCH, ["b#2"]),
    ]


def test_a_release_pr_is_skipped_and_says_why():
    """Red by construction: `test_fallback_devkit_ref_tracks_the_newest_tag` fails until
    the tag exists, and an agent sent at it can only fail or force."""
    red = [failure(head="release/v0.11.22", signature=("tests/test_new_project.py::t",))]
    (decision,) = fix_plan.plan(red, "v0-11-21", PREFIXES)
    assert decision.action == fix_plan.SKIP
    assert "red by construction" in decision.note


def test_an_adoption_of_an_older_release_is_skipped_as_superseded():
    red = [failure(head="agent/auto/devkit-upgrade-v0-11-20-0916")]
    (decision,) = fix_plan.plan(red, "v0-11-21", PREFIXES)
    assert decision.action == fix_plan.SKIP
    assert "superseded" in decision.note and "v0-11-21" in decision.note


def test_an_unknown_newest_release_calls_nothing_superseded():
    """`latest_tag` is empty when git cannot say, and that must not silently skip every
    adoption on the machine."""
    red = [failure(head="agent/auto/devkit-upgrade-v0-11-20-0916")]
    (decision,) = fix_plan.plan(red, "", PREFIXES)
    assert decision.action == fix_plan.DISPATCH


def test_a_nightly_is_never_read_as_a_release_or_an_adoption():
    red = [
        failure(kind=fix_plan.NIGHTLY, head="", workflow="Nightly", number=7, sha="", run_id="99")
    ]
    (decision,) = fix_plan.plan(red, "v0-11-21", PREFIXES)
    assert decision.action == fix_plan.DISPATCH


def test_the_plan_is_in_a_stable_order():
    red = [
        failure(project="b", number=2, signature=("t",)),
        failure(project="a", number=9, signature=("u",)),
    ]
    assert actions(fix_plan.plan(red, "", PREFIXES)) == [
        (fix_plan.DISPATCH, ["a#9"]),
        (fix_plan.DISPATCH, ["b#2"]),
    ]


def test_describe_names_the_reason_and_the_first_few_ids():
    many = failure(signature=tuple(f"tests/t.py::t{i}" for i in range(6)))
    text = fix_plan.describe(many)
    assert text.startswith("1 check failing: tests/t.py::t0")
    assert "(+2 more)" in text
    bare = failure(signature=())
    assert "no artifact" in fix_plan.describe(bare) and bare.url in fix_plan.describe(bare)
    nightly = failure(kind=fix_plan.NIGHTLY, workflow="Nightly", signature=("tests/t.py::a",))
    assert fix_plan.describe(nightly).startswith("Nightly workflow failing on origin/main")


# --- the ledger ---------------------------------------------------------------------


def test_the_key_names_the_failure_at_the_commit_it_was_seen_on():
    """A fix that pushed a new sha and is still red is a new key: worth a second look.
    The same sha under the same signature is the same dispatch."""
    same = fix_plan.failure_key(failure())
    assert same == fix_plan.failure_key(failure(title="renamed"))
    assert same != fix_plan.failure_key(failure(sha="def456"))
    assert same != fix_plan.failure_key(failure(signature=("tests/t.py::other",)))
    assert same.startswith("pr:carameli:412:abc123:")


def test_a_nightly_is_keyed_by_its_run():
    nightly = failure(kind=fix_plan.NIGHTLY, sha="", run_id="4242", number=7)
    assert fix_plan.failure_key(nightly).startswith("nightly:carameli:7:4242:")


def test_an_upstream_decision_is_one_key_for_the_group():
    group = (failure(project="a", number=1), failure(project="b", number=2))
    decision = fix_plan.Decision(fix_plan.UPSTREAM, "n", group)
    key = fix_plan.decision_key(decision)
    assert key.startswith("upstream:2:")
    reordered = fix_plan.Decision(fix_plan.UPSTREAM, "n", group[::-1])
    assert fix_plan.decision_key(reordered) == key
    repushed = fix_plan.Decision(
        fix_plan.UPSTREAM, "n", (group[0], failure(project="b", number=2, sha="new"))
    )
    assert fix_plan.decision_key(repushed) != key


def test_a_single_decision_is_keyed_as_its_failure():
    decision = fix_plan.Decision(fix_plan.DISPATCH, "n", (failure(),))
    assert fix_plan.decision_key(decision) == fix_plan.failure_key(failure())


def test_the_ledger_records_and_answers_the_second_click(tmp_path):
    path = tmp_path / "boxes" / "dispatch.json"
    decision = fix_plan.Decision(fix_plan.DISPATCH, "1 check failing", (failure(),))
    assert fix_plan.already_sent(decision, fix_plan.read_ledger(path)) == ""
    fix_plan.record(path, fix_plan.decision_key(decision), decision.note, NOW)
    ledger = fix_plan.read_ledger(path)
    assert fix_plan.already_sent(decision, ledger) == "2026-09-18T12:00:00+00:00"
    assert ledger[fix_plan.decision_key(decision)]["what"] == "1 check failing"


def test_a_corrupt_ledger_is_empty_rather_than_a_traceback(tmp_path):
    path = tmp_path / "dispatch.json"
    path.write_text("{not json", encoding="utf-8")
    assert fix_plan.read_ledger(path) == {}
    path.write_text("[1, 2]", encoding="utf-8")
    assert fix_plan.read_ledger(path) == {}


def test_a_conflict_is_its_own_decision_and_never_grouped_upstream():
    red = [
        failure(project="a", number=1, signature=(fix_plan.CONFLICT, *VENDORED_SIG)),
        failure(project="b", number=2, signature=VENDORED_SIG),
    ]
    decisions = fix_plan.plan(red, "v0-11-21", PREFIXES)
    assert actions(decisions) == [
        (fix_plan.RESOLVE, ["a#1"]),
        (fix_plan.DISPATCH, ["b#2"]),
    ]


# --- the report ---------------------------------------------------------------------


def test_the_report_says_what_will_be_sent_what_was_and_what_is_skipped(tmp_path):
    sent = fix_plan.Decision(fix_plan.DISPATCH, "1 check failing: t", (failure(number=1),))
    fresh = fix_plan.Decision(fix_plan.UPSTREAM, "one vendored failure", (failure(number=2),))
    skipped = fix_plan.Decision(fix_plan.SKIP, "red by construction", (failure(number=3),))
    path = tmp_path / "dispatch.json"
    fix_plan.record(path, fix_plan.decision_key(sent), sent.note, NOW)
    text = fix_plan.render([sent, fresh, skipped], fix_plan.read_ledger(path))
    lines = text.splitlines()
    assert lines[0].startswith("sent     carameli #1 -- already dispatched at 2026-09-18")
    assert lines[1].startswith("upstream carameli #2 -- one vendored failure")
    assert lines[2].startswith("skip     carameli #3 -- red by construction")


def test_an_empty_plan_says_so():
    assert fix_plan.render([], {}) == "nothing is red"


# --- a red default branch -----------------------------------------------------------


def red_main(**fields) -> fix_plan.Failure:
    base: dict[str, Any] = {
        "kind": fix_plan.BRANCH,
        "project": "devkit",
        "number": 0,
        "head": "",
        "base": "main",
        "sha": "fb17a31",
        "run_id": "35471200282",
        "workflow": "PR Gate",
        "url": "u/run",
        "reason": "",
        "signature": ("tests/test_new_project.py::" + fix_plan.RELEASE_TEST,),
    }
    base.update(fields)
    return failure(**base)


def test_a_pr_behind_its_base_is_updated_not_fixed_unless_it_conflicts():
    """#379 was red on a pip-audit finding master had already fixed, and the session
    sent at it did nothing but merge master in. A conflict still goes to the resolver:
    GitHub cannot update a conflicted branch."""
    behind = failure(number=1, head="agent/a", signature=("tests/t.py::a",), behind=True)
    conflicted = failure(number=2, head="agent/b", signature=(fix_plan.CONFLICT,), behind=True)
    plain = failure(number=3, head="agent/c", signature=("tests/t.py::a",))
    decisions = fix_plan.plan([behind, conflicted, plain], "v0-11-23", PREFIXES)
    assert actions(decisions) == [
        (fix_plan.UPDATE, ["carameli#1"]),
        (fix_plan.RESOLVE, ["carameli#2"]),
        (fix_plan.DISPATCH, ["carameli#3"]),
    ]
    assert decisions[0].note.endswith("behind origin/main")
    assert "sent" not in fix_plan.render(decisions, {}).splitlines()[0]
    assert fix_plan.render(decisions, {}).splitlines()[0].startswith("update   carameli #1")


def test_a_failure_is_named_by_its_pr_its_branch_or_its_default_branch():
    assert fix_plan.name_of(failure()) == "#412"
    assert fix_plan.name_of(failure(kind=fix_plan.NIGHTLY, number=7)) == "#7"
    assert fix_plan.name_of(failure(kind=fix_plan.COMMIT, number=0, head="agent/i")) == "agent/i"
    assert fix_plan.name_of(red_main()) == "origin/main"
    backlog = red_main(kind=fix_plan.LEDGER, signature=("agent-report devkit [a] x2", "b"))
    assert fix_plan.name_of(backlog) == "ledger"
    assert fix_plan.describe(backlog) == (
        "2 open group(s) on the harness-defect ledger: agent-report devkit [a] x2, b"
    )


def test_the_release_test_is_the_one_the_pipeline_expects_red():
    """One spelling, in two files: the pipeline judges a release PR by it, and the plan
    reads a red default branch by it."""
    pipeline = load_script("scripts/release-pipeline.py")
    assert fix_plan.RELEASE_TEST == pipeline.EXPECTED_RED_TEST


def test_a_release_commits_red_is_skipped_out_loud_and_any_other_red_main_is_sent():
    assert fix_plan.is_release_red(("tests/test_new_project.py::" + fix_plan.RELEASE_TEST,))
    assert not fix_plan.is_release_red(())
    assert not fix_plan.is_release_red((fix_plan.RELEASE_TEST, "tests/t.py::other"))
    decisions = fix_plan.plan(
        [red_main(), red_main(project="carameli", signature=("tests/t.py::other",))],
        "v0-11-23",
        PREFIXES,
    )
    assert actions(decisions) == [
        (fix_plan.SKIP, ["devkit#0"]),
        (fix_plan.DISPATCH, ["carameli#0"]),
    ]
    assert "red by construction" in decisions[0].note and "tag exists" in decisions[0].note
    assert fix_plan.skip_reason(decisions[0].failures[0], "v0-11-23", PREFIXES) == (
        decisions[0].note
    )
    assert fix_plan.skip_reason(decisions[1].failures[0], "v0-11-23", PREFIXES) == ""
    assert fix_plan.describe(decisions[1].failures[0]).startswith(
        "PR Gate workflow failing on origin/main: tests/t.py::other"
    )
