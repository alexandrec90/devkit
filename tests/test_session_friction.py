"""`scripts/session_friction.py` and `session_transcripts.py`: the turns the harness wasted.

Every transcript here is written under `tmp_path` in the shape each CLI writes, and each
detector is tested for what it must file and -- as much -- for what it must not: the
first uncalibrated harvest filed four false findings for every real one.
"""

from __future__ import annotations

import datetime as _dt
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

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


def test_claude_codes_isolation_guard_is_not_filed_on_devkits_ledger():
    """ea2e8832: engineering.md says devkit cannot change what that guard accepts, so a
    group for it could only ever be retired with the same note, and then refiled."""
    for refusal in (
        "This session is isolated in the worktree C:\\ws\\x\\.claude\\worktrees\\a, but...",
        "The command is too complex to verify that it stays inside the worktree",
    ):
        assert classes([call("git -C .. status", "1"), result(refusal, "1")]) == []


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


def test_claude_codes_sleep_guard_refusal_is_neither_a_block_nor_a_poll():
    """9854b541 and 363d8be7: one refused `sleep 60; tail` filed twice, once for the
    command and once for its refusal. Claude Code's own guard refused it and named the
    right wait in the same message, so it waited for nothing and devkit could change
    neither side -- a group only ever retired with that note, like the isolation guard."""
    command = "sleep 60; tail -5 /c/Users/a/.claude/jobs/9631/tmp/gate2.out"
    refusal = (
        "<tool_use_error>Blocked: sleep 60 followed by: tail -5 /c/x/gate2.out. To wait "
        "for a condition, use Monitor with an until-loop</tool_use_error>"
    )
    assert classes([call(command, "1"), result(refusal, "1")]) == []
    assert classes([call(command, "1")]) == ["poll"], "a poll that ran still waited"
    # A refusal retracts only its own call: a poll that ran first is still filed.
    ran_then_refused = [call(command, "1"), result("", "1", error=False)]
    assert classes([*ran_then_refused, call(command, "2"), result(refusal, "2")]) == ["poll"]
    # The wait that refusal prescribes is not a poll either; the same loop in Bash is.
    loop = "until [ -s gate2.out ]; do sleep 5; done"
    assert classes([_tool("Monitor", "1", command=loop)]) == []
    assert classes([call(loop, "1")]) == ["poll"]
    # Any other refusal is still a blocked call.
    other = "<tool_use_error>Blocked: rm -rf / is not allowed</tool_use_error>"
    assert classes([call("rm -rf /", "1"), result(other, "1")]) == ["blocked-call"]


def test_no_verify_in_a_scratch_repository_is_a_fixture_not_a_bypass():
    """7ce3ea59: a release repro committed its fixture with `--no-verify` in a clone
    under the job's `tmp/`. Nothing there is gated, so nothing was skipped."""
    scratch = (
        "cd /c/Users/a/.claude/jobs/963193fe/tmp/relrepro && git -c user.name=t "
        '-c user.email=t@t commit -qam "Release v0.11.31" --no-verify && python -c x'
    )
    assert classes([call(scratch, "1")]) == []
    assert classes([call('cd "$TMP/x" && git commit --no-verify -m x', "1")]) == []
    assert classes([call("git -C /tmp/tmpab12cd commit --no-verify -m x", "1")]) == []
    assert sf.scratch_only("cd C:\\Users\\a\\AppData\\Local\\Temp\\r && git commit --no-verify")
    assert not sf.scratch_only("cd /tmp/x && git status"), "no --no-verify, nothing to excuse"
    # A tree of the session's own is still a bypass, whatever its path reads like.
    for command in (
        "git commit --no-verify -m x",
        "cd scripts && git commit --no-verify -m x",
        "cd templates && git commit --no-verify -m x",
        "cd /tmp/x && git status; cd /ws/devkit && git push --no-verify",
    ):
        assert classes([call(command, "1")]) == ["no-verify"], command


