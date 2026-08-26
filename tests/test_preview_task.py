"""Tests for `scripts/preview-task.py`.

The script is a menu, and everything that has gone wrong with it has gone wrong in the
menu rather than in the previewing -- `worktree.py` owns that half and is tested next
door. So the weight here is on the pure assembly, and three of these are regressions for
defects the first working version shipped with:

  - **the ordering bug.** `gh` dates a PR `...Z` and git dates a branch `...-04:00`, so
    the string comparison the first `sort_key` used ranked a four-hour-old dependabot PR
    above a box committed to two minutes earlier. `_parse`/`_epoch` compare instants, and
    `test_a_utc_stamp_and_an_offset_stamp_are_compared_as_instants` is the assertion that
    fails if anyone reintroduces the text comparison;
  - **kind outranking recency**, which floated a box cut 39 hours ago above the PR opened
    an hour ago -- the change someone has just asked to see is always the newest row;
  - **a box dated by when it was cut**, which sank the freshest thing on the machine
    below every PR opened since it was created.

The other half is the cap. A menu that truncates without saying so reads as "that is
everything", so `trim` returns the count and `render_menu` prints it, and both are
asserted rather than left to the eye.
"""

from __future__ import annotations

import datetime as _dt
import json
import socket
import tomllib
import types

import pytest
from support import load_script, worktree

preview_task = load_script("scripts/preview-task.py")

NOW = _dt.datetime(2026, 8, 21, 18, 0, tzinfo=_dt.UTC)


def box(name, *, project="carameli", branch="agent/x", kind=None, tracks="", created="", slot=-1):
    return worktree.Box(
        name=name,
        project=project,
        branch=branch,
        slot=slot,
        session="s",
        created=created,
        kind=kind or worktree.TASK_KIND,
        tracks=tracks,
    )


def pr(number, head, *, title="", updated=""):
    return {"number": number, "headRefName": head, "title": title, "updatedAt": updated}


def result(stdout="", returncode=0):
    return types.SimpleNamespace(stdout=stdout, returncode=returncode, stderr="")


# --- timestamps: the ordering bug ---------------------------------------------


def test_a_utc_stamp_and_an_offset_stamp_are_compared_as_instants():
    """The regression. `10:15-04:00` is 14:15Z, so it is NEWER than `12:00Z`.

    Lexically it is not -- `"2026-08-21T10:..." < "2026-08-21T12:..."` -- which is
    exactly how a four-hour-stale PR came to sit above a box committed to minutes ago.
    """
    box_row = preview_task.Candidate(
        project="carameli",
        ref="agent/fresh",
        kind=preview_task.KIND_BOX,
        box="carameli--fresh",
        updated="2026-08-21T10:15:32-04:00",
    )
    pr_row = preview_task.Candidate(
        project="carameli",
        ref="agent/stale",
        kind=preview_task.KIND_PR,
        updated="2026-08-21T12:00:00Z",
    )
    assert sorted([pr_row, box_row], key=lambda c: c.sort_key) == [box_row, pr_row]


def test_a_naive_stamp_is_read_as_utc():
    assert preview_task._parse("2026-08-21T12:00:00").tzinfo is _dt.UTC


def test_an_unparseable_stamp_has_no_instant():
    assert preview_task._parse("last tuesday") is None
    assert preview_task._parse("") is None


def test_an_undated_row_sorts_to_the_end_of_its_rank():
    """`-inf`, not 0: a 1970 stamp sorts the same way today and stops the day it does not."""
    assert preview_task._epoch("nonsense") == float("-inf")
    dated = preview_task.Candidate(
        project="p", ref="a", kind=preview_task.KIND_PR, updated="2020-01-01T00:00:00Z"
    )
    undated = preview_task.Candidate(project="p", ref="b", kind=preview_task.KIND_PR)
    assert sorted([undated, dated], key=lambda c: c.sort_key) == [dated, undated]


def test_a_standing_preview_outranks_a_newer_row():
    """The one kind that still beats recency: it is already up, so it costs nothing."""
    standing = preview_task.Candidate(
        project="p", ref="a", kind=preview_task.KIND_STANDING, updated="2020-01-01T00:00:00Z"
    )
    newer = preview_task.Candidate(
        project="p", ref="b", kind=preview_task.KIND_PR, updated="2026-08-21T12:00:00Z"
    )
    assert sorted([newer, standing], key=lambda c: c.sort_key) == [standing, newer]


def test_a_box_a_pr_and_a_branch_of_different_ages_sort_by_age_alone():
    """Ranking the kinds against each other put a 39h box above a 1h PR. It must not."""
    rows = [
        preview_task.Candidate(
            project="p", ref="box", kind=preview_task.KIND_BOX, updated="2026-08-20T00:00:00Z"
        ),
        preview_task.Candidate(
            project="p", ref="pr", kind=preview_task.KIND_PR, updated="2026-08-21T17:00:00Z"
        ),
        preview_task.Candidate(
            project="p", ref="br", kind=preview_task.KIND_BRANCH, updated="2026-08-21T12:00:00Z"
        ),
    ]
    assert [c.ref for c in sorted(rows, key=lambda c: c.sort_key)] == ["pr", "br", "box"]


# --- what the menu declines to offer ------------------------------------------


def test_ignored_ref_reads_a_namespace_and_never_a_slug():
    """The two prefixes and the trap between them. `dependabot/` and the automation
    namespace are path segments a producer puts there; `auto-merge-label` is a topic a
    human dictated, and it has to survive."""
    assert preview_task.ignored_ref("dependabot/npm_and_yarn/vite-5.4.6")
    assert preview_task.ignored_ref(f"{preview_task.tb.AUTOMATION_PREFIX}devkit-upgrade-0823")
    assert not preview_task.ignored_ref("agent/auto-merge-label-0823")
    assert not preview_task.ignored_ref("agent/comic-book-ui-0820")


def test_keeps_row_spares_a_standing_preview_and_nothing_else():
    """The filter describes how a ref was *discovered*, so the kind is the whole test: a
    standing preview is serving on a port because somebody named it, and hiding it would
    leave a running box no row in this menu could stop."""
    ref = f"{preview_task.tb.AUTOMATION_PREFIX}devkit-upgrade-v0-11-2-0823"
    standing = preview_task.Candidate(project="p", ref=ref, kind=preview_task.KIND_STANDING)
    assert preview_task.keeps_row(standing)
    for kind in (preview_task.KIND_BOX, preview_task.KIND_PR, preview_task.KIND_BRANCH):
        assert not preview_task.keeps_row(preview_task.Candidate(project="p", ref=ref, kind=kind))
    # And the filter is the only thing dropping it -- an ordinary ref of every kind stays.
    for kind in (preview_task.KIND_BOX, preview_task.KIND_PR, preview_task.KIND_BRANCH):
        assert preview_task.keeps_row(
            preview_task.Candidate(project="p", ref="agent/comic-book-ui-0820", kind=kind)
        )


def test_a_ref_whose_pr_has_merged_is_dropped_whatever_found_it():
    """The bug this filter was written for, stated as its test.

    A squash merge deletes the remote branch, so the branch and PR sources stop offering
    the ref the moment it lands -- and the BOX does not: it is not reapable while its
    worktree exists and no age cutoff applies to it. `agent/project-number-pad-0824` was
    still the second row of carameli's menu days after merging.
    """
    merged = {"agent/landed-0824"}
    for kind in (preview_task.KIND_BOX, preview_task.KIND_PR, preview_task.KIND_BRANCH):
        row = preview_task.Candidate(project="p", ref="agent/landed-0824", kind=kind)
        assert not preview_task.keeps_row(row, merged)
    fresh = preview_task.Candidate(
        project="p", ref="agent/still-open-0825", kind=preview_task.KIND_PR
    )
    assert preview_task.keeps_row(fresh, merged)


def test_a_standing_preview_survives_its_prs_merge():
    """Same reasoning as the ignored-prefix case: it is serving on a port right now, and
    a row is the only way anything in this menu can reach it."""
    row = preview_task.Candidate(
        project="p", ref="agent/landed-0824", kind=preview_task.KIND_STANDING, box="b"
    )
    assert preview_task.keeps_row(row, {"agent/landed-0824"})


def test_the_merged_filter_defaults_to_empty_so_an_offline_scan_loses_nothing():
    """`merged_refs` is subtractive, so its failure has to add rows, never remove them."""
    row = preview_task.Candidate(project="p", ref="agent/x", kind=preview_task.KIND_PR)
    assert preview_task.keeps_row(row)


# --- merging the four sources -------------------------------------------------


def test_a_box_a_pr_and_a_branch_naming_one_ref_are_one_row():
    merged = preview_task.merge_candidates(
        "carameli",
        [box("carameli--ui", branch="agent/ui", created="2026-08-21T09:00:00Z")],
        [pr(164, "agent/ui", title="Comic book UI", updated="2026-08-21T16:00:00Z")],
        [("agent/ui", "2026-08-21T09:00:00Z", "subject")],
    )
    assert len(merged) == 1
    row = merged[0]
    assert (row.kind, row.box, row.pr, row.title) == (
        preview_task.KIND_BOX,
        "carameli--ui",
        164,
        "Comic book UI",
    )


def test_the_box_keeps_the_row_s_date_when_a_pr_agrees_about_the_ref():
    """A comment posted on the PR does not change how long the box has been up."""
    merged = preview_task.merge_candidates(
        "carameli",
        [box("carameli--ui", branch="agent/ui", created="2026-08-21T09:00:00Z")],
        [pr(164, "agent/ui", updated="2026-08-21T16:00:00Z")],
        [],
    )
    assert merged[0].updated == "2026-08-21T09:00:00Z"


def test_a_preview_box_is_keyed_on_the_branch_it_tracks():
    """Not on its own throwaway `preview/...` copy, which nobody asked about."""
    preview_box = box(
        "carameli--preview-ui",
        branch="preview/agent-ui",
        kind=worktree.PREVIEW_KIND,
        tracks="agent/ui",
        slot=7,
    )
    assert preview_task.box_ref(preview_box) == "agent/ui"
    merged = preview_task.merge_candidates("carameli", [preview_box], [pr(164, "agent/ui")], [])
    assert len(merged) == 1
    assert merged[0].kind == preview_task.KIND_STANDING
    assert merged[0].slot == 7


def test_a_box_row_prefers_its_branch_s_last_commit_to_when_it_was_cut():
    merged = preview_task.merge_candidates(
        "carameli",
        [box("carameli--ui", branch="agent/ui", created="2026-08-20T03:00:00Z")],
        [],
        [],
        dates={"agent/ui": "2026-08-21T17:58:00Z"},
    )
    assert merged[0].updated == "2026-08-21T17:58:00Z"


def test_a_box_whose_branch_git_cannot_date_falls_back_to_when_it_was_cut():
    merged = preview_task.merge_candidates(
        "carameli",
        [box("carameli--ui", branch="agent/ui", created="2026-08-20T03:00:00Z")],
        [],
        [],
        dates={"agent/other": "2026-08-21T17:58:00Z"},
    )
    assert merged[0].updated == "2026-08-20T03:00:00Z"


def test_a_source_row_with_no_ref_is_dropped_rather_than_keyed_on_empty():
    merged = preview_task.merge_candidates(
        "carameli", [box("orphan", branch="")], [pr(1, ""), {"number": 2}], []
    )
    assert merged == []


def test_a_dependabot_pr_contributes_no_row():
    """A dependency bump is never the UI change anyone opened this task to see."""
    rows = preview_task.merge_candidates(
        "carameli",
        [],
        [pr(9, "dependabot/pip/urllib3-2.0.5", title="Bump urllib3")],
        [],
    )
    assert rows == []


