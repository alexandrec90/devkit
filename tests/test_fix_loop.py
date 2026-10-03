"""`scripts/fix_loop.py`: every read-back turns a "no" into a finding, and plan writes nothing."""

from __future__ import annotations

import datetime as _dt
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fix_cycle
import fix_findings
import fix_ledger
import fix_loop
import fix_reports
import harness_triage as triage

NOW = _dt.datetime(2026, 9, 26, 12, 0, tzinfo=_dt.UTC)
KEY = "pr:carameli:412:abc:d:dispatch"
UPSTREAM_KEY = "upstream:2:deadbeef"
REAL_DEVKIT_FIXES = fix_loop.devkit_fixes  # `ctx` stubs it for every other test


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    (tmp_path / "devkit").mkdir()
    monkeypatch.setattr(fix_reports, "CLAUDE_PROJECTS", tmp_path / "projects")
    monkeypatch.setattr(fix_loop.session_friction, "harvest", lambda *a, **k: [])
    # The machine's own Task Scheduler, which `close` would otherwise read and file.
    monkeypatch.setattr(fix_loop.schedule_health, "query", lambda *a, **k: [])
    monkeypatch.setattr(
        fix_loop.fix_verify, "verify", lambda *a, **k: fix_loop.fix_verify.Outcome()
    )
    monkeypatch.setattr(
        fix_loop.bg_sessions, "stop_finished", lambda trees, runner: [f"x in {t}" for t in trees]
    )
    monkeypatch.setattr(fix_loop, "working_dirs", frozenset)
    monkeypatch.setattr(fix_loop.friction_pending, "detector_fixes", lambda gh, git: [])
    monkeypatch.setattr(fix_loop, "devkit_fixes", lambda ctx: ())  # GitHub's devkit PRs
    return fix_loop.Context(
        tmp_path,
        ["devkit", "carameli"],
        tmp_path / "devkit",
        tmp_path / ".worktrees" / "dispatch.json",
        tmp_path / "history.jsonl",
        fix_cycle.DISPATCH,
        NOW,
    )


def tree(
    ctx,
    monkeypatch,
    *,
    key=KEY,
    sent=NOW,
    blocked="",
    friction="",
    transcript_age=None,
    branch="agent/x-0919",
):
    """One stamped fixer tree, as `read_trees` would return it."""
    path = ctx.root / "carameli" / ".claude" / "worktrees" / "x"
    (path / "logs").mkdir(parents=True, exist_ok=True)
    fix_reports.stamp(path, key, "n", sent, problem="problem-1")
    if blocked:
        (path / fix_reports.BLOCKED_FILE).write_text(blocked, encoding="utf-8")
    if friction:
        (path / fix_reports.FRICTION_FILE).write_text(friction, encoding="utf-8")
    if transcript_age is not None:
        log = fix_reports.transcript_dir(path) / "s.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(
            f'{{"timestamp": "{(NOW - transcript_age).isoformat()}"}}\n', encoding="utf-8"
        )
        stamp = (NOW - transcript_age).timestamp()
        os.utime(log, (stamp, stamp))
    one = fix_reports.Tree(
        "carameli",
        path,
        branch,
        fix_reports.read_stamp(path),
        fix_reports.friction_lines(path),
    )
    monkeypatch.setattr(fix_loop.fix_reports, "read_trees", lambda root, projects: [one])
    return path


def close(ctx) -> tuple[fix_loop.Closed, fix_findings.Journal]:
    journal = fix_findings.Journal(ctx.devkit_dir)
    return fix_loop.close(ctx, journal), journal


def kinds(journal) -> list[str]:
    return sorted(f.kind for f in journal.findings)


def test_a_blocked_report_is_marked_filed_with_its_problem_and_read_once(ctx, monkeypatch):
    fix_ledger.record(ctx.ledger_path, KEY, "n", NOW)
    path = tree(ctx, monkeypatch, blocked="needs a database", transcript_age=_dt.timedelta(hours=3))
    closed, journal = close(ctx)
    assert closed.lines == ["carameli agent/x-0919 -- needs a database"]
    [found] = journal.findings
    assert (found.kind, found.key) == ("fixer-blocked", "problem-1"), "keyed, so the problem waits"
    assert fix_ledger.read_ledger(ctx.ledger_path)[KEY]["blocked"] == "needs a database"
    assert not (path / fix_reports.BLOCKED_FILE).exists()
    assert (path / "logs" / "fix-blocked.filed.md").exists()
    assert closed.trees == {"problem-1": str(path)}


def test_each_friction_line_is_filed_and_the_file_read_once(ctx, monkeypatch):
    path = tree(
        ctx, monkeypatch, friction="- the prompt named logs/gate/ and it was empty\n\n- no .venv\n"
    )
    _, journal = close(ctx)
    reported = [f for f in journal.findings if f.kind == "reported"]
    assert [f.detail for f in reported] == [
        "the prompt named logs/gate/ and it was empty",
        "no .venv",
    ]
    assert all(f.event == fix_findings.FRICTION for f in reported)
    assert (path / "logs" / "friction.filed.md").exists() and not (
        path / fix_reports.FRICTION_FILE
    ).exists()
    # The evidence named the file the pass had just renamed, so every friction row
    # pointed at nothing and a sweep spent a call finding that out.
    for finding in reported:
        assert Path(finding.evidence).is_file(), finding.evidence
        assert finding.detail in Path(finding.evidence).read_text(encoding="utf-8")


