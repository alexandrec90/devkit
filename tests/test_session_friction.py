"""`scripts/session_friction.py` and `session_transcripts.py`: the turns the harness wasted.

Every transcript here is written under `tmp_path` in the shape each CLI writes, and each
detector is tested for what it must file and -- as much -- for what it must not: the
first uncalibrated harvest filed four false findings for every real one.
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import session_friction as sf
import session_transcripts as st

NOW = _dt.datetime(2026, 9, 26, 12, 0, tzinfo=_dt.UTC)


def user(text: str, **extra) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}, **extra}


def call(command: str, call_id: str) -> dict:
    block = {"type": "tool_use", "id": call_id, "name": "Bash", "input": {"command": command}}
    return {"type": "assistant", "message": {"content": [block]}}


def result(text: str, call_id: str, error: bool = True) -> dict:
    block = {"type": "tool_result", "tool_use_id": call_id, "content": text, "is_error": error}
    return {"type": "user", "message": {"content": [block]}}


def transcript(path: Path, rows: list[dict], cwd: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({**row, "cwd": cwd}) for row in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def classes(rows: list[dict]) -> list[str]:
    events = [e for n, row in enumerate(rows, 1) for e in st.claude_events(row, n)]
    return sorted(cls for cls, _, _ in sf.detect(events))


# --- the detectors ------------------------------------------------------------------------


def test_a_guard_refusal_is_friction():
    refusal = "This session is isolated in the worktree C:\\ws\\x\\.claude\\worktrees\\a, but..."
    assert classes([call("git -C .. status", "1"), result(refusal, "1")]) == ["isolation-guard"]


def test_a_missing_module_is_friction_only_when_the_call_failed():
    """A file that quotes an error is not one: most of the first harvest's noise."""
    assert classes(
        [call("python -m pytest tests/x.py", "1"), result("No module named pytest", "1")]
    ) == ["environment"]
    quoted = result("docs: prints 'grep: command not found' when absent", "2", error=False)
    assert classes([call("cat notes.md", "2"), quoted]) == []


def test_a_failing_test_is_the_work_and_not_friction():
    """Red, edit, red, edit: each failure followed a change, so none was a wasted retry."""
    rows = []
    for i in range(4):
        rows += [call("python -m pytest tests/test_x.py", str(i)), result("1 failed", str(i))]
        rows.append(_tool("Edit", f"e{i}", file_path="x.py"))
    assert classes(rows) == []


def test_the_same_failing_command_three_times_is_a_repeat():
    rows = []
    for i in range(3):
        rows += [call("gh pr view 12 --json nope", str(i)), result("unknown field", str(i))]
    [(cls, what, _)] = sf.detect([e for n, r in enumerate(rows, 1) for e in st.claude_events(r, n)])
    assert cls == "repeat-failure" and what.startswith("x3 gh pr view")


def test_waiting_loops_and_no_verify_are_friction_whatever_they_return():
    assert classes([call("sleep 120 && gh pr checks 5", "1")]) == ["poll"]
    assert classes([call("cd x; until [ -s f ]; do sleep 5; done", "1")]) == ["poll"]
    assert classes([call("git commit --no-verify -m x", "1")]) == ["no-verify"]
    assert classes([call("sleep 2", "1")]) == [], "a short settle is not a poll"


def test_the_prescribed_wait_and_a_sleep_in_source_text_are_not_polls():
    """`gh pr checks --watch` is the one wait the engineering rule prescribes, and a
    test written through a heredoc naming `sleep 99` is text, not a command -- the
    first live harvest filed both."""
    assert classes([call("gh pr checks 5 --watch --fail-fast", "1")]) == []
    heredoc = 'cat >> t.py <<\'EOF\'\n    chunk = call("sleep 99", "1")\nEOF'
    assert classes([call(heredoc, "1")]) == []