def test_a_standing_preview_of_an_ignored_ref_keeps_its_row():
    """The filter drops discovery, never a box someone deliberately brought up."""
    standing = box(
        "carameli--pv-1",
        kind=worktree.PREVIEW_KIND,
        branch="preview/dependabot/pip/urllib3-2.0.5",
        tracks="dependabot/pip/urllib3-2.0.5",
    )
    rows = preview_task.merge_candidates(
        "carameli",
        [standing],
        [pr(9, "dependabot/pip/urllib3-2.0.5", title="Bump urllib3")],
        [],
    )
    assert [(r.kind, r.pr) for r in rows] == [(preview_task.KIND_STANDING, 9)]


def test_an_unattended_jobs_branch_contributes_no_row():
    """The row this menu was asked to stop printing. The nightly vendoring sweep cuts the
    same commit in every consumer, so on any given morning it owns most of the list --
    twenty-eight of twenty-nine rows, the day the menu was first printed -- and none of
    it is a UI change anyone asked to look at."""
    auto = f"{preview_task.tb.AUTOMATION_PREFIX}devkit-upgrade-v0-11-2-0823"
    rows = preview_task.merge_candidates(
        "carameli",
        [],
        [pr(9, auto, title="Adopt devkit v0.11.2")],
        [(auto, "2026-08-23T09:00:00Z", "Adopt devkit v0.11.2")],
    )
    assert rows == []


def test_an_unattended_jobs_box_contributes_no_row_either():
    """A live box is discovery too. The old rule kept every box on the grounds that "a
    box exists because someone is working in it" -- true of a session's box, and false of
    one a scheduler cut at 03:00 in six repos at once."""
    auto = f"{preview_task.tb.AUTOMATION_PREFIX}devkit-upgrade-v0-11-2-0823"
    rows = preview_task.merge_candidates("carameli", [box("carameli--x", branch=auto)], [], [])
    assert rows == []


def test_a_session_branch_that_merely_reads_as_automatic_keeps_its_row():
    """Why the marker is a path segment and not a word in the slug: `auto-merge-label` is
    a task somebody gave an agent, and a substring test would hide the very change they
    then asked to see."""
    rows = preview_task.merge_candidates(
        "carameli", [], [], [("agent/auto-merge-label-0823", "2026-08-23T09:00:00Z", "Label it")]
    )
    assert [row.ref for row in rows] == ["agent/auto-merge-label-0823"]


def test_a_standing_preview_of_an_unattended_jobs_branch_keeps_its_row():
    """Someone typed that ref to bring the box up, so it is no longer discovery -- and a
    hidden row is a preview holding a port that nothing in this menu could stop."""
    auto = f"{preview_task.tb.AUTOMATION_PREFIX}devkit-upgrade-v0-11-2-0823"
    standing = box(
        "carameli--pv-2", kind=worktree.PREVIEW_KIND, branch=f"preview/{auto}", tracks=auto
    )
    rows = preview_task.merge_candidates("carameli", [standing], [], [])
    assert [row.kind for row in rows] == [preview_task.KIND_STANDING]


def test_a_branch_never_overwrites_a_row_a_box_or_pr_already_claimed():
    merged = preview_task.merge_candidates(
        "carameli",
        [],
        [pr(164, "agent/ui", title="from the PR", updated="2026-08-21T16:00:00Z")],
        [("agent/ui", "2026-08-21T09:00:00Z", "from the branch")],
    )
    assert merged[0].title == "from the PR"
    assert merged[0].kind == preview_task.KIND_PR


def test_merge_candidates_spends_the_merged_set_after_the_fold():
    """Applied once at the end, not per source: a ref only has to be recognised as landed
    once however many of the three still offer it."""
    rows = preview_task.merge_candidates(
        "carameli",
        [box("carameli--ui", branch="agent/ui")],
        [pr(164, "agent/ui", title="Comic book UI")],
        [("agent/ui", "2026-08-21T09:00:00Z", "subject")],
        None,
        {"agent/ui"},
    )
    assert rows == []


def test_strip_remote_only_strips_the_remote():
    assert preview_task.strip_remote("origin/agent/ui") == "agent/ui"
    assert preview_task.strip_remote("agent/origin/ui") == "agent/origin/ui"


# --- the age cutoff -----------------------------------------------------------


def test_a_branch_inside_the_window_is_kept_and_one_outside_is_not():
    assert preview_task.fresh("2026-08-20T18:00:00Z", days=3.0, now=NOW)
    assert not preview_task.fresh("2026-08-17T17:00:00Z", days=3.0, now=NOW)


def test_a_branch_git_could_not_date_is_kept():
    """Asymmetric costs: a stale row is one line, a dropped row is unreachable."""
    assert preview_task.fresh("", now=NOW)
    assert preview_task.fresh("not a date", now=NOW)


@pytest.mark.parametrize(
    ("stamp", "expected"),
    [
        ("2026-08-21T17:58:00Z", "2m"),
        ("2026-08-21T14:00:00Z", "4h"),
        ("2026-08-19T18:00:00Z", "2d"),
        ("2026-08-21T18:30:00Z", "now"),
        ("nonsense", ""),
    ],
)
def test_age_is_lossy_and_total(stamp, expected):
    assert preview_task.age(stamp, now=NOW) == expected


# --- the cap ------------------------------------------------------------------


def test_trim_reports_what_it_dropped():
    rows = [
        preview_task.Candidate(project="p", ref=str(n), kind=preview_task.KIND_PR)
        for n in range(25)
    ]
    kept, dropped = preview_task.trim(rows, limit=20)
    assert (len(kept), dropped) == (20, 5)


def test_limit_zero_keeps_every_row():
    rows = [
        preview_task.Candidate(project="p", ref=str(n), kind=preview_task.KIND_PR)
        for n in range(25)
    ]
    assert preview_task.trim(rows, limit=0) == (rows, 0)


def test_a_short_list_is_not_trimmed():
    rows = [preview_task.Candidate(project="p", ref="a", kind=preview_task.KIND_PR)]
    assert preview_task.trim(rows, limit=20) == (rows, 0)


def test_the_menu_says_how_many_rows_it_did_not_print():
    rows = [preview_task.Candidate(project="p", ref="a", kind=preview_task.KIND_PR)]
    assert "and 5 older row(s) not shown" in preview_task.render_menu(rows, now=NOW, dropped=5)


def test_an_untrimmed_menu_says_nothing_about_dropping():
    rows = [preview_task.Candidate(project="p", ref="a", kind=preview_task.KIND_PR)]
    assert "not shown" not in preview_task.render_menu(rows, now=NOW)


def test_an_empty_menu_says_why_rather_than_printing_nothing():
    rendered = preview_task.render_menu([], now=NOW)
    assert "Nothing to preview" in rendered
    assert f"{preview_task.BRANCH_MAX_AGE_DAYS:g} days" in rendered


def test_a_row_shows_its_kind_its_pr_and_its_age():
    row = preview_task.Candidate(
        project="carameli",
        ref="agent/ui",
        kind=preview_task.KIND_STANDING,
        box="b",
        slot=4,
        pr=164,
        title="Comic book UI",
        updated="2026-08-21T14:00:00Z",
    )
    rendered = preview_task.render_menu([row], now=NOW)
    assert (
        '  1) carameli  agent/ui  preview box standing (slot 4) - PR #164 - 4h  "Comic book UI"'
        == rendered
    )


# --- the answer ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("3", ("pick", [3])),
        (" 3 \n", ("pick", [3])),
        ("1 3", ("pick", [1, 3])),
        ("1,3", ("pick", [1, 3])),
        (" 1, 3 \n", ("pick", [1, 3])),
        ("3 1", ("pick", [3, 1])),
        ("2 2", ("pick", [2])),
        ("a", ("all", [])),
        ("ALL", ("all", [])),
        ("", ("quit", [])),
        ("\n", ("quit", [])),
        ("q", ("quit", [])),
        ("no", ("quit", [])),
        ("0", ("again", [])),
        ("6", ("again", [])),
        ("frontend", ("again", [])),
        ("1 6", ("again", [])),
        ("1 frontend", ("again", [])),
    ],
)
def test_the_answer_grammar(text, expected):
    assert preview_task.parse_choice(text, count=5) == expected


def test_one_bad_number_rejects_the_whole_line():
    """The numbers are positions in a menu the reader is looking at, so serving two of the
    three they typed is the one outcome they have no way of noticing."""
    assert preview_task.parse_choice("2 9", count=5) == ("again", [])


def test_a_blank_line_quits_rather_than_looping():
    """Enter at the menu is how someone who opened the wrong task gets out of it."""
    assert preview_task.parse_choice("", count=5)[0] == "quit"


# --- dispatch -----------------------------------------------------------------


def test_a_live_box_is_previewed_by_name_and_never_by_ref():
    """Naming both makes `plan_preview` resolve the ref and cut a SECOND worktree."""
    row = preview_task.Candidate(
        project="carameli", ref="agent/ui", kind=preview_task.KIND_BOX, box="carameli--ui"
    )
    assert preview_task.preview_kwargs(row) == {"target": "carameli--ui"}


def test_a_row_with_no_box_is_previewed_by_project_and_branch():
    row = preview_task.Candidate(
        project="carameli", ref="agent/ui", kind=preview_task.KIND_PR, pr=164
    )
    assert preview_task.preview_kwargs(row) == {"target": "carameli", "branch": "agent/ui"}


def test_a_ui_request_goes_by_ref_even_when_the_row_is_a_box():
    """The row's box runs the full stack (or holds the branch under review); the cheap
    kind has its own name and lease, and `plan_preview(ui=True)` finds a standing one
    by that name -- so going by ref is what keeps re-picking a row idempotent."""
    row = preview_task.Candidate(
        project="carameli", ref="agent/ui", kind=preview_task.KIND_BOX, box="carameli--ui"
    )
    assert preview_task.preview_kwargs(row, ui=True) == {
        "target": "carameli",
        "branch": "agent/ui",
        "ui": True,
    }


def test_serve_passes_ui_through_to_the_planner(monkeypatch, tmp_path, capsys):
    seen = {}

    def plan(**kwargs):
        seen.update(kwargs)
        return worktree.PreviewPlan(
            box=worktree.Box(name="b", project="carameli", branch="preview/ui/x"),
            path="p",
            up=True,
        )

    monkeypatch.setattr(preview_task.worktree, "plan_preview", plan)
    monkeypatch.setattr(preview_task.worktree, "apply_preview", lambda plan, ws: (True, []))
    # The donor check opens a real socket against the checkout's app port, and `ui=True`
    # is the mode that asks for it. Its own tests inject the probe; this one is about the
    # planner, so it must not depend on what is listening on the machine running it.
    monkeypatch.setattr(preview_task, "donor_warning", lambda project, workspace: "")
    candidate = preview_task.Candidate(
        project="carameli", ref="agent/x", kind=preview_task.KIND_BRANCH
    )
    assert preview_task.serve(candidate, tmp_path, open_it=False, ui=True) is True
    assert seen["ui"] is True and seen["branch"] == "agent/x"
    assert "(UI only)" in capsys.readouterr().out


def test_the_url_report_puts_the_ui_on_its_own_line():
    """The failure the whole script exists to fix: the frontend is seventh in the list."""
    row = preview_task.Candidate(
        project="carameli", ref="agent/ui", kind=preview_task.KIND_PR, pr=164
    )
    urls = (("app", 8000, "http://localhost:8000"), ("frontend", 5173, "http://localhost:5173"))
    report = preview_task.url_report(row, urls)
    assert "OPEN THIS ->  http://localhost:5173" in report
    assert "(PR #164)" in report
    assert "http://localhost:8000" in report