def test_a_line_its_session_fixed_on_this_branch_is_filed_settled_by_that_branch(ctx, monkeypatch):
    """407df645, 6a3312bf, a8142f2a: a fixer's friction lines each said "fixed on this
    branch", were filed open, and sent a second fixer at three fixes already in review."""
    fixed = "evidence rewritten before it was read; fixed on this branch (`fix_findings.kept`)"
    tree(ctx, monkeypatch, friction=f"- {fixed}\n- no .venv\n")
    monkeypatch.setattr(fix_loop, "went_out", lambda path: True)
    _, journal = close(ctx)
    settles = {f.detail: f.settles_with for f in journal.findings if f.kind == "reported"}
    assert settles == {fixed: "agent/x-0919", "no .venv": ""}
    fix_loop.record(ctx, journal)
    [still_open] = triage.open_items(triage.load(ctx.devkit_dir))
    assert still_open.detail == "reported: no .venv", "only what nobody fixed stays open"


def test_a_long_line_keeps_the_fixed_on_this_branch_it_ends_with(ctx, monkeypatch):
    """2d0bc76f: a 460-character line ended "..., fixed on this branch", the friction reader
    cut every line to 400 before anything asked, and the group was filed open against a
    fix already merging on #435. The marker is at the end by the nature of the sentence."""
    fixed = "the triage CLI counted a pending group open; " + "x" * 400 + ", fixed on this branch"
    tree(ctx, monkeypatch, friction=f"- {fixed}\n")
    monkeypatch.setattr(fix_loop, "went_out", lambda path: True)
    _, journal = close(ctx)
    [found] = [f for f in journal.findings if f.kind == "reported"]
    assert (found.detail, found.settles_with) == (fixed, "agent/x-0919")
    fix_loop.record(ctx, journal)
    assert triage.open_items(triage.load(ctx.devkit_dir)) == []


def test_a_fixed_on_this_branch_whose_work_never_went_out_is_filed_open(ctx, monkeypatch):
    """7c16fabc: 5d9806b0 was settled on worktree-rippling-juggling-oasis, whose intent
    had been set aside over 19 uncommitted files -- the fix never reached its PR."""
    tree(ctx, monkeypatch, friction="- x; fixed on this branch\n")
    monkeypatch.setattr(fix_loop, "went_out", lambda path: False)
    _, journal = close(ctx)
    assert [f.settles_with for f in journal.findings if f.kind == "reported"] == [""]
    fix_loop.record(ctx, journal)
    assert len(triage.open_items(triage.load(ctx.devkit_dir))) == 1


def test_a_fixed_on_this_branch_from_a_session_still_at_work_is_held(ctx, monkeypatch):
    """c8d4f66b, 5d8e9962: harvested from peaceful-jumping-cocke a minute after its live
    session wrote them, both fixes still uncommitted there, and filed open -- so a second
    session was sent at a fix the first was still writing."""
    path = tree(
        ctx,
        monkeypatch,
        friction="- x; Fixed on this branch (`y`).\n- no .venv\n",
        transcript_age=_dt.timedelta(minutes=1),
    )
    monkeypatch.setattr(fix_loop, "went_out", lambda path: False)
    closed, journal = close(ctx)
    assert [f for f in journal.findings if f.kind == "reported"] == []
    assert (path / fix_reports.FRICTION_FILE).is_file(), "filed away, its lines were lost"
    assert closed.lines == [
        "carameli agent/x-0919 -- friction held: its session is still at work on a fix"
    ]


def test_a_session_listed_busy_holds_its_claimed_friction_too(ctx, monkeypatch):
    path = tree(ctx, monkeypatch, friction="- x; fixed on this branch\n")
    monkeypatch.setattr(fix_loop, "went_out", lambda path: False)
    busy = fix_loop.bg_sessions.working([{"cwd": str(path), "status": "busy"}])
    monkeypatch.setattr(fix_loop, "working_dirs", lambda: busy)
    _, journal = close(ctx)
    assert [f for f in journal.findings if f.kind == "reported"] == []


def test_the_held_lines_are_filed_once_the_session_goes_quiet(ctx, monkeypatch):
    tree(
        ctx,
        monkeypatch,
        friction="- x; fixed on this branch\n",
        transcript_age=fix_reports.QUIET_AFTER + _dt.timedelta(minutes=1),
    )
    monkeypatch.setattr(fix_loop, "went_out", lambda path: False)
    _, journal = close(ctx)
    assert [f.settles_with for f in journal.findings if f.kind == "reported"] == [""]


@pytest.mark.parametrize(
    ("friction", "out", "settles"),
    [
        ("- no .venv\n", False, [""]),  # nothing claimed: nothing to wait for
        ("- x; fixed on this branch\n", True, ["agent/x-0919"]),  # the fix already went out
    ],
)
def test_a_live_session_holds_only_a_claim_its_branch_does_not_carry_yet(
    ctx, monkeypatch, friction, out, settles
):
    tree(ctx, monkeypatch, friction=friction, transcript_age=_dt.timedelta(minutes=1))
    monkeypatch.setattr(fix_loop, "went_out", lambda path: out)
    _, journal = close(ctx)
    assert [f.settles_with for f in journal.findings if f.kind == "reported"] == settles


def _porcelain(code: int, out: str):
    seen: list[list[str]] = []

    def run(argv, cwd):
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, code, out, "")

    return run, seen


@pytest.mark.parametrize(
    ("stage", "code", "porcelain", "expected"),
    [
        (fix_loop.ship_intent.SHIPPED, 0, "", True),
        (fix_loop.ship_intent.SHIPPED, 0, " M scripts/a.py\n", False),
        (fix_loop.ship_intent.SHIPPED, 128, "", False),
        (fix_loop.ship_intent.FAILED, 0, "", False),
        (fix_loop.ship_intent.EMPTY, 0, "", False),
        (None, 0, "", False),
    ],
)
def test_went_out_is_a_shipped_state_over_a_clean_tree(tmp_path, stage, code, porcelain, expected):
    if stage:
        fix_loop.ship_intent.write_state(tmp_path, {"stage": stage})
    run, seen = _porcelain(code, porcelain)
    assert fix_loop.went_out(tmp_path, run) is expected
    assert seen == ([["git", "status", "--porcelain"]] if stage == "shipped" else [])


