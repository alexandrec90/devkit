"""`tray_state.py`: what colour the tray is, and why.

The design property worth pinning is that **the judgement is not made here**.
`schedule_health.problems` decides what counts as a problem; this only decides how loud
each answer is. A second opinion would be worse than no indicator, because it would
disagree with the session-start line about the same machine.

The other one is that a healthy-looking tray must never be the default. An empty machine
is amber, not green: green over a set of zero jobs is the most misleading thing this
could say.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass

import pytest
from support import load_script

fix_cycle = load_script("scripts/fix_cycle.py")
schedule_health = load_script("scripts/schedule_health.py")
tray_state = load_script("scripts/tray_state.py")


@dataclass(frozen=True)
class FakeJob:
    name: str


def jobs(*names: str) -> list[FakeJob]:
    return [FakeJob(name) for name in names]


@pytest.fixture(autouse=True)
def no_collectors(monkeypatch, tmp_path):
    """`refresh` also reads this machine's collector assignment and asks docker about it,
    and reads the fix pass's history out of this checkout. Off by default here, so a
    workstation that runs a collector or the pass gets the same answers as CI; the tests
    that want rows stub `collectors.tray_rows` or write a history themselves."""
    monkeypatch.setattr(tray_state.collectors, "tray_rows", lambda: [])
    monkeypatch.setattr(tray_state.collectors, "scheduled_tasks", lambda: {})
    monkeypatch.setattr(tray_state, "REPO_ROOT", tmp_path)


# --- reading a problem line --------------------------------------------------


def test_a_line_names_the_job_it_is_about():
    line = "devkit-rc-servers: last run failed (exit 2) at 2026-09-02 04:45"
    assert tray_state.named_in(line) == "devkit-rc-servers"


def test_a_line_that_is_not_about_a_devkit_job_names_nothing():
    """The prefix check is what stops a stray colon in some other output being read as a
    task name and inventing a job the scheduler never reported."""
    assert tray_state.named_in("something else: happened") == ""
    assert tray_state.named_in("no colon here") == ""


def test_the_timestamps_inside_a_line_do_not_confuse_the_split():
    line = "devkit-global-tools: has not run since 2026-09-01 04:30 (3 intervals ago)"
    assert tray_state.named_in(line) == "devkit-global-tools"


@pytest.mark.parametrize(
    "line",
    [
        "devkit-x: last run failed (exit 1) at 2026-09-02 04:00",
        "devkit-x: disabled -- nothing is running it",
    ],
    ids=["failed", "disabled"],
)
def test_something_already_broken_is_red(line):
    assert tray_state.severity(line) == tray_state.FAIL


@pytest.mark.parametrize(
    "line",
    [
        "devkit-x: registered but has never run",
        "devkit-x: has not run since 2026-09-01 04:30 (3 intervals ago)",
        "devkit-x: a run was still going at 2026-09-02 09:00, so the scheduled fire was skipped",
    ],
    ids=["never-ran", "stale", "overlapping"],
)
def test_late_or_unstarted_is_amber(line):
    """Worth knowing, but nothing has actually failed yet. Reporting these as red is how
    an indicator trains its owner to ignore it."""
    assert tray_state.severity(line) == tray_state.WARN


def test_every_fail_marker_still_matches_something_schedule_health_can_say():
    """This is the coupling that would otherwise rot silently: the markers are matched
    against `schedule_health`'s prose, so a reworded message turns every red into amber
    and nothing anywhere goes red about it."""
    source = schedule_health.__file__ and __import__("pathlib").Path(
        schedule_health.__file__
    ).read_text(encoding="utf-8")
    for marker in tray_state.FAIL_MARKERS:
        assert marker in source, (
            f"{marker!r} no longer appears in schedule_health.py, so nothing will ever "
            f"be classified FAIL by it. Re-read `problems` and update FAIL_MARKERS."
        )


# --- assembling the set ------------------------------------------------------


def test_every_registered_job_appears_not_only_the_broken_ones():
    """ "Nothing is ever totally invisible" is the point of the tray. A menu listing only
    failures is indistinguishable from one that lost track of a job."""
    found = tray_state.states(jobs("devkit-a", "devkit-b"), [])
    assert [item.name for item in found] == ["devkit-a", "devkit-b"]
    assert {item.state for item in found} == {tray_state.OK}


def test_the_worst_line_wins_for_a_job_named_twice():
    found = tray_state.states(
        jobs("devkit-a"),
        ["devkit-a: registered but has never run", "devkit-a: last run failed (exit 1) at x"],
    )
    assert found[0].state == tray_state.FAIL


def test_the_unhealthy_sort_to_the_top():
    found = tray_state.states(
        jobs("devkit-a", "devkit-b", "devkit-c"),
        ["devkit-c: last run failed (exit 1) at x", "devkit-b: registered but has never run"],
    )
    assert [item.name for item in found] == ["devkit-c", "devkit-b", "devkit-a"]


def test_a_line_about_a_job_the_scheduler_did_not_list_is_ignored():
    found = tray_state.states(jobs("devkit-a"), ["devkit-ghost: last run failed (exit 1) at x"])
    assert [item.name for item in found] == ["devkit-a"]
    assert found[0].state == tray_state.OK


def test_a_job_carries_the_detail_from_its_line():
    found = tray_state.states(jobs("devkit-a"), ["devkit-a: disabled -- nothing is running it"])
    assert found[0].detail == "disabled -- nothing is running it"


def test_a_job_knows_where_its_own_record_lives():
    assert tray_state.JobState("devkit-rc-servers", tray_state.OK).artifact == "logs/rc-servers.log"
    assert tray_state.JobState("devkit-unknown", tray_state.OK).artifact == ""


# --- the overall verdict -----------------------------------------------------


def test_all_healthy_is_green():
    assert tray_state.overall(tray_state.states(jobs("devkit-a"), [])) == tray_state.OK


def test_one_red_job_colours_the_whole_icon_red():
    """An indicator that averaged would go green while something was broken."""
    found = tray_state.states(
        jobs("devkit-a", "devkit-b", "devkit-c"), ["devkit-c: last run failed (exit 1) at x"]
    )
    assert tray_state.overall(found) == tray_state.FAIL


def test_a_machine_with_nothing_registered_is_not_green():
    """Green over a set of zero jobs is the most misleading thing this could say."""
    assert tray_state.overall([]) == tray_state.WARN


# --- what the user actually sees ---------------------------------------------


def test_the_tooltip_says_all_healthy_when_it_is():
    text = tray_state.tooltip(tray_state.states(jobs("devkit-a", "devkit-b"), []))
    assert "2 scheduled jobs" in text and "healthy" in text


def test_the_tooltip_names_the_first_thing_wrong():
    found = tray_state.states(jobs("devkit-a"), ["devkit-a: last run failed (exit 1) at x"])
    assert "devkit-a" in tray_state.tooltip(found)
    assert "1 of 1" in tray_state.tooltip(found)


def test_the_tooltip_says_so_when_nothing_is_registered():
    assert "no scheduled jobs" in tray_state.tooltip([])


def test_the_tooltip_is_truncated_here_rather_than_by_windows():
    """`szTip` is 128 wide including the terminator, and Windows cuts mid-word with no
    sign that it did."""
    found = tray_state.states(jobs("devkit-" + "x" * 200), ["devkit-" + "x" * 200 + ": disabled"])
    text = tray_state.tooltip(found)
    assert len(text) <= tray_state.TOOLTIP_LIMIT
    assert text.endswith("…")


def test_a_menu_row_marks_its_state_without_relying_on_colour():
    """A menu item cannot be coloured without owner-drawing the whole menu."""
    ok = tray_state.menu_label(tray_state.JobState("devkit-a", tray_state.OK))
    bad = tray_state.menu_label(tray_state.JobState("devkit-b", tray_state.FAIL, "it broke"))
    assert ok.startswith("OK") and "devkit-a" in ok
    assert bad.startswith("X") and "it broke" in bad


def test_every_state_has_a_colour_and_a_menu_mark():
    """A state added without one is a `KeyError` inside a Windows message loop, where
    the traceback goes to a callback nobody reads."""
    for level in (tray_state.OK, tray_state.WARN, tray_state.FAIL):
        assert level in tray_state.COLOURS
        assert tray_state.menu_label(tray_state.JobState("devkit-a", level))


def test_the_colours_differ_in_brightness_as_well_as_hue():
    """The half that survives the most common colour blindness."""
    luma = {
        level: 0.299 * r + 0.587 * g + 0.114 * b for level, (r, g, b) in tray_state.COLOURS.items()
    }
    assert len({round(value / 20) for value in luma.values()}) == 3


def test_refresh_asks_the_scheduler_and_returns_what_the_tray_draws(monkeypatch):
    """The one function the tray calls on a timer. It exists so the message loop holds
    no knowledge of `schedule_health` at all."""
    monkeypatch.setattr(
        schedule_health, "query", lambda also=frozenset(): jobs("devkit-a", "devkit-b")
    )
    monkeypatch.setattr(
        schedule_health,
        "problems",
        lambda found, now=None, deliberate=frozenset(), artifacts=None: [
            "devkit-b: disabled -- nothing runs it"
        ],
    )
    found = tray_state.refresh()
    assert [(item.name, item.state) for item in found] == [
        ("devkit-b", tray_state.FAIL),
        ("devkit-a", tray_state.OK),
    ]


def test_refresh_on_a_machine_with_no_scheduler_reports_nothing_registered(monkeypatch):
    monkeypatch.setattr(schedule_health, "query", lambda also=frozenset(): [])
    monkeypatch.setattr(
        schedule_health,
        "problems",
        lambda found, now=None, deliberate=frozenset(), artifacts=None: [],
    )
    assert tray_state.refresh() == []
    assert tray_state.overall(tray_state.refresh()) == tray_state.WARN


def test_a_stood_down_job_is_healthy_but_says_it_is_off():
    """Not a fault, and not disguised as a running job either -- the menu's one claim is
    that a stopped job cannot hide in it."""
    found = tray_state.states(jobs("devkit-a"), [], frozenset({"devkit-a"}))
    assert found[0].state == tray_state.OK
    assert "on purpose" in found[0].detail
    assert tray_state.overall(found) == tray_state.OK


def test_an_ordinary_healthy_job_gets_no_detail():
    found = tray_state.states(jobs("devkit-a"), [], frozenset())
    assert (found[0].state, found[0].detail) == (tray_state.OK, "")


def test_a_real_failure_outranks_the_stood_down_label():
    """Intent never overwrites a problem line: a job in the ledger that is somehow also
    failing is reported as failing."""
    found = tray_state.states(
        jobs("devkit-a"), ["devkit-a: last run failed (exit 1)"], frozenset({"devkit-a"})
    )
    assert found[0].state == tray_state.FAIL
    assert "last run failed" in found[0].detail


def test_refresh_tells_problems_what_was_stood_down_on_purpose(monkeypatch):
    """An always-visible red icon is the worst place to report a state someone chose:
    it cannot be dismissed, so it goes on saying "broken" about a working machine until
    the operator stops reading the icon. `problems` only knows the difference if its
    caller passes the ledger, and `refresh` is a caller it would be easy to forget."""
    seen = {}
    monkeypatch.setattr(schedule_health, "query", lambda also=frozenset(): jobs("devkit-a"))
    monkeypatch.setattr(schedule_health, "stood_down", lambda: frozenset({"devkit-a"}))
    monkeypatch.setattr(
        schedule_health,
        "problems",
        lambda found, now=None, deliberate=frozenset(), artifacts=None: (
            seen.update(passed=deliberate) or []
        ),
    )
    tray_state.refresh()
    assert seen["passed"] == frozenset({"devkit-a"})


# --- the collectors ride along ---------------------------------------------------


def test_a_collector_row_joins_the_jobs_and_sorts_by_how_loud_it_is(monkeypatch):
    monkeypatch.setattr(schedule_health, "query", lambda also=frozenset(): jobs("devkit-a"))
    monkeypatch.setattr(
        schedule_health,
        "problems",
        lambda found, now=None, deliberate=frozenset(), artifacts=None: [],
    )
    monkeypatch.setattr(
        tray_state.collectors,
        "tray_rows",
        lambda: [("collector: ibkr_trader", tray_state.FAIL, "not running (Exited (1))")],
    )
    found = tray_state.refresh()
    assert [(item.name, item.state) for item in found] == [
        ("collector: ibkr_trader", tray_state.FAIL),
        ("devkit-a", tray_state.OK),
    ]
    assert tray_state.overall(found) == tray_state.FAIL
    assert "collector: ibkr_trader" in tray_state.tooltip(found)


def test_a_collector_row_opens_the_collectors_log(monkeypatch):
    """Clicking a row opens its record; a collector is no scheduled task, so it names its
    own rather than being looked up in `schedule_health.ARTIFACTS`."""
    monkeypatch.setattr(
        tray_state.collectors, "tray_rows", lambda: [("collector: x", tray_state.OK, "")]
    )
    (item,) = tray_state.collector_states()
    assert item.artifact == "logs/collectors.log"
    assert tray_state.JobState("devkit-fix-pass", tray_state.OK).artifact == "logs/fix-pass.log"


def test_the_collector_levels_are_the_trays_levels():
    """`collectors` spells the three levels itself because this module imports it; a
    respelling on either side would leave a row with no colour (`RANK` KeyError)."""
    levels = {tray_state.collectors.OK, tray_state.collectors.WARN, tray_state.collectors.FAIL}
    assert levels == set(tray_state.RANK)


# --- a scheduled collector is a scheduler row under the collectors' label -------------


def test_a_line_about_a_known_non_devkit_job_names_it():
    line = "social-scraper: last run failed (exit 1) at 2026-10-02 14:30"
    assert tray_state.named_in(line) == ""
    assert tray_state.named_in(line, frozenset({"social-scraper"})) == "social-scraper"


def test_a_scheduled_collector_is_asked_about_in_the_same_query_and_judged_the_same(
    monkeypatch,
):
    asked = {}
    tasks = {"social-scraper": "logs/collector-social-scraper.log"}
    monkeypatch.setattr(tray_state.collectors, "scheduled_tasks", lambda: tasks)

    def query(also=frozenset()):
        asked["also"] = also
        return jobs("devkit-a", "social-scraper")

    def problems(found, now=None, deliberate=frozenset(), artifacts=None):
        asked["artifacts"] = artifacts
        return ["social-scraper: last run failed (exit 1) at 2026-10-02 14:30"]

    monkeypatch.setattr(schedule_health, "query", query)
    monkeypatch.setattr(schedule_health, "problems", problems)
    found = tray_state.refresh()
    assert asked["also"] == frozenset({"social-scraper"})
    assert asked["artifacts"]["social-scraper"] == "logs/collector-social-scraper.log"
    assert asked["artifacts"]["devkit-fix-pass"] == "logs/fix-pass.log"
    assert [(item.name, item.state) for item in found] == [
        ("collector: social-scraper", tray_state.FAIL),
        ("devkit-a", tray_state.OK),
    ]
    assert found[0].artifact == "logs/collector-social-scraper.log"


def test_collector_task_states_relabels_the_tasks_and_adds_the_missing_ones():
    found = [
        tray_state.JobState("devkit-a", tray_state.OK),
        tray_state.JobState("social-scraper", tray_state.WARN, "slow"),
    ]
    tasks = {"social-scraper": "logs/collector-social-scraper.log", "other": "logs/o.log"}
    relabelled = tray_state.collector_task_states(found, tasks)
    assert relabelled[:2] == [
        tray_state.JobState("devkit-a", tray_state.OK),
        tray_state.JobState(
            "collector: social-scraper",
            tray_state.WARN,
            "slow",
            "logs/collector-social-scraper.log",
        ),
    ]
    ((name, state),) = [(item.name, item.state) for item in relabelled[2:]]
    assert (name, state) == ("collector: other", tray_state.FAIL)
    assert tray_state.collector_task_states(found[:1], {}) == found[:1]


def test_a_scheduled_collector_with_no_task_is_red_rather_than_absent(monkeypatch):
    """Asked about by name, so a task never registered or deleted by hand is a row that
    says so -- not a row that silently is not there."""
    monkeypatch.setattr(
        tray_state.collectors,
        "scheduled_tasks",
        lambda: {"social-scraper": "logs/collector-social-scraper.log"},
    )
    monkeypatch.setattr(schedule_health, "query", lambda also=frozenset(): jobs("devkit-a"))
    monkeypatch.setattr(
        schedule_health,
        "problems",
        lambda found, now=None, deliberate=frozenset(), artifacts=None: [],
    )
    first, _devkit = tray_state.refresh()
    assert (first.name, first.state) == ("collector: social-scraper", tray_state.FAIL)
    assert "not scheduled" in first.detail and first.artifact == "logs/collectors.log"


# --- the fix pass ------------------------------------------------------------


def pass_row(when="2026-10-28T17:30:04+00:00", **fields):
    """One history line as `fix_cycle.history_line` writes it: a clean dispatch pass."""
    row = {"when": when, "mode": "dispatch", "harness": "clean", "sent": [], "waiting": {}}
    row.update({"working": [], "person": [], "owed": 0, **fields})
    return row


def test_a_machine_with_no_fix_pass_history_gets_no_fix_pass_rows():
    """The pass's scheduled job is a row of its own; one that never ran is its to say."""
    assert tray_state.fix_pass_states([]) == []