def test_the_prescribed_wait_and_a_sleep_in_source_text_are_not_polls():
    """`gh pr checks --watch` is the one wait the engineering rule prescribes, and a
    test written through a heredoc naming `sleep 99` is text, not a command -- the
    first live harvest filed both."""
    assert classes([call("gh pr checks 5 --watch --fail-fast", "1")]) == []
    heredoc = 'cat >> t.py <<\'EOF\'\n    chunk = call("sleep 99", "1")\nEOF'
    assert "poll" not in classes([call(heredoc, "1")])
    # 44a5ff19: one settle so the checks exist before that same blocking wait is a single
    # call, not a loop -- `--watch` right after a push reports no checks and returns.
    settle = "sleep 20; gh pr checks 411 --watch --fail-fast > logs/c.out 2>&1; tail -8 logs/c.out"
    assert classes([call(settle, "1")]) == []
    assert classes([call("sleep 120 && gh pr checks 5", "1")]) == ["poll"], "no --watch"


def test_a_complaint_is_filed_whole_with_the_tree_it_was_said_in(tmp_path):
    """The detail is a 90-character snippet, so a sweep parsed a 5,672-line transcript to
    read one complaint -- and then 5 calls to learn the session it was said to had its fix
    open already. The whole message, and where it was said, ride in `command`."""
    ask = (
        "a" * 200 + " it shouldn't just do ad hoc fixes, it should prevent it from happening again"
    )
    path = tmp_path / "t.jsonl"
    rows = [user("start the work"), say("done"), user(ask)]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    chunk = sf.st.read_new(path, 0, 0)
    tree = tmp_path / "devkit" / ".claude" / "worktrees" / "twilight"
    [found] = sf.session_findings(path, chunk, str(tree), tmp_path)
    assert found.kind == "user-frustration" and len(found.detail) <= sf.SNIPPET
    assert found.command.startswith(f"said in {tree}: ") and "happening again" in found.command


def test_reading_the_harness_own_logs_back_is_not_the_friction_they_quote():
    """A sweep read `logs/harness-triage.log`, which quotes "session is isolated in the
    worktree", in a call that failed for another reason -- and the detector filed a fresh
    isolation-guard group from the quote, which the next sweep spent 3 calls disproving."""
    for command in (
        "ls logs/ logs/gate/; cat logs/harness-triage.log",
        "grep -n isolated ../../logs/harness-events-desktop.log",
        "python -c \"import json; [print(l) for l in open('x.jsonl')]\"",
        "cat logs/friction.filed.md",
    ):
        rows = [call(command, "1"), result("session is isolated in the worktree ...", "1")]
        assert classes(rows) == [], command


def test_an_environment_failure_is_seen_through_a_pipe_that_hid_its_exit_code():
    """`python -m pytest ... | tail -3` exits 0 whatever pytest did, so "No module named
    pytest" went unfiled -- twice in one sweep. A test run or a git call is read for an
    environment failure whether or not the call failed; anything else is not."""
    run = call("python -m pytest tests/test_x.py -q | tail -3", "1")
    assert classes([run, result("No module named pytest", "1", error=False)]) == ["environment"]
    git = call("git show origin\\master;x", "2")
    said = "fatal: ambiguous argument 'origin\\master;x': unknown revision"
    assert classes([git, result(said, "2", error=False)]) == ["environment"]
    grep = call("grep -rn 'No module named' scripts", "3")
    assert classes([grep, result("x.py:1: No module named", "3", error=False)]) == []


