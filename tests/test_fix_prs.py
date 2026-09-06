"""`scripts/fix-prs.py`: what counts as broken, what the dropdown draws, and what the
agent is told.

Every decision in that script is a pure function taking the shapes `gh` returns, so this
suite drives those directly and never a network. The two that spawn take a runner, and
the tests for them assert the argv rather than the effect.
"""

from __future__ import annotations

import datetime as _dt
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
from support import load_script

# `support.load_script` rather than `_loader.load_by_path`, which is what the script
# itself uses: `load_by_path` overwrites `sys.modules[name]`, so reaching `agent-box.py`
# that way would hand this process a second copy of a module `tests/test_agent_box.py`
# has already loaded -- and it is the one this suite monkeypatches. It also costs no
# `sys.path` bootstrap here, so this file needs no file-wide `noqa` to sit under one.
fix_prs = load_script("scripts/fix-prs.py")
agent_box = load_script("scripts/agent-box.py")

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
    assert fix_prs.broken_reason(pr()) == ""


def test_a_conflicting_pr_is_broken():
    assert fix_prs.broken_reason(pr(mergeable="CONFLICTING")) == "merge conflict"


def test_a_failed_check_run_is_broken():
    entry = pr(statusCheckRollup=[{"conclusion": "SUCCESS"}, {"conclusion": "FAILURE"}])
    assert fix_prs.broken_reason(entry) == "1 check failing"


def test_a_failed_legacy_status_context_counts_too():
    """One rollup mixes both shapes, and only the check-run half carries `conclusion`."""
    entry = pr(statusCheckRollup=[{"state": "ERROR"}, {"state": "SUCCESS"}])
    assert fix_prs.broken_reason(entry) == "1 check failing"


def test_both_kinds_of_broken_are_reported_together():
    entry = pr(
        mergeable="CONFLICTING",
        statusCheckRollup=[{"conclusion": "FAILURE"}, {"conclusion": "TIMED_OUT"}],
    )
    assert fix_prs.broken_reason(entry) == "merge conflict + 2 checks failing"


def test_a_pending_gate_is_not_a_failure():
    """A run in flight is the normal state seconds after a push; a menu that called it
    broken would offer every PR on the machine."""
    assert fix_prs.broken_reason(pr(statusCheckRollup=[{"conclusion": None}])) == ""


@pytest.mark.parametrize("conclusion", ["SKIPPED", "NEUTRAL", "SUCCESS"])
def test_a_check_that_did_not_apply_is_not_a_failure(conclusion):
    assert fix_prs.broken_reason(pr(statusCheckRollup=[{"conclusion": conclusion}])) == ""


def test_unknown_mergeability_is_not_a_conflict():
    """GitHub reports UNKNOWN while the job is still running, which every fresh PR is."""
    assert fix_prs.broken_reason(pr(mergeable="UNKNOWN")) == ""


def test_a_draft_is_never_broken_however_red_it_is():
    """A draft is not asking to be merged, so an agent sent at it has no finish line."""
    entry = pr(isDraft=True, mergeable="CONFLICTING", statusCheckRollup=[{"conclusion": "FAILURE"}])
    assert fix_prs.broken_reason(entry) == ""


@pytest.mark.parametrize("rollup", [None, "FAILURE", 7, [None, "x", {"conclusion": 3}]])
def test_a_rollup_shape_this_does_not_know_counts_as_zero(rollup):
    """Total rather than raising: this decides whether a row appears in a dropdown, and a
    menu that could not be built is worse than a row that is merely wrong."""
    assert fix_prs.failing_checks(rollup) == 0


# --- the source ------------------------------------------------------------------


def gh_returning(code: int, out: str):
    def gh_for(_path):
        def gh(*_args):
            return subprocess.CompletedProcess([], code, out, "")

        return gh

    return gh_for


def test_only_the_broken_ones_are_listed(monkeypatch, tmp_path):
    payload = json.dumps([pr(number=1), pr(number=2, mergeable="CONFLICTING")])
    monkeypatch.setattr(fix_prs.sweep, "gh_for", gh_returning(0, payload))
    assert [entry["number"] for entry in fix_prs.broken_prs(tmp_path)] == [2]


