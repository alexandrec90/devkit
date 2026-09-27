"""`scripts/fix-pass-supervise.py`: the mechanical half of `/supervise-fix-pass`."""

from __future__ import annotations

import datetime as _dt
import json
import os
from pathlib import Path
from typing import Any

from support import REPO_ROOT, load_script

supervise = load_script("scripts/fix-pass-supervise.py")
fix_reports = supervise.fix_reports
# Their classes are invisible to mypy (loaded by path), so helpers returning one say Any.

NOW = _dt.datetime(2026, 9, 26, 12, 0, tzinfo=_dt.UTC)


# --- the record ---------------------------------------------------------------------------


def test_a_clean_record_breaks_nothing():
    record = "\n".join(
        [
            "fix-pass: mode=dispatch",
            "harness  clean",
            "capped   carameli #1 -- already dispatched at 2026-09-26T11:00:00+00:00",
            "capped   carameli #2 -- escalated: the devkit session has it on the harness ledger",
            "capped   devkit ledger -- held until the devkit session in C:/t finishes",
            "sent     carameli #3 -- dispatch",
            "ledger   0 open on the harness-defect ledger",
        ]
    )
    assert supervise.check_record(record, 0, set()) == []


def test_a_person_named_as_the_next_step_is_a_violation_in_any_wording():
    for line in (
        "capped   carameli #1 -- 2 session(s) sent -- needs a human",
        "capped   carameli #1 -- fuse: 24 sessions today -- read the record",
        "shipped  carameli x -- NOT shipped: move the work by hand",
    ):
        assert any("a person is named" in v for v in supervise.check_record(line, 0, set())), line


def test_a_hold_for_memory_is_a_wait_the_pass_tracks():
    """The pass re-sends it next pass, and a hold that lasts is a stale wait it files."""
    fix_send = load_script("scripts/fix_send.py")
    why = fix_send._no_memory(fix_send.fix_plan.Decision(fix_send.fix_plan.DISPATCH, "n", ()), 100)
    assert why.startswith(fix_send.HELD_FOR_MEMORY)
    assert supervise.check_record(f"capped   carameli #1 -- {why}", 0, set()) == []


def test_a_wait_must_name_what_it_waits_on():
    [found] = supervise.check_record("capped   carameli #1 -- because", 0, set())
    assert found.startswith("a wait nothing tracks")


def test_a_failure_needs_its_finding_open():
    line = "shipped  carameli agent/x-0919 -- failed: push: rejected"
    [found] = supervise.check_record(line, 1, set())
    assert "no ship-failed finding open" in found
    assert supervise.check_record(line, 1, {("ship-failed", "carameli")}) == []
    sent = "sent     devkit #9 -- FAILED to resolve"
    assert "no resolve-failed finding" in supervise.check_record(sent, 1, set())[0]


def test_the_pass_failing_itself_is_a_violation():
    found = supervise.check_record(
        "watchdog: the pass failed (pass-crashed): TypeError: x", 2, set()
    )
    assert any("exited 2" in v for v in found) and any("did not run" in v for v in found)
    assert supervise.check_record("fix-pass: CRASHED -- KeyError: 'x'", None, set())


# --- the sessions -------------------------------------------------------------------------


def test_a_session_without_an_outcome_or_with_friction_is_a_violation():
    done = supervise.Session("carameli", "C:/t/a", "n", fix_reports.DONE)
    dead = supervise.Session("carameli", "C:/t/b", "n", fix_reports.NO_OUTCOME)
    rough = supervise.Session("devkit", "C:/t/c", "n", fix_reports.DONE, friction=["poll: sleep N"])
    assert supervise.check_sessions([done]) == []
    found = supervise.check_sessions([dead, rough])
    assert "did not finish with an outcome (ended without an outcome): carameli b" in found[0]
    assert found[1] == "a dispatched session hit friction: devkit c: poll: sleep N"


def test_a_session_is_measured_from_its_transcript(tmp_path):
    rows = [
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "id": "1", "input": {"command": "sleep 300"}}]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "1", "content": "x", "is_error": True}
                ]
            },
        },
    ]
    path = tmp_path / "s.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    poll = supervise.session_friction.COMMAND_DETAIL["poll"]
    assert supervise.measure(path) == (1, 1, [f"poll: {poll}"], 0)
    assert supervise.measure(None) == (0, 0, [], 0)


def _tree(tmp_path: Path, sent: _dt.datetime) -> Any:
    path = tmp_path / "t"
    (path / "logs").mkdir(parents=True, exist_ok=True)
    fix_reports.stamp(path, "pr:carameli:1:a:d:dispatch", "n", sent)
    return fix_reports.Tree("carameli", path, "agent/x", fix_reports.read_stamp(path), ())