def test_a_detached_tree_settles_nothing_it_has_no_branch_to_merge(ctx, monkeypatch):
    path = tree(ctx, monkeypatch)
    one = fix_reports.Tree("carameli", path, "", {}, ("x; fixed on this branch",))
    monkeypatch.setattr(fix_loop.fix_reports, "read_trees", lambda root, projects: [one])
    _, journal = close(ctx)
    assert [f.settles_with for f in journal.findings if f.kind == "reported"] == [""]


def test_a_second_filing_keeps_the_lines_the_first_findings_point_at(ctx, monkeypatch):
    path = tree(ctx, monkeypatch, friction="- first\n")
    close(ctx)
    (path / fix_reports.FRICTION_FILE).write_text("- second\n", encoding="utf-8")
    monkeypatch.setattr(
        fix_loop.fix_reports,
        "read_trees",
        lambda root, projects: [
            fix_reports.Tree("carameli", path, "agent/x-0919", {}, ("second",))
        ],
    )
    close(ctx)
    filed = (path / "logs" / "friction.filed.md").read_text(encoding="utf-8")
    assert "first" in filed and "second" in filed


def test_a_session_that_never_started_frees_its_key_and_is_filed_once(ctx, monkeypatch):
    fix_ledger.record(ctx.ledger_path, KEY, "n", NOW - _dt.timedelta(hours=2))
    path = tree(ctx, monkeypatch, sent=NOW - _dt.timedelta(hours=2))
    _, journal = close(ctx)
    [found] = journal.findings
    assert found.kind == "fixer-no-outcome" and "never started" in found.detail
    assert not found.key, "a dead session is re-sent at once, not parked behind its finding"
    assert fix_ledger.read_ledger(ctx.ledger_path)[KEY]["dead"] == "never started"
    assert fix_reports.read_stamp(path)["dead"] == "never started"
    tree(ctx, monkeypatch, sent=NOW - _dt.timedelta(hours=2))  # the same stamp, re-read
    fix_reports.note_on_stamp(path, "dead", "never started")
    assert close(ctx)[1].findings == [], "judged once per dispatch"


def test_a_session_that_never_started_cites_what_its_launcher_said(ctx, monkeypatch):
    """3728bf21: the finding filed the tree alone, so learning that the launcher -- a
    `--disallowedTools` that swallowed the prompt -- was why took a sweep nine calls of
    transcript grepping. The launch record is the evidence, and says it in the detail."""
    path = tree(ctx, monkeypatch, sent=NOW - _dt.timedelta(hours=2))
    done = subprocess.CompletedProcess(["claude"], 1, "", "no prompt given")
    fix_reports.record_launch(path, ["claude", "--bg", "--", "fix it all"], done)
    [found] = close(ctx)[1].findings
    assert found.evidence == str(path / fix_reports.LAUNCH_FILE)
    assert "launcher exited 1" in found.detail


def test_one_launcher_failure_is_one_detail_whichever_branch_it_hit(ctx, monkeypatch):
    """1e5e57f4 and seven more: one unreachable service failed eight hourly re-sends, and
    each finding named its own fresh branch, so the ledger held eight groups for one
    defect. The branch is in the evidence; the detail is what the launcher said."""
    details = []
    for branch in ("agent/fix-0927-6", "agent/fix-0927-7"):
        path = tree(ctx, monkeypatch, sent=NOW - _dt.timedelta(hours=2), branch=branch)
        done = subprocess.CompletedProcess(["claude"], 1, "", "service unreachable")
        fix_reports.record_launch(path, ["claude", "--bg", "--", "fix it all"], done)
        [found] = close(ctx)[1].findings
        details.append(found.detail)
        assert branch not in found.detail
    assert details[0] == details[1]


def test_a_session_gone_quiet_without_an_outcome_is_dead(ctx, monkeypatch):
    tree(ctx, monkeypatch, sent=NOW - _dt.timedelta(hours=5), transcript_age=_dt.timedelta(hours=3))
    _, journal = close(ctx)
    [found] = journal.findings
    assert "ended without an outcome" in found.detail and found.evidence.endswith("s.jsonl")


def test_a_session_the_machine_restarted_under_is_sent_again_not_filed(ctx, monkeypatch):
    """4dc413a1, 9201a08f, e2feebab: the operator powered off at 02:26 on 2026-09-30 with
    three fixers mid-call, and each was filed as a dead fixer for a devkit session to
    investigate, while the two resolvers' lost sends escalated #480 and #482 as blind."""
    fix_ledger.record(ctx.ledger_path, KEY, "n", NOW - _dt.timedelta(minutes=10))
    tree(
        ctx,
        monkeypatch,
        sent=NOW - _dt.timedelta(minutes=10),
        transcript_age=_dt.timedelta(minutes=5),
    )
    restarted = fix_loop.Context(**{**vars(ctx), "booted": NOW - _dt.timedelta(minutes=2)})
    closed, journal = close(restarted)
    assert journal.findings == []
    assert closed.lines == ["carameli agent/x-0919 -- stopped by a restart; sent again"]
    entry = fix_ledger.read_ledger(ctx.ledger_path)[KEY]
    assert entry["dead"] == fix_reports.INTERRUPTED and fix_ledger.sends(entry) == 0