def test_an_error_quoted_inside_a_string_on_its_line_is_not_friction():
    """The first supervised rehearsal would have filed seven groups from text that quoted
    an error: a `git diff` of the rule's prose, pytest echoing an assertion's operands,
    and the detector's own regex source. Each match sat after an opening quote on its
    line, or in an assertion's report; a real error's line is neither."""
    quoted = (
        (
            "git diff origin/main...HEAD -- x.md",
            "+`python -m pytest` there fails with `No module named pytest`. The",
        ),
        (
            "python -m pytest tests/x.py -q",
            "E       assert \"ambiguous argument 'origin\\\\main;.devkit.toml'\" in '---'",
        ),
        (
            "python -m pytest tests/x.py -q",
            "E   AssertionError: backslashes: [\"ambiguous argument 'origin\\\\main;x'\"]",
        ),
        (
            "git diff scripts/session_friction.py",
            '66:            r"No module named|ModuleNotFoundError|"',
        ),
        # The supervisor's own red run, its assertion echoing the test's expected text.
        (
            "python -m pytest tests/x.py -q",
            "E           AssertionError: 'pytest' is not recognized as an internal or external",
        ),
    )
    for command, line in quoted:
        assert classes([call(command, "1"), result(line, "1")]) == [], line
    real = (
        ("python -m pytest tests/x.py", "C:\\py\\python.exe: No module named pytest"),
        (
            "python -m pytest tests/x.py",
            "ImportError: Error importing plugin \"randomly\": No module named 'randomly'",
        ),
        ("pytest tests/x.py", "'pytest' is not recognized as an internal or external command,"),
        ("git show origin/main:x", "fatal: ambiguous argument 'origin\\main;x': unknown revision"),
    )
    for command, line in real:
        assert classes([call(command, "1"), result(line, "1")]) == ["environment"], line
    # A quoted mention earlier in the output does not hide the real error after it.
    both = "x.md:3: prints \"No module named x\"\nModuleNotFoundError: No module named 'y'"
    events = [
        e
        for n, r in enumerate([call("pytest tests/x.py", "1"), result(both, "1")], 1)
        for e in st.claude_events(r, n)
    ]
    [(_, what, _)] = sf.detect(events)
    assert what == "ModuleNotFoundError: No module named 'y'"


def test_the_rule_names_the_spelling_that_avoids_the_error_the_detector_files():
    """A sweep documented the Git Bash `rev:path` rewrite only in the evidence file, and
    the next session paid for it again. The rule every session reads names the spelling,
    and the error it quotes is the one the detector files."""
    rule = (Path(__file__).resolve().parents[1] / ".claude" / "rules" / "engineering.md").read_text(
        encoding="utf-8"
    )
    assert "origin/main:./.devkit.toml" in rule and "`ambiguous argument`" in rule
    # Described there, not quoted: instruction files carry no backslashed paths.
    assert sf._result_class("ambiguous argument 'origin\\main;.devkit.toml'")[0] == "environment"


def test_a_file_written_through_a_shell_heredoc_is_friction():
    """Claude Code's Bash tool collapses backslashes in a heredoc, so a file written or
    patched through one comes out mangled. Three sessions lost turns to it on one day,
    each retired by pointing at the rule that says to use Write/Edit -- and it recurred,
    because only the sessions that noticed reported it. Every write that can be damaged
    is filed now, whether anyone noticed or not."""
    for command in (
        "cat > tests/test_x.py <<'EOF'\nassert '\\\\b'\nEOF",
        'cat >> t.py <<"EOF"\nx = "a\\\\b"\nEOF',
        "tee scripts/a.py <<EOF\nprint('\\\\n')\nEOF",
        "python - <<'EOF'\nfrom pathlib import Path\np = Path('a.py')\n"
        "p.write_text(p.read_text().replace('\\\\d', 'b'))\nEOF",
        "python3 - <<'PY'\nopen('x.txt', 'w').write('y\\\\\\n')\nPY",
        "cat > x.py <<'EOF'\n'\\\\s'",  # unterminated: the body runs to the end
    ):
        assert classes([call(command, "1")]) == ["heredoc-write"], command
    for harmless in (
        "python - <<'EOF'\nimport json; print(json.load(open('a.json')))\nEOF",
        "git commit -F - <<'EOF'\nsubject\\n\nEOF",
        "grep -c '<<' scripts/x.py",
    ):
        assert classes([call(harmless, "1")]) == [], harmless


