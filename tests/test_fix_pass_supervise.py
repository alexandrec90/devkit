"""`scripts/fix-pass-supervise.py`: the mechanical half of `/supervise-fix-pass`."""

from __future__ import annotations

import datetime as _dt
import json
import os
from pathlib import Path
from typing import Any

import pytest
from support import REPO_ROOT, load_script

supervise = load_script("scripts/fix-pass-supervise.py")
fix_reports = supervise.fix_reports
# Their classes are invisible to mypy (loaded by path), so helpers returning one say Any.

NOW = _dt.datetime(2026, 9, 26, 12, 0, tzinfo=_dt.UTC)


@pytest.fixture(autouse=True)
def _unelevated(monkeypatch):
    """`main` refuses a dispatch from an elevated shell; the suite may run in one."""
    monkeypatch.setattr(supervise.agent_tabs, "is_elevated", lambda: False)


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


def test_a_wait_on_a_devkit_pr_that_names_the_failure_is_tracked():
    """Supervision 2026-10-01, round 2: `fix_send.named_by`'s hold was reported as a
    wait nothing tracks, though the PR it names is what lifts it, merged or closed."""
    line = (
        "capped   roguelike origin/main, devkit ledger -- pending devkit #486, which names "
        "every failure it is for"
    )
    assert supervise.check_record(line, 0, set()) == []


def test_a_failure_needs_its_finding_open():
    line = "shipped  carameli agent/x-0919 -- failed: push: rejected"
    [found] = supervise.check_record(line, 1, set())
    assert "no ship-failed finding open" in found
    assert supervise.check_record(line, 1, {("ship-failed", "carameli")}) == []
    sent = "sent     devkit #9 -- FAILED to resolve"
    assert "no resolve-failed finding" in supervise.check_record(sent, 1, set())[0]


def test_a_record_that_is_not_this_iterations_pass_is_a_violation():
    """Run elevated, the pass handed every dispatch to the scheduled task and wrote no
    record, so each iteration read back the rehearsal's `mode=plan` record -- plus one
    more appended watchdog line -- and three iterations reported clean having sent
    nothing."""
    assert supervise.check_ran("fix-pass: mode=dispatch\nharness  clean", "dispatch") == []
    stale = "fix-pass: mode=plan\nharness  clean\nwatchdog: self-update -- current"
    assert supervise.check_ran(stale, "dispatch") == [
        "the pass did not run in dispatch mode here: fix-pass: mode=plan"
    ]
    yielded = "fix-pass: yielded -- another dispatching pass holds C:/b/fix-pass.lock"
    assert supervise.check_ran(yielded, "dispatch") == [
        f"the pass did not run in dispatch mode here: {yielded}"
    ]
    handed = "fix-pass: handed to devkit-fix-pass -- this shell is elevated"
    assert supervise.check_ran(handed, "dispatch") == [
        f"the pass did not run in dispatch mode here: {handed}"
    ]
    assert supervise.check_ran("", "plan") == ["the pass did not run in plan mode here: no record"]
    # A crash or a refusal is `check_record`'s to report, once.
    assert supervise.check_ran("fix-pass: CRASHED -- KeyError: 'x'", "dispatch") == []
    assert supervise.check_ran("fix-pass: FAILED -- no gh", "dispatch") == []


def test_an_elevated_dispatch_is_refused_before_the_first_iteration(tmp_path, monkeypatch, capsys):
    workspace = _workspace(tmp_path)
    monkeypatch.setattr(supervise.agent_tabs, "is_elevated", lambda: True)
    monkeypatch.setattr(supervise, "iterate", lambda *a: pytest.fail("an elevated run iterated"))
    monkeypatch.setattr(supervise, "REPO_ROOT", tmp_path)
    assert supervise.main(["--workspace", str(workspace)]) == supervise.EXIT_REFUSED
    assert "elevated" in capsys.readouterr().err
    ran = []
    monkeypatch.setattr(
        supervise,
        "iterate",
        lambda ws, n, mode, clock, since=None: (
            ran.append(mode) or supervise.Iteration(n, clock().isoformat(), 0, "")
        ),
    )
    argv = ["--mode", "plan", "--iterations", "1", "--workspace", str(workspace)]
    assert supervise.main(argv) == supervise.EXIT_CLEAN
    assert ran == ["plan"], "a rehearsal launches nothing, so it may run elevated"


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


