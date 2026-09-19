"""`scripts/broken_pr_menu.py`: what counts as broken, and the scan that finds it.

Split out of `tests/test_fix_prs.py` alongside the module itself, when `fix-prs.py`'s
`file_lines` was recorded a fifth time against the same never-cut seam. The division is
the one the two modules draw: everything here decides *what is red*, from the shapes
`gh` returns; everything left there *acts* -- reads one PR again, cuts the tree, opens
the session -- and the CLI tests stayed with it because the CLI did not move. The rows
a picker once drew from this scan are gone with the picker; `tests/test_fix_plan.py`
covers what replaced the ticking.

Every decision in the module is a pure function taking those shapes, so this suite
drives them directly and never a network. The two that spawn take a runner, and the
tests for them assert the argv rather than the effect.
"""

from __future__ import annotations

import datetime as _dt
import json
import subprocess

import pytest
from support import load_script

menu = load_script("scripts/broken_pr_menu.py")

NOW = _dt.datetime(2026, 9, 4, 12, 0, tzinfo=_dt.UTC)


def pr(**fields) -> dict:
    """An open, green, mergeable PR, overridden field by field."""
    base = {
        "number": 412,
        "title": "Teach the sweep about labels",
        "headRefName": "agent/sweep-labels-0904",
        "baseRefName": "main",
        "updatedAt": "2026-09-04T09:00:00Z",
        "url": "https://github.com/x/y/pull/412",
        "state": "OPEN",
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "statusCheckRollup": [{"conclusion": "SUCCESS"}],
    }
    base.update(fields)
    return base


# --- what counts as broken --------------------------------------------------------


def test_a_green_mergeable_pr_is_not_broken():
    assert menu.broken_reason(pr()) == ""


def test_a_conflicting_pr_is_broken():
    assert menu.broken_reason(pr(mergeable="CONFLICTING")) == "merge conflict"


@pytest.mark.parametrize("mergeable", ["UNKNOWN", None, "CONFLICTING"])
@pytest.mark.parametrize("rollup", [[], [{"conclusion": "SUCCESS"}]])
def test_a_dirty_merge_is_broken_without_failed_checks(mergeable, rollup):
    entry = pr(mergeable=mergeable, mergeStateStatus="DIRTY", statusCheckRollup=rollup)
    assert menu.broken_reason(entry) == "merge conflict"


@pytest.mark.parametrize("state", ["UNKNOWN", "BLOCKED", "BEHIND", "UNSTABLE", "CLEAN", None])
def test_other_merge_states_are_not_conflicts(state):
    assert menu.broken_reason(pr(mergeable="UNKNOWN", mergeStateStatus=state)) == ""


def test_a_failed_check_run_is_broken():
    entry = pr(statusCheckRollup=[{"conclusion": "SUCCESS"}, {"conclusion": "FAILURE"}])
    assert menu.broken_reason(entry) == "1 check failing"


def test_a_failed_legacy_status_context_counts_too():
    """One rollup mixes both shapes, and only the check-run half carries `conclusion`."""
    entry = pr(statusCheckRollup=[{"state": "ERROR"}, {"state": "SUCCESS"}])
    assert menu.broken_reason(entry) == "1 check failing"


@pytest.mark.parametrize("mergeable", ["CONFLICTING", "UNKNOWN"])
def test_both_kinds_of_broken_are_reported_together(mergeable):
    entry = pr(
        mergeable=mergeable,
        mergeStateStatus="DIRTY",
        statusCheckRollup=[{"conclusion": "FAILURE"}, {"conclusion": "TIMED_OUT"}],
    )
    assert menu.broken_reason(entry) == "merge conflict + 2 checks failing"


def test_a_pending_gate_is_not_a_failure():
    """A run in flight is the normal state seconds after a push; a menu that called it
    broken would offer every PR on the machine."""
    assert menu.broken_reason(pr(statusCheckRollup=[{"conclusion": None}])) == ""


@pytest.mark.parametrize("conclusion", ["SKIPPED", "NEUTRAL", "SUCCESS"])
def test_a_check_that_did_not_apply_is_not_a_failure(conclusion):
    assert menu.broken_reason(pr(statusCheckRollup=[{"conclusion": conclusion}])) == ""


def test_unknown_mergeability_is_not_a_conflict():
    """GitHub reports UNKNOWN while the job is still running, which every fresh PR is."""
    assert menu.broken_reason(pr(mergeable="UNKNOWN")) == ""


def test_a_draft_is_never_broken_however_red_it_is():
    """A draft is not asking to be merged, so an agent sent at it has no finish line."""
    entry = pr(isDraft=True, mergeable="CONFLICTING", statusCheckRollup=[{"conclusion": "FAILURE"}])
    assert menu.broken_reason(entry) == ""