def test_a_heredoc_with_no_backslash_in_its_body_is_not_friction():
    """b935e421 recurred on a `cat >>` whose body had no backslash, so nothing could be
    collapsed and the file was verified intact: a sweep could only retire it as "no
    defect", and the next such write reopened it. A backslash after the terminator -- a
    Windows path in the command that follows -- is not in the body."""
    b935e421 = (
        "cat >> scripts/hooks/tests/test_report_harness_defect.py <<'EOF'\n\n\n"
        "class TestAbsoluteEvidence:\n"
        "    def test_relative_path_joins_the_base(self, tmp_path):\n"
        '        assert report.absolute_evidence("a/b.txt", tmp_path) == '
        'str(tmp_path / "a" / "b.txt")\n'
        "EOF\ntail -25 scripts/hooks/tests/test_report_harness_defect.py; "
        ".venv\\Scripts\\python.exe -m pytest -q scripts/hooks/tests/test_report_harness_defect.py"
    )
    assert classes([call(b935e421, "1")]) == []
    assert not sf.damageable_heredoc(b935e421)
    assert sf.damageable_heredoc("cat <<-EOF > a.py\n\tx = '\\\\t'\n\tEOF")
    assert not sf.damageable_heredoc("echo 'a\\\\b' > x.txt")  # no heredoc at all


def test_a_heredoc_dumped_back_byte_for_byte_is_a_probe_not_a_write():
    """ccde706b: the session that established the doubled-backslash rule wrote both
    spellings to a scratch file and read them back with `od -c`, in one call. That is a
    measurement of the tool, and filing it reopened the group its own finding retired."""
    ccde706b = (
        "cat > \"C:/Users/alexa/.claude/jobs/c9e3f0fa/tmp/bs.txt\" <<'EOF'\n"
        "one:\\n\ntwo:\\\\n\nEOF\n"
        'od -c "C:/Users/alexa/.claude/jobs/c9e3f0fa/tmp/bs.txt"'
    )
    assert classes([call(ccde706b, "1")]) == []
    for dump in ("xxd x.txt", "hexdump -C x.txt", "Format-Hex x.txt"):
        assert not sf.damageable_heredoc(ccde706b.rsplit("\n", 1)[0] + "\n" + dump), dump
    # The same write with nothing reading it back is still the damage it always was,
    # and a word that only starts like a dump is not one.
    assert classes([call(ccde706b.rsplit("\n", 1)[0], "1")]) == ["heredoc-write"]
    assert sf.damageable_heredoc(ccde706b.rsplit("\n", 1)[0] + "\nodd -c x")


def test_a_heredoc_with_only_single_backslashes_is_not_friction():
    """46a1578d recurred on a `cat >` whose body's only backslashes were the `\\n` in
    f-strings. The Bash tool collapses a doubled backslash and nothing else: written
    through it, `a\\nb`, `r'\\s'`, `\\'`, `\\$` and a trailing `\\` all came out byte for
    byte, while `a\\\\b` came out `a\\b` and three in a row came out two."""
    a46a1578d = (
        "cd \"C:/Users/alexa/scratchpad\" && cat > dump.py <<'EOF'\n"
        "import json,sys\n"
        "for i,l in enumerate(lines,1):\n"
        '    if isinstance(c,str): print(f"=== L{i} STR\\n{c[:6000]}"); continue\n'
        "EOF\n"
        'python dump.py "C:/x.jsonl" 6,30 > b.txt; wc -c b.txt'
    )
    assert classes([call(a46a1578d, "1")]) == []
    assert not sf.damageable_heredoc(a46a1578d)
    for single in ("r'\\s'", "'\\t'", "\\'x\\'", "\\$HOME", "end\\"):
        assert not sf.damageable_heredoc(f"cat > a.py <<'EOF'\n{single}\nEOF"), single
    for doubled in ("a\\\\b", "a\\\\\\b", "'\\\\\\\\'"):
        assert sf.damageable_heredoc(f"cat > a.py <<'EOF'\n{doubled}\nEOF"), doubled


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