def test_judging_a_dead_session_in_plan_mode_files_it_and_writes_nothing(ctx, monkeypatch):
    fix_ledger.record(ctx.ledger_path, KEY, "n", NOW)
    path = tree(ctx, monkeypatch, sent=NOW - _dt.timedelta(hours=5))
    [one] = fix_reports.read_trees(ctx.root, ctx.projects)
    plan = fix_loop.Context(**{**vars(ctx), "mode": fix_cycle.PLAN})
    journal = fix_findings.Journal(ctx.devkit_dir)
    where = "carameli agent/x-0919"
    assert fix_loop.judge_dead(plan, one, where, fix_reports.NO_OUTCOME, "t.jsonl", journal) == []
    assert [f.kind for f in journal.findings] == ["fixer-no-outcome"]
    restart = fix_reports.INTERRUPTED
    assert fix_loop.judge_dead(plan, one, where, restart, "t.jsonl", journal) == [
        f"{where} -- {restart}; sent again"
    ]
    assert len(journal.findings) == 1, "a restart files nothing"
    assert "dead" not in fix_ledger.read_ledger(ctx.ledger_path)[KEY]
    assert "dead" not in fix_reports.read_stamp(path)


def test_a_working_devkit_session_is_the_harness_busy(ctx, monkeypatch):
    path = tree(
        ctx,
        monkeypatch,
        key=UPSTREAM_KEY,
        sent=NOW - _dt.timedelta(hours=1),
        transcript_age=_dt.timedelta(minutes=5),
    )
    closed, journal = close(ctx)
    assert closed.harness_busy == str(path) and journal.findings == []


def test_a_working_sweep_of_the_ledger_alone_is_the_harness_busy(ctx, monkeypatch):
    """The shape every sweep of 2026-09-26 had: one ledger failure sent upstream. Its key
    starts `ledger:`, a prefix test for `upstream:` missed it, and the pass sent 0926-16
    over -15 and 0926-18 over -17 (028731f9, 9cbdc674)."""
    path = tree(
        ctx,
        monkeypatch,
        key="ledger:devkit:0:a81ffd55430d:f6b83153228a:upstream",
        sent=NOW - _dt.timedelta(minutes=9),
        transcript_age=_dt.timedelta(minutes=1),
    )
    closed, _ = close(ctx)
    assert closed.harness_busy == str(path)


def test_a_session_still_busy_after_its_intent_holds_its_tree_and_the_harness(ctx, monkeypatch):
    """#422's own sweep had written its intent and was still mid-merge when the pass sent
    a resolver into the same tree (d821bd8f). Its transcript is the stamped one, so only
    the session listing can say it is still at work."""
    path = tree(
        ctx,
        monkeypatch,
        key="ledger:devkit:0:895d547878e6:6f7d5e150831:upstream",
        sent=NOW - _dt.timedelta(minutes=30),
        transcript_age=_dt.timedelta(minutes=1),
    )
    (path / "logs" / "ship-intent.md").write_text("S\n", encoding="utf-8")
    closed, _ = close(ctx)
    assert closed.busy == {} and closed.harness_busy == "", "idle: finished, holds nothing"
    listed = [{"kind": "background", "status": "busy", "cwd": str(path)}]
    monkeypatch.setattr(fix_loop, "working_dirs", lambda: fix_loop.bg_sessions.working(listed))
    closed, _ = close(ctx)
    assert closed.busy == {("carameli", "agent/x-0919"): str(path)}
    assert closed.harness_busy == str(path)


def test_the_session_listing_is_read_from_claude_agents(monkeypatch):
    rows = '[{"kind": "background", "status": "busy", "cwd": "C:\\\\ws\\\\t"}]'
    seen = []

    def runner(argv, **_kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, rows, "")

    monkeypatch.setattr(fix_loop.ship_intent, "run_quiet", runner)
    assert fix_loop.working_dirs() == frozenset({"c:/ws/t"})
    assert seen == [["claude", "agents", "--json"]]


def test_a_row_an_open_prs_detector_no_longer_files_is_resolved_against_that_pr(ctx, monkeypatch):
    """fc786188 was filed by main's detector while open #420 already stopped it, and a
    sweep was sent at it (20fe4a86)."""
    fix_findings.record_all(
        [fix_findings.Finding("environment", "devkit", "d", event=fix_findings.FRICTION)],
        [],
        ctx.devkit_dir,
    )
    [row] = triage.open_items(triage.load(ctx.devkit_dir))
    fix = fix_loop.friction_pending.Fix("420", "worktree-hazy", "src")
    monkeypatch.setattr(fix_loop.friction_pending, "detector_fixes", lambda gh, git: [fix])
    monkeypatch.setattr(
        fix_loop.friction_pending, "outdated_on", lambda f, items: [(i.id, "gone") for i in items]
    )
    plan = fix_loop.Context(
        *[getattr(ctx, n) for n in ("root", "projects", "devkit_dir")],
        ctx.ledger_path,
        ctx.history_path,
        fix_cycle.PLAN,
        NOW,
    )
    assert fix_loop.recheck_open(plan) == [f"would hold [{row.id}] on #420"]
    assert triage.open_items(triage.load(ctx.devkit_dir)) != []
    assert fix_loop.recheck_open(ctx) == [f"pending [{row.id}] on #420"]
    assert triage.open_items(triage.load(ctx.devkit_dir)) == []
    [resolved] = [i for i in triage.load(ctx.devkit_dir) if i.event == triage.RESOLVED_EVENT]
    assert resolved.fields["pr"] == "420" and "#420 (worktree-hazy)" in resolved.fields["note"]