@pytest.mark.parametrize(
    "code,out", [(1, ""), (0, "not json"), (0, json.dumps({"message": "Bad credentials"}))]
)
def test_a_gh_failure_loses_the_rows_and_keeps_the_menu(monkeypatch, tmp_path, code, out):
    """An offline or unauthenticated machine must not fail the reconcile pass that calls
    this; it loses the rows, and the next pass writes them again."""
    monkeypatch.setattr(fix_prs.sweep, "gh_for", gh_returning(code, out))
    assert fix_prs.broken_prs(tmp_path) == []


def test_the_pr_is_re_read_live_rather_than_trusted_to_the_menu(monkeypatch, tmp_path):
    """The dropdown can be a quarter of an hour old, so what the agent is told about a PR
    comes from here and not from the row that was clicked."""
    monkeypatch.setattr(fix_prs.sweep, "gh_for", gh_returning(0, json.dumps(pr(number=9))))
    assert fix_prs.pr_view(tmp_path, 9)["number"] == 9


@pytest.mark.parametrize("code,out", [(1, ""), (0, "not json"), (0, "[]")])
def test_a_pr_view_that_cannot_be_read_is_empty_rather_than_a_traceback(
    monkeypatch, tmp_path, code, out
):
    """`[]` is in here because `gh` returning the wrong SHAPE must land in the same place
    as `gh` failing: `run_one` branches on emptiness, and a list would reach `.get`."""
    monkeypatch.setattr(fix_prs.sweep, "gh_for", gh_returning(code, out))
    assert fix_prs.pr_view(tmp_path, 9) == {}


# --- the dropdown's options file --------------------------------------------------


def test_every_row_carries_every_field_as_a_string():
    """The extension appends options until an expression THROWS, and `undefined` does
    not throw -- a row missing one field draws ten thousand blank entries."""
    payload = fix_prs.menu_payload({"carameli": [pr(mergeable="CONFLICTING")]}, NOW)
    for rows in payload["rows"].values():
        for row in rows:
            assert set(row) == {"value", "label", "description", "detail"}
            assert all(isinstance(value, str) for value in row.values())


def test_a_healthy_checkout_still_draws_one_row():
    """An empty `rows` array would end the extension's list at the first healthy
    checkout, hiding every checkout after it."""
    payload = fix_prs.menu_payload({"devkit": [], "carameli": []}, NOW)
    assert [row["value"] for row in payload["rows"]["devkit"]] == ["devkit:none"]
    assert payload["rows"]["carameli"][0]["label"] == "nothing broken"


def test_every_registered_checkout_is_listed_even_with_nothing_wrong():
    payload = fix_prs.menu_payload({"devkit": [], "carameli": [pr()]}, NOW)
    assert {entry["name"] for entry in payload["projects"]} == {"devkit", "carameli"}


def test_the_checkout_with_the_most_broken_prs_comes_first():
    """The dropdown's top entry should answer the question it was opened to answer."""
    found = {"aaa": [], "zzz": [pr(number=1)], "mmm": [pr(number=2), pr(number=3)]}
    payload = fix_prs.menu_payload(found, NOW)
    assert [entry["name"] for entry in payload["projects"]] == ["mmm", "zzz", "aaa"]


def test_a_checkouts_note_counts_only_what_is_broken():
    payload = fix_prs.menu_payload({"devkit": [pr(), pr()]}, NOW)
    note = payload["projects"][0]["description"]
    assert note.startswith("2 broken -- as of ")


def test_rows_are_newest_first():
    older = pr(number=1, updatedAt="2026-09-01T09:00:00Z")
    newer = pr(number=2, updatedAt="2026-09-04T09:00:00Z")
    payload = fix_prs.menu_payload({"devkit": [older, newer]}, NOW)
    assert [row["value"] for row in payload["rows"]["devkit"]] == ["devkit:2", "devkit:1"]


