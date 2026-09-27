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


def at(hours_ago: float) -> str:
    return (NOW - _dt.timedelta(hours=hours_ago)).isoformat(timespec="seconds")


MERGED = fix_verify.Pr(fix_verify.LANDED)
OPEN = fix_verify.Pr(fix_verify.OPEN)
CLOSED = fix_verify.Pr(fix_verify.CLOSED)


def finding(hours_ago: float, detail: str = "installer-failed: an installer failed") -> str:
    return f"{at(hours_ago)}\tevent=fix-pass-finding\tproject=devkit\tdetail={detail}"


def test_a_merged_fix_stands_and_is_never_asked_about_again(tmp_path):
    asked = []
    lookup = lambda where, what: asked.append(what) or [MERGED]
    cache = tmp_path / "v.json"
    assert fix_verify.verify(items(resolution("agent/x")), lookup, cache, NOW).reopen == []
    assert fix_verify.verify(items(resolution("agent/x")), lookup, cache, NOW).reopen == []
    assert asked == ["agent/x"]


def test_a_fix_closed_unmerged_is_reopened(tmp_path):
    [(ref, why)] = fix_verify.verify(
        items(resolution("#7")), lambda *_: [CLOSED], tmp_path / "v.json", NOW
    ).reopen
    assert ref == "aaaabbbb" and "closed without merging" in why


def test_a_branch_no_pr_ever_came_from_is_reopened_after_the_grace_only(tmp_path):
    cache = tmp_path / "v.json"
    none = lambda *_: []
    fresh = fix_verify.verify(items(resolution("agent/x", days_ago=1)), none, cache, NOW)
    assert fresh.reopen == []
    [(_, why)] = fix_verify.verify(
        items(resolution("agent/x", days_ago=3)), none, cache, NOW
    ).reopen
    assert why.startswith("no PR from agent/x opened or merged after this resolution")


def test_an_open_pr_is_in_flight_and_left_alone(tmp_path):
    outcome = fix_verify.verify(
        items(resolution("agent/x", 10)), lambda *_: [OPEN], tmp_path / "v", NOW
    )
    assert outcome == fix_verify.Outcome()


def test_a_fix_that_has_not_landed_is_in_flight_until_verify_settles_it(tmp_path):
    """d677ea57's other half: what `harness_triage.pending_groups` needs -- every standing
    recent resolution naming a PR or branch that `verify` has not cached as merged."""
    cache = tmp_path / "v.json"
    lines = items(resolution("410", ref="11111111"), resolution("-", ref="22222222"))
    assert fix_verify.in_flight(lines, cache, NOW) == {"11111111": "410"}
    fix_verify.verify(lines, lambda *_: [MERGED], cache, NOW)
    assert fix_verify.in_flight(lines, cache, NOW) == {}


def test_rows_filed_while_the_fix_was_in_flight_retire_when_it_merges(tmp_path):
    """7a94f5bc: a row filed after the resolution but before its PR merged was held as
    pending, then read as `RECURRED` the moment the merge settled it -- a session sent to
    re-prove #434 hours after it had fixed the defect. Only a row after the merge is one."""
    first, waiting, after = finding(10), finding(5), finding(1)
    head = triage.item_id(first)
    lines = items(first, resolution("agent/x", days_ago=8 / 24, ref=head), waiting, after)
    merge = fix_verify.Pr(fix_verify.LANDED, at(3), "https://github.com/o/devkit/pull/434")
    cache = tmp_path / "v.json"
    outcome = fix_verify.verify(lines, lambda *_: [merge], cache, NOW)
    [(row, note, pr)] = outcome.covered
    assert row == triage.item_id(waiting), "the row after the merge is a real recurrence"
    assert pr == merge.url and f"[{head}]" in note and at(3) in note
    assert row in triage.load_settled(cache), "merged by construction: never in flight"


def test_nothing_is_covered_without_a_readable_merge_time_or_by_another_group(tmp_path):
    first, waiting = finding(10), finding(5)
    other = finding(5, detail="something else entirely")
    lines = items(first, resolution("agent/x", days_ago=8 / 24, ref=triage.item_id(first)))
    lines += items(waiting, other)
    assert fix_verify.verify(lines, lambda *_: [MERGED], tmp_path / "a", NOW).covered == []
    merge = fix_verify.Pr(fix_verify.LANDED, at(1))
    covered = fix_verify.verify(lines, lambda *_: [merge], tmp_path / "b", NOW).covered
    assert [row for row, _, _ in covered] == [triage.item_id(waiting)]
    assert covered[0][2] == "agent/x", "no url: the resolution's own pr= stands in"


