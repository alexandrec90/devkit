"""`scripts/fix_verify.py`: a harness-ledger resolution is held to the fix it names."""

from __future__ import annotations

import datetime as _dt
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fix_verify
import harness_triage as triage

NOW = _dt.datetime(2026, 9, 26, 12, 0, tzinfo=_dt.UTC)


def resolution(pr: str, days_ago: float = 3, ref: str = "aaaabbbb") -> str:
    stamp = (NOW - _dt.timedelta(days=days_ago)).isoformat(timespec="seconds")
    return f"{stamp}\tevent=triage-resolved\tref={ref}\tpr={pr}\tnote=fixed"


def items(*lines: str) -> list[triage.Item]:
    return triage.read_items("\n".join(lines))


def test_the_pr_field_names_a_number_a_url_a_branch_or_nothing():
    assert fix_verify.target("#402") == ("devkit", "402")
    assert fix_verify.target("402") == ("devkit", "402")
    assert fix_verify.target("https://github.com/o/carameli/pull/12") == ("carameli", "12")
    assert fix_verify.target("agent/fix-harness-ledger-0919") == (
        "",
        "agent/fix-harness-ledger-0919",
    )
    assert fix_verify.target("-") == ("", "")
    assert fix_verify.target("see the note") == ("", "")


def test_a_merged_fix_stands_and_is_never_asked_about_again(tmp_path):
    asked = []
    lookup = lambda where, what: asked.append(what) or [fix_verify.LANDED]
    cache = tmp_path / "v.json"
    assert fix_verify.verify(items(resolution("agent/x")), lookup, cache, NOW) == []
    assert fix_verify.verify(items(resolution("agent/x")), lookup, cache, NOW) == []
    assert asked == ["agent/x"]


def test_a_fix_closed_unmerged_is_reopened(tmp_path):
    [(ref, why)] = fix_verify.verify(
        items(resolution("#7")), lambda *_: [fix_verify.CLOSED], tmp_path / "v.json", NOW
    )
    assert ref == "aaaabbbb" and "closed without merging" in why


def test_a_branch_no_pr_ever_came_from_is_reopened_after_the_grace_only(tmp_path):
    cache = tmp_path / "v.json"
    none = lambda *_: []
    assert fix_verify.verify(items(resolution("agent/x", days_ago=1)), none, cache, NOW) == []
    [(_, why)] = fix_verify.verify(items(resolution("agent/x", days_ago=3)), none, cache, NOW)
    assert why.startswith("no PR was ever opened from agent/x")


def test_an_open_pr_is_in_flight_and_left_alone(tmp_path):
    assert (
        fix_verify.verify(
            items(resolution("agent/x", 10)), lambda *_: [fix_verify.OPEN], tmp_path / "v", NOW
        )
        == []
    )


def test_a_fix_that_has_not_landed_is_in_flight_until_verify_settles_it(tmp_path):
    """d677ea57's other half: what `harness_triage.pending_groups` needs -- every standing
    recent resolution naming a PR or branch that `verify` has not cached as merged."""
    cache = tmp_path / "v.json"
    lines = items(resolution("410", ref="11111111"), resolution("-", ref="22222222"))
    assert fix_verify.in_flight(lines, cache, NOW) == {"11111111": "410"}
    fix_verify.verify(lines, lambda *_: [fix_verify.LANDED], cache, NOW)
    assert fix_verify.in_flight(lines, cache, NOW) == {}


def test_only_standing_recent_resolutions_that_name_something_are_checked():
    old = resolution("agent/x", days_ago=30, ref="11111111")
    blank = resolution("-", ref="22222222")
    undone = resolution("agent/y", ref="33333333")
    stamp = NOW.isoformat(timespec="seconds")
    reopened = f"{stamp}\tevent=triage-reopened\tref=33333333\tnote=x"
    assert fix_verify.recent(items(old, blank, undone, reopened), NOW) == []


def test_the_lookup_asks_gh_by_number_in_one_project_and_by_branch_in_all(tmp_path):
    for name in ("devkit", "carameli"):
        (tmp_path / name).mkdir()
    calls = []

    def gh_for(project_dir):
        def gh(*args):
            calls.append((project_dir.name, args[:2]))
            payload = {"state": "MERGED"} if args[1] == "view" else [{"state": "CLOSED"}]
            return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")

        return gh

    lookup = fix_verify.gh_lookup(tmp_path, ["devkit", "carameli", "gone"], gh_for)
    assert lookup("devkit", "402") == ["MERGED"]
    assert lookup("", "agent/x") == ["CLOSED", "CLOSED"]
    assert calls == [
        ("devkit", ("pr", "view")),
        ("devkit", ("pr", "list")),
        ("carameli", ("pr", "list")),
    ]


def test_a_gh_that_cannot_answer_answers_nothing(tmp_path):
    (tmp_path / "devkit").mkdir()
    failing = lambda _d: lambda *a: subprocess.CompletedProcess(a, 1, "", "HTTP 401")
    assert fix_verify.gh_lookup(tmp_path, ["devkit"], failing)("devkit", "1") == []


def test_judge_is_landed_open_closed_or_unlanded():
    fresh = fix_verify.Resolution("r", (NOW - _dt.timedelta(hours=1)).isoformat(), "agent/x", "n")
    old = fix_verify.Resolution("r", (NOW - _dt.timedelta(days=3)).isoformat(), "agent/x", "n")
    assert fix_verify.judge(fresh, [fix_verify.CLOSED, fix_verify.LANDED], NOW) == fix_verify.LANDED
    assert fix_verify.judge(old, [fix_verify.OPEN], NOW) == ""
    assert "closed without merging" in fix_verify.judge(fresh, [fix_verify.CLOSED], NOW)
    assert fix_verify.judge(fresh, [], NOW) == ""
    assert fix_verify.judge(old, [], NOW).startswith("no PR was ever opened")