def test_a_session_is_judged_as_the_pass_files_it(tmp_path):
    """Supervision 2026-10-01: a resolver ran devkit's `run-tests.py` bare, as its prompt
    said -- 25 files for what changed -- and `measure` called it a whole suite, since it
    never asked the tree whether its runner is targeted. The pass filed nothing, so the
    violation was the supervisor disagreeing with the detector it audits."""
    tree = tmp_path / "devkit" / ".claude" / "worktrees" / "rippling"
    (tree / "scripts").mkdir(parents=True)
    (tree / ".claude" / "rules").mkdir(parents=True)
    (tree / "scripts" / "run-tests.py").write_text(
        'parser.add_argument("--all", action="store_true")\n', encoding="utf-8"
    )
    (tree / ".claude" / "rules" / "session-scope.md").write_text(
        f"# scope\n{supervise.session_friction.WHOLE_RUN_NAMED}\n", encoding="utf-8"
    )
    command = ".venv/Scripts/python.exe scripts/run-tests.py > out.txt 2>&1"
    rows = [
        {
            "type": "assistant",
            "cwd": str(tree),
            "message": {
                "content": [{"type": "tool_use", "id": "1", "input": {"command": command}}]
            },
        },
    ]
    path = tmp_path / "s.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    assert supervise.measure(path, str(tree))[2] == []
    (tree / "scripts" / "run-tests.py").write_text("# runs everything\n", encoding="utf-8")
    full = supervise.session_friction.COMMAND_DETAIL["full-suite"]
    assert supervise.measure(path, str(tree))[2] == [f"full-suite: {full}"]


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

    [session] = supervise.settle(lambda: [tree], now + _dt.timedelta(hours=1), lambda: now, nap)
    assert naps == [1] and session.state == fix_reports.DONE


def test_settle_holds_a_session_that_left_its_intent_while_it_is_still_busy(tmp_path):
    """A ledger sweep wrote its intent at 04:09 and worked 70 calls more; the audit read
    a transcript rendered at the intent and never saw the half with the stranded fix."""
    now = _dt.datetime.now(_dt.UTC)
    tree = _tree(tmp_path, now - _dt.timedelta(minutes=5))
    intent = tree.path / "logs" / "ship-intent.md"
    intent.write_text("S\n", encoding="utf-8")
    future = (now + _dt.timedelta(minutes=1)).timestamp()
    os.utime(intent, (future, future))
    polls = iter([frozenset({str(tree.path).replace("\\", "/").lower()}), frozenset()])
    naps = []
    [session] = supervise.settle(
        lambda: [tree],
        now + _dt.timedelta(hours=1),
        lambda: now,
        naps.append,
        busy=lambda: next(polls),
    )
    assert len(naps) == 1 and session.state == fix_reports.DONE


def test_live_dirs_are_the_busy_sessions_claude_agents_lists(monkeypatch):
    rows = [
        {"cwd": r"C:\ws\devkit\.claude\worktrees\a", "status": "busy"},
        {"cwd": r"C:\ws\devkit\.claude\worktrees\b", "status": "idle"},
    ]
    monkeypatch.setattr(supervise.bg_sessions, "listed", lambda _runner: rows)
    assert supervise.live_dirs() == frozenset({"c:/ws/devkit/.claude/worktrees/a"})


def test_settle_gives_up_at_its_deadline(tmp_path):
    now = _dt.datetime.now(_dt.UTC)
    tree = _tree(tmp_path, now - _dt.timedelta(minutes=5))
    [session] = supervise.settle(lambda: [tree], now, lambda: now, lambda _s: None)
    assert session.state == fix_reports.WORKING


def test_only_this_iterations_dispatches_are_waited_on(tmp_path, monkeypatch):
    old, new = _tree(tmp_path / "o", NOW - _dt.timedelta(hours=3)), _tree(tmp_path / "n", NOW)
    monkeypatch.setattr(supervise.fix_reports, "read_trees", lambda root, projects: [old, new])
    assert supervise.dispatched_since(tmp_path, [], NOW - _dt.timedelta(minutes=1)) == [new]


def test_a_session_another_pass_sends_during_the_wait_is_waited_on_and_audited(tmp_path):
    """2026-10-02: the scheduled pass sent a fixer at sports_betting #53 two minutes into
    iteration 2's wait; the trees were listed once, as the pass returned, so no iteration
    audited it."""
    now = _dt.datetime.now(_dt.UTC)
    ours = _tree(tmp_path / "a", now - _dt.timedelta(minutes=5))
    theirs = _tree(tmp_path / "b", now - _dt.timedelta(minutes=1))

    def finish(tree):
        intent = tree.path / "logs" / "ship-intent.md"
        intent.write_text("S\n", encoding="utf-8")
        future = (now + _dt.timedelta(minutes=1)).timestamp()
        os.utime(intent, (future, future))

    # Ours is working at the first poll; the other pass's tree is stamped by the second.
    polls = iter([[ours], [ours, theirs], [ours, theirs]])
    naps = []

    def nap(_seconds):
        naps.append(1)
        finish(ours)
        if len(naps) == 2:
            finish(theirs)

    sessions = supervise.settle(lambda: next(polls), now + _dt.timedelta(hours=1), lambda: now, nap)
    assert sorted(s.tree for s in sessions) == sorted([str(ours.path), str(theirs.path)])
    assert len(naps) == 2, "the other pass's working session held the wait"
    assert {s.state for s in sessions} == {fix_reports.DONE}