def test_a_checkout_that_publishes_nothing_says_so():
    row = preview_task.Candidate(
        project="devkit", ref="agent/x", kind=preview_task.KIND_BOX, box="b"
    )
    assert "nothing to open" in preview_task.url_report(row, ())


# --- reading the machine ------------------------------------------------------


def test_local_branch_dates_reads_one_ref_listing():
    dates = _local_dates(
        result("agent/ui\t2026-08-21T13:58:00-04:00\npreview/agent-ui\t2026-08-21T09:00:00Z\n")
    )
    assert dates == {
        "agent/ui": "2026-08-21T13:58:00-04:00",
        "preview/agent-ui": "2026-08-21T09:00:00Z",
    }


def test_local_branch_dates_is_empty_when_git_fails():
    assert _local_dates(result(returncode=128)) == {}
    assert _local_dates(OSError("no git")) == {}


def _local_dates(outcome):
    """`local_branch_dates` against a stubbed git. Restores the real one on any path."""
    original = preview_task.sweep.git_for
    preview_task.sweep.git_for = lambda _dir: lambda *argv: _raise_or(outcome)
    try:
        return preview_task.local_branch_dates(preview_task.REPO_ROOT)
    finally:
        preview_task.sweep.git_for = original


def test_local_branch_dates_asks_for_the_preview_namespace_too(monkeypatch):
    """A standing preview's branch is a `preview/...` head, so omitting it undates it."""
    seen = []
    monkeypatch.setattr(
        preview_task.sweep,
        "git_for",
        lambda _dir: lambda *argv: (seen.append(argv), result(""))[1],
    )
    preview_task.local_branch_dates(preview_task.REPO_ROOT)
    assert "refs/heads/preview" in seen[0]
    assert "refs/heads/agent" in seen[0]


def test_open_prs_survives_every_failure_path(monkeypatch):
    """An offline or unauthenticated machine loses the PR rows and keeps the menu."""
    for outcome in (result("not json"), result("[]", returncode=1), OSError("no gh")):
        monkeypatch.setattr(
            preview_task.sweep,
            "gh_for",
            lambda _dir, outcome=outcome: lambda *argv: _raise_or(outcome),
        )
        assert preview_task.open_prs(preview_task.REPO_ROOT) == []


def test_open_prs_drops_anything_that_is_not_an_object(monkeypatch):
    payload = json.dumps([{"number": 1, "headRefName": "agent/ui"}, "junk"])
    monkeypatch.setattr(preview_task.sweep, "gh_for", lambda _dir: lambda *argv: result(payload))
    assert preview_task.open_prs(preview_task.REPO_ROOT) == [
        {"number": 1, "headRefName": "agent/ui"}
    ]


def test_merged_refs_reads_the_head_branch_of_each_landed_pr(monkeypatch):
    payload = json.dumps(
        [{"headRefName": "agent/landed"}, {"headRefName": ""}, "junk", {"other": 1}]
    )
    monkeypatch.setattr(preview_task.sweep, "gh_for", lambda _dir: lambda *argv: result(payload))
    assert preview_task.merged_refs(preview_task.REPO_ROOT) == {"agent/landed"}


def test_merged_refs_is_empty_on_every_failure_path(monkeypatch):
    """Empty is the SAFE answer for this source, unlike every other one here.

    It subtracts rows, so a failure that guessed would hide a branch somebody is waiting
    to look at. An offline machine loses the filter and keeps the menu it had before.
    """
    for outcome in (result("not json"), result("[]", returncode=1), OSError("no gh")):
        monkeypatch.setattr(
            preview_task.sweep,
            "gh_for",
            lambda _dir, outcome=outcome: lambda *argv: _raise_or(outcome),
        )
        assert preview_task.merged_refs(preview_task.REPO_ROOT) == set()


def test_merged_refs_asks_for_merged_prs_and_nothing_else(monkeypatch):
    """`--state merged`, because `open_prs` next door asks the same command the other way
    and a copied argv would make this filter subtract the rows it exists to keep."""
    seen = []
    monkeypatch.setattr(
        preview_task.sweep,
        "gh_for",
        lambda _dir: lambda *argv: (seen.append(argv), result("[]"))[1],
    )
    preview_task.merged_refs(preview_task.REPO_ROOT, limit=12)
    assert "merged" in seen[0]
    assert "12" in seen[0]


def test_the_trunk_row_is_dated_from_origin_rather_than_the_local_branch(monkeypatch):
    """What a preview serves is `origin/<ref>`, so a local `master` three days behind
    would stamp the row with a date that does not describe what comes up."""
    monkeypatch.setattr(preview_task.tb, "detect_default_branch", lambda git: "master")
    monkeypatch.setattr(
        preview_task.sweep,
        "git_for",
        lambda _dir: lambda *argv: result("2026-08-21T09:00:00-04:00\n"),
    )
    row = preview_task.trunk_row("carameli", preview_task.REPO_ROOT)
    assert row == preview_task.Candidate(
        project="carameli",
        ref="master",
        kind=preview_task.KIND_TRUNK,
        updated="2026-08-21T09:00:00-04:00",
    )


def test_the_trunk_row_survives_a_checkout_git_cannot_answer_for(monkeypatch):
    """An undated row is fine -- `describe` drops the column. No row at all is not: the
    trunk row is what stops a quiet checkout drawing an empty pick list."""
    monkeypatch.setattr(
        preview_task.tb, "detect_default_branch", lambda git: _raise_or(OSError("no git"))
    )
    monkeypatch.setattr(
        preview_task.sweep, "git_for", lambda _dir: lambda *argv: _raise_or(OSError("no git"))
    )
    row = preview_task.trunk_row("carameli", preview_task.REPO_ROOT)
    assert row.ref == "main"
    assert row.updated == ""


def test_origin_ref_date_is_empty_when_git_has_no_such_ref(monkeypatch):
    """`for-each-ref` exits 0 and prints nothing for a ref that does not exist, which is
    the failure that would otherwise read as a successful empty stamp elsewhere."""
    for outcome in (result(""), result("x", returncode=1), OSError("no git")):
        git = lambda *argv, outcome=outcome: _raise_or(outcome)
        assert preview_task.origin_ref_date(git, "master") == ""


def test_recent_branches_parses_the_ref_listing(monkeypatch):
    listing = "origin/agent/ui\t2026-08-21T09:00:00-04:00\tComic book UI\norigin/agent/bare\t2026-08-20T09:00:00Z\t\n"
    monkeypatch.setattr(preview_task.sweep, "git_for", lambda _dir: lambda *argv: result(listing))
    assert preview_task.recent_branches(preview_task.REPO_ROOT, fetch=False) == [
        ("agent/ui", "2026-08-21T09:00:00-04:00", "Comic book UI"),
        ("agent/bare", "2026-08-20T09:00:00Z", ""),
    ]


def test_recent_branches_spends_its_count_on_refs_it_can_keep(monkeypatch):
    """Filtering only downstream would have been a filter that did nothing on a busy
    remote: git sorts and counts before this process sees a byte, and the sweep that cuts
    these runs nightly in every consumer -- so a whole `--count` page of them is the
    normal case. The over-fetch is what leaves a real branch to return, and the cap is
    still honoured on what survives."""
    asked = []
    auto = preview_task.tb.AUTOMATION_PREFIX
    listing = (
        "".join(
            f"origin/{auto}devkit-upgrade-v0-11-{n}-0823\t2026-08-23T0{n}:00:00Z\tAdopt\n"
            for n in range(1, 4)
        )
        + "origin/agent/comic-book-ui-0820\t2026-08-20T09:00:00Z\tComic book UI\n"
    )

    def git(*argv):
        asked.append(argv)
        return result(listing)

    monkeypatch.setattr(preview_task.sweep, "git_for", lambda _dir: git)

    found = preview_task.recent_branches(preview_task.REPO_ROOT, limit=2, fetch=False)

    assert [ref for ref, _date, _subject in found] == ["agent/comic-book-ui-0820"]
    count = next(arg for arg in asked[0] if arg.startswith("--count="))
    assert count == f"--count={2 * preview_task.BRANCH_OVERFETCH}"


def test_recent_branches_caps_what_survives_the_filter(monkeypatch):
    """The over-fetch widens the query, not the answer."""
    listing = "".join(
        f"origin/agent/ui-{n}-0820\t2026-08-2{n}T09:00:00Z\tUI {n}\n" for n in range(1, 5)
    )
    monkeypatch.setattr(preview_task.sweep, "git_for", lambda _dir: lambda *argv: result(listing))
    assert len(preview_task.recent_branches(preview_task.REPO_ROOT, limit=2, fetch=False)) == 2


def test_recent_branches_skips_a_line_it_cannot_split(monkeypatch):
    monkeypatch.setattr(
        preview_task.sweep, "git_for", lambda _dir: lambda *argv: result("garbage\n")
    )
    assert preview_task.recent_branches(preview_task.REPO_ROOT, fetch=False) == []


def test_recent_branches_is_empty_when_git_fails(monkeypatch):
    monkeypatch.setattr(
        preview_task.sweep, "git_for", lambda _dir: lambda *argv: result(returncode=1)
    )
    assert preview_task.recent_branches(preview_task.REPO_ROOT, fetch=False) == []


def _raise_or(outcome):
    if isinstance(outcome, BaseException):
        raise outcome
    return outcome


# --- the dropdown's option file -----------------------------------------------
#
# The VS Code picker cannot run a command, so the two dropdowns read a file this script
# wrote on its last run. That buys a shape with two hard constraints and one soft one,
# and all three are asserted here because none of them fails loudly: the extension builds
# a list by evaluating one expression per field against rising indices until one THROWS,
# so a malformed payload does not error -- it draws ten thousand blank rows, or silently
# omits a checkout whose only offer is its trunk.


def _payload(rows, projects=("carameli",), now=NOW):
    return preview_task.menu_payload(list(rows), list(projects), now)


def _branch(ref, *, project="carameli", updated=""):
    return preview_task.Candidate(
        project=project, ref=ref, kind=preview_task.KIND_BRANCH, updated=updated
    )


def _trunk(ref="master", *, project="carameli", updated=""):
    return preview_task.Candidate(
        project=project, ref=ref, kind=preview_task.KIND_TRUNK, updated=updated
    )


def test_every_row_carries_every_field_as_a_string():
    """The blank-rows failure, and the reason it would never be reported as one.

    An expression that resolves to `undefined` on a row that exists does not end the
    extension's loop -- only one that throws does. So a row missing `detail`, which is
    every branch with no PR title, appends blanks up to the extension's 10000 cap and
    then draws them.
    """
    payload = _payload([_branch("agent/x"), _branch("agent/y", updated="2026-08-21T17:00:00Z")])
    assert payload["rows"]["carameli"]
    for row in payload["rows"]["carameli"]:
        assert set(row) == {"value", "label", "description", "detail"}
        assert all(isinstance(value, str) for value in row.values())
    for entry in payload["projects"]:
        assert set(entry) == {"name", "label", "description"}
        assert all(isinstance(value, str) for value in entry.values())


def test_a_row_is_picked_by_project_and_ref_in_one_token():
    """A VS Code input resolves to one string, so the checkout travels inside the value."""
    payload = _payload([_branch("agent/x")])
    assert payload["rows"]["carameli"][0]["value"] == "carameli:agent/x"
    assert payload["rows"]["carameli"][0]["label"] == "agent/x"


def test_no_row_is_a_control_row():
    """Every row is servable, which is what removing `Rescan` bought.

    The old last row of every checkout resolved to no candidate at all, so `resolve_pick`
    had to be allowed to return nothing and every caller had to handle it. Now a value
    that survives to the resolver always names something that can be brought up.
    """
    payload = _payload([_trunk(), _branch("agent/x")], projects=("carameli", "ibkr_trader"))
    for project in ("carameli", "ibkr_trader"):
        for row in payload["rows"][project]:
            assert "__" not in row["value"]