def _rows(findings) -> list:
    """Findings as the ledger rows `record_all` would write, parsed back."""
    events = sf.fix_findings.harness_events
    lines = [events.event_line(NOW.isoformat(), f.event, f.fields()) for f in findings]
    return sf.fix_findings.triage.read_items("\n".join(lines))


def test_a_row_todays_detectors_no_longer_file_is_outdated(tmp_path, monkeypatch):
    """d677ea57: #412 fixed detectors whose rows stayed open, and a sweep spent ~13 calls
    re-proving them. A per-event row is re-judged against its own transcript line with
    the detectors as they are now; one they no longer file is outdated."""
    missing = "ModuleNotFoundError: No module named 'yaml'"
    session = transcript(
        tmp_path / "s.jsonl",
        [user("go"), call("sleep 300", "1"), call("python -m x", "2"), result(missing, "2")],
        str(tmp_path / "ws" / "devkit"),
    )
    rows = _rows(sf.session_findings(session, st.read_new(session, 0, 0), str(tmp_path), tmp_path))
    assert sorted(r.detail.split(":")[0] for r in rows) == ["environment", "poll"]
    assert sf.outdated(rows) == [], "both still fire"
    # Standing in for a detector a fix removed, as #410 removed `isolation-guard`.
    kept = tuple(p for p in sf.RESULT_PATTERNS if p[0] != "environment")
    monkeypatch.setattr(sf, "RESULT_PATTERNS", kept)
    [(ref, why)] = sf.outdated(rows)
    env = next(r for r in rows if r.detail.startswith("environment"))
    assert ref == env.id and "no longer" in why and f"{session}#L4" in why


def test_a_row_that_cannot_be_rejudged_is_never_called_outdated(tmp_path, monkeypatch):
    """Retiring is the one direction that must never guess: a transcript gone, a line
    that is no longer the event the row names (a transcript first read from its end
    numbers from there), and a class the whole session decides all stay open."""
    session = transcript(tmp_path / "s.jsonl", [user("go"), call("sleep 300", "1")], "c")
    [row] = _rows(sf.session_findings(session, st.read_new(session, 0, 0), str(tmp_path), tmp_path))
    monkeypatch.setattr(sf, "COMMAND_PATTERNS", ())
    assert [ref for ref, _ in sf.outdated([row])] == [row.id], "the premise: it would retire"
    moved = replace(row, fields={**row.fields, "command": "something else"})
    gone = replace(row, fields={**row.fields, "evidence": f"{tmp_path / 'gone.jsonl'}#L2"})
    whole = replace(row, fields={**row.fields, "detail": "repeat-failure: x3 gh"})
    assert sf.outdated([moved, gone, whole]) == []


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


def test_a_suite_root_named_beside_a_file_is_still_the_whole_suite():
    """pytest runs the union of its arguments: 0926-18 ran `pytest scripts/hooks/tests
    tests/test_temproot_wiring.py` twice, 1,388 tests and two minutes each, and the file
    named beside the root read as a targeted run."""
    ran = " scripts/hooks/tests tests/test_temproot_wiring.py -q -p no:cacheprovider 2"
    assert sf.full_suite(ran)
    assert sf.full_suite(" scripts\\hooks\\tests\\ tests/test_x.py"), "a backslash spelling"
    assert not sf.full_suite(" scripts/hooks/tests -k temproot"), "a selector still narrows"
    command = f".venv\\Scripts\\python -m pytest{ran}>&1 | Select-Object -Last 3"
    assert classes([call(command, "1")]) == ["full-suite"]


def test_a_shell_variable_argument_is_not_the_full_suite():
    """`pytest -q -p no:cacheprovider $p`, with `$p` one test file, was filed as a
    full-suite run (ledger `72f8865b`). A variable names something the detector cannot
    see, so it reads as narrowed rather than as naming nothing."""
    for rest in (" -q -p no:cacheprovider $p", ' "${files[@]}"', " %TARGET%", " $env:T"):
        assert not sf.full_suite(rest), rest
    assert sf.full_suite(" -q -p no:cacheprovider")
    # ed03763b: PowerShell's splat, `pytest -q @t` with `$t` an array of seven files.
    assert not sf.full_suite(" -q -p no:cacheprovider @t 2")
    assert sf.full_suite(" -q -p no:cacheprovider @"), "a bare @ names nothing"


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


