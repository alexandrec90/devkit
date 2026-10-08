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
from support import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "scripts"))
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


def test_the_bash_tools_own_shell_missing_its_coreutils_is_not_filed():
    """8bdaf003, c4642d3f, e35c772a: one social-scraper session's Bash started with no
    PATH -- `ls`, `cat`, `git`, `head`, `wc` all missing, the exact output -- and was filed
    as four environment groups. Git Bash ships those itself; nothing in devkit sets the
    Bash tool's PATH, so no change here could have prevented it."""
    for command, said in (
        (
            "ls; ls logs 2>/dev/null; cat .env.example; git log --oneline | head -30",
            "Exit code 127\n/usr/bin/bash: line 1: ls: command not found\n"
            "/usr/bin/bash: line 1: cat: command not found\n"
            "/usr/bin/bash: line 1: git: command not found\n"
            "/usr/bin/bash: line 1: head: command not found",
        ),
        (
            "docker ps 2>&1 | head -20",
            "Exit code 127\n/usr/bin/bash: line 1: head: command not found",
        ),
        ("wc -l a.py b.md", "/usr/bin/bash: line 1: wc: command not found"),
    ):
        assert classes([call(command, "1"), result(said, "1")]) == [], command


def test_a_missing_tool_is_filed_under_its_own_name():
    """The snippet ran from "command not found" on, so the tool's name -- the one thing a
    sweep needs -- was cut, and whatever followed made each recurrence its own group."""
    said = "/usr/bin/bash: line 1: docker: command not found\n/usr/bin/bash: line 2: ls: x"
    events = [
        e
        for n, row in enumerate([call("docker ps", "1"), result(said, "1")], 1)
        for e in st.claude_events(row, n)
    ]
    assert [(cls, what) for cls, what, _ in sf.detect(events)] == [
        ("environment", "docker: command not found")
    ]
    mixed = "bash: head: command not found\nbash: uv: command not found"
    assert sf._result_class(mixed) == ("environment", "uv: command not found"), "past the shell's"
    assert sf._result_class("zsh: command not found: uv") == (
        "environment",
        "command not found: uv",
    ), "a shell that names it after keeps it in the snippet"


def test_no_settings_devkit_writes_sets_the_shells_path():
    """`SHELL_OWN` is excused because nothing devkit writes reaches the Bash tool's PATH:
    once a settings `env` names it, a shell missing its coreutils is devkit's to fix."""
    for path in (
        REPO_ROOT / ".claude" / "settings.json",
        REPO_ROOT / "templates" / "core" / "dot-claude" / "settings.json.tmpl",
    ):
        text = path.read_text(encoding="utf-8")
        assert '"env"' in text, f"{path} still has the block this reads"
        assert '"PATH"' not in text.upper(), path


def test_a_missing_module_is_friction_only_when_the_call_failed():
    """A file that quotes an error is not one: most of the first harvest's noise."""
    assert classes(
        [call("python -m pytest tests/x.py", "1"), result("No module named pytest", "1")]
    ) == ["environment"]
    quoted = result("docs: prints 'grep: command not found' when absent", "2", error=False)
    assert classes([call("cat notes.md", "2"), quoted]) == []


def test_an_error_serialized_into_a_record_quotes_it():
    """4ae355df: a fixer read a container's health JSON, whose `last_traceback` holds a
    traceback as one escaped string, and PowerShell wrapped it so the string's opening
    quote sat a line above the error. The `\\n` before it is the escape a real traceback
    never prints: the error is a record's text, the very failure the fixer was sent at."""
    command = (
        "docker exec ibkr_trader-app-1 cat /app/logs/scheduler-health.json; "
        "git log --oneline -6 origin/HEAD"
    )
    record = (
        '        "last_traceback": "Traceback (most recent call last):\\n  File '
        '\\"/data-lake/src/data_lake/archive/store.py\\", \r\n'
        "line 102, in from_settings\\n    import boto3\\nModuleNotFoundError: No module named "
        "'boto3'\\n\\nThe above exception was \r\nthe direct cause\n4ffdf9d Merge pull request"
    )
    assert classes([call(command, "1"), result(record, "1", error=False)]) == []
    printed = (
        "  File store.py, line 102\n    import boto3\nModuleNotFoundError: No module named 'boto3'"
    )
    assert classes([call("git status; pytest tests/x.py", "2"), result(printed, "2")]) == [
        "environment"
    ], "the same error printed is still the environment"
    in_a_tree = "C:\\ws\\x\\.claude\\worktrees\\nifty-lark\\.venv\\Scripts\\python.exe: No module named pytest"
    assert classes([call("python -m pytest tests/x.py", "3"), result(in_a_tree, "3")]) == [
        "environment"
    ], "a `\\n` opening a path segment is no escaped newline"


def test_a_module_the_session_then_declares_was_the_projects_missing_dependency():
    """8d969865: a fixer wrote its regression test first and watched it fail on duckdb's
    undeclared `pytz`, then declared it in `pyproject.toml`. The run lacked what the
    project never asked for -- the defect it was fixing -- not a tree left unprovisioned."""
    run = ".venv/Scripts/python.exe -m pytest tests/test_lens.py -q 2>&1 | tail -8"
    failed = result(
        "E   _duckdb.InvalidInputException: Required module 'pytz' failed to import\r\n"
        "E   ModuleNotFoundError: No module named 'pytz'\r\n\r\n"
        "FAILED tests/test_lens.py::test_a_timestamp_column",
        "1",
        error=False,
    )
    ran = [call(run, "1"), failed]
    declare = _tool(
        "Edit",
        "2",
        file_path="C:\\ws\\social-scraper\\pyproject.toml",
        old_string='  "duckdb>=1.1",\n',
        new_string='  "duckdb>=1.1",\n  "pytz>=2024.1",\n',
    )
    assert classes([*ran, declare]) == []
    assert classes([*ran, call("uv add pytz && uv lock", "3")]) == []
    assert classes(ran) == ["environment"], "nothing declared: it stands"
    other = _tool("Edit", "2", file_path="pyproject.toml", old_string="a", new_string='"rich"')
    assert classes([*ran, other]) == ["environment"], "a different package declared"
    source = _tool("Edit", "2", file_path="lens.py", old_string="a", new_string="import pytz")
    assert classes([*ran, source]) == ["environment"], "not a dependency manifest"
    assert classes([declare, *ran]) == ["environment"], "declared before: the tree was not synced"


def test_declared_text_reads_manifests_and_adds_and_declares_matches_whole_names():
    edit = st.Event("call", 1, '"pytz>=2024.1",', tool="Edit", path="a/pyproject.toml")
    assert sf.declared_text(edit) == '"pytz>=2024.1",'
    patch = "*** Begin Patch\n*** Update File: requirements-dev.txt\n+typing_extensions\n"
    assert "typing_extensions" in sf.declared_text(
        st.Event("call", 1, command=patch, tool="apply_patch")
    )
    added = st.Event("call", 1, command="cd x && uv add pyyaml && pytest t.py", tool="Bash")
    assert "uv add pyyaml" in sf.declared_text(added)
    assert "pytest" not in sf.declared_text(added), "only the add"
    assert sf.declared_text(st.Event("call", 1, "import pytz", tool="Edit", path="lens.py")) == ""
    assert sf.declares("typing_extensions.x", "typing-extensions>=4")
    assert sf.declares("pytz", '"PyTZ>=2024.1"'), "case aside"
    assert not sf.declares("tz", '"pytz>=2024.1"'), "a whole name only"
    assert not sf.declares("pytz", '"pytz-deprecation-shim"')


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


