"""`scripts/fix_stall.py`: a hold, a cap or a skip that has lasted a day is filed."""

from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fix_cycle
import fix_plan
import fix_stall

NOW = _dt.datetime(2026, 9, 26, 12, 0, tzinfo=_dt.UTC)


def line(hours_ago: float, waiting: dict | None = None, skipped: dict | None = None) -> dict:
    when = (NOW - _dt.timedelta(hours=hours_ago)).isoformat(timespec="seconds")
    return {"when": when, "waiting": waiting or {}, "skipped": skipped or {}}


# A hold nothing else tracks: a red local run the scheduled one is to re-test.
HELD = {"carameli #8": "Medium Tier failing on main: the next scheduled local run re-tests the tip"}


def test_a_wait_is_dated_from_the_start_of_its_unbroken_run():
    history = [line(50, HELD), line(40), line(30, HELD), line(20, HELD), line(0, HELD)]
    assert fix_stall.streaks(history, "waiting") == {
        "carameli #8": (history[2]["when"], HELD["carameli #8"])
    }


def test_a_wait_past_a_day_is_filed_with_its_reason():
    history = [line(30, HELD), line(0, HELD)]
    [found] = fix_stall.stalled(history, NOW)
    assert (found.kind, found.project) == ("stalled", "carameli")
    assert found.detail.startswith(
        f"waiting since {history[0]['when'][:16]}: carameli #8 -- Medium"
    )


def test_a_wait_under_a_day_is_not():
    assert fix_stall.stalled([line(20, HELD), line(0, HELD)], NOW) == []


def test_a_hold_for_the_nightly_run_is_given_a_second_night():
    # 09f80e67: tvshell's red local e2e was held at 09:00 for a job that fires once a day,
    # and filed at 09:00 the next day while that day's run was still waiting for the PC to
    # go idle. One cadence is not a stall; two nights with no re-test is.
    failure = fix_plan.Failure(
        kind=fix_plan.LOCAL, project="tvshell", number=0, title="", url="", base="main"
    )
    name = f"tvshell {fix_plan.name_of(failure)}"
    assert name.endswith(f"({fix_plan.LOCAL_E2E_NAME})")
    held = {name: "Local E2E failing: the next scheduled local run re-tests the tip"}
    assert fix_stall.stalled([line(30, held), line(0, held)], NOW) == []
    [found] = fix_stall.stalled([line(49, held), line(0, held)], NOW)
    assert found.project == "tvshell" and name in found.detail
    # The medium tier re-tests within minutes, so a day of its hold is still a stall.
    tier = fix_plan.Failure(
        kind=fix_plan.LOCAL,
        project="tvshell",
        number=0,
        title="",
        url="",
        base="main",
        workflow=fix_plan.MEDIUM_TIER,
    )
    medium = f"tvshell {fix_plan.name_of(tier)}"
    assert fix_stall.stall_after(medium) == fix_stall.STALL_AFTER
    assert len(fix_stall.stalled([line(30, {medium: "x"}), line(0, {medium: "x"})], NOW)) == 1


def test_a_long_skip_is_a_release_or_a_sweep_that_is_not_happening():
    skip = {"devkit #410": "red by construction: a release PR fails the newest-tag test"}
    [found] = fix_stall.stalled([line(48, skipped=skip), line(0, skipped=skip)], NOW)
    assert found.detail.startswith("skipped since") and "devkit #410" in found.detail


def test_what_another_part_of_the_loop_already_tracks_is_not_filed_twice():
    tracked = {
        "a #1": "escalated: the devkit session has it on the harness ledger",
        "b #2": "backing off: retried at max effort after 2026-09-27 12:00",
        "c #3": "held until the devkit session in C:/t finishes",
        # A full Dependabot cap frees itself; filed, it would send the devkit session it saves.
        "d dependabot": f"{fix_stall.fix_budget.DAILY_CAPPED}: 2 of 2 sessions sent in the last 24h",
    }
    assert fix_stall.stalled([line(72, tracked), line(0, tracked)], NOW) == []
    assert all(fix_stall.tracked(why) for why in tracked.values())
    assert not fix_stall.tracked(HELD["carameli #8"]) and not fix_stall.tracked(None)


