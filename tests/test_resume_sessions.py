"""Tests for scripts/resume-sessions.py (the "reopen my last N sessions" task).

The properties worth pinning are the ones that decide *which* four sessions come back,
because every one of them is a way for a real session to be silently displaced by
something that is not a session at all: a subagent's transcript, an abandoned window, a
box that no longer exists. The ordering is the other half — "chronological" is the whole
request, and a single sort satisfies "most recent four" while getting it backwards.
"""

import json
import os
from pathlib import Path

import pytest

from support import REPO_ROOT, devkit_project, load_script

rs = load_script("scripts/resume-sessions.py")


# --- fixtures ---------------------------------------------------------------


def write_transcript(
    store: Path,
    session_id: str,
    *,
    cwd: Path,
    prompt: str = "do the thing",
    slug: str = "proj",
    mtime: float | None = None,
    sidechain: bool = False,
    extra: list[dict] | None = None,
) -> Path:
    """A transcript shaped like the real store: `<store>/<slug>/<session>.jsonl`."""
    records: list[dict] = [
        {"type": "mode", "mode": "normal", "sessionId": session_id},
        {
            "type": "user",
            "isSidechain": sidechain,
            "cwd": str(cwd),
            "sessionId": session_id,
            "message": {"role": "user", "content": prompt},
        },
    ]
    records += extra or []
    directory = store / slug
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def write_codex_rollout(
    store: Path,
    session_id: str,
    *,
    cwd: Path,
    prompt: str = "do the Codex thing",
    mtime: float | None = None,
) -> Path:
    """A rollout shaped like `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl`."""
    records = [
        {
            "type": "session_meta",
            "payload": {"id": session_id, "cwd": str(cwd), "source": "cli"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "# AGENTS.md instructions\ncontext"},
                    {"type": "input_text", "text": f"<environment_context>{cwd}"},
                ],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            },
        },
    ]
    directory = store / "2026" / "08" / "10"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rollout-2026-08-10T12-00-00-{session_id}.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def session(name: str, mtime: float, cwd: Path, prompt: str = "topic", agent: str = "claude"):
    """One `Session`. Deliberately unannotated: `rs` is loaded by path, so mypy has no
    name to resolve `rs.Session` against and reports the annotation as undefined."""
    return rs.Session(agent=agent, session_id=name, cwd=cwd, prompt=prompt, mtime=mtime)


# --- reading a transcript ----------------------------------------------------


def test_head_records_stops_at_the_line_limit(tmp_path):
    """A megabyte session must not be parsed to find a cwd that is in line two."""
    path = tmp_path / "big.jsonl"
    path.write_text("".join(json.dumps({"n": i}) + "\n" for i in range(50)), encoding="utf-8")
    assert len(rs.head_records(path, max_lines=10)) == 10


def test_head_records_stops_at_the_byte_limit(tmp_path):
    path = tmp_path / "fat.jsonl"
    path.write_text(
        "".join(json.dumps({"pad": "x" * 500}) + "\n" for _ in range(20)), encoding="utf-8"
    )
    assert len(rs.head_records(path, max_bytes=1200)) < 20


def test_a_half_written_last_line_is_skipped_not_fatal(tmp_path):
    """The store is append-only; a LIVE session's final line can be mid-write."""
    path = tmp_path / "live.jsonl"
    path.write_text(json.dumps({"type": "mode"}) + '\n{"type": "us', encoding="utf-8")
    assert rs.head_records(path) == [{"type": "mode"}]


def test_an_unreadable_transcript_yields_no_records(tmp_path):
    assert rs.head_records(tmp_path / "does-not-exist.jsonl") == []


# --- sidechains --------------------------------------------------------------


def test_a_subagents_transcript_is_a_sidechain():
    assert rs.is_sidechain([{"type": "user", "isSidechain": True}])


def test_a_parent_that_later_spawned_a_subagent_is_not_a_sidechain():
    """`any()` over the head would throw away every session that used a subagent."""
    records = [
        {"type": "user", "isSidechain": False},
        {"type": "user", "isSidechain": True},
    ]
    assert not rs.is_sidechain(records)


def test_records_with_no_flag_at_all_are_not_sidechains():
    assert not rs.is_sidechain([{"type": "mode"}])


# --- the opening prompt ------------------------------------------------------