@pytest.mark.parametrize(
    "stamp,expected",
    [
        ("2026-09-04T11:30:00Z", "just now"),
        ("2026-09-04T09:00:00Z", "3h ago"),
        ("2026-09-01T12:00:00Z", "3d ago"),
        ("not a date", "?"),
        ("", "?"),
    ],
)
def test_age_is_coarse_and_never_raises(stamp, expected):
    assert fix_prs.age(stamp, NOW) == expected


def test_the_menu_is_written_atomically_and_reads_back(tmp_path):
    path = tmp_path / "nested" / "broken-prs.json"
    assert fix_prs.write_menu(fix_prs.menu_payload({"devkit": [pr()]}, NOW), path) == path
    assert json.loads(path.read_text(encoding="utf-8"))["rows"]["devkit"]


def test_writing_the_menu_never_raises(tmp_path):
    """A rider on somebody else's pass: the cost of a swallowed error is one stale
    dropdown, and the next pass rewrites it within the quarter hour."""
    blocked = tmp_path / "file"
    blocked.write_text("not a directory", encoding="utf-8")
    assert fix_prs.write_menu({}, blocked / "menu.json") is None


def test_refresh_menu_is_total_against_an_unreadable_workspace(tmp_path):
    """`worktree.reconcile` calls this; a menu that could not be built must never fail a
    pass that reaped boxes correctly."""
    assert fix_prs.refresh_menu(tmp_path / "nope.code-workspace", tmp_path / "out.json") is None


def test_the_scan_covers_the_registry_not_just_the_stack_projects(monkeypatch, tmp_path):
    workspace = tmp_path / "alex.code-workspace"
    workspace.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(fix_prs.devkit_project, "known_projects", lambda _text: ["a", "b"])
    monkeypatch.setattr(fix_prs, "broken_prs", lambda _dir: [])
    assert sorted(fix_prs.scan(workspace)) == ["a", "b"]


def test_a_pick_is_one_token_the_extension_can_return():
    """A VS Code input resolves to a single string, so both halves ride in one value."""
    assert fix_prs.pick_value("carameli", 412) == "carameli:412"


def test_a_row_names_the_pr_says_what_is_wrong_and_how_old_the_scan_is():
    row = fix_prs.menu_row("carameli", pr(mergeable="CONFLICTING"), NOW)
    assert row["value"] == "carameli:412"
    assert row["label"] == "#412 agent/sweep-labels-0904"
    assert row["description"] == "merge conflict -- 3h ago"
    assert row["detail"] == "Teach the sweep about labels"


def test_the_placeholder_row_says_picking_it_runs_nothing():
    """It exists to keep the array non-empty, so it has to read as a non-action rather
    than as a PR whose title nobody filled in."""
    row = fix_prs.placeholder_row("devkit")
    assert row["value"] == "devkit:none"
    assert fix_prs.parse_pick(row["value"]) is None
    assert "nothing" in row["label"] and "runs nothing" in row["detail"]


def test_a_checkout_with_nothing_broken_says_so_rather_than_saying_zero():
    """The first dropdown's second column is read at a glance; `0 broken` is a number to
    parse where `nothing broken` is an answer."""
    assert (
        fix_prs.project_note([], "2026-09-04 12:00") == "nothing broken -- as of 2026-09-04 12:00"
    )
    assert fix_prs.project_note([pr()], "2026-09-04 12:00").startswith("1 broken -- ")


# --- reading a pick ---------------------------------------------------------------


def test_a_pick_is_a_checkout_and_a_number():
    assert fix_prs.parse_pick("carameli:412") == fix_prs.Pick("carameli", 412)


def test_the_placeholder_row_parses_to_nothing_rather_than_failing():
    assert fix_prs.parse_pick("devkit:none") is None


@pytest.mark.parametrize("token", ["carameli", "", ":412", "carameli:head"])
def test_a_token_the_menu_could_not_have_written_is_refused(token):
    """A malformed pick means the menu file and this parser disagree; running the rest of
    the batch while dropping one is how a PR looks looked-at and was not."""
    with pytest.raises(fix_prs.FixError):
        fix_prs.parse_pick(token)