def say(text: str) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def test_a_dispatched_session_ending_on_a_decision_for_someone_else_is_friction():
    """The ledger sweep's last words were "The last group needs your decision." -- nobody
    was there, and it had already marked the option it would pick as recommended. Only
    the final message counts: mid-session, "should I check X? yes" is thinking aloud."""
    opening = user("Sweep the ledger. ... the fix pass commits, pushes, opens or updates the PR")
    for ending in (
        "Four groups are retired. The last group needs your decision.",
        "Both work. Do you want me to pin it by rev?",
        "Let me know which option you prefer.",
    ):
        assert classes([opening, say(ending)]) == ["handed-back"], ending
    decided = say("I pinned data-lake by rev: it keeps worktrees independent.")
    assert classes([opening, say("Should I read the tests first? Yes."), decided]) == []
    assert classes([user("help me choose"), say("Do you want option 1?")]) == []
    working = [opening, say("Should I pin it? I will."), call("uv lock", "1")]
    assert classes(working) == [], "a harvest mid-session: it went on working"
    asked = [opening, say("This needs your decision."), _tool("AskUserQuestion", "2")]
    assert classes(asked) == ["asked-user", "handed-back"]


def test_the_same_test_run_three_times_with_no_edit_between_is_a_rerun():
    run = "python -m pytest tests/test_x.py tests/test_y.py -q"
    rows = [call(f"{run} | tail -{n}", str(n)) for n in (3, 5, 9)]
    assert classes(rows) == ["rerun-unchanged"]
    edited = [call(run, "1"), call(run, "2"), _tool("Edit", "e", file_path="a.py"), call(run, "3")]
    assert classes(edited) == [], "red, fix, green is the work"
    assert classes([call("git status", str(n)) for n in range(4)]) == [], "reading is not a rerun"


def test_a_rerun_after_changing_the_environment_is_not_a_rerun():
    """eda7aed7: the carameli session started its db between the first and second run and
    brought compose up before the third. Each run read something new; the friction was the
    missing database, which the session reported itself."""
    run = ".venv/Scripts/python scripts/run-tests.py tests/unit/test_pointers.py"
    rows = [
        call(f"{run} 2>&1 | tail -25", "1"),
        call("docker start carameli-db-1 carameli-redis-1 2>&1", "2"),
        call(f"{run} 2>&1 | tail -12; head -c 2000 logs/test-failures.log", "3"),
        call("docker stop carameli-db-1; docker compose up -d db redis 2>&1 | tail -8", "4"),
        call(f"{run} 2>&1 | tail -8; head -c 3000 logs/test-failures.log", "5"),
    ]
    assert classes(rows) == []
    for change in ("npm ci --prefix frontend", "uv sync", 'psql -c "CREATE DATABASE x"'):
        rows = [call(run, "1"), call(change, "2"), call(run, "3"), call(run, "4")]
        assert classes(rows) == [], change
    rows = [call(run, "1"), call("docker ps -a", "2"), call(run, "3"), call(run, "4")]
    assert classes(rows) == ["rerun-unchanged"], "looking at docker changes nothing"


def test_a_patch_script_failing_its_own_assert_is_friction():
    out = 'Traceback (most recent call last):\n  File "<stdin>", line 96, in <module>\nAssertionError: t1'
    assert classes([call("python - <<'EOF'", "1"), result(out, "1")]) == ["patch-failed"]


def test_a_mangled_revision_path_is_an_environment_problem():
    out = "fatal: ambiguous argument 'origin\\master;.devkit.toml': unknown revision"
    assert classes([call("git show origin/master:.devkit.toml", "1"), result(out, "1")]) == [
        "environment"
    ]