def test_plan_mode_reads_back_and_writes_nothing(ctx, monkeypatch, tmp_path):
    plan = fix_loop.Context(
        *[
            getattr(ctx, f)
            for f in ("root", "projects", "devkit_dir", "ledger_path", "history_path")
        ],
        fix_cycle.PLAN,
        NOW,
    )
    fix_ledger.record(ctx.ledger_path, KEY, "n", NOW)
    path = tree(plan, monkeypatch, blocked="needs a database", friction="no .venv")
    _closed, journal = close(plan)
    assert kinds(journal) == ["fixer-blocked", "reported"]
    assert (path / fix_reports.BLOCKED_FILE).exists() and (
        path / fix_reports.FRICTION_FILE
    ).exists()
    assert "blocked" not in fix_ledger.read_ledger(ctx.ledger_path)[KEY]
    assert fix_loop.record(plan, journal) == [
        "would file: carameli fixer-blocked: carameli agent/x-0919: needs a database",
        "would file: carameli reported: no .venv",
    ]
    assert triage.load(ctx.devkit_dir) == [] and journal.findings == []


def test_record_files_once_and_empties_the_journal(ctx):
    journal = fix_findings.Journal(ctx.devkit_dir)
    journal.add(fix_findings.Finding("ship-failed", "carameli", "agent/x: rejected"))
    assert fix_loop.record(ctx, journal) == ["carameli ship-failed: agent/x: rejected"]
    assert journal.findings == []
    journal.add(fix_findings.Finding("ship-failed", "carameli", "agent/x: rejected"))
    assert fix_loop.record(ctx, journal) == [], "already open"
    assert fix_loop.backlog(ctx) == 1


def test_verify_reopens_what_did_not_land(ctx, monkeypatch):
    monkeypatch.setattr(fix_loop.fix_reports, "read_trees", lambda root, projects: [])
    report = f"{NOW.isoformat()}\tevent=agent-report\tproject=devkit\tmessage=x"
    (ctx.devkit_dir / "logs").mkdir(parents=True)
    (ctx.devkit_dir / "logs" / "harness-events.log").write_text(report + "\n", encoding="utf-8")
    ref = triage.item_id(report)
    triage.resolve([ref], "fixed", pr="agent/x", root=ctx.devkit_dir)
    outcome = fix_loop.fix_verify.Outcome(reopen=[(ref, "closed unmerged")])
    monkeypatch.setattr(fix_loop.fix_verify, "verify", lambda *a, **k: outcome)
    closed, _ = close(ctx)
    assert closed.lines == [f"reopened [{ref}] -- closed unmerged"]
    assert [i.id for i in triage.open_items(triage.load(ctx.devkit_dir))] == [ref]


def test_verify_retires_what_was_filed_while_the_merged_fix_waited(ctx, monkeypatch):
    """7a94f5bc: the rows `fix_verify` covers are resolved against the merged PR, so the
    backlog step never sees them as a recurrence of the fix that just landed."""
    monkeypatch.setattr(fix_loop.fix_reports, "read_trees", lambda root, projects: [])
    row = f"{NOW.isoformat()}\tevent=fix-pass-finding\tproject=devkit\tdetail=x"
    (ctx.devkit_dir / "logs").mkdir(parents=True)
    (ctx.devkit_dir / "logs" / "harness-events.log").write_text(row + "\n", encoding="utf-8")
    ref = triage.item_id(row)
    outcome = fix_loop.fix_verify.Outcome(covered=[(ref, "filed while #434 waited", "u/434")])
    monkeypatch.setattr(fix_loop.fix_verify, "verify", lambda *a, **k: outcome)
    closed, _ = close(ctx)
    assert f"retired [{ref}] -- filed while #434 waited" in closed.lines
    assert triage.open_items(triage.load(ctx.devkit_dir)) == []
    [resolution] = [i for i in triage.load(ctx.devkit_dir) if i.event == triage.RESOLVED_EVENT]
    assert resolution.fields["pr"] == "u/434"


def test_verify_names_the_merged_pr_that_held_a_fix_its_branch_never_landed(ctx, monkeypatch):
    """950c4a96: the ledger reads the PR that holds the fix, keeping when it was made."""
    monkeypatch.setattr(fix_loop.fix_reports, "read_trees", lambda root, projects: [])
    row = f"{NOW.isoformat()}\tevent=fix-pass-finding\tproject=devkit\tdetail=x"
    (ctx.devkit_dir / "logs").mkdir(parents=True)
    (ctx.devkit_dir / "logs" / "harness-events.log").write_text(row + "\n", encoding="utf-8")
    ref = triage.item_id(row)
    made = (NOW - _dt.timedelta(days=3)).isoformat()
    was = fix_loop.fix_verify.Resolution(ref, made, "agent/x-3", "split by cause")
    fix = fix_loop.fix_verify.Pr(fix_loop.fix_verify.LANDED, NOW.isoformat(), "u/440")
    outcome = fix_loop.fix_verify.Outcome(found=[(was, fix)])
    monkeypatch.setattr(fix_loop.fix_verify, "verify", lambda *a, **k: outcome)
    closed, _ = close(ctx)
    assert f"settled [{ref}] on u/440 -- agent/x-3 never landed it" in closed.lines
    [resolution] = [i for i in triage.load(ctx.devkit_dir) if i.event == triage.RESOLVED_EVENT]
    assert resolution.fields["pr"] == "u/440"
    assert resolution.fields["note"].startswith("split by cause -- merged as u/440")
    assert triage.resolved_at(resolution) == made


def test_a_friction_row_todays_detectors_would_not_file_is_retired(ctx, monkeypatch):
    """d677ea57: once a detector's fix merges, its open rows retire themselves on the next
    pass, with the reason, instead of waiting for a sweep to re-prove each one."""
    monkeypatch.setattr(fix_loop.fix_reports, "read_trees", lambda root, projects: [])
    row = f"{NOW.isoformat()}\tevent=session-friction\tproject=devkit\tdetail=poll: x"
    (ctx.devkit_dir / "logs").mkdir(parents=True)
    (ctx.devkit_dir / "logs" / "harness-events.log").write_text(row + "\n", encoding="utf-8")
    ref = triage.item_id(row)
    monkeypatch.setattr(fix_loop.session_friction, "outdated", lambda items: [(ref, "gone")])
    closed, _ = close(ctx)
    assert f"retired [{ref}] -- gone" in closed.lines
    assert triage.open_items(triage.load(ctx.devkit_dir)) == []