def test_ticked_rows_split_on_the_space_and_de_duplicate():
    assert fix_prs.split_picks("a:1 b:2 a:1") == ["a:1", "b:2"]


def test_nothing_ticked_splits_to_nothing():
    assert fix_prs.split_picks("") == []


# --- what the agent is told -------------------------------------------------------


def test_the_prompt_names_the_pr_the_fault_and_the_finish_line():
    text = fix_prs.seed_prompt("carameli", pr(), "merge conflict")
    assert "#412" in text
    assert "carameli" in text
    assert "merge conflict" in text
    assert "origin/main" in text
    assert "agent/sweep-labels-0904" in text
    assert "green" in text


def test_the_prompt_is_one_line_with_no_semicolon_in_it():
    """`wt.exe` parses its own command line: an unescaped `;` starts a second sub-command
    and a newline ends the command outright, so a prompt carrying either opens a tab
    running half a sentence."""
    text = fix_prs.seed_prompt("x", pr(title="a; b"), "1 check failing; and more")
    assert ";" not in text
    assert "\n" not in text


def test_tab_safe_collapses_whitespace_and_replaces_semicolons():
    assert fix_prs.tab_safe(" a;\n b  c ") == "a, b c"


def test_a_prompt_reaches_powershell_as_a_single_quoted_literal():
    """Single quotes because PowerShell expands `$` and backticks inside double ones."""
    command = agent_box.agent_command("claude", False, "fix $env:PATH and `x`")
    assert command == "claude 'fix $env:PATH and `x`'"


def test_an_apostrophe_in_a_prompt_is_doubled_not_escaped():
    assert agent_box.ps_quote("it's") == "'it''s'"


def test_a_session_with_no_prompt_is_unchanged():
    """`spawn` and `attach` hand over a box with no topic, and must keep doing so."""
    assert agent_box.agent_command("codex", False) == "codex"


def test_the_hooks_off_prefix_survives_a_prompt(monkeypatch):
    command = agent_box.agent_command("claude", True, "do the thing")
    assert command.startswith("$env:")
    assert command.endswith("claude 'do the thing'")


# --- opening the session ----------------------------------------------------------


@dataclass(frozen=True)
class FakeBox:
    project: str
    branch: str
    path: str


def test_a_live_box_on_that_branch_is_reused_rather_than_resumed():
    """`resume_plan` refuses a branch already checked out -- and this task's ordinary
    second click is on a PR whose box is still open from the first."""
    boxes = {"b": FakeBox("carameli", "agent/x", "/boxes/b")}
    assert fix_prs.existing_box(boxes, "carameli", "agent/x").path == "/boxes/b"


def test_a_box_of_another_checkout_on_the_same_branch_name_is_not_reused():
    boxes = {"b": FakeBox("devkit", "agent/x", "/boxes/b")}
    assert fix_prs.existing_box(boxes, "carameli", "agent/x") is None


def test_resume_asks_worktree_for_a_box_on_the_prs_own_branch(tmp_path):
    seen = {}

    def runner(argv, **kwargs):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, json.dumps({"path": "/boxes/b"}), "")

    workspace = tmp_path / "alex.code-workspace"
    assert fix_prs.resume_box("carameli", "agent/x", workspace, runner) == Path("/boxes/b")
    argv = seen["argv"]
    assert argv[2:6] == ["resume", "carameli", "--branch", "agent/x"]
    assert "--yes" in argv and "--json" in argv


def test_a_refused_resume_returns_no_box_rather_than_a_path(tmp_path):
    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 2, "", "already checked out")

    assert fix_prs.resume_box("c", "b", tmp_path / "w", runner) is None


def test_the_background_argv_passes_the_prompt_as_one_argument():
    """No shell in this mode, so no quoting -- and the words handed over are the same
    ones the tab mode hands over."""
    assert fix_prs.background_argv("claude", "do a; b") == ["claude", "--bg", "do a; b"]


