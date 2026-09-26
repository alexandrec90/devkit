"""`scripts/fix_loop.py`: every read-back turns a "no" into a finding, and plan writes nothing."""

from __future__ import annotations

import datetime as _dt
import os
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


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    (tmp_path / "devkit").mkdir()
    monkeypatch.setattr(fix_reports, "CLAUDE_PROJECTS", tmp_path / "projects")
    monkeypatch.setattr(fix_loop.session_friction, "harvest", lambda *a, **k: [])
    monkeypatch.setattr(fix_loop.fix_verify, "verify", lambda *a, **k: [])
    return fix_loop.Context(
        tmp_path,
        ["devkit", "carameli"],
        tmp_path / "devkit",
        tmp_path / ".worktrees" / "dispatch.json",
        tmp_path / "history.jsonl",
        fix_cycle.DISPATCH,
        NOW,
    )


def tree(ctx, monkeypatch, *, key=KEY, sent=NOW, blocked="", friction="", transcript_age=None):
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
        "agent/x-0919",
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


def test_a_session_gone_quiet_without_an_outcome_is_dead(ctx, monkeypatch):
    tree(ctx, monkeypatch, sent=NOW - _dt.timedelta(hours=5), transcript_age=_dt.timedelta(hours=3))
    _, journal = close(ctx)
    [found] = journal.findings
    assert "ended without an outcome" in found.detail and found.evidence.endswith("s.jsonl")


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
    monkeypatch.setattr(fix_loop.fix_verify, "verify", lambda *a, **k: [(ref, "closed unmerged")])
    closed, _ = close(ctx)
    assert closed.lines == [f"reopened [{ref}] -- closed unmerged"]
    assert [i.id for i in triage.open_items(triage.load(ctx.devkit_dir))] == [ref]


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


def test_the_files_sessions_are_told_to_write_are_the_files_the_pass_reads():
    """Prose has no compiler: the channel the ship skill, the engineering rule and every
    fixer prompt name must be the path `fix_reports` reads, or the lines go nowhere."""
    root = Path(__file__).resolve().parents[1]
    channel = fix_reports.FRICTION_FILE.as_posix()
    for doc in (".claude/skills/ship/SKILL.md", ".claude/rules/engineering.md"):
        assert channel in (root / doc).read_text(encoding="utf-8"), doc
    assert channel in fix_loop.fix_findings.__doc__ or channel in fix_reports.__doc__


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
        f'{{"timestamp": "{(NOW - _dt.timedelta(days=1)).isoformat()}"}}\n', encoding="utf-8"
    )
    os.utime(resident, (NOW.timestamp(), NOW.timestamp()))
    closed, _ = close(ctx)
    assert closed.busy == {("carameli", "agent/x-0919"): str(path)}