def test_settle_waits_for_a_working_session_and_stops_at_its_outcome(tmp_path, monkeypatch):
    now = _dt.datetime.now(_dt.UTC)
    tree = _tree(tmp_path, now - _dt.timedelta(minutes=5))
    naps = []

    def nap(_seconds):
        naps.append(1)
        (tree.path / "logs" / "ship-intent.md").write_text("S\n", encoding="utf-8")
        future = (now + _dt.timedelta(minutes=1)).timestamp()
        os.utime(tree.path / "logs" / "ship-intent.md", (future, future))

    [session] = supervise.settle([tree], now + _dt.timedelta(hours=1), lambda: now, nap)
    assert naps == [1] and session.state == fix_reports.DONE


def test_settle_gives_up_at_its_deadline(tmp_path):
    now = _dt.datetime.now(_dt.UTC)
    tree = _tree(tmp_path, now - _dt.timedelta(minutes=5))
    [session] = supervise.settle([tree], now, lambda: now, lambda _s: None)
    assert session.state == fix_reports.WORKING


def test_only_this_iterations_dispatches_are_waited_on(tmp_path, monkeypatch):
    old, new = _tree(tmp_path / "o", NOW - _dt.timedelta(hours=3)), _tree(tmp_path / "n", NOW)
    monkeypatch.setattr(supervise.fix_reports, "read_trees", lambda root, projects: [old, new])
    assert supervise.dispatched_since(tmp_path, [], NOW - _dt.timedelta(minutes=1)) == [new]


# --- across iterations, and the report ------------------------------------------------------


def _iteration(number: int, backlog: int) -> Any:
    return supervise.Iteration(number, NOW.isoformat(), 0, "", backlog=backlog)


def test_a_backlog_that_grows_every_iteration_is_flagged():
    assert supervise.check_progress([_iteration(1, 3), _iteration(2, 4)]) == []
    assert supervise.check_progress([_iteration(1, 3), _iteration(2, 4), _iteration(3, 4)]) == []
    [found] = supervise.check_progress([_iteration(1, 3), _iteration(2, 4), _iteration(3, 6)])
    assert found.endswith("3 -> 4 -> 6")


def test_the_report_is_a_parseable_file_and_a_log_that_leads_with_violations(tmp_path):
    one = _iteration(1, 2)
    one.violations = ["a wait nothing tracks: capped x"]
    one.sessions = [supervise.Session("devkit", "C:/t", "n", "done", "s.jsonl", 12, 1)]
    path = supervise.write_report([one], tmp_path)
    assert json.loads(path.read_text(encoding="utf-8"))[0]["sessions"][0]["calls"] == 12
    log = (tmp_path / supervise.LOG).read_text(encoding="utf-8")
    assert "VIOLATION a wait nothing tracks" in log and "12 calls" in log


def test_open_kinds_reads_the_kind_off_each_open_finding(tmp_path):
    finding = supervise.fix_findings.Finding("ship-failed", "carameli", "agent/x: rejected")
    supervise.fix_findings.record_all([finding], [], tmp_path)
    assert supervise.open_kinds(tmp_path) == {("ship-failed", "carameli")}