@pytest.mark.parametrize("rollup", [None, "FAILURE", 7, [None, "x", {"conclusion": 3}]])
def test_a_rollup_shape_this_does_not_know_counts_as_zero(rollup):
    """Total rather than raising: this decides whether a row appears in a dropdown, and a
    menu that could not be built is worse than a row that is merely wrong."""
    assert menu.failing_checks(rollup) == 0


# --- the source ------------------------------------------------------------------


def gh_returning(code: int, out: str):
    def gh_for(_path):
        def gh(*_args):
            return subprocess.CompletedProcess([], code, out, "")

        return gh

    return gh_for


def test_only_the_broken_ones_are_listed(monkeypatch, tmp_path):
    payload = json.dumps([pr(number=1), pr(number=2, mergeable="CONFLICTING")])
    monkeypatch.setattr(menu.sweep, "gh_for", gh_returning(0, payload))
    assert [entry["number"] for entry in menu.broken_prs(tmp_path)] == [2]


@pytest.mark.parametrize("mergeable", ["UNKNOWN", None])
@pytest.mark.parametrize(
    "fresh,reason,asks",
    [
        ({"mergeable": "CONFLICTING"}, "merge conflict", 1),
        ({"mergeable": "UNKNOWN", "mergeStateStatus": "DIRTY"}, "merge conflict", 1),
        ({"mergeable": "MERGEABLE"}, "", 1),
        # The answer that is not one. Asked again to the budget and then left as the list
        # had it, which reads as clean -- bounded, rather than a poll a person waits out.
        ({"mergeable": "UNKNOWN"}, "", menu.mergeability.ASKS),
        # Both of these settle the row on the first ask, so neither spends the budget:
        # a closed PR is one GitHub will never judge, and a draft is never a row.
        ({"mergeable": "CONFLICTING", "state": "CLOSED"}, "", 1),
        ({"mergeable": "CONFLICTING", "isDraft": True}, "", 1),
    ],
)
def test_the_scan_asks_again_about_a_verdict_that_has_not_arrived(
    monkeypatch, tmp_path, mergeable, fresh, reason, asks
):
    calls = []

    def gh(*args):
        calls.append(args[:3])
        asked = args[args.index("--json") + 1].split(",")
        entry = pr(mergeable=mergeable, statusCheckRollup=[])
        if args[1] == "view":
            entry.update(fresh)
        payload = {key: value for key, value in entry.items() if key in asked}
        return subprocess.CompletedProcess(
            [], 0, json.dumps([payload] if args[1] == "list" else payload), ""
        )

    monkeypatch.setattr(menu.sweep, "gh_for", lambda _path: gh)
    monkeypatch.setattr(menu.mergeability, "WAIT", 0)
    found = menu.broken_prs(tmp_path)
    assert calls == [("pr", "list", "--state"), *[("pr", "view", "412")] * asks]
    assert [menu.broken_reason(entry) for entry in found] == ([reason] if reason else [])
    if found:
        assert found[0]["updatedAt"] == pr()["updatedAt"]


def test_refresh_failure_keeps_known_check_failures(monkeypatch, tmp_path):
    entry = pr(mergeable="UNKNOWN", statusCheckRollup=[{"conclusion": "FAILURE"}])
    asked = []
    monkeypatch.setattr(menu.sweep, "gh_for", gh_returning(0, json.dumps([entry])))
    monkeypatch.setattr(menu, "pr_view", lambda *_args: asked.append(1) or {})
    monkeypatch.setattr(menu.mergeability, "WAIT", 0)
    assert menu.broken_prs(tmp_path) == [entry]
    # A view that answers nothing is indistinguishable from one that answers `UNKNOWN`,
    # so it costs the same budget and no more.
    assert len(asked) == menu.mergeability.ASKS


def test_settled_conflicts_and_drafts_need_no_refresh(monkeypatch, tmp_path):
    entries = [
        pr(mergeable="CONFLICTING", statusCheckRollup=[]),
        pr(isDraft=True, mergeable="UNKNOWN"),
    ]
    monkeypatch.setattr(menu.sweep, "gh_for", gh_returning(0, json.dumps(entries)))
    monkeypatch.setattr(menu, "pr_view", lambda *_args: pytest.fail("unnecessary refresh"))
    assert menu.broken_prs(tmp_path) == entries[:1]


@pytest.mark.parametrize("fields", [menu.PR_LIST_FIELDS, menu.PR_VIEW_FIELDS])
def test_both_queries_request_both_conflict_signals(fields):
    assert {"mergeable", "mergeStateStatus"} <= set(fields.split(","))