def test_a_fix_not_merged_yet_is_in_flight_off_the_cache_verify_keeps(ctx, monkeypatch):
    """What the pass hands the backlog step, so a group waiting on a merge sends nothing."""
    monkeypatch.setattr(fix_loop.fix_reports, "read_trees", lambda root, projects: [])
    report = f"{NOW.isoformat()}\tevent=agent-report\tproject=devkit\tmessage=x"
    (ctx.devkit_dir / "logs").mkdir(parents=True)
    (ctx.devkit_dir / "logs" / "harness-events.log").write_text(report + "\n", encoding="utf-8")
    ref = triage.item_id(report)
    triage.resolve([ref], "fixed", pr="410", root=ctx.devkit_dir)
    assert fix_loop.in_flight(ctx) == {ref: "410"}
    assert close(ctx)[0].in_flight == {ref: "410"}, "read back for the backlog step"
    cache = ctx.ledger_path.parent / fix_loop.fix_verify.CACHE_NAME
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(f'["{ref}"]', encoding="utf-8")
    assert fix_loop.in_flight(ctx) == {}, "merged: settled in the cache"


def test_a_step_that_raises_costs_that_step_only(ctx, monkeypatch):
    monkeypatch.setattr(fix_loop.fix_reports, "read_trees", lambda root, projects: [])

    def broken(*_a, **_k):
        raise OSError("transcripts unreadable")

    monkeypatch.setattr(fix_loop.session_friction, "harvest", broken)
    history = [
        {
            "when": (NOW - _dt.timedelta(days=2)).isoformat(),
            "waiting": {"carameli #8": "held until x"},
        }
    ]
    monkeypatch.setattr(
        fix_loop.fix_stall,
        "read_history",
        lambda _p: [*history, {**history[0], "when": NOW.isoformat()}],
    )
    _, journal = close(ctx)
    assert kinds(journal) == ["pass-step-crashed", "stalled"]
    assert journal.crashed == ["harvest"]


def test_a_plan_harvest_reads_from_the_real_cursor_and_leaves_it_where_it_was(ctx, monkeypatch):
    """A plan pass harvested from an empty cursor, so every rehearsal listed the whole
    three-day window -- 17 "would file" lines, nearly all filed and resolved hours before
    -- and the supervisor's "is any filed line noise?" had nothing it could read."""
    cursor = ctx.ledger_path.parent / fix_loop.session_friction.CURSOR_NAME
    cursor.parent.mkdir(parents=True, exist_ok=True)
    cursor.write_text('{"t.jsonl": {"offset": 40, "line": 3, "cwd": "x"}}\n', encoding="utf-8")
    seen = []

    def harvest(root, path, now):
        seen.append(path.read_text(encoding="utf-8"))
        path.write_text("{}\n", encoding="utf-8")  # a harvest moves the cursor it is given
        return []

    monkeypatch.setattr(fix_loop.session_friction, "harvest", harvest)
    plan = fix_loop.Context(
        *[getattr(ctx, n) for n in ("root", "projects", "devkit_dir")],
        ctx.ledger_path,
        ctx.history_path,
        fix_cycle.PLAN,
        NOW,
    )
    assert fix_loop._harvest(plan, cursor) == []
    assert seen == ['{"t.jsonl": {"offset": 40, "line": 3, "cwd": "x"}}\n']
    assert cursor.read_text(encoding="utf-8").startswith('{"t.jsonl"'), "the cursor did not move"


def _job(name: str, result: int, ran: _dt.datetime) -> fix_loop.schedule_health.Job:
    later = ran + _dt.timedelta(minutes=15)
    return fix_loop.schedule_health.Job(
        name, True, result, ran.replace(tzinfo=None), later.replace(tzinfo=None)
    )


def test_a_failing_scheduled_job_is_filed_whatever_wraps_it(ctx):
    """Only `log-wrap.py --always` jobs filed their failures, and eight of twelve ran
    bare: reconcile exited 1 every 15 minutes over four undeletable husks and six
    stranded boxes, and no pass ever saw it (2026-09-27)."""
    ran = NOW - _dt.timedelta(minutes=5)
    [found] = fix_loop.job_findings(ctx, [_job("devkit-worktree-reconcile", 1, ran)])
    assert (found.kind, found.project) == (fix_loop.JOB_KIND, "devkit")
    assert found.detail == "devkit-worktree-reconcile: last run failed (exit 1)", "no time in it"
    assert found.evidence.endswith("reconcile.log")
    healthy = _job("devkit-worktree-reconcile", 0, ran)
    assert fix_loop.job_findings(ctx, [healthy]) == []


def test_a_job_failure_names_the_failing_runs_kept_copy_as_its_evidence(ctx):
    """3a8c74a3 named `reconcile.log`, which the next clean pass rewrote before any
    sweep read it; the run's `.failed.log` copy is what survives."""
    ran = _dt.datetime.now() - _dt.timedelta(minutes=5)
    kept = ctx.devkit_dir / "logs" / "reconcile.failed.log"
    kept.parent.mkdir(parents=True)
    kept.write_text("# exit=1\n", encoding="utf-8")
    job = fix_loop.schedule_health.Job("devkit-worktree-reconcile", True, 1, ran, None)
    [found] = fix_loop.job_findings(ctx, [job])
    assert found.evidence == str(kept)