def test_the_scans_age_is_on_the_checkout_rather_than_a_row():
    """Removing the `Rescan` row removed the only place the timestamp was shown.

    It moved up a level rather than away: the first dropdown's second column carries it,
    so it is read once per run instead of once per checkout's worth of scrolling.
    """
    payload = _payload([_trunk(), _branch("agent/x")])
    assert payload["asOf"] in payload["projects"][0]["description"]


def test_a_checkout_with_nothing_under_review_still_offers_its_trunk():
    """Otherwise the branch pushed thirty seconds ago picks an empty list and stops.

    `collect` adds a trunk row per checkout unconditionally, which is what makes an empty
    pick list unreachable now that no control row exists to fill one.
    """
    payload = _payload(
        [_trunk(), _trunk(project="ibkr_trader", ref="main")],
        projects=("carameli", "ibkr_trader"),
    )
    assert sorted(entry["name"] for entry in payload["projects"]) == ["carameli", "ibkr_trader"]
    assert [row["label"] for row in payload["rows"]["carameli"]] == ["master"]
    assert "trunk only" in payload["projects"][0]["description"]


def test_checkouts_are_ordered_by_their_freshest_row():
    rows = [
        _branch("agent/old", project="ibkr_trader", updated="2026-08-20T10:00:00Z"),
        _branch("agent/new", project="carameli", updated="2026-08-21T17:00:00Z"),
    ]
    payload = _payload(rows, projects=("ibkr_trader", "carameli"))
    assert [entry["name"] for entry in payload["projects"]] == ["carameli", "ibkr_trader"]


def test_a_checkout_note_says_how_many_are_already_standing():
    rows = [
        preview_task.Candidate(
            project="carameli", ref="agent/up", kind=preview_task.KIND_STANDING, box="b"
        ),
        _branch("agent/x"),
    ]
    note = _payload(rows)["projects"][0]["description"]
    assert "2 to look at" in note
    assert "1 already standing" in note


def test_a_row_the_scan_found_is_present_even_though_the_menu_would_trim_it():
    """The cache is written from the untrimmed scan: a dropdown has no screen to fill."""
    rows = [_branch(f"agent/{n}") for n in range(preview_task.MENU_LIMIT + 5)]
    payload = _payload(rows)
    assert len(payload["rows"]["carameli"]) == len(rows)


# --- resolving what the dropdown sent back -------------------------------------


def test_a_pick_resolves_back_to_the_row_it_was_drawn_from():
    rows = [_branch("agent/x"), _branch("agent/y")]
    picked = preview_task.resolve_pick(preview_task.pick_value(rows[1]), rows)
    assert picked == rows[1]


def test_a_pick_prefers_the_checkout_it_names():
    rows = [_branch("agent/x", project="ibkr_trader"), _branch("agent/x", project="carameli")]
    assert preview_task.resolve_pick("carameli:agent/x", rows).project == "carameli"


def test_a_bare_ref_finds_its_checkout():
    """Typed by hand, `--pick-ref agent/x` should not require naming the checkout twice."""
    rows = [_branch("agent/x")]
    assert preview_task.resolve_pick("agent/x", rows) == rows[0]


def test_a_pick_for_a_row_that_has_gone_is_served_as_a_plain_branch():
    """The list was written last run: its box may have been reaped since, its PR merged.

    What survives all of that is the ref, which is the only thing `plan_preview` needs.
    """
    picked = preview_task.resolve_pick("carameli:agent/gone", [])
    assert picked == preview_task.Candidate(
        project="carameli", ref="agent/gone", kind=preview_task.KIND_BRANCH
    )
    assert preview_task.preview_kwargs(picked) == {"target": "carameli", "branch": "agent/gone"}


def test_a_bare_ref_that_matches_nothing_names_no_checkout_to_serve_it_from():
    with pytest.raises(ValueError, match="names no checkout"):
        preview_task.resolve_pick("agent/gone", [])


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("${input:previewRow}", True),
        ("  ${input:previewRow}", True),
        ("carameli:agent/x", False),
        ("agent/x", False),
    ],
)
def test_an_unsubstituted_input_token_reads_as_a_cancel(text, expected):
    assert preview_task.unresolved(text) is expected


def test_a_trunk_row_resolves_back_to_a_plain_branch_preview():
    """The first row of every checkout, and the whole point is that it needs no special case.

    `preview_kwargs` sees a ref like any other, so previewing `master` cuts a `preview/`
    copy of it exactly the way previewing a PR branch does -- the static checkout is never
    touched and never has to be on that branch.
    """
    picked = preview_task.resolve_pick("carameli:master", [_trunk()])
    assert picked.kind == preview_task.KIND_TRUNK
    assert preview_task.preview_kwargs(picked) == {"target": "carameli", "branch": "master"}


def test_a_ref_with_a_slash_survives_the_round_trip():
    """`git check-ref-format` refuses a colon in a ref, so the first one always splits."""
    assert preview_task.parse_pick("carameli:preview/agent/x-0821") == (
        "carameli",
        "preview/agent/x-0821",
    )


# --- several picks at once ----------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("carameli:agent/x", ["carameli:agent/x"]),
        ("carameli:agent/x carameli:agent/y", ["carameli:agent/x", "carameli:agent/y"]),
        ("  carameli:agent/x   carameli:agent/y \n", ["carameli:agent/x", "carameli:agent/y"]),
        ("carameli:agent/x carameli:agent/x", ["carameli:agent/x"]),
        ("", []),
        ("   ", []),
    ],
)
def test_the_ticked_rows_split_on_whitespace(text, expected):
    """A space is the one character `git check-ref-format` refuses that the extension will
    also emit, so it cannot fall inside the half that varies."""
    assert preview_task.split_picks(text) == expected


def test_several_picks_resolve_in_the_order_they_were_ticked():
    rows = [_branch("agent/x"), _branch("agent/y"), _branch("agent/z")]
    picked = preview_task.resolve_picks("carameli:agent/z carameli:agent/x", rows)
    assert [row.ref for row in picked] == ["agent/z", "agent/x"]


def test_every_ticked_row_resolves_to_something_servable():
    """The return is a plain list now, not a list plus a "and rescan" flag.

    Nothing a checkbox list can send back is a control any more, so there is no second
    thing for the caller to be told and no branch in `main` that serves zero rows on
    purpose. Trunk ticked beside a branch is two previews, like any other pair.
    """
    rows = [_trunk(), _branch("agent/x")]
    picked = preview_task.resolve_picks("carameli:master carameli:agent/x", rows)
    assert [row.ref for row in picked] == ["master", "agent/x"]


def test_one_unservable_token_fails_the_whole_value():
    """`--pick-ref` is machine-written, so serving the other two would hide the bug behind
    a preview that worked."""
    with pytest.raises(ValueError, match="names no checkout"):
        preview_task.resolve_picks("carameli:agent/x agent/gone", [_branch("agent/x")])


def _standing(ref, box, *, project="carameli"):
    return preview_task.Candidate(
        project=project, ref=ref, kind=preview_task.KIND_STANDING, box=box
    )


def test_a_rows_serve_target_is_the_box_it_would_land_in():
    box_row = _standing("agent/x", "carameli--preview-x-0824")
    assert preview_task.serve_target(box_row) == "carameli--preview-x-0824"
    assert preview_task.serve_target(_branch("agent/x-0824")) == worktree.preview_box_name(
        "carameli", "agent/x-0824"
    )


def test_a_ui_pick_targets_the_ui_box_even_for_a_row_that_carries_a_full_one():
    """`preview_kwargs` sends a `--ui` row by ref whatever box it names, so the identity
    has to follow it there -- otherwise a full-preview row and a branch row for the same
    ref look like two boxes under `--ui` and are one."""
    box_row = _standing("agent/x-0824", "carameli--preview-x-0824")
    assert preview_task.serve_target(box_row, ui=True) == worktree.preview_box_name(
        "carameli", "agent/x-0824", ui=True
    )
    assert preview_task.serve_target(box_row, ui=True) == preview_task.serve_target(
        _branch("agent/x-0824"), ui=True
    )


# Two distinct refs, one box: `preview_box_name` slugifies the topic, so an underscore
# and a hyphen spelling of the same one collide. Two menu rows, and `plan_preview` would
# serve the second as a refresh of the first -- correct, and for the price of a second
# wait nobody could explain.
COLLIDING = ("agent/fix_login-0824", "agent/fix-login-0824")


def test_the_refs_this_file_calls_colliding_really_do_land_in_one_box():
    """Stated rather than assumed: the pair above is a fixture, and a slug rule that stops
    collapsing it would leave every test below asserting nothing."""
    assert worktree.preview_box_name("carameli", COLLIDING[0]) == worktree.preview_box_name(
        "carameli", COLLIDING[1]
    )


def test_two_picks_that_land_in_one_box_are_served_once_and_said_so():
    rows = [_branch(COLLIDING[0]), _branch(COLLIDING[1])]
    kept, notes = preview_task.dedupe(rows)
    assert kept == [rows[0]]
    assert len(notes) == 1 and "picked once, not twice" in notes[0]
    assert COLLIDING[1] in notes[0] and COLLIDING[0] in notes[0]


def test_dedupe_keeps_rows_that_land_in_different_boxes():
    rows = [_branch("agent/x-0824"), _branch("agent/y-0824")]
    kept, notes = preview_task.dedupe(rows)
    assert kept == rows and notes == []


def test_a_ui_pick_collapses_a_standing_full_box_onto_the_branch_row_beside_it():
    """`--ui` names how to CUT a box rather than which one is standing, so the full
    preview's row and a plain branch row for the same ref are one UI box -- and are two
    rows in a menu drawn before anyone said `--ui`."""
    rows = [_standing("agent/x-0824", "carameli--preview-x-0824"), _branch("agent/x-0824")]
    kept, notes = preview_task.dedupe(rows, ui=True)
    assert kept == [rows[0]] and len(notes) == 1
    # ...and stay two boxes without it: the full preview is the box, the branch is a
    # second full preview of the same ref, which is the same box again -- so this pair
    # collapses either way, for two different reasons. Assert the reason, not the count.
    assert preview_task.serve_target(rows[0]) != preview_task.serve_target(rows[0], ui=True)


# --- the CLI ------------------------------------------------------------------


@pytest.fixture
def stub(monkeypatch, tmp_path):
    """A workspace file that exists, a fixed menu, and `serve_all` recorded rather than run.

    `serve_all` rather than `serve` because that is what `main` calls, and it is the seam
    that carries the whole answer: the refs recorded here are the rows `main` decided on,
    in order, after `dedupe` has had them.
    """
    workspace = tmp_path / "alex-projects.code-workspace"
    workspace.write_text("{}", encoding="utf-8")
    served = []

    def serve_all(rows, *a, **k):
        served.extend(row.ref for row in rows)
        return 0

    monkeypatch.setattr(preview_task, "serve_all", serve_all)
    monkeypatch.setattr(preview_task, "MENU_CACHE", tmp_path / "logs" / "preview-menu.json")
    return types.SimpleNamespace(
        workspace=workspace,
        served=served,
        monkeypatch=monkeypatch,
        cache=tmp_path / "logs" / "preview-menu.json",
    )


def _menu(stub, rows):
    stub.monkeypatch.setattr(preview_task, "collect", lambda *a, **k: rows)


# --- the scan, and the scheduled pass that keeps it current ---------------------


