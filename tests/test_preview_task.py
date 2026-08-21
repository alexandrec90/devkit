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


def test_a_branch_never_overwrites_a_row_a_box_or_pr_already_claimed():
    merged = preview_task.merge_candidates(
        "carameli",
        [],
        [pr(164, "agent/ui", title="from the PR", updated="2026-08-21T16:00:00Z")],
        [("agent/ui", "2026-08-21T09:00:00Z", "from the branch")],
    )
    assert merged[0].title == "from the PR"
    assert merged[0].kind == preview_task.KIND_PR


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
        ("3", ("pick", 3)),
        (" 3 \n", ("pick", 3)),
        ("a", ("all", 0)),
        ("ALL", ("all", 0)),
        ("", ("quit", 0)),
        ("\n", ("quit", 0)),
        ("q", ("quit", 0)),
        ("no", ("quit", 0)),
        ("0", ("again", 0)),
        ("6", ("again", 0)),
        ("frontend", ("again", 0)),
    ],
)
def test_the_answer_grammar(text, expected):
    assert preview_task.parse_choice(text, count=5) == expected


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


def test_recent_branches_parses_the_ref_listing(monkeypatch):
    listing = "origin/agent/ui\t2026-08-21T09:00:00-04:00\tComic book UI\norigin/agent/bare\t2026-08-20T09:00:00Z\t\n"
    monkeypatch.setattr(preview_task.sweep, "git_for", lambda _dir: lambda *argv: result(listing))
    assert preview_task.recent_branches(preview_task.REPO_ROOT, fetch=False) == [
        ("agent/ui", "2026-08-21T09:00:00-04:00", "Comic book UI"),
        ("agent/bare", "2026-08-20T09:00:00Z", ""),
    ]


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


# --- the CLI ------------------------------------------------------------------


@pytest.fixture
def stub(monkeypatch, tmp_path):
    """A workspace file that exists, a fixed menu, and `serve` recorded rather than run."""
    workspace = tmp_path / "alex-projects.code-workspace"
    workspace.write_text("{}", encoding="utf-8")
    served = []
    monkeypatch.setattr(preview_task, "serve", lambda c, *a, **k: served.append(c.ref) or True)
    return types.SimpleNamespace(workspace=workspace, served=served, monkeypatch=monkeypatch)


def _menu(stub, rows):
    stub.monkeypatch.setattr(preview_task, "collect", lambda *a, **k: rows)


def test_a_missing_workspace_registry_is_reported_not_traced(tmp_path, capsys):
    assert preview_task.main(["--workspace", str(tmp_path / "gone.code-workspace")]) == 2
    assert "no workspace registry" in capsys.readouterr().out


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


def test_all_with_no_standing_preview_says_so(stub, capsys):
    _menu(
        stub, [preview_task.Candidate(project="p", ref="c", kind=preview_task.KIND_BOX, box="bc")]
    )
    assert preview_task.main(["--workspace", str(stub.workspace), "--all"]) == 0
    assert "nothing to bring back up" in capsys.readouterr().out
    assert stub.served == []


def test_a_failed_serve_is_a_nonzero_exit(stub):
    _menu(stub, [preview_task.Candidate(project="p", ref="a", kind=preview_task.KIND_PR)])
    stub.monkeypatch.setattr(preview_task, "serve", lambda *a, **k: False)
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


# --- the task that runs it ----------------------------------------------------


def test_the_dispatcher_registers_the_action_against_devkit_alone():
    """Machine-scoped: one box registry and one port registry, so no project picker."""
    from support import devkit_project

    action = devkit_project.ACTIONS["preview"]
    assert action.script == "scripts/preview-task.py"
    assert action.projects == devkit_project.DEVKIT_ONLY
    assert (preview_task.REPO_ROOT / action.script).is_file()