def test_a_reused_branch_name_does_not_settle_a_fix_written_after_its_old_pr_merged(tmp_path):
    """The pass cuts `agent/fix-harness-ledger-0927` again after #427 merged from it, so
    `gh pr list --head` answers #427 for a resolution written a day later -- which then
    settled on the spot, before the fix it names had a PR at all."""
    old = fix_verify.Pr(fix_verify.LANDED, at(40), "https://github.com/o/devkit/pull/427")
    lines = items(resolution("agent/fix-harness-ledger-0927", days_ago=1 / 24))
    cache = tmp_path / "v.json"
    assert fix_verify.verify(lines, lambda *_: [old], cache, NOW) == fix_verify.Outcome()
    assert fix_verify.in_flight(lines, cache, NOW) == {"aaaabbbb": "agent/fix-harness-ledger-0927"}
    new = fix_verify.Pr(fix_verify.LANDED, at(0), "https://github.com/o/devkit/pull/440")
    fix_verify.verify(lines, lambda *_: [old, new], cache, NOW)
    assert fix_verify.in_flight(lines, cache, NOW) == {}


def test_a_number_names_one_pr_even_one_merged_before_the_resolution(tmp_path):
    lines = items(resolution("#430", days_ago=1 / 24))
    earlier = fix_verify.Pr(fix_verify.LANDED, at(40))
    fix_verify.verify(lines, lambda *_: [earlier], tmp_path / "v.json", NOW)
    assert fix_verify.in_flight(lines, tmp_path / "v.json", NOW) == {}


def test_relevant_drops_only_a_branchs_merges_from_before_the_resolution():
    branch = fix_verify.Resolution("r", at(10), "agent/x", "n")
    number = fix_verify.Resolution("r", at(10), "#427", "n")
    stale = fix_verify.Pr(fix_verify.LANDED, at(20))
    fresh = fix_verify.Pr(fix_verify.LANDED, at(1))
    unknown = fix_verify.Pr(fix_verify.LANDED, "")
    assert fix_verify.relevant(branch, [stale, fresh, unknown, OPEN]) == [fresh, unknown, OPEN]
    assert fix_verify.relevant(number, [stale, fresh]) == [stale, fresh]


def test_the_first_merge_after_the_resolution_is_when_the_fix_landed():
    written = fix_verify.Resolution("r", at(10), "agent/x", "n")
    stale = fix_verify.Pr(fix_verify.LANDED, at(20))
    first = fix_verify.Pr(fix_verify.LANDED, "2026-09-26T06:00:00Z")
    later = fix_verify.Pr(fix_verify.LANDED, at(1))
    assert fix_verify.landed(written, [later, stale, first, OPEN]) == first
    assert fix_verify.landed(written, [stale, fix_verify.Pr(fix_verify.LANDED, "junk")]) is None


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
            assert args[-2:] == ("--json", "state,mergedAt,url")
            merged = {"state": "MERGED", "mergedAt": "2026-09-26T06:00:00Z", "url": "u/402"}
            payload = merged if args[1] == "view" else [{"state": "CLOSED", "mergedAt": None}]
            return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")

        return gh

    lookup = fix_verify.gh_lookup(tmp_path, ["devkit", "carameli", "gone"], gh_for)
    assert lookup("devkit", "402") == [fix_verify.Pr("MERGED", "2026-09-26T06:00:00Z", "u/402")]
    assert lookup("", "agent/x") == [CLOSED, CLOSED]
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
    assert fix_verify.judge(fresh, [CLOSED, MERGED], NOW) == fix_verify.LANDED
    assert fix_verify.judge(old, [OPEN], NOW) == ""
    assert "closed without merging" in fix_verify.judge(fresh, [CLOSED], NOW)
    assert fix_verify.judge(fresh, [], NOW) == ""
    assert fix_verify.judge(old, [], NOW).startswith("no PR from agent/x")