def test_first_prompt_reads_a_plain_string_message():
    records = [{"type": "user", "message": {"role": "user", "content": "fix the gate"}}]
    assert rs.claude_first_prompt(records) == "fix the gate"


def test_first_prompt_reads_text_blocks_and_ignores_tool_results():
    records = [
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "content": "hook error"},
                ],
            },
        },
        {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": "real prompt"}]},
        },
    ]
    assert rs.claude_first_prompt(records) == "real prompt"


def test_injected_wrappers_do_not_become_the_title():
    """A slash command's expansion is a user-role record nobody typed."""
    records = [
        {
            "type": "user",
            "message": {"role": "user", "content": "<command-name>/ship</command-name>"},
        },
        {"type": "user", "isMeta": True, "message": {"role": "user", "content": "meta"}},
        {"type": "user", "message": {"role": "user", "content": "<system-reminder>ctx"}},
        {"type": "user", "message": {"role": "user", "content": "the actual ask"}},
    ]
    assert rs.claude_first_prompt(records) == "the actual ask"


def test_a_session_nobody_typed_in_has_no_prompt():
    assert rs.claude_first_prompt([{"type": "mode"}, {"type": "assistant"}]) == ""


def test_codex_first_prompt_skips_injected_context():
    records = [
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "# AGENTS.md instructions\npolicy"},
                    {
                        "type": "input_text",
                        "text": "<environment_context>cwd</environment_context>",
                    },
                ],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "resume Codex sessions"}],
            },
        },
    ]
    assert rs.codex_first_prompt(records) == "resume Codex sessions"


# --- parsing one session -----------------------------------------------------


def test_parse_session_reads_the_id_cwd_and_prompt(tmp_path):
    store, cwd = tmp_path / "store", tmp_path / "repo"
    cwd.mkdir()
    path = write_transcript(store, "abc-123", cwd=cwd, prompt="add the task", mtime=1000.0)
    parsed = rs.parse_claude_session(path)
    assert parsed is not None
    assert (parsed.session_id, parsed.cwd, parsed.prompt, parsed.mtime) == (
        "abc-123",
        cwd,
        "add the task",
        1000.0,
    )


def test_the_id_comes_from_the_filename_not_the_records(tmp_path):
    """The filename is the key `--resume` looks up; a copied transcript disagrees."""
    store, cwd = tmp_path / "store", tmp_path / "repo"
    cwd.mkdir()
    path = write_transcript(store, "recorded-id", cwd=cwd)
    renamed = path.with_name("filename-id.jsonl")
    path.rename(renamed)
    assert rs.parse_claude_session(renamed).session_id == "filename-id"


def test_a_sidechain_transcript_is_not_resumable(tmp_path):
    store, cwd = tmp_path / "store", tmp_path / "repo"
    cwd.mkdir()
    path = write_transcript(store, "sub", cwd=cwd, sidechain=True)
    assert rs.parse_claude_session(path) is None


def test_a_session_opened_and_abandoned_is_not_resumable(tmp_path):
    """It is recent enough to displace a real session, and there is nothing in it."""
    store = tmp_path / "store"
    (store / "proj").mkdir(parents=True)
    path = store / "proj" / "empty.jsonl"
    path.write_text(json.dumps({"type": "mode", "cwd": str(tmp_path)}) + "\n", encoding="utf-8")
    assert rs.parse_claude_session(path) is None


def test_a_transcript_with_no_cwd_is_not_resumable(tmp_path):
    store = tmp_path / "store"
    (store / "proj").mkdir(parents=True)
    path = store / "proj" / "nowhere.jsonl"
    path.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n",
        encoding="utf-8",
    )
    assert rs.parse_claude_session(path) is None


def test_collect_walks_every_project_directory(tmp_path):
    store, cwd = tmp_path / "store", tmp_path / "repo"
    cwd.mkdir()
    write_transcript(store, "one", cwd=cwd, slug="proj-a")
    write_transcript(store, "two", cwd=cwd, slug="proj-b")
    write_transcript(store, "sub", cwd=cwd, slug="proj-b", sidechain=True)
    assert {s.session_id for s in rs.collect(store, "claude")} == {"one", "two"}


def test_collect_on_a_machine_with_no_store_is_empty(tmp_path):
    assert rs.collect(tmp_path / "absent", "claude") == []