def test_devkit_fixes_are_the_open_prs_and_those_merged_inside_the_hold(ctx):
    """f9ebcfd4: a merged fix still holds the consumer failures it names until adoption
    carries it there; one merged before the hold, or closed unmerged, holds nothing."""
    merged = (NOW - _dt.timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    stale = (NOW - fix_loop.MERGED_FIX_HOLDS - _dt.timedelta(minutes=1)).isoformat()
    rows = [
        {"number": 490, "body": "open", "state": "OPEN", "mergedAt": None},
        {"number": 483, "body": "merged", "state": "MERGED", "mergedAt": merged},
        {"number": 470, "body": "old", "state": "MERGED", "mergedAt": stale},
        {"number": 480, "body": "closed", "state": "CLOSED", "mergedAt": None},
        {"number": "x", "body": "malformed", "state": "OPEN"},
    ]
    asked: list[tuple[str, ...]] = []

    def gh_for(_root):
        def gh(*args):
            asked.append(args)
            return subprocess.CompletedProcess(args, 0, fix_loop.json.dumps(rows), "")

        return gh

    assert REAL_DEVKIT_FIXES(ctx, gh_for) == ((490, "open"), (483, "merged"))
    assert "--state" in asked[0] and "all" in asked[0]


def test_devkit_fixes_holds_nothing_when_gh_cannot_say(ctx):
    def failing(_root):
        return lambda *a: subprocess.CompletedProcess(a, 1, "", "gh: not logged in")

    def garbled(_root):
        return lambda *a: subprocess.CompletedProcess(a, 0, "{not json", "")

    assert REAL_DEVKIT_FIXES(ctx, failing) == ()
    assert REAL_DEVKIT_FIXES(ctx, garbled) == ()


def test_a_job_failures_command_reads_the_checkout_the_evidence_names(ctx, monkeypatch, tmp_path):
    """104d356c: a pass run from a supervisor's tree filed "no logs/reconcile.log" beside
    evidence naming the checkout's kept copy, because the hint stat'ed the tree's own
    empty `logs/`. Both halves read `ctx.devkit_dir`."""
    monkeypatch.setattr(fix_loop.schedule_health, "REPO_ROOT", tmp_path / "a-tree")
    ran = _dt.datetime.now() - _dt.timedelta(minutes=5)
    kept = ctx.devkit_dir / "logs" / "reconcile.failed.log"
    kept.parent.mkdir(parents=True)
    kept.write_text("# exit=1\n", encoding="utf-8")
    job = fix_loop.schedule_health.Job("devkit-worktree-reconcile", True, 1, ran, None)
    [found] = fix_loop.job_findings(ctx, [job])
    assert found.command.endswith("see logs/reconcile.failed.log"), found.command


def test_a_job_failure_resolved_after_that_run_is_not_filed_again_until_it_recurs(ctx):
    """The scheduler repeats a daily job's last result for a day after its fix lands; a
    group resolved since that run is not reopened by the same run, only by a later one."""
    ran = NOW - _dt.timedelta(hours=3)
    job = _job("devkit-installers", 2, ran)
    [found] = fix_loop.job_findings(ctx, [job])
    fix_findings.record_all([found], [], ctx.devkit_dir)
    [item] = triage.open_items(triage.load(ctx.devkit_dir))
    triage.resolve([item.id], "fixed", root=ctx.devkit_dir)
    assert fix_loop.job_findings(ctx, [job]) == [], "resolved after the run it reports"
    again = _job("devkit-installers", 2, _dt.datetime.now(_dt.UTC) + _dt.timedelta(days=1))
    assert len(fix_loop.job_findings(ctx, [again])) == 1, "it failed again after the fix"


def _resolved_job(ctx) -> tuple[fix_loop.schedule_health.Job, float]:
    """A `devkit-reap-stale` failure filed and resolved, then failing again a minute
    after the resolution; with the resolution's POSIX time."""
    [found] = fix_loop.job_findings(ctx, [_job("devkit-reap-stale", 2, NOW)])
    fix_findings.record_all([found], [], ctx.devkit_dir)
    [item] = triage.open_items(triage.load(ctx.devkit_dir))
    triage.resolve([item.id], "kept the tree whole", root=ctx.devkit_dir)
    [made] = [i for i in triage.load(ctx.devkit_dir) if i.event == triage.RESOLVED_EVENT]
    ran = _dt.datetime.now() + _dt.timedelta(minutes=1)  # the scheduler's local time
    return fix_loop.schedule_health.Job("devkit-reap-stale", True, 2, ran, None), (
        _dt.datetime.fromisoformat(made.stamp).timestamp()
    )


def _git_at(ran: float, head: float, asked: list[str], code: int = 0, stderr: str = ""):
    """A git whose checkout ran a commit made at `ran` and now has one made at `head`."""

    def git(argv, **_):
        asked.append(argv[-1])
        when = head if argv[-1] == "HEAD" else ran
        return subprocess.CompletedProcess(argv, code, f"{when:.0f}\n", stderr)

    return git


def test_a_job_run_on_code_older_than_its_fix_does_not_reopen_it(ctx):
    """cda106d0: reap-stale fired at 19:00:00 and the checkout fast-forwarded onto the
    merged fix at 19:00:02, so the run that predated the fix was filed as the fix not
    holding. The run's code is the checkout's HEAD at the run's start, off its reflog."""
    job, made = _resolved_job(ctx)
    asked: list[str] = []
    git = _git_at(made - 600, made + 60, asked)
    assert fix_loop.job_findings(ctx, [job], git) == [], "the next run tests the fix"
    since = job.last_run.astimezone(_dt.UTC).strftime("%Y-%m-%d %H:%M:%S +0000")
    assert f"HEAD@{{{since}}}" in asked, asked


@pytest.mark.parametrize(
    ("ran", "head", "code", "stderr", "why"),
    [
        (60, 120, 0, "", "the run had the fix and failed anyway"),
        (-600, -300, 0, "", "the checkout never moved past the fix: nothing newer will run"),
        (-600, 60, 128, "fatal: not a git repository", "git cannot say"),
        (-600, 60, 0, "warning: log for 'HEAD' only goes back to Tue", "reflog too short"),
    ],
)
def test_a_job_run_that_could_have_held_its_fix_is_filed(ctx, ran, head, code, stderr, why):
    job, made = _resolved_job(ctx)
    git = _git_at(made + ran, made + head, [], code, stderr)
    assert len(fix_loop.job_findings(ctx, [job], git)) == 1, why


def test_a_job_failure_never_resolved_asks_git_nothing(ctx):
    asked: list[str] = []
    job = _job("devkit-reap-stale", 2, NOW)
    assert len(fix_loop.job_findings(ctx, [job], _git_at(0, 0, asked))) == 1
    assert asked == []


def test_a_job_the_scheduler_only_remembers_failing_is_not_filed(ctx, monkeypatch):
    monkeypatch.setattr(
        fix_loop.schedule_health,
        "artifact_hint",
        lambda *a, **k: (
            " -- x is empty and was rewritten later: a later pass finished clean "
            "and the scheduler is reporting history"
        ),
    )
    job = _job("devkit-upgrade-projects", 2, NOW - _dt.timedelta(hours=9))
    assert fix_loop.job_findings(ctx, [job]) == []


def test_the_files_sessions_are_told_to_write_are_the_files_the_pass_reads():
    """Prose has no compiler: the channel the ship skill, the engineering rule and every
    fixer prompt name must be the path `fix_reports` reads, or the lines go nowhere."""
    root = Path(__file__).resolve().parents[1]
    channel = fix_reports.FRICTION_FILE.as_posix()
    for doc in (".claude/skills/ship/SKILL.md", ".claude/rules/engineering.md"):
        assert channel in (root / doc).read_text(encoding="utf-8"), doc
    assert channel in fix_loop.fix_findings.__doc__ or channel in fix_reports.__doc__


def test_the_ship_skill_adds_to_the_friction_file_rather_than_replacing_it():
    """ "Write one line per thing" read as the Write tool: a resolver replaced the two lines
    a live ledger sweep had left in the same tree, which only survived because that
    sweep noticed and put them back."""
    root = Path(__file__).resolve().parents[1]
    skill = " ".join((root / ".claude/skills/ship/SKILL.md").read_text(encoding="utf-8").split())
    assert "add one line per thing" in skill and "keeping the lines already there" in skill
    # Every session ships through the skill, not only a fixer the prompt told.
    assert "`fixed on this branch`" in skill and fix_reports.fixed_here("fixed on this branch")


def test_a_tree_another_session_is_live_in_is_busy_and_the_pass_own_session_is_not(
    ctx, monkeypatch
):
    path = tree(
        ctx,
        monkeypatch,
        sent=NOW - _dt.timedelta(minutes=20),
        transcript_age=_dt.timedelta(minutes=10),
    )
    closed, _ = close(ctx)
    assert closed.busy == {}, "the stamped session working in its own tree is in flight, not busy"
    resident = fix_reports.transcript_dir(path) / "resident.jsonl"
    resident.write_text(
        f'{{"timestamp": "{(NOW - _dt.timedelta(days=1)).isoformat()}"}}\n'
        f'{{"timestamp": "{(NOW - _dt.timedelta(minutes=2)).isoformat()}"}}\n',
        encoding="utf-8",
    )
    closed, _ = close(ctx)
    assert closed.busy == {("carameli", "agent/x-0919"): str(path)}


def test_the_pass_own_session_that_finished_does_not_hold_its_tree(ctx, monkeypatch):
    """Round four's rehearsal held six branches as "a session is working in" -- each by
    the very fixer whose intent this same pass was shipping. The stamped session is never
    another session, working or done."""
    path = tree(
        ctx,
        monkeypatch,
        sent=NOW - _dt.timedelta(minutes=40),
        transcript_age=_dt.timedelta(minutes=5),
    )
    (path / "logs" / "ship-intent.md").write_text("S\n", encoding="utf-8")
    closed, _ = close(ctx)
    assert closed.finished == [str(path)] and closed.busy == {}


def test_a_finished_or_dead_sessions_idle_process_is_stopped_and_a_working_one_is_not(
    ctx, monkeypatch
):
    """Fourteen finished fixers were still alive after two supervised runs."""
    path = tree(ctx, monkeypatch, sent=NOW - _dt.timedelta(hours=2))  # never started
    closed, _ = close(ctx)
    assert closed.finished == [str(path)] and closed.stopped == [f"x in {path}"]
    tree(
        ctx,
        monkeypatch,
        sent=NOW - _dt.timedelta(minutes=20),
        transcript_age=_dt.timedelta(minutes=5),
    )
    closed, _ = close(ctx)
    assert closed.finished == [] and closed.stopped == []


def test_still_working_is_a_busy_listing_or_a_background_task_still_out(monkeypatch, tmp_path):
    """8d2f56f5: a fixer that ended its turn to wait on its suite lists idle."""
    busy = frozenset({str(tmp_path / "listed").replace("\\", "/").lower()})
    waiting = {str(tmp_path / "waiting")}
    monkeypatch.setattr(fix_reports, "awaiting_task", lambda tree, now: str(tree) in waiting)
    assert fix_loop.still_working(busy, tmp_path / "listed", NOW)
    assert fix_loop.still_working(frozenset(), tmp_path / "waiting", NOW)
    assert not fix_loop.still_working(busy, tmp_path / "idle", NOW)