def test_a_commit_message_that_quotes_errors_is_not_an_environment_failure():
    """fc786188: a fixer ran `git show --stat` on the commit that taught this detector
    about quoted errors, and the harvest -- still on the old detector -- filed the
    message's own examples as an environment group. `git` output is read even on exit 0,
    so every quoted example in a message has to be seen as quoted."""
    command = (
        "git branch -a --contains 21631ef | head; git show --stat 21631ef | head -30; "
        "gh pr list --state all --head agent/supervise-fix-pass-0926 --json number,state,title"
    )
    out = (
        "commit 21631efe96a4e91818873bf2ac2df1665156f429\n"
        "Author: t <t@example.invalid>\n\n"
        "    ## Friction detector: an error quoted on its line is not one\n"
        "    \n"
        "    - a `git diff` of the engineering rule's prose (`` `No module named pytest` ``);\n"
        "    - pytest echoing an assertion's operands (`E  assert \"ambiguous argument ...\" in '...'`);\n"
        "    - `python.exe: No module named pytest`\n"
        "    - `fatal: ambiguous argument 'origin\\main;...'`\n"
        "    - `'pytest' is not recognized ...`\n"
    )
    assert classes([call(command, "1"), result(out, "1", error=False)]) == []


def test_an_escaped_quote_does_not_close_the_string_an_error_is_quoted_in():
    """83496ae4: a `git diff` of this file's own tests, where the error sits after a `\\"`
    inside a string literal -- counted as a closing quote, it read as unquoted."""
    line = (
        '+    both = "x.md:3: prints \\"No module named x\\"\\n'
        "ModuleNotFoundError: No module named 'y'\""
    )
    command = "git diff origin/main...origin/worktree-hazy-wibbling-pearl -- tests/x.py"
    assert classes([call(command, "1"), result(line, "1", error=False)]) == []
    # A real error on a line that merely has an escape before it still files.
    real = "C:\\py\\python.exe: No module named pytest"
    assert classes([call("python -m pytest tests/x.py", "1"), result(real, "1")]) == ["environment"]


def test_the_push_gate_and_the_vendored_suite_are_full_suites():
    assert classes([call(".venv/Scripts/python.exe scripts/precommit/run_push_gate.py", "1")]) == [
        "full-suite"
    ]
    assert classes([call("python -m pytest scripts/hooks/tests -q", "1")]) == ["full-suite"]
    assert classes([call("python -m pytest scripts/hooks/tests/test_ship.py", "1")]) == []
    assert classes([call("pre-commit run devkit-push-gate --hook-stage pre-push", "1")]) == [
        "full-suite"
    ]


def test_a_command_that_only_names_the_push_gate_is_not_a_full_suite():
    """Ledger 30d0035d: a grep whose file list named `run_push_gate.py` was filed as a
    session running the whole gate."""
    grep = (
        'grep -n "instruction-budget" scripts/devkit_manifest.py '
        "scripts/precommit/run_push_gate.py tests/test_gate_parity.py | head"
    )
    assert classes([call(grep, "1")]) == []
    assert classes([call("sed -n 1,40p scripts/precommit/run_push_gate.py", "1")]) == []


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


def test_a_complaint_carries_the_task_branch_its_session_shipped_on(tmp_path):
    """4806bd8d: seven of eight complaint rows were fixed by the corrected session
    itself; the branch lets the ledger settle them when it merges."""
    rows = (
        (1, user("fix the flaky test")),
        (2, user("why did you run the whole suite again?")),
        (3, call("sleep 99", "1")),
    )
    chunk = st.Chunk(rows, 0, 3)
    asked: list[str] = []

    def branch_of(cwd):
        asked.append(cwd)
        return "agent/flaky-0926"

    cwd = str(tmp_path / "carameli")
    found = sf.session_findings(tmp_path / "s.jsonl", chunk, cwd, tmp_path, branch_of)
    settles = {f.kind: f.settles_with for f in found}
    assert settles == {"user-frustration": "agent/flaky-0926", "poll": ""}
    assert asked == [cwd]
    quiet = st.Chunk(((1, call("sleep 99", "1")),), 0, 1)
    sf.session_findings(tmp_path / "s.jsonl", quiet, cwd, tmp_path, branch_of)
    assert asked == [cwd], "no complaint, no git call"


