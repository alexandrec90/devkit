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

from dataclasses import dataclass

import pytest
from support import load_script

schedule_health = load_script("scripts/schedule_health.py")
tray_state = load_script("scripts/tray_state.py")


@dataclass(frozen=True)
class FakeJob:
    name: str


def jobs(*names: str) -> list[FakeJob]:
    return [FakeJob(name) for name in names]


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
    monkeypatch.setattr(schedule_health, "query", lambda: jobs("devkit-a", "devkit-b"))
    monkeypatch.setattr(
        schedule_health,
        "problems",
        lambda found, now=None: ["devkit-b: disabled -- nothing runs it"],
    )
    found = tray_state.refresh()
    assert [(item.name, item.state) for item in found] == [
        ("devkit-b", tray_state.FAIL),
        ("devkit-a", tray_state.OK),
    ]


def test_refresh_on_a_machine_with_no_scheduler_reports_nothing_registered(monkeypatch):
    monkeypatch.setattr(schedule_health, "query", lambda: [])
    monkeypatch.setattr(schedule_health, "problems", lambda found, now=None: [])
    assert tray_state.refresh() == []
    assert tray_state.overall(tray_state.refresh()) == tray_state.WARN