def test_parse_codex_session_reads_metadata_and_human_prompt(tmp_path):
    store, cwd = tmp_path / "store", tmp_path / "repo"
    cwd.mkdir()
    path = write_codex_rollout(store, "codex-id", cwd=cwd, prompt="fix the task", mtime=42.0)
    parsed = rs.parse_codex_session(path)
    assert parsed is not None
    assert (parsed.session_id, parsed.cwd, parsed.prompt, parsed.mtime) == (
        "codex-id",
        cwd,
        "fix the task",
        42.0,
    )


def test_collect_codex_walks_the_dated_rollout_tree(tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    write_codex_rollout(tmp_path, "one", cwd=cwd)
    write_codex_rollout(tmp_path, "two", cwd=cwd)
    assert {s.session_id for s in rs.collect(tmp_path, "codex")} == {"one", "two"}


# --- choosing which to reopen ------------------------------------------------


def test_select_takes_the_most_recent_and_returns_them_oldest_first(tmp_path):
    sessions = [session(f"s{i}", float(i), tmp_path) for i in range(6)]
    assert [s.session_id for s in rs.select(sessions, 4)] == ["s2", "s3", "s4", "s5"]


def test_select_returns_everything_when_there_are_fewer_than_asked(tmp_path):
    sessions = [session("a", 2.0, tmp_path), session("b", 1.0, tmp_path)]
    assert [s.session_id for s in rs.select(sessions, 4)] == ["b", "a"]


def test_select_any_takes_one_global_recency_slice_across_agents(tmp_path):
    sessions = [
        session("claude-old", 1.0, tmp_path, agent="claude"),
        session("claude-new", 4.0, tmp_path, agent="claude"),
        session("codex-middle", 2.0, tmp_path, agent="codex"),
        session("codex-new", 3.0, tmp_path, agent="codex"),
    ]
    assert [(s.agent, s.session_id) for s in rs.select(sessions, 3)] == [
        ("codex", "codex-middle"),
        ("codex", "codex-new"),
        ("claude", "claude-new"),
    ]


def test_a_reaped_box_is_partitioned_out(tmp_path):
    """`--resume` is keyed to a directory; a reaped box has nothing to reopen."""
    live_dir = tmp_path / "checkout"
    live_dir.mkdir()
    live, orphaned = rs.partition(
        [session("live", 2.0, live_dir), session("gone", 1.0, tmp_path / "reaped-box")]
    )
    assert [s.session_id for s in live] == ["live"]
    assert [s.session_id for s in orphaned] == ["gone"]


# --- the launch command ------------------------------------------------------


def test_wt_args_opens_one_tab_per_session_in_its_own_directory(tmp_path):
    sessions = [session("aaa", 1.0, tmp_path / "one"), session("bbb", 2.0, tmp_path / "two")]
    args = rs.wt_args(sessions)
    assert args[:2] == ["-w", "-1"]  # a NEW window, never someone else's
    assert args.count("new-tab") == 2
    assert args.count(";") == 2  # one separator between tabs, one before focus-tab
    assert args[-3:] == ["focus-tab", "-t", "0"]
    for name, directory in (("aaa", "one"), ("bbb", "two")):
        index = args.index(name)
        assert args[index - 1] == "--resume"
        assert str(tmp_path / directory) in args


def test_wt_args_lays_the_tabs_out_in_the_order_given(tmp_path):
    sessions = [session("first", 1.0, tmp_path), session("second", 2.0, tmp_path)]
    args = rs.wt_args(sessions)
    assert args.index("first") < args.index("second")


def test_each_agent_uses_its_own_resume_syntax(tmp_path):
    claude = rs.wt_args([session("a", 1.0, tmp_path)], agent="claude")
    codex = rs.wt_args([session("a", 1.0, tmp_path)], agent="codex")
    assert claude[claude.index("claude") :] == [
        "claude",
        "--resume",
        "a",
        ";",
        "focus-tab",
        "-t",
        "0",
    ]
    assert codex[codex.index("codex") :] == ["codex", "resume", "a", ";", "focus-tab", "-t", "0"]


def test_a_mixed_window_uses_each_sessions_own_resume_syntax(tmp_path):
    args = rs.wt_args(
        [
            session("claude-id", 1.0, tmp_path, agent="claude"),
            session("codex-id", 2.0, tmp_path, agent="codex"),
        ]
    )
    assert args[args.index("claude") : args.index(";")] == [
        "claude",
        "--resume",
        "claude-id",
    ]
    codex_index = args.index("codex")
    assert args[codex_index : codex_index + 3] == ["codex", "resume", "codex-id"]


def test_a_prompt_cannot_rearrange_the_window(tmp_path):
    """wt treats `;` as its tab separator, so a prompt containing one would split a tab."""
    hostile = session("a", 1.0, tmp_path, prompt='drop; new-tab --title "x"')
    title = rs.tab_title(hostile)
    assert ";" not in title
    assert '"' not in title


def test_a_long_prompt_is_trimmed_to_fit_a_tab(tmp_path):
    title = rs.tab_title(session("a", 1.0, tmp_path / "repo", prompt="word " * 40))
    assert title.startswith("repo - ")
    assert len(title) <= len("repo - ") + rs.TITLE_MAX


def test_shell_lines_are_the_no_windows_terminal_fallback(tmp_path):
    selected = [session("abc", 1.0, tmp_path)]
    assert rs.shell_lines(selected, "claude") == [f'cd "{tmp_path}" && claude --resume abc']
    assert rs.shell_lines(selected, "codex") == [f'cd "{tmp_path}" && codex resume abc']


# --- the entrypoint ----------------------------------------------------------


def test_list_reports_the_four_most_recent_and_launches_nothing(tmp_path, capsys, monkeypatch):
    store, cwd = tmp_path / "store", tmp_path / "repo"
    cwd.mkdir()
    for index in range(6):
        write_transcript(
            store, f"sess-{index}", cwd=cwd, prompt=f"task {index}", mtime=1000.0 + index
        )

    def refuse(*_args, **_kwargs):
        raise AssertionError("--list must not launch anything")

    monkeypatch.setattr(rs.subprocess, "run", refuse)
    assert rs.main(["--sessions-dir", str(store), "--list"]) == 0

    out = capsys.readouterr().out
    assert "task 1" not in out  # the two oldest of the six are out of scope
    assert [out.index(f"task {i}") for i in (2, 3, 4, 5)] == sorted(
        out.index(f"task {i}") for i in (2, 3, 4, 5)
    )


def test_an_orphaned_session_recent_enough_to_matter_is_named(tmp_path, capsys):
    store, cwd = tmp_path / "store", tmp_path / "repo"
    cwd.mkdir()
    write_transcript(store, "live", cwd=cwd, prompt="still here", mtime=1000.0)
    write_transcript(store, "gone", cwd=tmp_path / "reaped", prompt="in a box", mtime=1001.0)
    assert rs.main(["--sessions-dir", str(store), "--list"]) == 0
    out = capsys.readouterr().out
    assert "still here" in out
    assert "in a box" in out and "directory is gone" in out


def test_an_ancient_orphan_is_not_reported(tmp_path, capsys):
    """Every box ever reaped is in the store; only the ones that were candidates matter."""
    store, cwd = tmp_path / "store", tmp_path / "repo"
    cwd.mkdir()
    write_transcript(store, "live", cwd=cwd, prompt="still here", mtime=1000.0)
    write_transcript(store, "ancient", cwd=tmp_path / "reaped", prompt="last year", mtime=1.0)
    rs.main(["--sessions-dir", str(store), "--list"])
    assert "last year" not in capsys.readouterr().out


def test_an_empty_store_fails_loudly_and_names_the_path(tmp_path, capsys):
    assert rs.main(["--sessions-dir", str(tmp_path / "nothing"), "--list"]) == 1
    assert str(tmp_path / "nothing") in capsys.readouterr().err


def test_a_zero_count_is_rejected_rather_than_opening_no_tabs(tmp_path):
    assert rs.main(["--sessions-dir", str(tmp_path), "--count", "0"]) == 2


def test_dry_run_prints_the_command_line_and_launches_nothing(tmp_path, capsys, monkeypatch):
    store, cwd = tmp_path / "store", tmp_path / "repo"
    cwd.mkdir()
    write_transcript(store, "sess", cwd=cwd, prompt="a task", mtime=1000.0)
    monkeypatch.setattr(rs, "find_terminal", lambda: r"C:\wt.exe")

    def refuse(*_args, **_kwargs):
        raise AssertionError("--dry-run must not launch anything")

    monkeypatch.setattr(rs.subprocess, "run", refuse)
    assert rs.main(["--sessions-dir", str(store), "--dry-run"]) == 0
    assert "--resume sess" in capsys.readouterr().out


def test_codex_list_reads_rollouts_instead_of_claude_transcripts(tmp_path, capsys):
    store, cwd = tmp_path / "store", tmp_path / "repo"
    cwd.mkdir()
    write_codex_rollout(store, "codex-session", cwd=cwd, prompt="Codex topic")
    assert rs.main(["--agent", "codex", "--sessions-dir", str(store), "--list"]) == 0
    out = capsys.readouterr().out
    assert "Codex session" in out
    assert "Codex topic" in out


def test_any_list_merges_both_stores_before_applying_the_count(tmp_path, capsys, monkeypatch):
    claude_root = tmp_path / "claude"
    codex_root = tmp_path / "codex"
    cwd = tmp_path / "repo"
    cwd.mkdir()
    write_transcript(claude_root, "claude-old", cwd=cwd, prompt="claude-old", mtime=1.0)
    write_transcript(claude_root, "claude-new", cwd=cwd, prompt="claude-new", mtime=4.0)
    write_codex_rollout(codex_root, "codex-middle", cwd=cwd, prompt="codex-middle", mtime=2.0)
    write_codex_rollout(codex_root, "codex-new", cwd=cwd, prompt="codex-new", mtime=3.0)
    monkeypatch.setattr(
        rs,
        "sessions_root",
        lambda agent: claude_root if agent == "claude" else codex_root,
    )

    assert rs.main(["--agent", "claude,codex", "--count", "3", "--list"]) == 0
    out = capsys.readouterr().out
    assert "claude-old" not in out
    assert out.index("codex-middle") < out.index("codex-new") < out.index("claude-new")


def test_any_is_the_cli_alias_for_both_agents():
    assert rs._parse_agents("any") == ("claude", "codex")
    assert rs._parse_agents("codex,claude") == ("claude", "codex")


def test_an_unknown_agent_selection_is_rejected():
    with pytest.raises(rs.argparse.ArgumentTypeError, match="choose one of"):
        rs._parse_agents("claude,other")


def test_without_windows_terminal_it_prints_the_commands_and_fails(tmp_path, capsys, monkeypatch):
    """A launcher that could not launch must not report success."""
    store, cwd = tmp_path / "store", tmp_path / "repo"
    cwd.mkdir()
    write_transcript(store, "sess", cwd=cwd, prompt="a task", mtime=1000.0)
    monkeypatch.setattr(rs, "find_terminal", lambda: "")
    assert rs.main(["--sessions-dir", str(store)]) == 1
    assert "claude --resume sess" in capsys.readouterr().err


def test_each_agent_store_honours_its_config_home(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "elsewhere"))
    assert rs.sessions_root("claude") == tmp_path / "elsewhere" / "projects"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert rs.sessions_root("claude") == Path.home() / ".claude" / "projects"
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    assert rs.sessions_root("codex") == tmp_path / "codex-home" / "sessions"
    monkeypatch.delenv("CODEX_HOME")
    assert rs.sessions_root("codex") == Path.home() / ".codex" / "sessions"


# --- the harness contract ----------------------------------------------------


def test_the_script_is_stdlib_only():
    """devkit ships no runtime dependencies, and this runs from a VS Code task."""
    source = (REPO_ROOT / "scripts" / "resume-sessions.py").read_text(encoding="utf-8")
    for line in source.splitlines():
        if line.startswith(("import ", "from ")) and "support" not in line:
            module = line.split()[1].split(".")[0]
            assert module in {
                "__future__",
                "argparse",
                "json",
                "os",
                "re",
                "shutil",
                "subprocess",
                "sys",
                "time",
                "dataclasses",
                "pathlib",
            }, f"non-stdlib import: {line}"


def test_the_workspace_task_passes_the_resume_agent_checkboxes():
    source = devkit_project.canonical_tasks_text()
    task = source[source.index('"label": "Agents: Resume Recent Sessions"') :]
    task = task[: task.index('"problemMatcher"')]
    assert '"--agent"' in task
    assert '"${input:resumeAgents}"' in task

    picker = source[source.index('"id": "resumeAgents"') :]
    picker = picker[: picker.index('"id": "resumeSessionCount"')]
    assert '"multiPick": true' in picker
    assert '"minCount": 1' in picker
    assert '"claude"' in picker
    assert '"codex"' in picker