def test_the_sleep_guards_powershell_refusal_is_not_a_blocked_call():
    """228353d8: the same guard refusing a PowerShell `Start-Sleep 40; Get-Content` was
    filed as a blocked call, since only the Bash spelling was known as the guard's."""
    command = 'Start-Sleep 40; Get-Content "$env:TEMP\\claude\\x\\scratchpad\\cli.log" -Tail 8'
    refusal = (
        '<tool_use_error>Blocked: Start-Sleep 40 followed by: Get-Content "$env:TEMP\\claude'
        '\\x\\scratchpad\\cli.log" -Tail 8. To wait for a condition, use Monitor with an '
        "until-loop (e.g. `until <check>; do sleep 2; done` — Monitor runs bash).</tool_use_error>"
    )
    assert classes([_tool("PowerShell", "1", command=command), result(refusal, "1")]) == []


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


def test_a_backgrounded_wait_is_one_call_and_its_notice_not_a_poll():
    """The supervisor's `until [ -f done ]; do sleep 60; done` on a detached run, and a
    fixer's one `sleep 25; claude agents` probe, both run in the background: one call and
    a completion notice, the shape the engineering rule prescribes for a long wait."""
    loop = "until [ -f logs/supervise-run.done ]; do sleep 60; done; cat logs/run.out"
    block = {
        "type": "tool_use",
        "id": "1",
        "name": "Bash",
        "input": {"command": loop, "run_in_background": True},
    }
    backgrounded = {"type": "assistant", "message": {"content": [block]}}
    assert classes([backgrounded]) == []
    assert classes([call(loop, "1")]) == ["poll"], "in the foreground it holds the turn"


def test_the_rule_prescribes_every_wait_the_poll_detector_excuses():
    """e1043d81 came back after five resolutions, each excusing one more shape of wait,
    because only a CI gate's wait was written down: a rescue session waited on its own
    probe's file with a foreground `until` loop, the shape Claude Code's sleep guard
    names. The rule has to say what the detector accepts -- backgrounded, or `Monitor`."""
    rule = (REPO_ROOT / ".claude" / "rules" / "engineering.md").read_text(encoding="utf-8")
    section = rule.split("## Waiting", 1)[1].split("\n## ", 1)[0]
    assert "run_in_background" in section and "`until` loop" in section
    assert all(f"`{tool}`" in section for tool in sf.WAIT_TOOLS)


def test_a_module_the_tree_itself_holds_is_a_code_defect_not_the_environment(tmp_path):
    """0929-8 switched a test to a bare `import fix_plan`, which resolves only once another
    module has put `scripts/` on the path; collected first, it failed. `fix_plan` is a
    module in the tree: that is the test's defect, and filing it as a missing package
    sent the devkit session at the machine."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "fix_plan.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    ours = "ModuleNotFoundError: No module named 'fix_plan' ERROR tests/test_run_tests.py"
    assert sf.local_module(ours, str(tmp_path))
    assert sf.local_module("No module named 'pkg.sub'", str(tmp_path))
    assert not sf.local_module("No module named 'yaml'", str(tmp_path))
    assert not sf.local_module("No module named pytest", str(tmp_path)), "unquoted: the tool"
    assert not sf.local_module(ours, ""), "no tree to look in"
    rows = [("environment", ours, None), ("environment", "No module named 'yaml'", None)]
    assert [what for _, what, _ in sf.outside_the_tree(rows, str(tmp_path))] == [
        "No module named 'yaml'"
    ]


def test_a_ledger_evidence_line_is_read_back_as_the_audit_lines_around_it(tmp_path, capsys):
    """0929-6 and 0929-8 each wrote a transcript reader of their own to read a finding's
    `transcript#L<n>`; the module's own `render` had no command line."""
    path = tmp_path / "t.jsonl"
    rows = [call(f"echo {i}", str(i)) for i in range(1, 101)]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    assert st.main([f"{path}#L50", "--around", "2"]) == 0
    out = capsys.readouterr().out
    assert [line.split()[0] for line in out.splitlines()] == ["L48", "L49", "L50", "L51", "L52"]
    assert st.main([str(path)]) == 0
    assert len(capsys.readouterr().out.splitlines()) == 100, "no #L: the whole session"
    assert st.main([str(tmp_path / "gone.jsonl#L3")]) == 2
    assert "no transcript at" in capsys.readouterr().err
    assert st.window("L1 CALL: a\nL9 CALL: b\nnoise\n", 1, 3) == "L1 CALL: a\n"


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
    chunk = sf.st.read_new(path, 0)
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


def test_a_separator_inside_a_quoted_argument_starts_no_test_run():
    """0d422fe7: the `\\|pytest` of a grep's alternation read as pytest in command
    position, and what it grepped -- this file -- quoted "No module named pytest"."""
    command = (
        'grep -n "full-suite\\|pytest\\|run-tests" tests/test_session_friction.py | sed -n 1,80p;'
        ' grep -rln "session_friction" tests/ | head'
    )
    printed = (
        '190:    """`python -m pytest ... | tail -3` exits 0 whatever pytest did, so "No module'
        ' named\n191:    pytest" went unfiled -- twice in one sweep. A test run or a git ca'
    )
    assert classes([call(command, "1"), result(printed, "1", error=False)]) == []
    assert not sf.runs_tests(command)
    assert classes([call('grep -n "x\\|pytest tests" a.py', "2")]) == []
    assert classes([call("grep 'a;pytest' b.py", "3")]) == []
    assert classes([call('pytest "tests/test_x.py" | tail -3', "4")]) == []
    assert classes([call('echo "x"; pytest tests -q', "5")]) == ["full-suite"]