@pytest.mark.parametrize(
    "code,out", [(1, ""), (0, "not json"), (0, json.dumps({"message": "Bad credentials"}))]
)
def test_a_gh_failure_loses_the_rows_and_keeps_the_menu(monkeypatch, tmp_path, code, out):
    """An offline or unauthenticated machine must not fail the reconcile pass that calls
    this; it loses the rows, and the next pass writes them again."""
    monkeypatch.setattr(menu.sweep, "gh_for", gh_returning(code, out))
    assert menu.broken_prs(tmp_path) == []


def test_the_pr_is_re_read_live_rather_than_trusted_to_the_menu(monkeypatch, tmp_path):
    """The dropdown can be a quarter of an hour old, so what the agent is told about a PR
    comes from here and not from the row that was clicked."""
    monkeypatch.setattr(menu.sweep, "gh_for", gh_returning(0, json.dumps(pr(number=9))))
    assert menu.pr_view(tmp_path, 9)["number"] == 9


@pytest.mark.parametrize("code,out", [(1, ""), (0, "not json"), (0, "[]")])
def test_a_pr_view_that_cannot_be_read_is_empty_rather_than_a_traceback(
    monkeypatch, tmp_path, code, out
):
    """`[]` is in here because `gh` returning the wrong SHAPE must land in the same place
    as `gh` failing: `run_one` branches on emptiness, and a list would reach `.get`."""
    monkeypatch.setattr(menu.sweep, "gh_for", gh_returning(code, out))
    assert menu.pr_view(tmp_path, 9) == {}


def test_the_scan_covers_the_registry_not_just_the_stack_projects(monkeypatch, tmp_path):
    workspace = tmp_path / "alex.code-workspace"
    workspace.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(menu.devkit_project, "known_projects", lambda _text: ["a", "b"])
    monkeypatch.setattr(menu, "broken_prs", lambda _dir: [])
    assert sorted(menu.scan(workspace)) == ["a", "b"]


def test_the_scan_asks_every_checkout_at_once_and_keeps_the_answers_paired(monkeypatch, tmp_path):
    """A person is waiting on this now, so the calls run concurrently -- and a pool hands
    results back in completion order, which would pair the wrong PRs with the wrong
    checkout if the mapping were built from that."""
    workspace = tmp_path / "alex.code-workspace"
    workspace.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(menu.devkit_project, "known_projects", lambda _text: ["slow", "fast"])
    monkeypatch.setattr(
        menu,
        "broken_prs",
        lambda project_dir: [pr(number=1 if project_dir.name == "slow" else 2)],
    )
    found = menu.scan(workspace)
    assert found["slow"][0]["number"] == 1
    assert found["fast"][0]["number"] == 2


def test_an_empty_registry_scans_to_nothing_rather_than_an_empty_pool(tmp_path):
    """`ThreadPoolExecutor` refuses `max_workers=0`, so a workspace with no checkouts has
    to be answered before the pool is built."""
    workspace = tmp_path / "alex.code-workspace"
    workspace.write_text("{}", encoding="utf-8")
    assert menu.scan(workspace, projects=[]) == {}


def test_a_pick_is_a_checkout_and_a_number():
    assert menu.parse_pick("carameli:412") == menu.Pick("carameli", 412)


@pytest.mark.parametrize("token", ["carameli", "", ":412", "carameli:head", "none"])
def test_a_token_that_is_not_a_pick_is_refused(token):
    """A malformed pick is a typo in a hand-written argument; running the rest of the
    batch while dropping one is how a PR looks looked-at and was not. `none` is here
    because the sentinel row that once meant it is gone with the picker."""
    with pytest.raises(menu.FixError):
        menu.parse_pick(token)


def test_the_scan_settles_with_this_checkouts_own_gh(monkeypatch, tmp_path):
    """`pr_mergeability` owns the asking and is tested on its own; what is left here is
    the binding -- that `settle_mergeability` hands it this checkout's `gh pr view`."""
    asked = []
    monkeypatch.setattr(
        menu, "pr_view", lambda directory, number: asked.append((directory, number)) or {}
    )
    monkeypatch.setattr(menu.mergeability, "WAIT", 0)
    menu.settle_mergeability(tmp_path, [pr(mergeable="UNKNOWN")])
    assert asked == [(tmp_path, 412)] * menu.mergeability.ASKS


def test_ticked_rows_split_on_the_space_and_de_duplicate():
    assert menu.split_picks("a:1 b:2 a:1") == ["a:1", "b:2"]


def test_nothing_ticked_splits_to_nothing():
    assert menu.split_picks("") == []