def test_each_step_awaiting_a_person_is_a_red_row_that_opens_the_record():
    """A step no fixer can take reached the person only through the morning toast, once
    a day: 26291670's `claude auth login` sat behind it for three days."""
    step = "run `claude auth login` on this machine -- then: python scripts/harness_triage.py"
    found = tray_state.fix_pass_states([pass_row(person=[step])])
    assert found[0] == tray_state.JobState("your step", tray_state.FAIL, step, "logs/fix-pass.log")
    assert tray_state.overall(found) == tray_state.FAIL


def test_the_fixers_row_says_what_is_at_work_waiting_owed_and_last_sent():
    history = [
        pass_row("2026-10-28T12:00:06+00:00", sent=["carameli #485 -- dispatch"]),
        pass_row(working=["devkit agent/fix-x"], waiting={"devkit #1": "held"}, owed=2),
    ]
    [fixers] = tray_state.fix_pass_states(history)
    assert (fixers.name, fixers.state) == ("fixers", tray_state.OK)
    assert fixers.detail == "1 at work, 1 waiting, 2 owed, last sent 12:00Z"


def test_the_fixers_row_follows_every_person_step_and_reads_the_whole_history():
    history = [pass_row(sent=["carameli #485 -- dispatch"]), pass_row(person=["a", "b"])]
    *person, fixers = tray_state.fix_pass_states(history)
    assert [row.name for row in person] == ["your step", "your step"]
    assert fixers == tray_state.fixers_state(history)
    assert fixers.detail == "0 at work, last sent 17:30Z", "the send is an older line's"