def test_the_full_suite_is_friction_and_a_targeted_run_is_not():
    assert classes([call(".venv/Scripts/python.exe -m pytest -q", "1")]) == ["full-suite"]
    assert classes([call("python scripts/run-tests.py", "1")]) == ["full-suite"]
    assert classes([call("python -m pytest tests -q -n auto", "1")]) == ["full-suite"]
    for narrowed in (
        "python -m pytest tests/test_x.py -q",
        "python -m pytest -k fix_pass",
        "python scripts/run-tests.py --changed",
        "python -m pytest --collect-only",
    ):
        assert classes([call(narrowed, "1")]) == [], narrowed


def test_pytest_inside_source_text_is_not_a_test_run():
    heredoc = "python - <<'EOF'\nimport pytest\nprint(pytest.__version__)\nEOF"
    assert classes([call(heredoc, "1")]) == []
    assert classes([call('echo "pytest-timeout" >> reqs.txt', "1")]) == []


def test_the_user_objecting_is_friction_but_not_the_opening_task_or_an_injected_row():
    rows = [
        user("Why did you run the whole suite? That is the task."),
        user("why did you fix though? That was not asked."),
        user("<command-name>/effort</command-name> why did you"),
        user("stop doing that", isMeta=True),
        user("please fix the tests"),
    ]
    events = [e for n, row in enumerate(rows, 1) for e in st.claude_events(row, n)]
    [(cls, what, event)] = sf.detect(events)
    assert cls == "user-frustration" and event.line == 2 and what.startswith("why did you fix")


def test_normalize_keeps_what_recurs():
    text = "\x1b[31mC:\\ws\\devkit\\.claude\\worktrees\\abc-def\\x failed at deadbeef1 in 1234ms"
    assert sf.normalize(text) == "<tree>\\x failed at <sha> in Nms"


# --- the two formats ----------------------------------------------------------------------


def test_codex_rows_become_the_same_events():
    rows = [
        {"type": "session_meta", "payload": {"cwd": "C:/ws/devkit"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "fix it"}},
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "call_id": "c",
                "arguments": json.dumps({"command": ["git", "commit", "--no-verify"]}),
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "c",
                "output": "Exit code: 1\nNo module named x",
            },
        },
    ]
    assert st.cwd_of(rows[0]) == "C:/ws/devkit" and st.cwd_of(rows[1]) == ""
    events = [e for n, row in enumerate(rows, 1) for e in st.codex_events(row, n)]
    assert [e.kind for e in events] == ["user", "call", "result"]
    assert events[1].command == "git commit --no-verify" and events[2].error
    assert sorted(cls for cls, _, _ in sf.detect(events)) == ["environment", "no-verify"]


def test_read_new_returns_only_complete_new_lines_numbered_on(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text('{"a": 1}\n{"b": 2}\n{"c": ', encoding="utf-8")
    chunk = st.read_new(path, 0, 0)
    assert [n for n, _ in chunk.rows] == [1, 2] and chunk.line == 2
    assert st.read_new(path, chunk.offset, chunk.line).rows == (), "the torn line waits"
    with path.open("a", encoding="utf-8") as handle:
        handle.write('3}\n{"d": 4}\n')
    later = st.read_new(path, chunk.offset, chunk.line)
    assert [(n, row) for n, row in later.rows] == [(3, {"c": 3}), (4, {"d": 4})]
    path.write_text('{"z": 0}\n', encoding="utf-8")
    assert st.read_new(path, later.offset, later.line).rows == ((1, {"z": 0}),), (
        "rewritten: from the top"
    )


# --- the harvest --------------------------------------------------------------------------


def test_the_harvest_files_each_friction_once_and_moves_the_cursor(tmp_path):
    workspace = tmp_path / "ws"
    session = transcript(
        tmp_path / "projects" / "ws-devkit" / "s.jsonl",
        [user("go"), call("sleep 300", "1")],
        str(workspace / "devkit"),
    )
    elsewhere = transcript(
        tmp_path / "projects" / "other" / "s.jsonl",
        [user("go"), call("sleep 300", "1")],
        str(tmp_path / "other"),
    )
    cursor = tmp_path / "cursor.json"
    [found] = sf.harvest(workspace, cursor, NOW, [session, elsewhere])
    assert (found.kind, found.project, found.event) == ("poll", "devkit", "session-friction")
    assert found.evidence == f"{session}#L2" and found.command == "sleep 300"
    assert sf.harvest(workspace, cursor, NOW, [session, elsewhere]) == [], "read once"
    with session.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({**call("git push --no-verify", "2"), "cwd": str(workspace / "devkit")})
            + "\n"
        )
    [more] = sf.harvest(workspace, cursor, NOW, [session])
    assert more.kind == "no-verify" and more.evidence.endswith("#L3")