def test_a_tree_stamped_again_is_held_once_at_its_newer_dispatch(tmp_path):
    first = _tree(tmp_path, NOW - _dt.timedelta(hours=1))
    again = _tree(tmp_path, NOW)
    assert supervise._joined([first], [again]) == [again]


def test_each_iteration_audits_what_was_stamped_since_the_last_one_ended(tmp_path, monkeypatch):
    """A session sent between two iterations -- after one's wait, before the next pass --
    belongs to the next iteration's audit, not to neither."""
    workspace = _workspace(tmp_path)
    monkeypatch.setattr(supervise, "RECORD", tmp_path / "fix-pass.log")
    monkeypatch.setattr(supervise, "run_pass", lambda ws, mode: (0, ""))
    asked = []

    def since(root, projects, when):
        asked.append(when)
        return []

    monkeypatch.setattr(supervise, "dispatched_since", since)
    ended = NOW - _dt.timedelta(minutes=20)
    supervise.iterate(workspace, 2, "dispatch", lambda: NOW, ended)
    assert asked and set(asked) == {ended}
    asked.clear()
    supervise.iterate(workspace, 1, "dispatch", lambda: NOW)
    assert set(asked) == {NOW}


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
    # The script refuses an elevated dispatch; the skill says how to get past that.
    assert "runas /trustlevel:0x20000" in skill
    # 2523011c: groups fixed in the supervisor's tree and left open were sent to a fixer.
    assert "--resolve-like <id>" in skill and "--pr <this tree's branch>" in skill


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
    # Every ledger row a second after the last, as on a loaded machine: a row is
    # content-addressed with its stamp, so a second one is a new id.
    ticks = iter(range(1, 100))

    class Clock(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW + _dt.timedelta(seconds=next(ticks))

    events = supervise.fix_findings.harness_events
    monkeypatch.setattr(events, "_dt", type("dt", (), {"datetime": Clock, "UTC": _dt.UTC}))

    def fake_pass(ws, mode):
        record.write_text("capped   carameli #1 -- needs a human\n", encoding="utf-8")
        finding = supervise.fix_findings.Finding("ship-failed", "carameli", "x")
        # Against the open ledger, as the pass files: with no items the second iteration
        # wrote a second row, whose id differs once the clock crosses a second.
        open_now = supervise.triage.load(tmp_path / "devkit")
        supervise.fix_findings.record_all([finding], open_now, tmp_path / "devkit")
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

    def fake_iterate(ws, number, mode, clock, since=None):
        runs.append((number, mode, since is None))
        return supervise.Iteration(
            number, clock().isoformat(), 0, "", violations=["x"] if number == 2 else []
        )

    monkeypatch.setattr(supervise, "iterate", fake_iterate)
    monkeypatch.setattr(supervise, "REPO_ROOT", tmp_path)
    argv = ["--iterations", "2", "--min-gap", "0", "--workspace", str(workspace)]
    assert supervise.main(argv) == supervise.EXIT_VIOLATED
    # The second iteration is handed when the first ended; the first, nothing.
    assert runs == [(1, "dispatch", True), (2, "dispatch", False)]
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
    [session] = supervise.settle(
        lambda: [tree], NOW + _dt.timedelta(hours=1), lambda: NOW, naps.append
    )
    assert naps == [] and session.state == "unknown" and session.readable == ""


def test_a_settled_sessions_transcript_is_rendered_for_the_audit(tmp_path, monkeypatch):
    transcript = tmp_path / "s.jsonl"
    transcript.write_text('{"type": "user", "message": {"content": "go"}}\n', encoding="utf-8")
    tree = _tree(tmp_path, NOW)
    monkeypatch.setattr(
        supervise.fix_reports, "session_state", lambda p, now: (fix_reports.DONE, str(transcript))
    )
    [session] = supervise.settle(
        lambda: [tree], NOW, lambda: NOW, lambda _s: None, out=tmp_path / "out"
    )
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

    def costly(ws, number, mode, clock, since=None):
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