@pytest.fixture
def scan(monkeypatch, tmp_path):
    """`collect` with every source stubbed: one checkout, and whatever the test gives it."""
    workspace = tmp_path / "alex-projects.code-workspace"
    workspace.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(preview_task, "stack_projects", lambda ws: ["carameli"])
    monkeypatch.setattr(preview_task, "ui_projects", lambda ws: ["carameli"])
    monkeypatch.setattr(preview_task.worktree, "live_boxes", lambda root: {})
    monkeypatch.setattr(preview_task, "recent_branches", lambda d, fetch=True: [])
    monkeypatch.setattr(preview_task, "open_prs", lambda d: [])
    monkeypatch.setattr(preview_task, "local_branch_dates", lambda d: {})
    monkeypatch.setattr(preview_task, "merged_refs", lambda d: set())
    monkeypatch.setattr(
        preview_task,
        "trunk_row",
        lambda project, d: preview_task.Candidate(
            project=project, ref="master", kind=preview_task.KIND_TRUNK
        ),
    )
    return types.SimpleNamespace(workspace=workspace, monkeypatch=monkeypatch, tmp=tmp_path)


# --- which checkouts each menu is allowed to offer ------------------------------


def _checkout(root, name: str, manifest: str = "") -> None:
    """A directory beside the workspace file, with the `.devkit.toml` a test asks for."""
    (root / name).mkdir(parents=True, exist_ok=True)
    if manifest:
        (root / name / ".devkit.toml").write_text(manifest, encoding="utf-8")


@pytest.mark.parametrize(
    "manifest, expected",
    [
        ('[frontend]\nenabled = true\ndir = "frontend"\n', "frontend"),
        ('[frontend]\nenabled = false\ndir = "frontend"\n', ""),
        ("[frontend]\nenabled = true\n", ""),
        ("[project]\nname = 'x'\n", ""),
    ],
)
def test_frontend_rel_reads_an_enabled_frontend(manifest, expected):
    assert preview_task.frontend_rel(tomllib.loads(manifest)) == expected


def test_frontend_dir_for_reads_the_checkouts_manifest(tmp_path):
    _checkout(tmp_path, "carameli", '[frontend]\nenabled = true\ndir = "frontend"\n')
    assert preview_task.frontend_dir_for(tmp_path / "carameli") == "frontend"


@pytest.mark.parametrize("manifest", ["", "[frontend\nenabled = true\n"])
def test_frontend_dir_for_is_empty_when_the_manifest_cannot_be_read(tmp_path, manifest):
    """A missing manifest and an unparseable one are both "cannot be served", not a crash.

    The caller is either a refusal that names the project or the list that leaves it out,
    and neither has anything better to do with an exception.
    """
    _checkout(tmp_path, "carameli", manifest)
    assert preview_task.frontend_dir_for(tmp_path / "carameli") == ""


def test_the_dropdown_offers_only_checkouts_that_declare_a_frontend(tmp_path, monkeypatch):
    """`ui_projects` is what stops the task offering a checkout it would then refuse.

    The three cases are the three real ones on this machine: a frontend project, a
    backend-only service that switches the tier off, and a registered checkout that is
    not on this disk at all.
    """
    # Not named for the live registry: `known_projects` is stubbed, so the name is
    # arbitrary here -- and a test that *spells* the live one is required to carry
    # `@needs_live_workspace`, which this does not need and CI would then skip.
    workspace = tmp_path / "scratch.code-workspace"
    workspace.write_text("{}", encoding="utf-8")
    _checkout(tmp_path, "carameli", '[frontend]\nenabled = true\ndir = "frontend"\n')
    _checkout(tmp_path, "ibkr_trader", "[frontend]\nenabled = false\n")
    monkeypatch.setattr(
        preview_task.worktree,
        "known_projects",
        lambda ws: ["carameli", "ibkr_trader", "not_cloned"],
    )
    assert preview_task.ui_projects(workspace) == ["carameli"]


def test_collect_scans_only_the_checkouts_it_is_given(scan, monkeypatch):
    """The narrowing is a parameter, not a filter on the result: each checkout in the list
    costs a `git fetch` and a `gh pr list`, and this runs every fifteen minutes."""
    scanned = []
    monkeypatch.setattr(preview_task, "open_prs", lambda d: scanned.append(d.name) or [])
    preview_task.collect(scan.workspace, fetch=False, projects=["ibkr_trader"])
    assert scanned == ["ibkr_trader"]


def test_menu_payload_drops_a_row_from_an_unlisted_checkout():
    """Belt and braces over `collect`'s narrowing, and the same reason: the file this
    builds is read by a task that can only serve a frontend, so a row from anywhere else
    is an option that refuses when it is picked."""
    rows = [
        preview_task.Candidate(project="carameli", ref="agent/x", kind=preview_task.KIND_BRANCH),
        preview_task.Candidate(project="ibkr_trader", ref="agent/y", kind=preview_task.KIND_BRANCH),
    ]
    payload = preview_task.menu_payload(rows, ["carameli"])
    assert [entry["name"] for entry in payload["projects"]] == ["carameli"]
    assert list(payload["rows"]) == ["carameli"]


def test_every_checkout_contributes_its_trunk_row(scan):
    rows = preview_task.collect(scan.workspace, fetch=False)
    assert [(row.ref, row.kind) for row in rows] == [("master", preview_task.KIND_TRUNK)]


def test_the_trunk_row_sorts_above_everything_else(scan):
    """The user-visible half of the request: the default branch is the FIRST option.

    Rank, not recency -- `master` is usually older than the branch under review, so a
    date-only sort would bury the row that exists to be reached without scrolling.
    """
    scan.monkeypatch.setattr(
        preview_task, "recent_branches", lambda d, fetch=True: [("agent/x", NOW.isoformat(), "s")]
    )
    rows = preview_task.collect(scan.workspace, fetch=False, now=NOW)
    assert [row.ref for row in rows] == ["master", "agent/x"]


def test_a_richer_row_for_the_default_branch_is_not_duplicated_by_the_trunk_row(scan):
    """A live box or an open PR on `master` keeps its own row rather than getting a
    second, plainer one saying the same thing."""
    scan.monkeypatch.setattr(
        preview_task, "open_prs", lambda d: [pr(9, "master", title="a release PR")]
    )
    rows = preview_task.collect(scan.workspace, fetch=False)
    assert [row.ref for row in rows] == ["master"]
    assert rows[0].kind == preview_task.KIND_PR
    assert rows[0].title == "a release PR"


def test_refresh_menu_writes_the_options_file(scan):
    path = scan.tmp / "logs" / "menu.json"
    assert preview_task.refresh_menu(scan.workspace, fetch=False, path=path) == path
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["rows"]["carameli"][0]["label"] == "master"


def test_refresh_menu_swallows_a_failure_rather_than_raising(scan):
    """It is a rider on `worktree.py reconcile`, so it must never fail a pass that reaped
    boxes correctly. A menu that could not be built costs one stale dropdown."""
    scan.monkeypatch.setattr(
        preview_task,
        "ui_projects",
        lambda ws: _raise_or(RuntimeError("registry is a directory")),
    )
    assert preview_task.refresh_menu(scan.workspace, fetch=False) is None


def test_a_missing_workspace_registry_is_reported_not_traced(tmp_path, capsys):
    assert preview_task.main(["--workspace", str(tmp_path / "gone.code-workspace")]) == 2
    assert "no workspace registry" in capsys.readouterr().out


def test_an_escaped_dropdown_cancels_before_the_scan(stub, capsys):
    """Esc at either dropdown runs the task with `${input:previewRow}` as literal text.

    That is a cancel, so it must exit 0 without fetching, scanning, or serving --
    the first version split the token on its colon and sent project `"${input"` into
    `worktree.py`, which traced out of `resolve_project`.
    """
    stub.monkeypatch.setattr(
        preview_task, "collect", lambda *a, **k: pytest.fail("a cancelled run must not scan")
    )
    argv = ["--workspace", str(stub.workspace), "--pick-ref", "${input:previewRow}"]
    assert preview_task.main(argv) == 0
    assert stub.served == []
    assert "cancelled" in capsys.readouterr().out


def test_a_pick_naming_an_unknown_checkout_is_reported_not_traced(monkeypatch, tmp_path, capsys):
    """`ProjectError` is a wrong answer -- a stale or hand-typed pick -- not a crash."""

    def refuse(**kwargs):
        raise preview_task.devkit_project.ProjectError("unknown project 'nope'")

    monkeypatch.setattr(preview_task.worktree, "plan_preview", refuse)
    candidate = preview_task.Candidate(project="nope", ref="agent/x", kind=preview_task.KIND_BRANCH)
    assert preview_task.serve(candidate, tmp_path) is False
    assert "failed: unknown project 'nope'" in capsys.readouterr().out


def test_a_full_port_registry_is_reported_not_traced(monkeypatch, tmp_path, capsys):
    """The failure this task actually hit. `next_lease_slot` raises `RegistryError` when
    every slot is leased, and it carries the three ways out in its own message -- so the
    one thing that must not happen is the reader having to find that remedy under a
    stack trace in `logs/preview-open-a-ui-branch.log`."""

    def full(**kwargs):
        raise preview_task.devkit_ports.RegistryError("all 16 port slots are in use. Reap a box")

    monkeypatch.setattr(preview_task.worktree, "plan_preview", full)
    candidate = preview_task.Candidate(
        project="carameli", ref="agent/x", kind=preview_task.KIND_BRANCH
    )
    assert preview_task.serve(candidate, tmp_path) is False
    assert "failed: all 16 port slots are in use. Reap a box" in capsys.readouterr().out