def test_a_transcript_first_seen_old_is_not_read_back(tmp_path):
    """Adopting the harvest must not file a month of history in one pass."""
    old = transcript(
        tmp_path / "p" / "s.jsonl", [user("go"), call("sleep 300", "1")], str(tmp_path)
    )
    stale = (NOW - sf.LOOKBACK - _dt.timedelta(days=1)).timestamp()
    import os

    os.utime(old, (stale, stale))
    assert sf.harvest(tmp_path, tmp_path / "c.json", NOW, [old]) == []
    with old.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({**call("sleep 300", "2"), "cwd": str(tmp_path)}) + "\n")
    assert [f.kind for f in sf.harvest(tmp_path, tmp_path / "c.json", NOW, [old])] == ["poll"]


def test_a_codex_session_keeps_its_cwd_across_reads(tmp_path):
    """Codex names its working directory once, in the first row a later read skips."""
    path = tmp_path / "sessions" / "rollout-1.jsonl"
    path.parent.mkdir(parents=True)
    meta = {"type": "session_meta", "payload": {"cwd": str(tmp_path / "devkit")}}
    path.write_text(json.dumps(meta) + "\n", encoding="utf-8")
    cursor = tmp_path / "c.json"
    assert sf.harvest(tmp_path, cursor, NOW, [path]) == []
    poll = {
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "call_id": "c",
            "arguments": json.dumps({"command": "sleep 90"}),
        },
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(poll) + "\n")
    [found] = sf.harvest(tmp_path, cursor, NOW, [path])
    assert (found.kind, found.agent) == ("poll", "codex")


def test_full_suite_reads_arguments_not_substrings():
    assert (
        sf.full_suite("")
        and sf.full_suite(" -q -n auto -p no:randomly")
        and sf.full_suite(" tests")
    )
    assert not sf.full_suite(" tests/test_x.py") and not sf.full_suite(" -k=slow")
    assert not sf.full_suite(" tests\\test_x.py::t")


def test_a_shell_variable_argument_is_not_the_full_suite():
    """`pytest -q -p no:cacheprovider $p`, with `$p` one test file, was filed as a
    full-suite run (ledger `72f8865b`). A variable names something the detector cannot
    see, so it reads as narrowed rather than as naming nothing."""
    for rest in (" -q -p no:cacheprovider $p", ' "${files[@]}"', " %TARGET%", " $env:T"):
        assert not sf.full_suite(rest), rest
    assert sf.full_suite(" -q -p no:cacheprovider")


def test_session_findings_are_nothing_outside_the_workspace(tmp_path):
    chunk = st.Chunk(((1, call("sleep 99", "1")),), 0, 1)
    assert sf.session_findings(tmp_path / "s.jsonl", chunk, "", tmp_path) == []
    assert sf.session_findings(tmp_path / "s.jsonl", chunk, "D:/elsewhere", tmp_path) == []
    [found] = sf.session_findings(tmp_path / "s.jsonl", chunk, str(tmp_path / "carameli"), tmp_path)
    assert (found.project, found.agent) == ("carameli", "claude")