def test_the_three_modes_map_to_two_clis_and_two_ways_of_opening():
    """Codex has no background session, so there is deliberately no `codex-bg`."""
    assert fix_prs.AGENT_MODES["claude"] == ("claude", fix_prs.TAB)
    assert fix_prs.AGENT_MODES["claude-bg"] == ("claude", fix_prs.BACKGROUND)
    assert fix_prs.AGENT_MODES["codex"] == ("codex", fix_prs.TAB)
    assert "codex-bg" not in fix_prs.AGENT_MODES


def test_the_background_launch_runs_the_resolved_exe_in_the_box(monkeypatch, tmp_path):
    seen = {}

    def runner(argv, **kwargs):
        seen.update(argv=argv, kwargs=kwargs)
        return subprocess.CompletedProcess(argv, 0, "session abc123", "")

    # `which` resolves to an absolute path, and that resolved path is what must be
    # spawned: the argv is handed to `subprocess.run` with no shell, so the bare name
    # would be looked up a second time -- against the child's PATH, not this one's.
    resolved = r"C:\bin\claude.exe"
    monkeypatch.setattr(fix_prs.shutil, "which", lambda _cli: resolved)
    code = fix_prs.launch_background("claude", tmp_path, "fix #412", False, runner)
    assert code == fix_prs.EXIT_OK
    assert seen["argv"] == [resolved, "--bg", "fix #412"]
    assert seen["kwargs"]["cwd"] == str(tmp_path)
    assert fix_prs.agent_box.harness_switch.HOOKS_OFF_ENV not in seen["kwargs"]["env"]