def test_a_red_harness_holds_every_send_and_says_so():
    [fixers] = tray_state.fix_pass_states([pass_row(harness=["1 harness failure(s) open"])])
    assert fixers.state == tray_state.WARN
    assert fixers.detail.startswith("harness RED, sends held -- 1 harness failure(s) open")


def test_owed_with_nothing_sent_for_six_passes_is_amber_and_five_is_not():
    """What a lapsed login, an unstartable launcher or a plan that drops the backlog all
    look like from outside: work owed and no fixer going at it."""
    stuck = [pass_row(owed=1)] * tray_state.STUCK_PASSES
    [fixers] = tray_state.fix_pass_states(stuck)
    assert fixers.state == tray_state.WARN and "none sent for 6 passes" in fixers.detail
    [fixers] = tray_state.fix_pass_states(stuck[1:])
    assert fixers.state == tray_state.OK


def test_a_fixer_at_work_or_a_reasoned_wait_is_not_stuck():
    for busy in ({"working": ["devkit x"]}, {"waiting": {"devkit #1": "held"}}):
        history = [pass_row(owed=1, **busy)] * tray_state.STUCK_PASSES
        assert tray_state.fix_pass_states(history)[0].state == tray_state.OK


def test_history_written_before_the_tray_fields_is_never_judged_stuck():
    old = {"when": "2026-10-20T00:00:00+00:00", "mode": "dispatch", "harness": "clean"}
    [fixers] = tray_state.fix_pass_states([old] * tray_state.STUCK_PASSES)
    assert (fixers.state, fixers.detail) == (tray_state.OK, "none sent")