def test_a_hold_behind_the_harness_or_an_adoption_is_not_a_stall():
    """39517cbb, 4e79a5bd: carameli #526, held a day behind the red harness and then behind
    its adoption #530, was filed as a stall on the harness ledger -- which kept the harness
    red, so #526 stayed held and #530 with it. Each waits on a finding already worked."""
    harness = fix_cycle.Harness(clean=False, reasons=("1 harness failure(s) open",))
    adopting = fix_cycle.Harness(clean=True, reasons=(), adopting=("carameli",))
    pr = fix_plan.Failure(fix_plan.PR, "carameli", 526, "chore(deps): bump", "u", head="b")
    held = {}
    for state in (harness, adopting):
        decision = fix_plan.Decision(fix_plan.DISPATCH, "red", (pr,))
        [(_, why)] = fix_cycle.phase([decision], {}, state)[1]
        held[f"carameli #{len(held)}"] = why
    assert fix_stall.stalled([line(48, held), line(0, held)], NOW) == []
    assert all(fix_stall.tracked(why) for why in held.values())


def test_a_skip_naming_a_persons_step_is_the_persons_not_a_stall():
    """2026-11-01: five PRs GitHub refused over billing were each filed as a stall that
    carried the step, so the tray showed the account's one step six times."""
    why = (
        "4 checks failing: Lint / refused by the account's Actions billing; GitHub refused "
        f"-- {fix_stall.fix_findings.triage.PERSON_STEP} settle the GitHub account's billing"
    )
    skip = {"productivity-tracker #3": why, "social-scraper #108": why}
    assert fix_stall.stalled([line(48, skipped=skip), line(0, skipped=skip)], NOW) == []
    assert fix_stall.tracked(why)


def test_a_tracked_wait_breaks_the_run_an_untracked_one_is_dated_from():
    # data-lake dependabot, 2026-10-05: a session sent, then a day under the Dependabot
    # cap, then half an hour behind a red harness -- filed as a day-long harness hold.
    sent = {"carameli #8": "already dispatched at 2026-09-25T06:00:05+00:00"}
    capped = {"carameli #8": f"{fix_stall.fix_budget.DAILY_CAPPED}: 2 of 2 sessions sent"}
    history = [line(30, HELD), line(26, sent), line(20, capped), line(0.5, HELD), line(0, HELD)]
    assert fix_stall.streaks(history, "waiting") == {
        "carameli #8": (history[3]["when"], HELD["carameli #8"])
    }
    assert fix_stall.stalled(history, NOW) == []


class Row:
    def __init__(self, detail: str):
        self.fields = {"detail": detail}


def test_waited_name_reads_the_name_back_out_of_a_filed_stall():
    [found] = fix_stall.stalled([line(30, HELD), line(0, HELD)], NOW)
    assert fix_stall.waited_name(f"{found.kind}: {found.detail}") == "carameli #8"
    assert fix_stall.waited_name("stalled: waiting since x: no reason") == ""
    assert fix_stall.waited_name("push-failed: waiting since x: a -- b") == ""
    assert fix_stall.waited_name("stalled: held since x: a -- b") == ""
    assert fix_stall.waited_name("") == ""


def test_a_stall_ends_when_the_newest_pass_stops_making_its_wait():
    kept = Row("stalled: waiting since 2026-09-24T12:00: carameli #8 -- held")
    gone = Row("stalled: skipped since 2026-09-24T12:00: devkit #410 -- red by construction")
    other = Row("push-failed: carameli: rejected")
    history = [line(30, HELD, {"devkit #410": "red"}), line(0, HELD)]
    assert fix_stall.ended(history, [kept, gone, other]) == [gone]
    assert fix_stall.ended([], [kept, gone]) == [], "no history proves nothing"


def test_history_reads_skip_junk_and_a_missing_file_is_empty(tmp_path):
    path = tmp_path / "h.jsonl"
    assert fix_stall.read_history(path) == []
    path.write_text(json.dumps(line(1, HELD)) + "\nnot json\n[1]\n", encoding="utf-8")
    assert len(fix_stall.read_history(path)) == 1
    assert fix_stall.streaks([{"when": "x"}], "waiting") == {}, "an older line with no names"