def test_the_background_launch_carries_the_hooks_switch_as_an_env_var(monkeypatch, tmp_path):
    """There is no shell in this mode, so the `$env:` prefix the tab uses has nowhere to
    go -- the switch has to reach the child through its environment or not at all."""
    seen = {}

    def runner(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(fix_prs.shutil, "which", lambda _cli: "claude")
    fix_prs.launch_background("claude", tmp_path, "p", True, runner)
    switch = fix_prs.agent_box.harness_switch
    assert seen["env"][switch.HOOKS_OFF_ENV] == switch.HOOKS_OFF_VALUE


def test_a_cli_that_is_not_on_path_is_reported_rather_than_spawned(monkeypatch, tmp_path, capsys):
    def explode(*_a, **_k):
        raise AssertionError("nothing should be spawned")

    monkeypatch.setattr(fix_prs.shutil, "which", lambda _cli: None)
    assert fix_prs.launch_background("claude", tmp_path, "p", False, explode) == fix_prs.EXIT_FAILED
    assert "not on PATH" in capsys.readouterr().out


def test_a_background_session_that_failed_to_start_is_a_failure(monkeypatch, tmp_path):
    def runner(argv, **_kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "no credit")

    monkeypatch.setattr(fix_prs.shutil, "which", lambda _cli: "claude")
    assert fix_prs.launch_background("claude", tmp_path, "p", False, runner) == fix_prs.EXIT_FAILED


# --- one PR, end to end -----------------------------------------------------------


def run_one_with(monkeypatch, tmp_path, view: dict, mode: str = "claude"):
    """`run_one` with every subprocess replaced. Returns `(code, opened)`."""
    (tmp_path / "carameli").mkdir(exist_ok=True)
    workspace = tmp_path / "w" / "alex.code-workspace"
    workspace.parent.mkdir(exist_ok=True)
    opened: dict = {}
    monkeypatch.setattr(fix_prs, "pr_view", lambda _dir, _n: view)
    monkeypatch.setattr(fix_prs.worktree, "live_boxes", lambda _root: {})
    monkeypatch.setattr(fix_prs, "resume_box", lambda *a, **k: Path("/boxes/b"))
    monkeypatch.setattr(
        fix_prs.agent_box,
        "open_agent",
        lambda *args, **kwargs: opened.update(args=args, kwargs=kwargs) or 0,
    )
    monkeypatch.setattr(
        fix_prs, "launch_background", lambda *args, **kwargs: opened.update(bg=args) or 0
    )
    (tmp_path / "w" / "carameli").mkdir(exist_ok=True)
    code = fix_prs.run_one(fix_prs.Pick("carameli", 412), workspace, mode)
    return code, opened


def test_a_pr_that_went_green_since_the_scan_is_reported_not_given_a_box(
    monkeypatch, tmp_path, capsys
):
    """The menu can be a quarter of an hour old. Reporting good news as a failure would
    put a red icon and a toast on a PR that fixed itself."""
    code, opened = run_one_with(monkeypatch, tmp_path, pr())
    assert code == 0
    assert not opened
    assert "nothing to do" in capsys.readouterr().out


@pytest.mark.parametrize("state", ["CLOSED", "MERGED"])
def test_a_pr_that_left_the_open_set_since_the_scan_gets_no_box(
    monkeypatch, tmp_path, capsys, state
):
    """The failure this was written for: a red PR was closed between the reconcile pass
    that wrote the menu and the click, GitHub deleted its head branch on the way out, and
    `resume` refused a branch `origin` no longer has -- an exit 1 and a message about
    worktrees for what is a stale row. A closed PR is still `red` to `broken_reason`,
    which only ever sees open ones from the scan, so the state is checked before it."""
    view = pr(state=state, statusCheckRollup=[{"conclusion": "FAILURE"}])
    code, opened = run_one_with(monkeypatch, tmp_path, view)
    assert code == 0
    assert not opened
    assert "nothing to do" in capsys.readouterr().out


def test_a_pr_view_without_a_state_is_treated_as_open(monkeypatch, tmp_path):
    """`state` missing is `gh` answering a shape this asked for and did not get; the
    scan already filtered to open PRs, so the safe reading is to carry on rather than
    to swallow every pick on the day the field is renamed."""
    view = pr(statusCheckRollup=[{"conclusion": "FAILURE"}])
    view.pop("state")
    code, opened = run_one_with(monkeypatch, tmp_path, view)
    assert code == 0
    assert opened


def test_a_pr_turned_draft_since_the_scan_gets_no_box(monkeypatch, tmp_path, capsys):
    """`isDraft` is in the view fields for the same reason as `state`: `broken_reason`
    already excludes drafts, and could not while the field it reads was never asked for."""
    view = pr(isDraft=True, statusCheckRollup=[{"conclusion": "FAILURE"}])
    code, opened = run_one_with(monkeypatch, tmp_path, view)
    assert code == 0
    assert not opened
    assert "nothing to do" in capsys.readouterr().out


def test_the_view_asks_for_every_field_the_launch_path_reads():
    """A field `run_one` branches on and `PR_VIEW_FIELDS` omits is always absent, which
    is indistinguishable from the harmless value -- how the closed-PR bug survived."""
    asked = set(fix_prs.PR_VIEW_FIELDS.split(","))
    assert {"state", "isDraft", "mergeable", "statusCheckRollup", "headRefName"} <= asked


def test_a_broken_pr_opens_a_tab_titled_for_the_pr(monkeypatch, tmp_path):
    """Several tabs can be open at once on branches that all begin `agent/`."""
    code, opened = run_one_with(monkeypatch, tmp_path, pr(mergeable="CONFLICTING"))
    assert code == 0
    assert opened["kwargs"]["title"] == "carameli #412"
    assert "#412" in opened["kwargs"]["prompt"]


def test_the_background_mode_does_not_open_a_tab(monkeypatch, tmp_path):
    _code, opened = run_one_with(monkeypatch, tmp_path, pr(mergeable="CONFLICTING"), "claude-bg")
    assert "bg" in opened
    assert "kwargs" not in opened


def test_a_pr_gh_cannot_read_is_a_failure_rather_than_a_silent_skip(monkeypatch, tmp_path):
    code, opened = run_one_with(monkeypatch, tmp_path, {})
    assert code == fix_prs.EXIT_FAILED
    assert not opened


def test_a_batch_reports_the_worst_outcome(monkeypatch, tmp_path):
    """One failure among three must not be reported as a green run."""
    workspace = tmp_path / "alex.code-workspace"
    codes = iter([0, 1, 0])
    monkeypatch.setattr(fix_prs, "run_one", lambda *a, **k: next(codes))
    picks = [fix_prs.Pick("a", 1), fix_prs.Pick("a", 2), fix_prs.Pick("a", 3)]
    assert fix_prs.run(picks, workspace, "claude") == 1


# --- the CLI ----------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path):
    path = tmp_path / "alex.code-workspace"
    path.write_text("{}", encoding="utf-8")
    return path


def test_a_dismissed_picker_runs_nothing_and_is_not_a_failure(workspace, capsys):
    """Ahead of argparse: a cancel reported as a usage error is a red icon, a toast and
    a `logs/` artifact for a run the user called off."""
    code = fix_prs.main(
        ["--picks", "${input:brokenPrRow}", "--agent", "claude", "--workspace", str(workspace)]
    )
    assert code == 0
    assert "cancelled" in capsys.readouterr().out


def test_the_guard_sits_ahead_of_the_choices_check(workspace, capsys):
    """`--agent` carries `choices=`, which would turn the literal into a usage error."""
    code = fix_prs.main(["--agent", "${input:fixAgent}", "--workspace", str(workspace)])
    assert code == 0
    assert "cancelled" in capsys.readouterr().out


def test_nothing_ticked_runs_nothing(workspace, capsys):
    assert fix_prs.main(["--picks", "", "--workspace", str(workspace)]) == 0
    assert "nothing to do" in capsys.readouterr().out


def test_only_the_placeholder_ticked_runs_nothing(workspace, capsys):
    assert fix_prs.main(["--picks", "devkit:none", "--workspace", str(workspace)]) == 0
    assert "nothing to do" in capsys.readouterr().out


def test_refresh_writes_the_menu_and_stops(workspace, monkeypatch, capsys):
    monkeypatch.setattr(fix_prs, "refresh_menu", lambda _ws: Path("logs/broken-prs.json"))
    assert fix_prs.main(["--refresh", "--workspace", str(workspace)]) == 0
    assert "broken-prs.json" in capsys.readouterr().out


def test_a_missing_workspace_file_is_a_usage_error(tmp_path, capsys):
    assert fix_prs.main(["--workspace", str(tmp_path / "nope")]) == fix_prs.EXIT_USAGE
    assert "no workspace file" in capsys.readouterr().err


def test_list_prints_the_same_rows_the_dropdown_would_draw(workspace, monkeypatch, capsys):
    monkeypatch.setattr(
        fix_prs, "scan", lambda _ws: {"devkit": [pr(mergeable="CONFLICTING")], "carameli": []}
    )
    assert fix_prs.main(["--list", "--workspace", str(workspace)]) == 0
    out = capsys.readouterr().out
    assert "devkit: 1 broken" in out
    assert "#412 agent/sweep-labels-0904 -- merge conflict" in out
    assert "carameli: nothing broken" in out


def test_an_unknown_checkout_is_a_usage_error_not_a_traceback(workspace, capsys):
    code = fix_prs.main(["--picks", "nosuch:1", "--workspace", str(workspace)])
    assert code == fix_prs.EXIT_USAGE
    assert "unknown checkout" in capsys.readouterr().err


def test_the_terminal_listing_draws_the_same_rows_as_the_dropdown():
    """`--list` is what a machine with no VS Code has, so it must not be a second answer
    to the question the menu answers."""
    found = {"devkit": [pr(mergeable="CONFLICTING")], "carameli": []}
    text = fix_prs.render_scan(found)
    assert "devkit: 1 broken" in text
    assert "  #412 agent/sweep-labels-0904 -- merge conflict" in text
    assert "carameli: nothing broken" in text


def test_the_parser_defaults_to_a_watchable_tab_and_offers_only_the_known_modes():
    """The default is the tab because a session that pushes to a real branch and can merge
    a real PR is one worth being able to interrupt; `choices` is `AGENT_MODES` so a row in
    the picker and a mode here can never drift apart."""
    parser = fix_prs.build_parser()
    args = parser.parse_args([])
    assert (args.agent, args.picks, args.refresh, args.list) == ("claude", "", False, False)
    action = next(a for a in parser._actions if a.dest == "agent")
    assert sorted(action.choices) == sorted(fix_prs.AGENT_MODES)