def test_list_json_emits_every_field_of_every_row(stub, capsys):
    _menu(
        stub,
        [
            preview_task.Candidate(
                project="carameli", ref="agent/ui", kind=preview_task.KIND_PR, pr=164
            )
        ],
    )
    assert preview_task.main(["--workspace", str(stub.workspace), "--list", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["ref"] == "agent/ui"
    assert payload[0]["pr"] == 164


def test_list_and_pick_number_the_same_rows(stub, capsys):
    """Trimmed once, in `main`, so `--pick 3` cannot mean two different rows."""
    rows = [
        preview_task.Candidate(project="p", ref=f"agent/{n}", kind=preview_task.KIND_PR, updated="")
        for n in range(5)
    ]
    _menu(stub, rows)
    assert preview_task.main(["--workspace", str(stub.workspace), "--list", "--limit", "3"]) == 0
    listed = [line for line in capsys.readouterr().out.splitlines() if line.strip()[:1].isdigit()]
    assert len(listed) == 3
    assert (
        preview_task.main(["--workspace", str(stub.workspace), "--pick", "3", "--limit", "3"]) == 0
    )
    assert stub.served == [listed[2].split()[2]]


def test_a_pick_past_the_end_of_the_menu_reprints_it(stub, capsys):
    _menu(stub, [preview_task.Candidate(project="p", ref="agent/ui", kind=preview_task.KIND_PR)])
    assert preview_task.main(["--workspace", str(stub.workspace), "--pick", "4"]) == 2
    out = capsys.readouterr().out
    assert "--pick 4 is out of range" in out
    assert "agent/ui" in out
    assert stub.served == []


def test_an_empty_machine_asks_nothing(stub, capsys):
    _menu(stub, [])
    assert preview_task.main(["--workspace", str(stub.workspace)]) == 0
    assert "Nothing to preview" in capsys.readouterr().out


def test_all_serves_every_standing_preview_and_nothing_else(stub):
    _menu(
        stub,
        [
            preview_task.Candidate(project="p", ref="a", kind=preview_task.KIND_STANDING, box="ba"),
            preview_task.Candidate(project="p", ref="b", kind=preview_task.KIND_STANDING, box="bb"),
            preview_task.Candidate(project="p", ref="c", kind=preview_task.KIND_BOX, box="bc"),
        ],
    )
    assert preview_task.main(["--workspace", str(stub.workspace), "--all"]) == 0
    assert stub.served == ["a", "b"]


def test_ui_with_all_is_refused_before_the_scan(stub, capsys):
    """`--all` re-serves boxes as their leases say; `--ui` names how to CUT one.
    Combined they would cut a UI twin of every standing full preview."""
    stub.monkeypatch.setattr(
        preview_task, "collect", lambda *a, **k: pytest.fail("refused before the scan")
    )
    assert preview_task.main(["--workspace", str(stub.workspace), "--ui", "--all"]) == 2
    assert "Drop one" in capsys.readouterr().out
    assert stub.served == []


def test_main_passes_ui_to_serve(stub):
    _menu(stub, [preview_task.Candidate(project="p", ref="agent/ui", kind=preview_task.KIND_PR)])
    kwargs_seen = []
    stub.monkeypatch.setattr(
        preview_task, "serve_all", lambda rows, *a, **k: kwargs_seen.append(k) or 0
    )
    assert preview_task.main(["--workspace", str(stub.workspace), "--pick", "1", "--ui"]) == 0
    assert kwargs_seen[0]["ui"] is True


def test_all_with_no_standing_preview_says_so(stub, capsys):
    _menu(
        stub, [preview_task.Candidate(project="p", ref="c", kind=preview_task.KIND_BOX, box="bc")]
    )
    assert preview_task.main(["--workspace", str(stub.workspace), "--all"]) == 0
    assert "nothing to bring back up" in capsys.readouterr().out
    assert stub.served == []


def test_a_failed_serve_is_a_nonzero_exit(stub):
    _menu(stub, [preview_task.Candidate(project="p", ref="a", kind=preview_task.KIND_PR)])
    stub.monkeypatch.setattr(preview_task, "serve_all", lambda *a, **k: 1)
    assert preview_task.main(["--workspace", str(stub.workspace), "--pick", "1"]) == 1


def test_eof_at_the_menu_quits_and_names_the_non_interactive_spelling(stub, capsys, monkeypatch):
    """A scheduled run has no stdin; that is "nothing was picked", not an error."""
    _menu(stub, [preview_task.Candidate(project="p", ref="a", kind=preview_task.KIND_PR)])
    monkeypatch.setattr(preview_task.sys, "stdin", types.SimpleNamespace(readline=lambda: ""))
    assert preview_task.main(["--workspace", str(stub.workspace)]) == 0
    assert "--pick N" in capsys.readouterr().out
    assert stub.served == []


def test_the_menu_retries_on_an_answer_it_cannot_read(stub, capsys, monkeypatch):
    _menu(stub, [preview_task.Candidate(project="p", ref="a", kind=preview_task.KIND_PR)])
    answers = iter(["frontend\n", "1\n"])
    monkeypatch.setattr(
        preview_task.sys, "stdin", types.SimpleNamespace(readline=lambda: next(answers))
    )
    assert preview_task.main(["--workspace", str(stub.workspace)]) == 0
    assert "not one of the options" in capsys.readouterr().out
    assert stub.served == ["a"]


def test_the_prompt_offers_all_only_when_something_is_standing(stub, capsys, monkeypatch):
    monkeypatch.setattr(preview_task.sys, "stdin", types.SimpleNamespace(readline=lambda: ""))
    _menu(stub, [preview_task.Candidate(project="p", ref="a", kind=preview_task.KIND_PR)])
    preview_task.main(["--workspace", str(stub.workspace)])
    assert "for all" not in capsys.readouterr().out
    _menu(
        stub,
        [preview_task.Candidate(project="p", ref="a", kind=preview_task.KIND_STANDING, box="b")],
    )
    preview_task.main(["--workspace", str(stub.workspace)])
    assert "`a` for all 1 standing preview(s)" in capsys.readouterr().out


def test_every_run_leaves_the_dropdown_a_fresh_option_file(stub, monkeypatch):
    """Nothing schedules this refresh: the previous preview is what keeps the list warm."""
    monkeypatch.setattr(preview_task, "ui_projects", lambda w: ["carameli"])
    _menu(stub, [_branch("agent/x")])
    assert preview_task.main(["--workspace", str(stub.workspace), "--pick", "1"]) == 0
    payload = json.loads(stub.cache.read_text(encoding="utf-8"))
    assert payload["rows"]["carameli"][0]["value"] == "carameli:agent/x"


def test_refresh_writes_the_options_and_picks_nothing(stub, capsys, monkeypatch):
    monkeypatch.setattr(preview_task, "ui_projects", lambda w: ["carameli"])
    _menu(stub, [_branch("agent/x")])
    assert preview_task.main(["--workspace", str(stub.workspace), "--refresh"]) == 0
    assert stub.served == []
    assert stub.cache.is_file()
    assert str(stub.cache) in capsys.readouterr().out


def test_refresh_is_a_failure_when_the_options_cannot_be_written(stub, capsys, monkeypatch):
    monkeypatch.setattr(preview_task, "write_menu", lambda *a, **k: None)
    _menu(stub, [])
    assert preview_task.main(["--workspace", str(stub.workspace), "--refresh"]) == 1
    assert "Could not write" in capsys.readouterr().out


def test_pick_ref_serves_that_row_without_asking(stub, monkeypatch):
    monkeypatch.setattr(preview_task.sys, "stdin", types.SimpleNamespace(readline=lambda: ""))
    _menu(stub, [_branch("agent/x"), _branch("agent/y")])
    argv = ["--workspace", str(stub.workspace), "--pick-ref", "carameli:agent/y"]
    assert preview_task.main(argv) == 0
    assert stub.served == ["agent/y"]


def test_pick_ref_reaches_a_row_the_terminal_menu_would_have_trimmed(stub):
    """`--limit` is a screenful, not a scope: the dropdown offers rows past the end."""
    rows = [_branch(f"agent/{n}") for n in range(preview_task.MENU_LIMIT + 3)]
    _menu(stub, rows)
    last = rows[-1].ref
    assert (
        preview_task.main(["--workspace", str(stub.workspace), "--pick-ref", f"carameli:{last}"])
        == 0
    )
    assert stub.served == [last]


def test_pick_ref_on_an_empty_machine_still_serves_the_ref(stub):
    """A branch pushed since the last scan is not in the list and is still previewable."""
    _menu(stub, [])
    argv = ["--workspace", str(stub.workspace), "--pick-ref", "carameli:agent/brand-new"]
    assert preview_task.main(argv) == 0
    assert stub.served == ["agent/brand-new"]


def test_several_ticked_rows_are_all_served_in_one_run(stub, monkeypatch):
    """The whole point of the checkbox list: two branches on screen from one click."""
    monkeypatch.setattr(preview_task.sys, "stdin", types.SimpleNamespace(readline=lambda: ""))
    _menu(stub, [_branch("agent/x"), _branch("agent/y"), _branch("agent/z")])
    value = "carameli:agent/z carameli:agent/x"
    assert preview_task.main(["--workspace", str(stub.workspace), "--pick-ref", value]) == 0
    assert stub.served == ["agent/z", "agent/x"]


def test_two_ticked_rows_sharing_a_box_are_served_once_and_the_drop_is_named(stub, capsys):
    """The half of the no-duplicates guarantee this script owns. `worktree.plan_preview`
    holds the other half -- it refreshes a box of that name rather than cutting one -- so
    a duplicate here would have been correct and merely wasteful, which is the kind of
    waste nothing reports."""
    _menu(stub, [_branch(COLLIDING[0]), _branch(COLLIDING[1])])
    value = f"carameli:{COLLIDING[0]} carameli:{COLLIDING[1]}"
    assert preview_task.main(["--workspace", str(stub.workspace), "--pick-ref", value]) == 0
    assert stub.served == [COLLIDING[0]]
    assert "picked once, not twice" in capsys.readouterr().out


def test_the_trunk_row_is_served_like_any_other_pick(stub):
    """The first row of every checkout's list, and it goes through no special path."""
    _menu(stub, [_trunk(), _branch("agent/x")])
    argv = ["--workspace", str(stub.workspace), "--pick-ref", "carameli:master"]
    assert preview_task.main(argv) == 0
    assert stub.served == ["master"]


def test_the_terminal_menu_takes_several_numbers_too(stub, monkeypatch):
    """The terminal menu is the dropdown's fallback, so a single-select one would
    dead-end the caller who reached it wanting two rows."""
    monkeypatch.setattr(preview_task.sys, "stdin", types.SimpleNamespace(readline=lambda: "2 1\n"))
    _menu(stub, [_branch("agent/x"), _branch("agent/y")])
    assert preview_task.main(["--workspace", str(stub.workspace)]) == 0
    assert stub.served == ["agent/y", "agent/x"]


def test_a_pick_ref_that_names_no_checkout_is_reported_not_traced(stub, capsys):
    _menu(stub, [])
    assert preview_task.main(["--workspace", str(stub.workspace), "--pick-ref", "agent/gone"]) == 2
    assert "matches no row" in capsys.readouterr().out
    assert stub.served == []


# --- waiting for the page, rather than opening a refused connection -----------


class Clock:
    """A monotonic clock that only moves when something sleeps.

    The wait is the one part of this script measured in minutes, so every test of it
    would otherwise be a test that really waits. Injecting the pair keeps the assertion
    on the *shape* of the wait and the runtime at zero.
    """

    def __init__(self) -> None:
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def __call__(self) -> float:
        return self.now


def test_an_http_error_status_counts_as_an_answer(monkeypatch):
    """Vite 404s for a path its router does not know, and a UI-only box whose borrowed
    backend is down 502s through the proxy. Both mean a server accepted the connection,
    which is the only question here -- reading either as "not ready" waits out the whole
    timeout on a preview that is already on screen."""

    def not_found(url, timeout=0):
        raise preview_task.urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setattr(preview_task.urllib.request, "urlopen", not_found)
    assert preview_task.probe("http://127.0.0.1:5180/") is True


def test_a_refused_connection_is_not_an_answer(monkeypatch):
    def refuse(url, timeout=0):
        raise preview_task.urllib.error.URLError(ConnectionRefusedError())

    monkeypatch.setattr(preview_task.urllib.request, "urlopen", refuse)
    assert preview_task.probe("http://127.0.0.1:5180/") is False


def test_the_wait_returns_the_moment_the_url_answers():
    clock = Clock()
    seen = []
    ready, waited = preview_task.wait_for_ready(
        "http://x",
        poll=2.0,
        check=lambda url: seen.append(url) or len(seen) >= 3,
        sleep=clock.sleep,
        clock=clock,
    )
    assert (ready, waited) == (True, 4.0)
    assert len(seen) == 3


def test_the_wait_gives_up_rather_than_hanging_forever():
    """A box wedged on a build takes forever; the timeout is what keeps it from holding
    the terminal. With no `progress` to reset it, the deadline is plain elapsed time --
    the shape this had before it learned to watch the logs, and still the shape any
    caller gets that has nothing to watch."""
    clock = Clock()
    ready, waited = preview_task.wait_for_ready(
        "http://x",
        timeout=10.0,
        poll=2.0,
        tick=1000.0,
        check=lambda url: False,
        sleep=clock.sleep,
        clock=clock,
    )
    assert (ready, waited) == (False, 10.0)


def test_the_wait_speaks_on_the_tick_and_not_on_every_poll(capsys):
    """Silence for a minute reads as a hang -- which is the report that started this.
    A line per poll is the other failure: 40 identical lines say nothing either."""
    clock = Clock()
    preview_task.wait_for_ready(
        "http://x",
        timeout=40.0,
        poll=5.0,
        tick=15.0,
        check=lambda url: False,
        sleep=clock.sleep,
        clock=clock,
    )
    assert capsys.readouterr().out.count("is not answering yet") == 2


def test_a_slow_install_is_not_abandoned_while_it_is_still_talking():
    """The measured failure, 2026-08-24: carameli's frontend box printed `added 702
    packages, and audited 703 packages in 8m` and then served its page fine -- but the
    flat 420s deadline had expired at 7m, so the task reported a working preview as one
    that "may still be starting" and never opened the tab.

    Raising the number would only move the edge. What is asserted here is that elapsed
    time no longer ends the wait at all while the stack is still saying things: this run
    passes 420s of silence-budget more than once over and still returns ready.
    """
    clock = Clock()
    ready, waited = preview_task.wait_for_ready(
        "http://x",
        timeout=420.0,
        poll=5.0,
        tick=15.0,
        progress=lambda: f"frontend-1  | npm install ... {clock.now:.0f}s",
        check=lambda url: clock.now >= 1000.0,
        sleep=clock.sleep,
        clock=clock,
    )
    assert (ready, waited) == (True, 1000.0)


def test_a_stack_that_stops_saying_anything_new_is_given_up_on():
    """The other half: silence is the give-up condition, so a box repeating one line
    forever is as dead as a box printing nothing. The clock starts at the last *new*
    line -- here 15s, so it ends at 55s and not at 40s."""
    clock = Clock()
    ready, waited = preview_task.wait_for_ready(
        "http://x",
        timeout=40.0,
        poll=5.0,
        tick=15.0,
        progress=lambda: "worker-1  | waiting for db",
        check=lambda url: False,
        sleep=clock.sleep,
        clock=clock,
    )
    assert (ready, waited) == (False, 55.0)


def test_the_ceiling_ends_a_wait_that_a_chatty_neighbour_would_not():
    """Progress is sampled across the whole stack, not per service, so a database
    checkpointing every second keeps the box looking alive while the one container being
    waited on is dead. The ceiling is the only bound that case has."""
    clock = Clock()
    ready, waited = preview_task.wait_for_ready(
        "http://x",
        timeout=40.0,
        poll=5.0,
        tick=15.0,
        ceiling=100.0,
        progress=lambda: f"db-1  | checkpoint {clock.now:.0f}",
        check=lambda url: False,
        sleep=clock.sleep,
        clock=clock,
    )
    assert (ready, waited) == (False, 100.0)


def test_the_tick_says_what_the_container_is_doing(capsys):
    """A bare "not answering yet", repeated, is exactly what a hang looks like -- which
    is how a box that was installing got reported as spinning forever. The last log line
    separates the two, and it is free: the same sample already decides whether to wait."""
    clock = Clock()
    preview_task.wait_for_ready(
        "http://x",
        timeout=40.0,
        poll=5.0,
        tick=15.0,
        progress=lambda: "frontend-1  | added 702 packages",
        check=lambda url: False,
        sleep=clock.sleep,
        clock=clock,
    )
    out = capsys.readouterr().out
    assert "-- frontend-1  | added 702 packages" in out
    assert "(the container is up)" not in out


def test_the_tick_shows_the_line_that_appeared_and_not_a_fixed_one(capsys):
    """A stack answers for every container at once, so something has to pick. Picking by
    position picks by container name, which has nothing to do with which container is the
    slow one -- here the db never changes and would be shown forever. What appeared since
    the last sample is the answer, and it costs nothing: it is the same comparison that
    decides whether to keep waiting."""
    clock = Clock()
    samples = iter(
        [
            "db-1  | ready to accept connections\nfrontend-1  | npm install",
            "db-1  | ready to accept connections\nfrontend-1  | added 702 packages",
        ]
    )
    preview_task.wait_for_ready(
        "http://x",
        timeout=40.0,
        poll=5.0,
        tick=15.0,
        progress=lambda: next(samples, "db-1  | ready to accept connections"),
        check=lambda url: False,
        sleep=clock.sleep,
        clock=clock,
    )
    ticks = [
        line for line in capsys.readouterr().out.splitlines() if "is not answering yet" in line
    ]
    assert ticks[0].endswith("-- frontend-1  | npm install")
    assert ticks[1].endswith("-- frontend-1  | added 702 packages")
    # The third sample drops a line rather than adding one, which is not news: the last
    # thing that actually happened stays on screen instead of a container going quiet
    # being reported as the newest event.
    assert ticks[2].endswith("-- frontend-1  | added 702 packages")


def test_the_progress_source_is_sampled_on_the_tick_and_not_on_every_poll():
    """It shells out to docker. One call every two seconds for the length of an install
    would cost more than the thing being waited for, and the ticks are the only moment
    the answer is used -- for the line printed and for the deadline behind it."""
    clock = Clock()
    calls = []
    preview_task.wait_for_ready(
        "http://x",
        timeout=40.0,
        poll=5.0,
        tick=15.0,
        progress=lambda: calls.append(clock.now) or "the same line every time",
        check=lambda url: False,
        sleep=clock.sleep,
        clock=clock,
    )
    assert calls == [15.0, 30.0, 45.0]


# --- waiting on several boxes against ONE deadline ----------------------------


def test_several_urls_cost_the_slowest_rather_than_the_sum():
    """The regression the multi-pick exists to avoid. Two cold boxes install their
    dependencies simultaneously whether or not anything is watching, so waiting on them in
    turn would charge the reviewer 2x for work the machine had already overlapped -- and
    ticking two boxes would come out slower than clicking the task twice."""
    clock = Clock()
    polls = {"http://fast": 0, "http://slow": 0}

    def check(url):
        polls[url] += 1
        return polls[url] >= (2 if url == "http://fast" else 6)

    outcomes = preview_task.wait_for_all(
        ["http://fast", "http://slow"],
        poll=2.0,
        tick=1000.0,
        check=check,
        sleep=clock.sleep,
        clock=clock,
    )
    assert outcomes["http://fast"] == (True, 2.0)
    assert outcomes["http://slow"] == (True, 10.0)
    # Sequentially the slow box would not have been probed until the fast one had
    # finished; the total is the slow one alone, not the two added together.
    assert clock.now == 10.0


def test_the_fast_box_is_not_held_behind_one_that_never_answers():
    clock = Clock()
    outcomes = preview_task.wait_for_all(
        ["http://up", "http://wedged"],
        timeout=10.0,
        poll=2.0,
        tick=1000.0,
        check=lambda url: url == "http://up",
        sleep=clock.sleep,
        clock=clock,
    )
    assert outcomes == {"http://up": (True, 0.0), "http://wedged": (False, 10.0)}


def test_each_box_owns_its_quiet_clock():
    """One chatty box cannot disguise a different box that stopped making progress."""
    clock = Clock()
    outcomes = preview_task.wait_for_all(
        ["http://silent", "http://chatty"],
        timeout=40.0,
        poll=5.0,
        tick=15.0,
        ceiling=100.0,
        progress={
            "http://silent": lambda: "frontend-1  | waiting for db",
            "http://chatty": lambda: f"worker-1  | step {clock.now:.0f}",
        },
        check=lambda url: False,
        sleep=clock.sleep,
        clock=clock,
    )
    assert outcomes == {
        "http://silent": (False, 55.0),
        "http://chatty": (False, 100.0),
    }


def test_the_shared_wait_says_how_many_are_still_silent(capsys):
    clock = Clock()
    preview_task.wait_for_all(
        ["http://a", "http://b"],
        timeout=40.0,
        poll=5.0,
        tick=15.0,
        check=lambda url: False,
        sleep=clock.sleep,
        clock=clock,
    )
    out = capsys.readouterr().out
    assert out.count("not answering yet") == 2
    assert "2 of 2" in out and "http://a, http://b" in out


def test_one_url_is_the_single_wait_and_not_a_second_implementation_of_it(monkeypatch):
    """With nothing to interleave the two are the same wait, and one spelling of "is it up
    yet" is one place for that to be wrong. It is also what keeps `serve`'s output tests
    able to substitute the wait they are not about."""
    monkeypatch.setattr(preview_task, "wait_for_ready", lambda url, **k: (True, 7.0))
    assert preview_task.wait_for_all(["http://only"]) == {"http://only": (True, 7.0)}


def test_no_urls_is_no_wait_at_all():
    assert preview_task.wait_for_all([]) == {}


# --- the backend a UI-only preview borrows ------------------------------------


def _registry(slots, services=None):
    return worktree.devkit_ports.Registry(
        max_slots=16, services=services or {"app": 8000, "frontend": 5180}, slots=slots
    )


def test_port_is_open_answers_for_a_socket_that_is_listening_and_one_that_is_not():
    """The default probe behind `donor_warning`. A real socket on an ephemeral port
    rather than a stub, because the thing worth checking is that a refused connection
    is False rather than an exception -- and it costs a millisecond on loopback."""
    with socket.socket() as server:
        server.bind((preview_task.LOOPBACK, 0))
        server.listen(1)
        port = server.getsockname()[1]
        assert preview_task.port_is_open(port) is True
    assert preview_task.port_is_open(port, timeout=0.25) is False


def test_a_ui_preview_warns_when_the_backend_it_borrows_is_not_listening(monkeypatch, tmp_path):
    """The whole failure mode of `--ui`: the dev server is fine, the page loads, and
    every request in it fails -- which reads as the branch being broken."""
    monkeypatch.setattr(preview_task.worktree, "load_registry", lambda root: _registry({"c": 0}))
    warning = preview_task.donor_warning(
        "c", tmp_path / "ws.code-workspace", listening=lambda p: False
    )
    assert "8000" in warning and "--ui" in warning


def test_no_warning_when_the_backend_answers(monkeypatch, tmp_path):
    monkeypatch.setattr(preview_task.worktree, "load_registry", lambda root: _registry({"c": 0}))
    ports = []
    warning = preview_task.donor_warning(
        "c", tmp_path / "ws.code-workspace", listening=lambda p: ports.append(p) or True
    )
    assert warning == "" and ports == [8000]


@pytest.mark.parametrize(
    "registry",
    [
        pytest.param(lambda root: _registry({"other": 0}), id="checkout-not-pinned"),
        pytest.param(lambda root: None, id="workspace-keeps-no-registry"),
    ],
)
def test_a_warning_that_cannot_be_told_is_not_printed(monkeypatch, tmp_path, registry):
    """A warning that fires on its own uncertainty is one people learn to scroll past."""
    monkeypatch.setattr(preview_task.worktree, "load_registry", registry)

    def listening(port):
        pytest.fail("nothing to probe when there is no port")

    assert (
        preview_task.donor_warning("c", tmp_path / "ws.code-workspace", listening=listening) == ""
    )


def test_an_unreadable_registry_is_not_this_tool_s_news(monkeypatch, tmp_path):
    def broken(root):
        raise worktree.devkit_ports.RegistryError("ports.toml is malformed")

    monkeypatch.setattr(preview_task.worktree, "load_registry", broken)
    assert preview_task.donor_warning("c", tmp_path / "ws.code-workspace") == ""


# --- what `serve` prints, and when it opens a tab ------------------------------


URL = "http://127.0.0.1:5180/"


def _up(monkeypatch, urls=(("frontend", 5180, URL),), up=True):
    """`plan_preview`/`apply_preview` stubbed to a box that came up publishing `urls`."""
    plan = worktree.PreviewPlan(
        box=worktree.Box(name="carameli--preview-x", project="carameli", branch="preview/x"),
        path="p",
        urls=tuple(urls),
        up=up,
    )
    monkeypatch.setattr(preview_task.worktree, "plan_preview", lambda **k: plan)
    monkeypatch.setattr(preview_task.worktree, "apply_preview", lambda p, w: (True, []))
    opened: list[str] = []
    monkeypatch.setattr(preview_task.webbrowser, "open", opened.append)
    return opened


def _candidate():
    return preview_task.Candidate(project="carameli", ref="agent/x", kind=preview_task.KIND_BRANCH)


def test_a_plan_that_brings_nothing_up_fails_fast_rather_than_waiting(
    monkeypatch, tmp_path, capsys
):
    """The measured failure (2026-08-23): a half-reaped box's plan collapsed `up` to
    False, so nothing was started -- while the slot's URLs were still published, so
    `serve` printed "containers started in 0s" and polled a dead port for the whole
    `READY_QUIET`, seven minutes of what read as a working preview coming up."""
    opened = _up(monkeypatch, up=False)
    monkeypatch.setattr(
        preview_task,
        "wait_for_ready",
        lambda *a, **k: pytest.fail("nothing was started, so there is nothing to wait for"),
    )
    assert preview_task.serve(_candidate(), tmp_path) is False
    out = capsys.readouterr().out
    assert "nothing was started" in out
    assert "containers started" not in out
    assert opened == []


def test_serve_waits_for_the_page_then_opens_it(monkeypatch, tmp_path, capsys):
    opened = _up(monkeypatch)
    monkeypatch.setattr(preview_task, "wait_for_ready", lambda url, **k: (True, 61.0))
    assert preview_task.serve(_candidate(), tmp_path) is True
    assert opened == [URL]
    out = capsys.readouterr().out
    assert "containers started in" in out and "answered after 61s" in out and "total:" in out


def test_serve_gives_the_wait_this_boxs_logs_to_watch(monkeypatch, tmp_path):
    """The deadline is silence, so the wait needs a source of noise -- and it has to be
    *this* box's. `compose_tail` is asked for the box's own project name and its own
    path, which is what keeps a neighbouring stack's chatter from holding this wait open
    (or, on the other side, this box's own progress from being invisible)."""
    _up(monkeypatch)
    asked: dict[str, object] = {}
    monkeypatch.setattr(
        preview_task.worktree,
        "compose_tail",
        lambda path, project: asked.update(path=path, project=project) or "db-1  | ready",
    )
    relayed: dict[str, object] = {}

    def fake_wait(url, **kwargs):
        relayed["note"] = kwargs["progress"]()
        return True, 1.0

    monkeypatch.setattr(preview_task, "wait_for_ready", fake_wait)
    assert preview_task.serve(_candidate(), tmp_path) is True
    assert relayed["note"] == "db-1  | ready"
    assert asked == {"path": preview_task.Path("p"), "project": "carameli--preview-x"}


def test_serve_never_opens_a_url_that_has_not_answered(monkeypatch, tmp_path, capsys):
    """The measured failure: `compose up` returns while `npm install` still has a minute
    to run, so the browser opened on a refused connection and the reviewer read that as
    the preview being broken. Reloading by hand is the step they should not have to know."""
    opened = _up(monkeypatch)
    monkeypatch.setattr(preview_task, "wait_for_ready", lambda url, **k: (False, 420.0))
    assert preview_task.serve(_candidate(), tmp_path) is True
    assert opened == []
    out = capsys.readouterr().out
    assert "still silent after 420s" in out and "logs -f" in out
    assert URL in out  # the report still says where it will be


def test_no_wait_returns_as_soon_as_the_containers_start(monkeypatch, tmp_path):
    opened = _up(monkeypatch)
    monkeypatch.setattr(
        preview_task, "wait_for_ready", lambda *a, **k: pytest.fail("--no-wait must not probe")
    )
    assert preview_task.serve(_candidate(), tmp_path, wait=False) is True
    assert opened == [URL]


def test_a_box_that_publishes_nothing_is_not_waited_on(monkeypatch, tmp_path):
    """A stackless project's box has no URL, so there is nothing to poll and no tab."""
    opened = _up(monkeypatch, urls=())
    monkeypatch.setattr(
        preview_task, "wait_for_ready", lambda *a, **k: pytest.fail("no URL to wait for")
    )
    assert preview_task.serve(_candidate(), tmp_path) is True
    assert opened == []


# --- the two halves a multi-row run is made of --------------------------------
#
# `serve` used to be one function, and the split is what lets several rows share a wait.
# These name `start`, `finish` and `Started` directly; the section below drives them
# through `serve_all`, which is how they are actually called.


def test_start_reports_a_ref_it_could_not_plan_and_carries_no_plan_forward(
    monkeypatch, tmp_path, capsys
):
    def blow_up(**kwargs):
        raise worktree.WorktreeError("no such ref agent/x")

    monkeypatch.setattr(preview_task.worktree, "plan_preview", blow_up)

    started = preview_task.start(_branch("agent/x"), tmp_path)

    assert isinstance(started, preview_task.Started)
    assert not started.ok
    assert started.plan is None
    assert started.primary == ""  # nothing for the shared wait to poll
    assert "failed: no such ref agent/x" in capsys.readouterr().out


def test_finish_adds_nothing_for_a_row_that_never_came_up(capsys):
    """`plan is None` is the whole flag, rather than a second boolean beside `ok`.

    A failed row has already said why and a `--down` row has already said where it
    stopped, so anything printed here would be the second line about the same event --
    and on a multi-row run it would land after the other rows' reports, detached from it.
    """
    started = preview_task.Started(_branch("agent/x"), ok=False)
    assert preview_task.finish(started) is False
    assert preview_task.finish(preview_task.Started(_branch("agent/y"), ok=True)) is True
    assert capsys.readouterr().out == ""


def test_finish_opens_the_tab_only_once_the_wait_has_answered(monkeypatch, tmp_path):
    """A tab on a refused connection is the failure the wait exists to stop reporting."""
    opened = _up(monkeypatch)
    started = preview_task.start(_candidate(), tmp_path)

    preview_task.finish(started, outcome=(False, 420.0))
    assert opened == []

    preview_task.finish(started, outcome=(True, 12.0))
    assert opened == [URL]


def test_finish_says_how_long_the_row_took_only_when_something_waited(
    monkeypatch, tmp_path, capsys
):
    """`outcome=None` is `--no-wait`, or a slot publishing nothing -- no elapsed time to
    report, and no silence to warn about."""
    _up(monkeypatch)
    started = preview_task.start(_candidate(), tmp_path)
    capsys.readouterr()

    preview_task.finish(started, outcome=None, open_it=False)
    quiet = capsys.readouterr().out
    assert "total:" not in quiet
    assert "answered after" not in quiet

    preview_task.finish(started, outcome=(True, 9.0), open_it=False)
    spoken = capsys.readouterr().out
    assert "answered after 9s" in spoken
    assert "total:" in spoken


# --- several boxes in one run -------------------------------------------------


def _serve_all_harness(monkeypatch, failing=""):
    """`plan_preview` giving each branch its own box and URL, with the order recorded."""
    events: list[str] = []

    def plan_preview(**kwargs):
        branch = kwargs["branch"]
        events.append(f"start {branch}")
        if branch == failing:
            raise worktree.WorktreeError(f"no such ref {branch}")
        return worktree.PreviewPlan(
            box=worktree.Box(
                name=f"carameli--preview-{branch[-1]}",
                project="carameli",
                branch=f"preview/{branch}",
            ),
            path="p",
            urls=(("frontend", 5180, f"http://127.0.0.1/{branch[-1]}"),),
            up=True,
        )

    monkeypatch.setattr(preview_task.worktree, "plan_preview", plan_preview)
    monkeypatch.setattr(preview_task.worktree, "apply_preview", lambda p, w: (True, []))
    opened: list[str] = []
    monkeypatch.setattr(preview_task.webbrowser, "open", opened.append)

    def wait_for_all(urls, **kwargs):
        events.append("wait " + " ".join(urls))
        return {url: (True, 12.0) for url in urls}

    monkeypatch.setattr(preview_task, "wait_for_all", wait_for_all)
    return events, opened


def test_the_boxes_start_in_turn_and_then_wait_as_one(monkeypatch, tmp_path):
    """Which half is sequential is the design. Starting is -- one git repo, one port
    registry, one image builder. Waiting is not, and a wait per row would hand back the
    time the containers were already spending simultaneously."""
    events, opened = _serve_all_harness(monkeypatch)
    rows = [_branch("agent/a"), _branch("agent/b")]
    assert preview_task.serve_all(rows, tmp_path) == 0
    assert events == [
        "start agent/a",
        "start agent/b",
        "wait http://127.0.0.1/a http://127.0.0.1/b",
    ]
    assert opened == ["http://127.0.0.1/a", "http://127.0.0.1/b"]


def test_a_row_that_never_started_is_counted_and_the_rest_still_come_up(monkeypatch, tmp_path):
    """One bad ref among four must not cost the other three their preview -- and must not
    be reported as a run that worked."""
    events, opened = _serve_all_harness(monkeypatch, failing="agent/b")
    rows = [_branch("agent/a"), _branch("agent/b"), _branch("agent/c")]
    assert preview_task.serve_all(rows, tmp_path) == 1
    assert "wait http://127.0.0.1/a http://127.0.0.1/c" in events
    assert opened == ["http://127.0.0.1/a", "http://127.0.0.1/c"]


def test_no_wait_skips_the_shared_wait_entirely(monkeypatch, tmp_path):
    events, opened = _serve_all_harness(monkeypatch)
    rows = [_branch("agent/a"), _branch("agent/b")]
    assert preview_task.serve_all(rows, tmp_path, wait=False) == 0
    assert not any(event.startswith("wait") for event in events)
    assert opened == ["http://127.0.0.1/a", "http://127.0.0.1/b"]


def test_main_passes_no_wait_through_to_serve(stub):
    _menu(stub, [preview_task.Candidate(project="p", ref="agent/ui", kind=preview_task.KIND_PR)])
    kwargs_seen = []
    stub.monkeypatch.setattr(
        preview_task, "serve_all", lambda rows, *a, **k: kwargs_seen.append(k) or 0
    )
    argv = ["--workspace", str(stub.workspace), "--pick", "1", "--no-wait"]
    assert preview_task.main(argv) == 0
    assert kwargs_seen[0]["wait"] is False


def test_the_wait_is_on_by_default(stub):
    _menu(stub, [preview_task.Candidate(project="p", ref="agent/ui", kind=preview_task.KIND_PR)])
    kwargs_seen = []
    stub.monkeypatch.setattr(
        preview_task, "serve_all", lambda rows, *a, **k: kwargs_seen.append(k) or 0
    )
    assert preview_task.main(["--workspace", str(stub.workspace), "--pick", "1"]) == 0
    assert kwargs_seen[0]["wait"] is True


# --- the task that runs it ----------------------------------------------------


def test_no_task_dispatches_this_script():
    """A terminal tool since 2026-08-25, and the ratchet is the point.

    It held `Preview: Open a UI Branch` while that label meant a compose stack, so one
    click on "show me this branch" cut a box and built images to look at a button. The
    label belongs to `preview-ui-host.py` now. Re-registering this one would not merely
    add a task -- there is one label, and taking it back is how the expensive behaviour
    would return under the name of the cheap one.
    """
    from support import devkit_project

    assert "preview" not in devkit_project.ACTIONS
    assert not [
        name
        for name, action in devkit_project.ACTIONS.items()
        if action.script == "scripts/preview-task.py"
    ]


# --- the small CLI surface ----------------------------------------------------


def test_echo_prints_a_whole_flushed_line(capsys):
    preview_task.echo("hello")
    assert capsys.readouterr().out == "hello\n"


def test_build_parser_defaults_to_an_interactive_fetching_run():
    args = preview_task.build_parser().parse_args([])
    assert args.pick == 0 and args.pick_ref == ""
    assert args.fetch and not args.refresh and not args.all and not args.down and not args.ui
    assert args.limit == preview_task.MENU_LIMIT and args.workspace is None