@pytest.mark.parametrize(
    "code, out, branch",
    [(0, "agent/x-0926\n", "agent/x-0926"), (0, "main\n", ""), (0, "\n", ""), (128, "", "")],
    ids=["task", "default", "detached", "gone"],
)
def test_only_a_task_branch_can_settle_a_complaint(code, out, branch):
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, code, out, "")

    assert sf.task_branch("C:/t", runner=run) == branch

    def missing(argv, **kwargs):
        raise FileNotFoundError("git")

    assert sf.task_branch("C:/t", runner=missing) == ""


def test_git_printing_a_file_that_quotes_an_error_is_not_friction():
    """83496ae4 was a `git diff` of `tests/test_session_friction.py`, whose fixtures spell
    "No module named" inside escaped quotes; a quote count can be taught escapes, but not
    prose that names an error with no quotes at all. Git's output is the repository's text
    plus git's own `fatal:`/`error:` lines, and only those report the environment --
    whether the call exited 0 or not."""
    diff = (
        "git diff origin/main...origin/x -- tests/test_session_friction.py; "
        'git diff origin/main...origin/x -- scripts/session_friction.py > "$T/sf.patch"'
    )
    quoted = (
        "@@ -153,6 +153,58 @@\n"
        '+    both = "x.md:3: prints \\"No module named x\\"\\nModuleNotFoundError: No module '
        "named 'y'\"\n"
        "+A bare run there fails with No module named pytest, so use the venv.\n"
    )
    for error in (False, True):
        assert classes([call(diff, "1"), result(quoted, "1", error=error)]) == [], error
    fatal = quoted + "fatal: ambiguous argument 'origin\\main;x': unknown revision\n"
    assert classes([call(diff, "2"), result(fatal, "2", error=False)]) == ["environment"]
    # A test run in the same call is read whole: its output is not the repository's text.
    both = call(diff + "; python -m pytest tests/test_x.py | tail -3", "3")
    assert classes([both, result("E   ModuleNotFoundError: No module named 'y'", "3")]) == [
        "environment"
    ]


def test_only_git_printing_the_repository_is_read_for_its_diagnostics_alone():
    """A hook's output comes through `git commit`, and its "No module named" is real."""
    assert sf.git_reads_only("git diff a b | head -40 && git log -3\ngit --no-pager show x > f")
    assert not sf.git_reads_only("git diff a; python -m pytest t.py")
    assert not sf.git_reads_only("git commit -m x")
    assert not sf.git_reads_only("")
    said = "+ x = 'No module named y'\nfatal: bad revision 'z'\nwarning: LF will be replaced"
    assert sf.environment_text("git diff", said) == (
        "fatal: bad revision 'z'\nwarning: LF will be replaced"
    )
    assert sf.environment_text("pytest t.py", said) == said


def test_a_cd_before_git_printing_the_repository_prints_nothing_of_its_own():
    """72e04231: `cd <tree> && git diff | head` of carameli's `pytest.ini`, whose comment
    counts "55 ModuleNotFoundError errors", was filed as the environment, because the `cd`
    made the line more than git reading."""
    command = (
        'cd /c/x/roguelike/.claude/worktrees/t && git diff --text | cat -A | grep -v "^ " | '
        "head -40; cd /c/x/carameli/.claude/worktrees/t && git diff | head -30"
    )
    assert sf.git_reads_only(command)
    diff = "+# breaks collection of that suite with 55 ModuleNotFoundError errors.\n"
    assert classes([call(command, "1"), result(diff, "1", error=False)]) == []
    assert not sf.git_reads_only("cd /c/x")
    assert not sf.git_reads_only("cd /c/x && python -m pytest t.py")