def test_the_skill_drives_this_script_and_says_not_to_poll_it():
    skill = (REPO_ROOT / ".claude" / "skills" / "supervise-fix-pass" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "scripts/fix-pass-supervise.py" in skill and "run_in_background" in skill
    assert "Do not poll it" in skill and "logs/friction.md" in skill


# --- the loop -------------------------------------------------------------------------------


def _workspace(tmp_path: Path) -> Path:
    (tmp_path / "devkit").mkdir()
    workspace = tmp_path / "w.code-workspace"
    workspace.write_text('{"folders": [{"path": "devkit"}], "settings": {}}', encoding="utf-8")
    return workspace


def test_an_iteration_runs_the_pass_reads_its_record_and_what_it_filed(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    record = tmp_path / "fix-pass.log"
    monkeypatch.setattr(supervise, "RECORD", record)

    def fake_pass(ws, mode):
        record.write_text("capped   carameli #1 -- needs a human\n", encoding="utf-8")
        finding = supervise.fix_findings.Finding("ship-failed", "carameli", "x")
        supervise.fix_findings.record_all([finding], [], tmp_path / "devkit")
        return 0, ""

    monkeypatch.setattr(supervise, "run_pass", fake_pass)
    monkeypatch.setattr(supervise, "dispatched_since", lambda root, projects, since: [])
    one = supervise.iterate(workspace, 1, "dispatch", lambda: NOW)
    assert one.filed == ["fix-pass-finding carameli: ship-failed: x"] and one.backlog == 1
    assert any("a person is named" in v for v in one.violations)
    plan = supervise.iterate(workspace, 2, "plan", lambda: NOW)
    assert plan.sessions == [] and plan.filed == []


def test_main_runs_each_iteration_and_exits_on_whether_any_broke_the_contract(
    tmp_path, monkeypatch
):
    workspace = _workspace(tmp_path)
    runs = []

    def fake_iterate(ws, number, mode, clock):
        runs.append((number, mode))
        return supervise.Iteration(
            number, clock().isoformat(), 0, "", violations=["x"] if number == 2 else []
        )

    monkeypatch.setattr(supervise, "iterate", fake_iterate)
    monkeypatch.setattr(supervise, "REPO_ROOT", tmp_path)
    argv = ["--iterations", "2", "--min-gap", "0", "--workspace", str(workspace)]
    assert supervise.main(argv) == supervise.EXIT_VIOLATED
    assert runs == [(1, "dispatch"), (2, "dispatch")]
    assert (
        supervise.main(
            [*argv[:1], "1", "--min-gap", "0", "--mode", "plan", "--workspace", str(workspace)]
        )
        == 0
    )
    assert (tmp_path / supervise.REPORT).is_file()


def test_the_pass_is_run_through_the_watchdog(tmp_path, monkeypatch):
    script = tmp_path / "watchdog.py"
    script.write_text(
        "import sys\nprint(' '.join(sys.argv[1:]), sys.flags.utf8_mode, '\\u201d')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(supervise, "WATCHDOG", script)
    code, output = supervise.run_pass(tmp_path / "w", "plan")
    assert code == 0 and output.startswith("--mode plan --workspace")
    assert output.rstrip().endswith(" 1 \u201d"), "UTF-8 end to end, as the pass runs"
    assert supervise.utc_now().tzinfo is not None


def test_a_session_this_cannot_see_does_not_hold_the_wait(tmp_path):
    """A stamp for a Codex tab reads `""`: waiting on it held a whole `SETTLE`."""
    path = tmp_path / "t"
    (path / "logs").mkdir(parents=True)
    fix_reports.stamp(path, "k", "n", NOW, agent="codex")
    tree = fix_reports.Tree("carameli", path, "agent/x", fix_reports.read_stamp(path), ())
    naps = []
    [session] = supervise.settle([tree], NOW + _dt.timedelta(hours=1), lambda: NOW, naps.append)
    assert naps == [] and session.state == "unknown" and session.readable == ""


def test_a_settled_sessions_transcript_is_rendered_for_the_audit(tmp_path, monkeypatch):
    transcript = tmp_path / "s.jsonl"
    transcript.write_text('{"type": "user", "message": {"content": "go"}}\n', encoding="utf-8")
    tree = _tree(tmp_path, NOW)
    monkeypatch.setattr(
        supervise.fix_reports, "session_state", lambda p, now: (fix_reports.DONE, str(transcript))
    )
    [session] = supervise.settle([tree], NOW, lambda: NOW, lambda _s: None, out=tmp_path / "out")
    assert Path(session.readable).read_text(encoding="utf-8") == "L1 USER: go\n"


# --- the spend watch, which stands in for the daily fuses ---------------------------------


def test_output_tokens_are_counted_once_per_response():
    """One record per content block, each repeating the response's usage."""
    usage = {"output_tokens": 500}
    rows = [
        {"type": "assistant", "requestId": "r1", "message": {"id": "m1", "usage": usage}},
        {"type": "assistant", "requestId": "r1", "message": {"id": "m1", "usage": usage}},
        {
            "type": "assistant",
            "requestId": "r2",
            "message": {"id": "m2", "usage": {"output_tokens": 70}},
        },
        {"type": "user", "message": {"content": "hi"}},
        {"type": "assistant", "message": {"usage": "junk"}},
    ]
    assert supervise.output_tokens(rows) == 570


def test_a_session_or_an_iteration_past_the_watch_is_a_violation():
    lean = supervise.Session("devkit", "C:/t/a", "n", fix_reports.DONE, calls=40, tokens=9000)
    heavy = supervise.Session("devkit", "C:/t/b", "n", fix_reports.DONE, calls=40, tokens=500_000)
    assert supervise.check_spend([lean]) == []
    assert supervise.check_spend([heavy]) == ["spend: devkit b made 40 calls, 500000 output tokens"]
    many = [lean] * (supervise.SPEND_SESSIONS + 1)
    assert supervise.check_spend(many)[0].startswith(
        f"spend: {supervise.SPEND_SESSIONS + 1} sessions"
    )


def test_the_brake_stops_the_run_past_its_token_total(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    runs = []

    def costly(ws, number, mode, clock):
        runs.append(number)
        session = supervise.Session("devkit", "C:/t", "n", fix_reports.DONE, tokens=600)
        return supervise.Iteration(number, clock().isoformat(), 0, "", sessions=[session])

    monkeypatch.setattr(supervise, "iterate", costly)
    monkeypatch.setattr(supervise, "REPO_ROOT", tmp_path)
    argv = [
        "--iterations",
        "5",
        "--min-gap",
        "0",
        "--brake-tokens",
        "1000",
        "--workspace",
        str(workspace),
    ]
    assert supervise.main(argv) == supervise.EXIT_VIOLATED
    assert runs == [1, 2], "the second iteration crossed 1000 and nothing after it started"