def test_a_pass_that_sends_nothing_by_its_mode_says_so():
    [fixers] = tray_state.fix_pass_states([pass_row(mode="plan")])
    assert fixers.state == tray_state.OK
    assert fixers.detail.startswith("mode=plan, sends nothing; ")


def test_the_dispatch_mode_is_the_passes_own_spelling():
    assert tray_state.DISPATCH == fix_cycle.DISPATCH


def test_the_tray_reads_the_line_the_pass_writes():
    """The two halves agree on field names only through this: the pass's own history
    line, read back by the tray."""
    account = fix_cycle.Account(
        fix_cycle.DISPATCH,
        fix_cycle.harness_state({}, True, []),
        sent=("devkit ledger -- upstream",),
        working=("devkit agent/fix-x",),
        person=("run `claude auth login` on this machine",),
        owed=1,
    )
    now = _dt.datetime(2026, 10, 28, 18, 0, tzinfo=_dt.UTC)
    row = json.loads(fix_cycle.history_line(account, now))
    person, fixers = tray_state.fix_pass_states([row])
    assert (person.state, person.detail) == (tray_state.FAIL, account.person[0])
    assert fixers.detail == "1 at work, 1 owed, last sent 18:00Z"


def test_read_history_skips_what_is_not_a_line_of_json(tmp_path):
    path = tmp_path / "history.jsonl"
    path.write_text('{"when": "a"}\nnot json\n[1]\n{"when": "b"}\n', encoding="utf-8")
    assert tray_state.read_history(path) == [{"when": "a"}, {"when": "b"}]
    assert tray_state.read_history(tmp_path / "absent.jsonl") == []


def test_refresh_adds_the_fix_pass_rows_from_this_checkouts_history(monkeypatch, tmp_path):
    monkeypatch.setattr(schedule_health, "query", lambda also=frozenset(): jobs("devkit-a"))
    monkeypatch.setattr(
        schedule_health,
        "problems",
        lambda found, now=None, deliberate=frozenset(), artifacts=None: [],
    )
    history = tmp_path / tray_state.FIX_HISTORY
    history.parent.mkdir(parents=True)
    history.write_text(json.dumps(pass_row(person=["log in"])) + "\n", encoding="utf-8")
    found = tray_state.refresh()
    assert [(item.name, item.state) for item in found] == [
        ("your step", tray_state.FAIL),
        ("devkit-a", tray_state.OK),
        ("fixers", tray_state.OK),
    ]