def test_the_reader_finds_both_stores_and_tells_them_apart(tmp_path):
    claude = tmp_path / "claude" / "slug" / "a.jsonl"
    codex = tmp_path / "codex" / "2026" / "09" / "rollout-x.jsonl"
    stray = tmp_path / "codex" / "notes.jsonl"
    for path in (claude, codex, stray):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    assert st.transcripts(tmp_path / "claude", tmp_path / "codex") == [claude, codex]
    assert st.is_codex(codex) and not st.is_codex(claude)
    assert st.transcripts(tmp_path / "none", tmp_path / "none") == []


def test_events_reads_each_format_with_its_own_reader(tmp_path):
    claude_rows = ((1, user("hi")),)
    codex_rows = ((1, {"type": "event_msg", "payload": {"type": "user_message", "message": "hi"}}),)
    assert st.events(tmp_path / "a.jsonl", claude_rows) == [st.Event("user", 1, "hi")]
    assert st.events(tmp_path / "rollout-a.jsonl", codex_rows) == [st.Event("user", 1, "hi")]


# --- what the first supervised run's audit found the detectors missing -------------------


def _tool(name: str, call_id: str, **input_) -> dict:
    block = {"type": "tool_use", "id": call_id, "name": name, "input": input_}
    return {"type": "assistant", "message": {"content": [block]}}


def test_a_dispatched_session_asking_a_question_is_friction_and_an_interactive_one_is_not():
    dispatched = user("PR #4 is stuck. ... the fix pass commits, pushes, opens or updates the PR")
    ask = _tool("AskUserQuestion", "1", questions=[])
    assert classes([dispatched, ask]) == ["asked-user"]
    assert classes([user("help me design this"), ask]) == []


def test_the_same_test_run_three_times_with_no_edit_between_is_a_rerun():
    run = "python -m pytest tests/test_x.py tests/test_y.py -q"
    rows = [call(f"{run} | tail -{n}", str(n)) for n in (3, 5, 9)]
    assert classes(rows) == ["rerun-unchanged"]
    edited = [call(run, "1"), call(run, "2"), _tool("Edit", "e", file_path="a.py"), call(run, "3")]
    assert classes(edited) == [], "red, fix, green is the work"
    assert classes([call("git status", str(n)) for n in range(4)]) == [], "reading is not a rerun"


def test_a_patch_script_failing_its_own_assert_is_friction():
    out = 'Traceback (most recent call last):\n  File "<stdin>", line 96, in <module>\nAssertionError: t1'
    assert classes([call("python - <<'EOF'", "1"), result(out, "1")]) == ["patch-failed"]


def test_a_mangled_revision_path_is_an_environment_problem():
    out = "fatal: ambiguous argument 'origin\\master;.devkit.toml': unknown revision"
    assert classes([call("git show origin/master:.devkit.toml", "1"), result(out, "1")]) == [
        "environment"
    ]


def test_the_push_gate_and_the_vendored_suite_are_full_suites():
    assert classes([call(".venv/Scripts/python.exe scripts/precommit/run_push_gate.py", "1")]) == [
        "full-suite"
    ]
    assert classes([call("python -m pytest scripts/hooks/tests -q", "1")]) == ["full-suite"]
    assert classes([call("python -m pytest scripts/hooks/tests/test_ship.py", "1")]) == []


def test_render_is_a_transcript_as_lines_an_audit_can_read(tmp_path):
    rows = [
        user("fix the gate"),
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Reading the log."}]},
        },
        _tool("Bash", "1", command="python -m pytest tests/test_x.py"),
        result("E   assert 1 == 2", "1"),
    ]
    path = transcript(tmp_path / "s.jsonl", rows, str(tmp_path))
    lines = st.render(path).splitlines()
    assert lines == [
        "L1 USER: fix the gate",
        "L2 SAY: Reading the log.",
        "L3 CALL Bash: python -m pytest tests/test_x.py",
        "L4 ERROR: E assert 1 == 2",
    ]
    long = transcript(tmp_path / "l.jsonl", [user("x" * 5000)], str(tmp_path))
    assert st.render(long).rstrip().endswith("...[+2000]")