def test_command_position_keeps_the_length_and_the_quoted_text():
    command = "grep -n 'a|b' x.py; git log --grep \"c;d\""
    blanked = sf.command_position(command)
    assert len(blanked) == len(command)
    assert blanked == "grep -n 'a b' x.py; git log --grep \"c d\""


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
    and the error it quotes is the one the detector files. `MSYS2_ARG_CONV_EXCL` now
    exempts `origin/` revs, so the spelling it names is for a rev the setting does not."""
    rule = (Path(__file__).resolve().parents[1] / ".claude" / "rules" / "engineering.md").read_text(
        encoding="utf-8"
    )
    assert "feature/x:./.devkit.toml" in rule and "`ambiguous argument`" in rule
    assert "`MSYS2_ARG_CONV_EXCL`" in rule and "`origin/`" in rule
    # Described there, not quoted: instruction files carry no backslashed paths.
    assert sf._result_class("ambiguous argument 'origin\\main;.devkit.toml'")[0] == "environment"


def test_the_rules_every_session_reads_answer_the_two_frictions_they_file():
    """8c344a25 and d607d6b8's own session wrote through `python - <<'EOF'` patch scripts,
    and the ban sat in a subordinate clause of the worktree-guard section, below
    bypass mode's own advice to use the shell for small edits. 2ca551a4's session ran
    `uv run pytest` in a fresh `claude --worktree` tree, which no git hook provisions,
    and got a `.venv` with no pytest. Both answers lead their own sentence now."""
    rules = Path(__file__).resolve().parents[1] / ".claude" / "rules"
    engineering = (rules / "engineering.md").read_text(encoding="utf-8")
    assert (
        "**Write and edit files with the Write and Edit tools, never through Bash**" in engineering
    )
    assert "`python -` patch script" in engineering
    scope = " ".join((rules / "session-scope.md").read_text(encoding="utf-8").split())
    assert "run it bare: it builds the `.venv` a `claude --worktree` tree arrives without" in scope


def test_the_scope_rule_names_the_whole_run_sessions_type():
    """98e676fc then 2bf712f8, each retired as the session's habit: a social-scraper
    session ran `python -m pytest tests` over a few hundred tests after a refactor, under
    a rule that said only "the whole test suite". The rule names the spelling now, and
    the detector still reads it as whole, so the two cannot drift apart."""
    rules = Path(__file__).resolve().parents[1] / ".claude" / "rules"
    scope = " ".join((rules / "session-scope.md").read_text(encoding="utf-8").split())
    assert "run the whole test suite — `pytest tests`, or any run naming no test file" in scope
    ran = ".venv\\Scripts\\python.exe -m pytest tests -q -x -p no:cacheprovider 2>&1"
    assert sf.whole_runs(ran) == ["tests"]
    assert sf.whole_runs(ran.replace("tests", "", 1)) == [""], "naming no test file at all"


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


def test_a_scratch_heredoc_printed_back_with_cat_is_a_probe_not_a_write():
    """fbeea67b: the fixer that found `"\\\\"` survives the Bash tool mapped which spellings
    collapse by writing them to its job's `tmp/` and reading them back with `cat`, not
    `od`, and the pass filed its own probe as the write it measured."""
    fbeea67b = (
        "cat > \"$CLAUDE_JOB_DIR/tmp/p6.txt\" <<'EOF'\n"
        '1 a\\\\b\n2 "\\\\"\n3 \'\\\\\'\n4 \\\\"\n5 "a\\\\b"\n6 x\\\\\n7 \\\\n\n8 "\\\\n"\n'
        "9 (\\\\)\n10 \\\\ b\nEOF\n"
        'cat "$CLAUDE_JOB_DIR/tmp/p6.txt"'
    )
    assert classes([call(fbeea67b, "0")]) == []
    assert classes([call(fbeea67b.rsplit("\n", 1)[0], "0")]) == ["heredoc-write"]
    body = "<<'EOF'\na\\\\b\nEOF\n"
    for probe in (
        f'cd "$CLAUDE_JOB_DIR/tmp" && cat > hd.txt {body}cat hd.txt',
        "cat <<'EOF' > /tmp/p.txt\na\\\\b\nEOF\nhead -3 /tmp/p.txt",
        f"tee -a $TMP/p.txt {body}Get-Content $TMP/p.txt 2>&1",
    ):
        assert sf.printed_back(probe), probe
        assert not sf.damageable_heredoc(probe), probe
    # A file in the tree, a scratch file the command goes on to run or copy, and a
    # program that writes its own file are each still a write the work depends on.
    for write in (
        f"cat > scripts/x.py {body}cat scripts/x.py",
        f'cat > "$CLAUDE_JOB_DIR/tmp/x.py" {body}cat "$CLAUDE_JOB_DIR/tmp/x.py"; '
        'python "$CLAUDE_JOB_DIR/tmp/x.py"',
        f"cat > /tmp/x.py {body}cat /tmp/x.py > scripts/x.py",
        f"python - {body}cat /tmp/x.txt",
    ):
        assert not sf.printed_back(write), write
        assert sf.damageable_heredoc(write), write


def test_heredoc_target_names_the_file_a_heredoc_statement_writes():
    heredoc = sf.WRITTEN_HEREDOC
    assert sf.heredoc_target(f'cat > "$T/tmp/a b.txt" {heredoc}') == "$T/tmp/a b.txt"
    assert sf.heredoc_target(f"cat >> logs/a.txt {heredoc}") == "logs/a.txt"
    assert sf.heredoc_target(f"cat {heredoc} > logs/a.txt") == "logs/a.txt"
    assert sf.heredoc_target(f"tee -a 'logs/a.txt' {heredoc}") == "logs/a.txt"
    assert sf.heredoc_target(f"tee {heredoc}") == ""
    assert sf.heredoc_target(f"python - {heredoc}") == ""


def test_only_prints_is_a_printer_with_no_output_redirect():
    for printing in ("cat a.txt", "od -c a.txt", "head -3 a.txt 2>&1", "tail a 2>/dev/null"):
        assert sf.only_prints(printing), printing
    for using in ("cat a.txt > b.txt", "cat a >> b", "python a.py", "cp a b", ""):
        assert not sf.only_prints(using), using


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


def test_a_doubled_backslash_before_a_double_quote_is_not_friction():
    """70e4798c: a roguelike session wrote `replaceAll("\\\\", "/")` through a heredoc,
    and the file on disk held both backslashes. Written through the tool on 2026-10-04, a
    run of backslashes that ends at a `"` came out byte for byte -- `"\\\\"`, `\\\\"`,
    `\\\\\\\\"`, `\\\\\\"` -- while `\\\\'`, `'\\\\'`, `\\\\$` and `\\\\` before anything else
    still came out halved."""
    a70e4798c = (
        "cat > logs/deps.mjs <<'EOF'\n"
        "for (const m of src.matchAll(/(?:import|export)\\s[^'\"]*?from\\s+[\"']([^\"']+)[\"']/g)) {\n"
        'const rel = (s) => [...s].map((f) => relative(root, f).replaceAll("\\\\", "/"));\n'
        'console.log("\\nSHARED:\\n" + shared.join("\\n"));\n'
        "EOF\n"
        "node logs/deps.mjs > logs/deps.txt; head -3 logs/deps.txt"
    )
    assert classes([call(a70e4798c, "1")]) == []
    assert not sf.damageable_heredoc(a70e4798c)
    for kept in ('"\\\\"', '\\\\"', '\\\\\\\\"', '\\\\\\"', 'a\\\\"b'):
        assert not sf.damageable_heredoc(f"cat > a.py <<'EOF'\n{kept}\nEOF"), kept
    for halved in ("\\\\'", "'\\\\'", "\\\\$", "x\\\\", 'a\\\\"b\\\\c', '"a\\\\b"'):
        assert sf.damageable_heredoc(f"cat > a.py <<'EOF'\n{halved}\nEOF"), halved


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
    chunk = st.read_new(path, 0)
    assert [n for n, _ in chunk.rows] == [1, 2]
    assert st.read_new(path, chunk.offset).rows == (), "the torn line waits"
    with path.open("a", encoding="utf-8") as handle:
        handle.write('3}\n{"d": 4}\n')
    later = st.read_new(path, chunk.offset)
    assert [(n, row) for n, row in later.rows] == [(3, {"c": 3}), (4, {"d": 4})]
    path.write_text('{"z": 0}\n', encoding="utf-8")
    assert st.read_new(path, later.offset).rows == ((1, {"z": 0}),), "rewritten: from the top"


def test_a_transcript_rewritten_under_the_cursor_is_numbered_by_its_own_lines(tmp_path):
    """e1aaca09's evidence named line 1119 of a transcript whose call was on line 1003:
    Claude Code rewrote it under the cursor (a session relocated into a worktree) without
    shrinking it below the offset, and a running line count went on from the old file.
    A row's number is its line in the file as it is read, whatever came before."""
    path = tmp_path / "t.jsonl"
    path.write_bytes(b'{"a": 1}\n{"b": 2}\n{"c": 3}\n')
    first = st.read_new(path, 0)
    path.write_bytes(b'{"abcdefgh": 1, "i": 2}\n{"c": 3}\n{"d": 4}\n')
    assert first.offset == len(b'{"a": 1}\n{"b": 2}\n{"c": 3}\n')
    later = st.read_new(path, first.offset)
    assert [n for n, _ in later.rows] == [3], "numbered by the file, not the cursor"
    assert later.rows[0][1] == {"d": 4}, "the torn line the offset landed in is skipped"
    numbered = [n for n, _ in st.read_new(path, 0).rows]
    assert numbered == [1, 2, 3] and st.read_new(path, path.stat().st_size).rows == ()


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
    rows = _rows(sf.session_findings(session, st.read_new(session, 0), str(tmp_path), tmp_path))
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
    that is no longer the event the row names (a transcript rewritten since), and a class
    the whole session decides all stay open."""
    session = transcript(tmp_path / "s.jsonl", [user("go"), call("sleep 300", "1")], "c")
    [row] = _rows(sf.session_findings(session, st.read_new(session, 0), str(tmp_path), tmp_path))
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


def test_the_first_run_after_a_suite_wide_change_is_the_targeted_one():
    """77e3c01d, retired four times and back: a fixer restored data-lake's dependency
    floors, relocked, and ran data-lake's whole suite -- which is every test the change
    touches (7458ed23 was a pytest config's `addopts`). The exact calls, in order."""
    tree = "C:\\Users\\alexa\\vs-code\\data-lake\\.claude\\worktrees\\keep-dependency-floors-0928"
    ran = (
        "cd /c/Users/alexa/vs-code/data-lake/.claude/worktrees/keep-dependency-floors-0928 && "
        'git diff --text uv.lock | grep "^[-+]" | grep -v "^[-+][-+]"; '
        "ls scripts/hooks/tests/test_ci_workflow_contract.py && .venv/Scripts/python.exe -m "
        "pytest -q -p no:cacheprovider --tb=short 2>&1 | tail -3"
    )
    floors = _tool("Edit", "e", file_path=f"{tree}\\pyproject.toml", old_string="a", new_string="b")
    relock = call(f"cd {tree} && uv lock 2>&1 | tail -3 && git diff --stat", "l")
    assert classes([floors, relock, call(ran, "1")]) == []
    assert classes([call("uv lock", "l"), call(ran, "1")]) == [], "a relock alone"
    for name in ("uv.lock", "conftest.py", "pytest.ini", "requirements-dev.txt", "package.json"):
        assert classes([_tool("Write", "w", file_path=f"x/{name}"), call(ran, "1")]) == [], name
    patch = (
        "apply_patch *** Begin Patch\n*** Update File: pyproject.toml\n@@\n-a\n+b\n*** End Patch"
    )
    assert classes([call(patch, "p"), call(ran, "1")]) == [], "a Codex patch of the file"


def test_a_suite_wide_change_excuses_one_whole_run_and_nothing_else():
    """The change is checked by the first whole run after it; the whole suite again, or
    after an ordinary edit, is the habit the detector exists for. A targeted run checks
    one file, not the change, so it leaves the excuse standing."""
    whole = ".venv/Scripts/python.exe -m pytest -q"
    floors = _tool("Edit", "e", file_path="pyproject.toml")
    assert classes([floors, call(whole, "1"), call(whole, "2")]) == ["full-suite"]
    targeted = call("python -m pytest tests/test_x.py -q", "t")
    assert classes([floors, targeted, call(whole, "2")]) == []
    assert classes([floors, targeted, call(whole, "2"), call(whole, "3")]) == ["full-suite"]
    again = call(f"{whole} && {whole}", "2")
    assert classes([floors, again]) == ["full-suite"], "twice in one command is twice"
    for path in ("scripts/x.py", "docs/pyproject.toml.md", "tests/test_uv.lock.py"):
        assert classes([_tool("Edit", "e", file_path=path), call(whole, "1")]) == ["full-suite"]
    assert classes([call("uv sync", "s"), call(whole, "1")]) == ["full-suite"], "not a change"
    gate = call("python scripts/hooks/run_push_gate.py", "g")
    assert classes([floors, gate]) == ["full-suite"], "the push gate is still the gate's job"


def test_each_suite_root_is_excused_once_after_a_suite_wide_change():
    """A relock changes every test in both tiers: the vendored suite and the project's
    own are two roots, and running each whole once is checking the change, not a habit."""
    lock = _tool("Edit", "e", file_path="uv.lock")
    vendored = call(".venv/Scripts/python.exe -m pytest scripts/hooks/tests/ -q", "v")
    project = call(".venv/Scripts/python.exe -m pytest -q -p no:cacheprovider", "p")
    assert classes([lock, vendored, project]) == []
    assert classes([lock, vendored, project, call("pytest scripts/hooks/tests", "x")]) == [
        "full-suite"
    ]
    assert sf.whole_runs("pytest scripts\\hooks\\tests\\ -q && pytest -q; pytest t/x.py") == [
        "scripts/hooks/tests",
        "",
    ]
    assert sf.suite_root(" ./ -q") == "." and sf.suite_root(" tests/ tests/x.py") == "tests"


def test_a_suite_wide_file_git_reports_changed_is_a_suite_wide_change():
    """493a237b: an ibkr_trader fixer's tree arrived with `uv.lock` relocked by its
    provisioning -- data-lake's pytest floor had moved -- and its first `git status`
    said so. Its whole-suite runs were what checked that; the transcript held no edit or
    relock of its own, so the detector filed them as the habit. The exact calls."""
    status = (
        "cat .claude/fixer.md; ls logs/gate; git status --short; git diff --stat",
        "# Fixer sessions\n| a | b |\nfailed-jobs.log\n M uv.lock\n uv.lock | 2 +-\n"
        " 1 file changed, 1 insertion(+), 1 deletion(-)\n",
    )
    lint = (
        ".venv/Scripts/python.exe -m ruff check src tests; .venv/Scripts/python.exe "
        "scripts/hooks/untested_symbols.py 2>&1 | tail -3; .venv/Scripts/python.exe -m pytest "
        "scripts/hooks/tests/ -q 2>&1 | tail -3"
    )
    rows = [
        call(status[0], "s"),
        result(status[1], "s", error=False),
        call(".venv/Scripts/python.exe -m pytest tests/test_ci_data_lake_pin.py -q", "t"),
        call(lint, "l"),
        call(".venv/Scripts/python.exe -m pytest -q -p no:cacheprovider 2>&1 | tail -3", "p"),
    ]
    assert classes(rows) == []
    assert classes([rows[0], result("", "s", error=False), *rows[2:]]) == ["full-suite"]
    # The lock stays dirty all session: reading it again excuses nothing new.
    rerun = [*rows, call(status[0], "s2"), result(status[1], "s2", error=False), rows[-1]]
    assert classes(rerun) == ["full-suite"]


@pytest.mark.parametrize(
    "command, text, changed",
    [
        ("git status --short", " M uv.lock\n?? notes.md\n", True),
        ("git status", "Changes not staged:\n\tmodified:   tests/conftest.py\n", True),
        ("git diff --stat", " pyproject.toml | 4 ++--\n", True),
        ("git diff", "diff --git a/package.json b/package.json\n", True),
        ("git status --short", "M  src/app.py\n?? uv.lock.bak\n", False),
        ("git status --short", " M docs/pyproject.toml.md\n", False),
        ("cat notes.txt", " M uv.lock\n", False),  # not git's answer about the tree
        ("git log --stat -1", " uv.lock | 2 +-\n", False),  # history, not the tree
    ],
)
def test_reports_suite_change_reads_git_s_answer_about_the_tree(command, text, changed):
    assert sf.reports_suite_change(command, text) is changed


def test_an_edit_call_carries_the_file_it_names():
    for key in ("file_path", "notebook_path"):
        [event] = st.claude_events(_tool("Edit", "e", **{key: "a/b.py"}), 1)
        assert event.path == "a/b.py", key
    [bash] = st.claude_events(call("ls", "1"), 1)
    assert bash.path == ""


def test_changes_the_suite_reads_the_file_edited_and_the_command_run():
    def edit(path: str) -> st.Event:
        return st.Event("call", 1, tool="Edit", path=path)

    def run(command: str) -> st.Event:
        return st.Event("call", 1, command=command, tool="Bash")

    assert sf.changes_the_suite(edit("C:\\t\\pyproject.toml"))
    assert sf.changes_the_suite(edit("tests/conftest.py"))
    assert sf.changes_the_suite(edit("frontend/vitest.config.ts"))
    assert not sf.changes_the_suite(edit("scripts/pyproject_tools.py"))
    assert not sf.changes_the_suite(edit(""))
    read = st.Event("call", 1, tool="Read", path="pyproject.toml")
    assert not sf.changes_the_suite(read), "reading the manifest changes nothing"
    for command in ("uv lock --upgrade-package x", "cd t && uv add httpx", "npm uninstall y"):
        assert sf.changes_the_suite(run(command)), command
    for command in ("uv sync", "uv run pytest", "git diff uv.lock", "echo uv lock"):
        assert not sf.changes_the_suite(run(command)), command
    assert sf.changes_the_suite(run("apply_patch *** Begin Patch\n*** Add File: a/uv.lock\n"))


def test_session_findings_are_nothing_outside_the_workspace(tmp_path):
    chunk = st.Chunk(((1, call("sleep 99", "1")),), 0)
    assert sf.session_findings(tmp_path / "s.jsonl", chunk, "", tmp_path) == []
    assert sf.session_findings(tmp_path / "s.jsonl", chunk, "D:/elsewhere", tmp_path) == []
    [found] = sf.session_findings(tmp_path / "s.jsonl", chunk, str(tmp_path / "carameli"), tmp_path)
    assert (found.project, found.agent) == ("carameli", "claude")


def test_a_whole_suite_is_friction_only_where_the_scope_rule_asked_for_less(tmp_path):
    """0a4b17f3: a data-lake session ran this before its first edit. data-lake is held
    back from adoption and carries no session-scope rule, so nothing there asked for a
    targeted run -- its CLAUDE.md calls `run-tests.py` the suite -- and the row's detail
    was false about it. A tree that carries the rule still files; a tree gone defers to
    its project's checkout; neither there to read files, as before."""
    run = "python scripts/run-tests.py 2>&1 | tail -5; uv run pytest --cov=data_lake -q 2>&1 | tail -8"
    chunk = st.Chunk(((1, user("review the architecture")), (2, call(run, "1"))), 0)

    def kinds(cwd: Path) -> list[str]:
        return [
            f.kind for f in sf.session_findings(tmp_path / "s.jsonl", chunk, str(cwd), tmp_path)
        ]

    held, adopted = tmp_path / "data-lake", tmp_path / "ibkr_trader"
    for checkout in (held, adopted):
        (checkout / ".claude" / "rules").mkdir(parents=True)
    (adopted / sf.SCOPE_RULE).write_text(SCOPE_RULE_TEXT, encoding="utf-8")
    assert kinds(held) == []
    assert kinds(adopted) == ["full-suite"]
    gone = ".claude/worktrees/nifty-coalescing-lark"
    assert kinds(held / gone) == [], "the held project's checkout decides"
    assert kinds(adopted / gone) == ["full-suite"]
    assert kinds(tmp_path / "elsewhere") == ["full-suite"], "nothing to read: it stands"
    # The tree decides over its project's checkout while it is still there.
    assert not sf.asks_for_targeted_runs(str(held), tmp_path, "ibkr_trader")
    assert sf.asks_for_targeted_runs(str(tmp_path / "gone"), tmp_path, "ibkr_trader")
    assert not sf.asks_for_targeted_runs(str(tmp_path / "gone"), tmp_path, "data-lake")


# A scope rule as the vendored one reads since it named the spelling sessions type.
SCOPE_RULE_TEXT = (
    "# Rule: Stop at the change\n\n- run the whole test suite — `pytest tests`, or any run\n"
    "  naming no test file, however few tests it holds — or wait on a CI gate.\n"
)


def test_a_whole_suite_under_a_scope_rule_older_than_its_spelling_is_the_pull_owed(tmp_path):
    """8d219c5f, marked RECURRED: a social-scraper session ran `pytest tests` on
    v0.11.46, whose rule still said only "run the whole test suite" -- the spelling that
    retired the group last time landed four hours later and was in no release yet. That
    tree was never told, so the fix is the pull it owes, as for a heredoc ban."""
    run = ".venv\\Scripts\\python.exe -m pytest tests -q -p no:cacheprovider 2>&1"
    chunk = st.Chunk(((1, user("expand the scraper")), (2, call(run, "1"))), 0)
    stale, current = tmp_path / "social-scraper", tmp_path / "carameli"
    for checkout in (stale, current):
        (checkout / ".claude" / "rules").mkdir(parents=True)
    (stale / sf.SCOPE_RULE).write_text(
        "# Rule: Stop at the change\n\n- run the whole test suite, or wait on a CI gate.\n",
        encoding="utf-8",
    )
    (current / sf.SCOPE_RULE).write_text(SCOPE_RULE_TEXT, encoding="utf-8")

    def kinds(cwd: Path) -> list[str]:
        return [
            f.kind for f in sf.session_findings(tmp_path / "s.jsonl", chunk, str(cwd), tmp_path)
        ]

    assert kinds(stale) == []
    assert kinds(stale / ".claude/worktrees/federated-skipping-bee") == []
    assert kinds(current) == ["full-suite"]
    assert sf.asks_for_targeted_runs(str(REPO_ROOT), tmp_path, "devkit"), (
        "devkit's own rule carries the spelling the detector reads"
    )


def test_a_heredoc_write_is_friction_only_where_the_rule_bans_it(tmp_path):
    """45bd36c5: a sports_betting session on v0.11.32 (`devkit.onHold`) patched `cli.py`
    through `python - <<'EOF'`, hours after v0.11.41 shipped the rule's leading ban. Its
    copy said so only in passing, so the fix was the pull its project is held from, not a
    devkit change. A dispatched session was handed the ban in its system prompt anyway."""
    patch = (
        "python - <<'EOF'\nfrom pathlib import Path\np = Path(\"sports_betting/cli.py\")\n"
        's = p.read_text(encoding="utf-8")\n'
        "s = s.replace('serve', 'print(\"\\\\n\")')\n"
        'p.write_text(s, encoding="utf-8")\nEOF'
    )

    def kinds(cwd: Path, opening: str = "build the overlay") -> list[str]:
        chunk = st.Chunk(((1, user(opening)), (2, call(patch, "1"))), 0)
        return [
            f.kind for f in sf.session_findings(tmp_path / "s.jsonl", chunk, str(cwd), tmp_path)
        ]

    held, current = tmp_path / "sports_betting", tmp_path / "carameli"
    for checkout in (held, current):
        (checkout / ".claude" / "rules").mkdir(parents=True)
    (held / sf.ENGINEERING_RULE).write_text(
        "A heredoc is not a trigger alone, but write files with Write or Edit anyway.\n",
        encoding="utf-8",
    )
    (current / sf.ENGINEERING_RULE).write_text(
        f"{sf.FILE_WRITES_BAN} -- no heredoc\n", encoding="utf-8"
    )
    assert kinds(held) == []
    assert kinds(current) == ["heredoc-write"]
    gone = ".claude/worktrees/harmonic-humming-kay"
    assert kinds(held / gone) == [], "the held project's checkout decides"
    assert kinds(current / gone) == ["heredoc-write"]
    assert kinds(tmp_path / "elsewhere") == ["heredoc-write"], "nothing to read: it stands"
    assert kinds(held, f"You are a fixer. ... {sf.DISPATCHED} ...") == ["heredoc-write"]
    assert sf.bans_shell_writes(str(REPO_ROOT), tmp_path, "devkit"), (
        "devkit's own rule carries the ban in the spelling the detector reads"
    )


def test_a_bare_run_tests_is_whole_only_where_the_runner_defaults_to_the_suite(tmp_path):
    """0a9897d7, retired six times before: a fixer ran devkit's `run-tests.py` bare, which
    printed "7 test file(s) for 15 changed path(s) ... --all runs the suite". devkit's
    runner and the template's run the tests for what changed; only `--all` or a suite
    root is the suite. A project runner with no `--all` still runs everything bare."""
    ran = ".venv/Scripts/python.exe scripts/run-tests.py 2>&1 | tail -20; cat logs/test-failures.log | head -60"
    events = [e for e in st.claude_events(call(ran, "1"), 1)]
    assert sf.detect(events, targeted_runner=True) == []
    assert [cls for cls, _, _ in sf.detect(events)] == ["full-suite"]
    assert sf.whole_runs("python scripts/run-tests.py --all", targeted_runner=True) == [""]
    assert sf.whole_runs("python scripts/run-tests.py tests", targeted_runner=True) == ["tests"]
    assert sf.whole_runs("python scripts/run-tests.py --changed", targeted_runner=True) == []
    assert sf.whole_runs("python scripts/run-tests.py; pytest -q", targeted_runner=True) == [""]

    chunk = st.Chunk(((1, user("fix it")), (2, call(ran, "1"))), 0)
    targeted, whole = tmp_path / "devkit", tmp_path / "social-scraper"
    for checkout in (targeted, whole):
        (checkout / ".claude" / "rules").mkdir(parents=True)
        (checkout / sf.SCOPE_RULE).write_text(SCOPE_RULE_TEXT, encoding="utf-8")
        (checkout / "scripts").mkdir()
    (targeted / sf.RUNNER).write_text(
        'parser.add_argument("--all", action="store_true")\n', encoding="utf-8"
    )
    (whole / sf.RUNNER).write_text("subprocess.run(['pytest'])\n", encoding="utf-8")

    def kinds(cwd: Path) -> list[str]:
        return [
            f.kind for f in sf.session_findings(tmp_path / "s.jsonl", chunk, str(cwd), tmp_path)
        ]

    assert kinds(targeted) == []
    assert kinds(targeted / ".claude/worktrees/gone") == [], "the checkout's runner decides"
    assert kinds(whole) == ["full-suite"]
    assert kinds(tmp_path / "elsewhere") == ["full-suite"], "no runner to read: it stands"
    events = st.events(tmp_path / "s.jsonl", chunk.rows)
    assert sf.judged(events, str(targeted), tmp_path) == [], "the supervisor's audit agrees"
    assert [row[0] for row in sf.judged(events, str(whole), tmp_path)] == ["full-suite"]
    assert (REPO_ROOT / sf.RUNNER).is_file() and sf.runner_defaults_targeted(
        str(REPO_ROOT), tmp_path, "devkit"
    ), "devkit's own runner is the one 0a9897d7 ran"
    template = REPO_ROOT / "templates" / "core" / "scripts" / "run-tests.py.tmpl"
    assert sf.TARGETED_RUNNER.search(template.read_text(encoding="utf-8")), (
        "and every generated one"
    )


def test_the_machines_python_in_a_tree_with_its_own_venv_is_no_missing_environment(tmp_path):
    """The supervisor of 2026-10-01 filed against itself: it ran pytest on the machine's
    python in a tree whose `.venv` was there all along. A tree with no `.venv` of its own
    is the environment that is missing, and stays filed (4eab478e, 2026-09-29)."""
    ran = 'python -m pytest tests/test_session_trees.py -q -k "engine" 2>&1 | tail -5'
    said = "C:\\Users\\alexa\\AppData\\Local\\Python\\pythoncore-3.14-64\\python.exe: No module named pytest"
    rows = [call(ran, "1"), result(said, "1", error=False)]
    events = [e for n, row in enumerate(rows, 1) for e in st.claude_events(row, n)]
    tree = tmp_path / "devkit" / ".claude" / "worktrees" / "pinwheel"
    tree.mkdir(parents=True)
    assert [row[0] for row in sf.judged(events, str(tree), tmp_path)] == ["environment"]
    (tree / ".venv" / "Scripts").mkdir(parents=True)
    (tree / ".venv" / "Scripts" / "python.exe").write_text("", encoding="utf-8")
    assert sf.has_own_venv(str(tree)) and not sf.has_own_venv("")
    assert sf.judged(events, str(tree), tmp_path) == []
    venv_ran = [call(".venv/Scripts/python -m pytest tests/x.py", "2"), result(said, "2")]
    venv_events = [e for n, row in enumerate(venv_ran, 1) for e in st.claude_events(row, n)]
    assert [row[0] for row in sf.judged(venv_events, str(tree), tmp_path)] == ["environment"], (
        "the tree's own interpreter missing pytest is the environment"
    )


def test_the_machines_python_c_in_a_tree_with_its_own_venv_is_no_missing_environment(tmp_path):
    """9b16713a, the exact call: a social-scraper session read parquet with the machine's
    `python -c` while its tree's `.venv` held the pyarrow the tree imports."""
    ran = (
        'cd /c/Users/alexa/vs-code && grep -n "ARCHIVE_S3_PREFIX" sports_betting/.env.example; '
        'cd data-lake/data/archive && python -c "\nimport pyarrow.dataset as ds\n'
        "for d in ['sports_odds']:\n    t=ds.dataset(d,format='parquet').to_table()\n\""
    )
    said = "Exit code 1\nModuleNotFoundError: No module named 'pyarrow'"
    rows = [call(ran, "1"), result(said, "1")]
    events = [e for n, row in enumerate(rows, 1) for e in st.claude_events(row, n)]
    tree = _git_tree(tmp_path / "social-scraper", {"archive.py": "import pyarrow as pa\n"})
    assert [row[0] for row in sf.judged(events, str(tree), tmp_path)] == ["environment"]
    (tree / ".venv" / "Scripts").mkdir(parents=True)
    (tree / ".venv" / "Scripts" / "python.exe").write_text("", encoding="utf-8")
    assert sf.judged(events, str(tree), tmp_path) == []
    for spelled in ("python -I -c 'x'", "python3 -X utf8 -m pytest", "py -3 -Im pytest"):
        assert sf.BARE_INTERPRETER.search(spelled), f"3310eb1d: flags first, {spelled}"
    assert not sf.BARE_INTERPRETER.search("python -I scripts/x.py"), "a script is no -c"
    own = [call('.venv/Scripts/python.exe -c "import json; x()"', "2"), result(said, "2")]
    own_events = [e for n, row in enumerate(own, 1) for e in st.claude_events(row, n)]
    assert [row[0] for row in sf.judged(own_events, str(tree), tmp_path)] == ["environment"], (
        "the tree's own interpreter missing it is the environment"
    )


def test_a_session_whose_skill_runs_the_suite_whole_was_asked_for_the_whole_run():
    """3db42b06, filed after five retirements: a `/supervise-fix-pass` session ran the
    POSIX rehearsal's suite whole, which that skill's checklist tells the supervisor to
    run before each iteration. The exact call; the invocation row as Claude Code writes it."""
    ran = (
        '$env:PYTHONPATH = "tests"; .venv\\Scripts\\python.exe -m pytest -p '
        'posix_rehearsal_plugin -p no:cacheprovider -v -n 4 > "$env:TEMP\\ps-full.txt" 2>&1; '
        '"exit=$LASTEXITCODE"'
    )
    invoked = user(
        "<command-message>supervise-fix-pass</command-message>\n"
        "<command-name>/supervise-fix-pass</command-name>\n<command-args>3</command-args>"
    )
    assert classes([invoked, call(ran, "1")]) == []
    assert classes([call(ran, "1")]) == ["full-suite"], "no skill asked for it"
    ship = user("<command-message>ship</command-message>\n<command-name>/ship</command-name>")
    assert classes([ship, call(ran, "1")]) == ["full-suite"], "a skill that does not ask"
    gate = call("python scripts/precommit/run_push_gate.py", "g")
    assert classes([invoked, gate]) == [], "the gate is on the supervisor's checklist too"


@pytest.mark.parametrize("skill", sorted(sf.WHOLE_SUITE_SKILLS))
def test_each_skill_excused_a_whole_run_still_asks_for_one(skill):
    """The exemption holds only while the skill's own checklist runs the suite whole:
    once it stops naming the rehearsal, a whole run there is the habit again."""
    text = (REPO_ROOT / ".claude" / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
    assert "scripts/posix-rehearsal.py" in text


def test_a_bisect_is_not_one_command_failing_again():
    """967e40ba: a session bisected an order-dependent failure with a scratch script, a
    different range each call. The key was `normalize` cut to 90 characters, so the
    script's path filled it before the ranges began: seven calls, filed as one repeated."""
    script = (
        '& "C:\\Users\\alexa\\AppData\\Local\\Temp\\claude\\C--Users-alexa-vs-code-devkit\\'
        'bad2b8fc-e5ba-481a-ba2b-498fd0ccce44\\scratchpad\\bisect.ps1"'
    )
    ranges = ("0 50", '"0-50"', '"25-50"', '"38-50"', '"41-44,49-50"', '"42-43,49-50"')
    rows = []
    for i, args in enumerate(ranges):
        rows += [call(f"{script} {args}", str(i)), result("Exit code 1", str(i))]
    assert classes(rows) == []
    again = [
        row
        for i in range(3)
        for row in (call(f"{script} 0 50", f"a{i}"), result("Exit code 1", f"a{i}"))
    ]
    [(cls, what, _)] = sf.detect(
        [e for n, r in enumerate(again, 1) for e in st.claude_events(r, n)]
    )
    assert cls == "repeat-failure" and what.startswith("x3 & ") and len(what) <= 3 + sf.SNIPPET
    assert sf.same_command("git -C C:\\ws\\.claude\\worktrees\\a  log") == "git -C <tree> log"


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


def test_a_probe_of_git_bashs_path_conversion_is_a_measurement_not_friction():
    """d71a2caf: the fixer that set `MSYS2_ARG_CONV_EXCL` in the agent env reproduced the
    rewrite beside the setting that stops it, in one call, and the probe was filed as the
    defect it was measuring. The exact call and output; a real hit is still filed."""
    command = (
        'P=".venv/Scripts/python.exe"; MSYS2_ARG_CONV_EXCL="origin/;upstream/;refs/" $P -c '
        '"import sys;print(sys.argv[1:])" origin/main:.github/x upstream/main:.github/x '
        "refs/heads/main:.github/x /c/Users /tmp:/usr feature/x:.github/y; git show "
        'origin/main:.github/dependabot.yml | head -2; MSYS2_ARG_CONV_EXCL="origin/" git show '
        'origin/main:.github/dependabot.yml | head -2; grep -n "settings.json" '
        "scripts/devkit_manifest.py | head"
    )
    out = (
        "['origin/main:.github/x', 'upstream/main:.github/x', 'refs/heads/main:.github/x', "
        "'C:/Users', 'C:\\\\Users\\\\alexa\\\\AppData\\\\Local\\\\Temp;C:\\\\Program Files\\\\Git\\\\usr', "
        "'feature\\\\x;.github\\\\y']\n"
        "fatal: ambiguous argument 'origin\\main;.github\\dependabot.yml': unknown revision or "
        "path not in the working tree.\n# Dependency updates for devkit itself.\n"
    )
    assert classes([call(command, "1"), result(out, "1", error=False)]) == []
    assert classes([call(command, "1"), result(out, "1")]) == [], "failed or not"
    for probe in (
        "MSYS_NO_PATHCONV=1 git show a/b:c",
        "export MSYS2_ARG_CONV_EXCL='*'; git show a/b:c",
    ):
        hit = "fatal: ambiguous argument 'a\\b;c': unknown revision"
        assert classes([call(probe, "1"), result(hit, "1")]) == [], probe
    missing = "No module named pytest"
    assert classes([call(command, "1"), result(missing, "1")]) == ["environment"], (
        "a probe excuses only the rewrite it measures"
    )


def test_an_import_probe_is_answered_by_the_missing_module_it_asked_about():
    """31af383b: a social-scraper session reproduced its user's report that the system
    Python lacks the project's packages, by asking it -- the exact call and output -- and
    the answer was filed as an environment the session lost turns to."""
    command = (
        "(Get-Command python -ErrorAction SilentlyContinue).Source; (Get-Command uv "
        "-ErrorAction SilentlyContinue).Source; (Get-Command social-scraper -ErrorAction "
        'SilentlyContinue).Source; python -c "import playwright, pydantic_settings" 2>&1 | '
        "Select-Object -Last 1"
    )
    out = (
        "Exit code 1\nC:\\Users\\a\\AppData\\Local\\Microsoft\\WindowsApps\\python.exe\r\n"
        "C:\\Users\\a\\AppData\\Roaming\\Python\\Python314\\Scripts\\uv.exe\r\n"
        "ModuleNotFoundError: No module named 'playwright'"
    )
    assert classes([call(command, "1"), result(out, "1")]) == []
    for probe in (
        "python3 -c 'from playwright.sync_api import sync_playwright'",
        '.venv/Scripts/python.exe -c "import playwright.sync_api as p; import yaml"',
    ):
        assert classes([call(probe, "1"), result(out, "1")]) == [], probe


def test_an_import_probe_excuses_only_the_modules_it_imported():
    """A program that does more than import is work, and a module it did not ask about
    is something else missing: both are still the environment."""
    out = "ModuleNotFoundError: No module named 'playwright'"
    for command in (
        'python -c "import playwright; playwright.run()"',
        'python -c "import yaml"',
        "python -m social_scraper login x",
    ):
        assert classes([call(command, "1"), result(out, "1")]) == ["environment"], command
    assert sf.probed_modules('python -c "import a.b as c, d; from e.f import (g, h)"') == {
        "a",
        "d",
        "e",
    }
    assert sf.probed_modules('python -c "import a; print(a)"') == frozenset()


def _git_tree(root: Path, files: dict[str, str]) -> Path:
    """A repository at `root` tracking `files`: what `git grep` reads."""
    root.mkdir(parents=True)
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    return root


SCRATCH = "C:/Users/a/AppData/Local/Temp/claude/C--ws-roguelike/acd9/scratchpad"


def test_a_scratch_program_reaching_for_a_package_its_tree_never_imports_is_not_friction(
    tmp_path,
):
    """e1aaca09: a roguelike session compared two screenshots with Pillow from its
    scratchpad -- the exact call -- and the machine's Python lacking it was filed as a
    harness defect. Roguelike has no Python and nothing provisions Pillow: the session
    reached for a package no one promised it, which no change to the harness prevents."""
    ran = (
        f'cd "{SCRATCH}" && python -c "\nfrom PIL import Image\n'
        "a=Image.open('main.png').convert('RGB'); b=Image.open('engine.png').convert('RGB')\n"
        'box=(400,40,880,320)\nw=box[2]-box[0]; h=box[3]-box[1]\n"'
    )
    said = (
        'Exit code 1\nTraceback (most recent call last):\r\n  File "<string>", line 2, in '
        "<module>\r\n    from PIL import Image\r\nModuleNotFoundError: No module named 'PIL'"
    )
    rows = [call(ran, "1"), result(said, "1")]
    events = [e for n, row in enumerate(rows, 1) for e in st.claude_events(row, n)]
    assert classes(rows) == ["environment"], "the premise: the detector fires"
    roguelike = _git_tree(tmp_path / "roguelike", {"src/main.ts": "import { x } from './x';\n"})
    assert sf.judged(events, str(roguelike), tmp_path) == []
    script = f'python "{SCRATCH}/cmp.py" main.rgba.json engine.rgba.json'
    scripted = [call(script, "2"), result(said, "2")]
    script_events = [e for n, row in enumerate(scripted, 1) for e in st.claude_events(row, n)]
    assert sf.judged(script_events, str(roguelike), tmp_path) == [], "a scratch script too"
    # 3310eb1d, the same session's next call: an isolating `-I` before the `-c` got past
    # an excuse that knew only `python -c`, and the group came back after its resolution.
    isolated = (
        f'cd "{SCRATCH}" && python -I -c " from PIL import Image im = Image.open('
        "'painted-10.png') im.crop((330, 100, 530, 230)).resize((800, 520), Image.NEAREST)"
        ".save('crop.png') print('ok') \""
    )
    flagged = [call(isolated, "3"), result(said, "3")]
    flagged_events = [e for n, row in enumerate(flagged, 1) for e in st.claude_events(row, n)]
    assert sf.judged(flagged_events, str(roguelike), tmp_path) == [], "any flags before -c"
    relative = f'cd "{SCRATCH}" && python cmp.py main.rgba.json'
    moved = [call(relative, "4"), result(said, "4")]
    moved_events = [e for n, row in enumerate(moved, 1) for e in st.claude_events(row, n)]
    assert sf.judged(moved_events, str(roguelike), tmp_path) == [], "a script cd'd beside"
    [row] = sf.detect(events)
    assert sf.reached_past_the_tree(row, [roguelike])
    assert not sf.reached_past_the_tree(("poll", row[1], row[2]), [roguelike]), "its class only"


def _bash_spelling(path: Path) -> str:
    """`C:\\x\\y` as Git Bash spells it, `/c/x/y`."""
    text = path.as_posix()
    return f"/{text[0].lower()}{text[2:]}" if text[1:2] == ":" else text


def test_a_scratch_program_under_another_checkouts_venv_is_not_friction(tmp_path):
    """721497ef: a data-lake session read R2 sizes with ibkr_trader's interpreter -- the
    exact call's shape -- and ibkr_trader's static checkout carrying no `archive` extra was
    filed as the environment. data-lake imports boto3, so the import excuse did not hold;
    but the interpreter was another project's, which nothing provisions for this tree."""
    said = (
        'Exit code 1\nTraceback (most recent call last):\n  File "<string>", line 2, in '
        "<module>\nModuleNotFoundError: No module named 'boto3'"
    )
    tree = _git_tree(tmp_path / "data-lake", {"src/store.py": "import boto3\n"})
    other = tmp_path / "ibkr_trader"
    program = "-I -c \"\nimport boto3,re\nprint(boto3.client('s3'))\n\""

    def judged(command: str) -> list[str]:
        rows = [call(command, "1"), result(said, "1")]
        events = [e for n, row in enumerate(rows, 1) for e in st.claude_events(row, n)]
        return [row[0] for row in sf.judged(events, str(tree), tmp_path)]

    assert judged(f"cd {_bash_spelling(other)} && .venv/Scripts/python.exe {program}") == []
    assert judged(f'cd "{other}" && .venv/Scripts/python.exe {program}') == []
    assert judged(f"& {other}\\.venv\\Scripts\\python.exe {program}") == []
    assert judged(f".venv/Scripts/python.exe {program}") == ["environment"], "its own"
    assert judged(f"cd {_bash_spelling(tree)} && .venv/Scripts/python.exe {program}") == [
        "environment"
    ], "its own, by its full path"
    assert judged(f"cd {_bash_spelling(other)} && python scripts/x.py") == ["environment"], (
        "only a throwaway program is excused"
    )


@pytest.mark.parametrize(
    ("command", "home"),
    [
        ("cd /c/ws/ibkr_trader && .venv/Scripts/python.exe -c 'x'", "/c/ws/ibkr_trader"),
        (r"C:\ws\social-scraper\.venv\Scripts\python.exe -I x.py", r"C:\ws\social-scraper"),
        ("cd /c/ws && ibkr_trader/.venv/bin/python -c 'x'", "/c/ws/ibkr_trader"),
        (".venv/Scripts/python.exe -c 'x'", ""),
        ("cd scripts && ../.venv/Scripts/python.exe -c 'x'", ""),
        ("python -c 'x'", ""),
    ],
)
def test_interpreter_home_is_the_checkout_whose_venv_runs(command, home):
    assert sf.interpreter_home(command) == home


def test_a_checkout_is_compared_however_it_is_spelled():
    roots = [Path(r"C:\ws\data-lake")]
    assert not sf.foreign_environment("cd /c/ws/data-lake && .venv/bin/python -c 'x'", roots)
    assert not sf.foreign_environment(r"C:\WS\Data-Lake\.venv\Scripts\python.exe x", roots)
    assert sf.foreign_environment("cd /c/ws/data-lake-2 && .venv/bin/python -c 'x'", roots)
    assert not sf.foreign_environment("cd /c/ws/other && .venv/bin/python -c 'x'", []), (
        "no tree to compare with: it stands"
    )


def test_a_missing_package_the_tree_imports_or_its_own_program_needs_stays_filed(tmp_path):
    """The excuse is narrow: a package the tree's own code imports is one its provisioning
    owes, and a program of the tree's own failing on one is the environment, whoever
    imports it. A tree nothing can read stands, as every judgement here does."""
    said = "ModuleNotFoundError: No module named 'PIL'"
    adhoc = "python -c \"from PIL import Image; Image.open('a.png')\""

    def judged(command: str, cwd: Path) -> list[str]:
        rows = [call(command, "1"), result(said, "1")]
        events = [e for n, row in enumerate(rows, 1) for e in st.claude_events(row, n)]
        return [row[0] for row in sf.judged(events, str(cwd), tmp_path)]

    uses = _git_tree(tmp_path / "uses", {"tools/shots.py": "import os\nfrom PIL import Image\n"})
    assert judged(adhoc, uses) == ["environment"], "the tree imports it"
    bare = _git_tree(tmp_path / "bare", {"src/main.ts": ""})
    assert judged("python scripts/shots.py", bare) == ["environment"], "its own program"
    assert judged(adhoc, tmp_path / "gone") == ["environment"], "nothing to read"
    assert sf.ad_hoc_program(adhoc) and sf.ad_hoc_program(f"python {SCRATCH}/x.py")
    assert not sf.ad_hoc_program("python scripts/x.py") and not sf.ad_hoc_program("ls")
    for spelled in (
        "python -I -c 'x'",
        "python -X utf8 -c 'x'",
        "py -3 -B -c 'x'",
        "python -Ic 'x'",
    ):
        assert sf.ad_hoc_program(spelled), spelled
    assert not sf.ad_hoc_program("python -I scripts/x.py"), "a flag is not a program"
    assert not sf.ad_hoc_program(f'cd "{SCRATCH}" && python C:/ws/bare/scripts/x.py')
    assert not sf.ad_hoc_program("cd scripts && python x.py"), "the tree's own directory"
    assert sf.imported_by_tree("PIL", [uses]) and not sf.imported_by_tree("PIL", [bare])
    assert not sf.imported_by_tree("PI", [uses]), "a whole name, not a prefix"

    def missing(argv, **kwargs):
        raise FileNotFoundError("git")

    def broken(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 128, "", "fatal: not a git repository")

    for runner in (missing, broken):
        assert sf.imported_by_tree("PIL", [bare], runner=runner), "unknown: it stands"


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
    chunk = st.Chunk(rows, 0)
    asked: list[str] = []

    def branch_of(cwd):
        asked.append(cwd)
        return "agent/flaky-0926"

    cwd = str(tmp_path / "carameli")
    found = sf.session_findings(tmp_path / "s.jsonl", chunk, cwd, tmp_path, branch_of)
    settles = {f.kind: f.settles_with for f in found}
    assert settles == {"user-frustration": "agent/flaky-0926", "poll": ""}
    assert asked == [cwd]
    quiet = st.Chunk(((1, call("sleep 99", "1")),), 0)
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


def test_git_status_before_a_diff_is_still_only_git_reading():
    """5453c7ae: a ship step's `git status --short; git diff`, whose diff added
    `except ModuleNotFoundError:`, was filed as the environment: `status` made the line
    more than git reading, though it prints only the tree's own paths."""
    command = "git status --short; git diff scripts/sync-devkit.py scripts/devkit_manifest.py"
    assert sf.git_reads_only(command)
    diff = " M scripts/sync-devkit.py\n+    except ModuleNotFoundError:\n+        return []\n"
    assert classes([call(command, "1"), result(diff, "1", error=False)]) == []
    assert not sf.git_reads_only("git status && python -m pytest t.py")
