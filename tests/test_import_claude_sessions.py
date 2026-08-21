"""Tests for importing Claude sessions stopped by a usage limit into Codex."""

import base64
import json
import os
from pathlib import Path

from support import REPO_ROOT, load_script

ics = load_script("scripts/import-claude-sessions.py")


def write_transcript(
    store: Path,
    session_id: str,
    *,
    cwd: Path,
    prompt: str = "finish the interrupted task",
    mtime: float = 1_000.0,
    sidechain: bool = False,
    ending: str = "limit",
) -> Path:
    records = [
        {"type": "mode", "mode": "normal", "sessionId": session_id},
        {
            "type": "user",
            "isSidechain": sidechain,
            "cwd": str(cwd),
            "sessionId": session_id,
            "message": {"role": "user", "content": prompt},
        },
        {
            "type": "assistant",
            "isSidechain": sidechain,
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "work in progress"}],
                "stop_reason": "end_turn",
            },
        },
    ]
    if ending in {"limit", "generic-429", "then-success"}:
        text = (
            "You've hit your session limit · resets 8:30pm (America/New_York)"
            if ending != "generic-429"
            else "Rate limit exceeded; retry this request"
        )
        records.append(
            {
                "type": "assistant",
                "isSidechain": sidechain,
                "error": "rate_limit",
                "isApiErrorMessage": True,
                "apiErrorStatus": 429,
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": text}],
                    "stop_reason": "stop_sequence",
                },
            }
        )
    if ending in {"success", "then-success"}:
        records.append(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "finished normally"}],
                    "stop_reason": "end_turn",
                },
            }
        )
    directory = store / "project"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def test_parse_accepts_the_real_claude_limit_record_shape(tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    path = write_transcript(tmp_path / "store", "limited", cwd=cwd, mtime=42.0)

    found = ics.parse_session(path)

    assert found is not None
    assert (found.session_id, found.cwd, found.prompt, found.mtime) == (
        "limited",
        cwd,
        "finish the interrupted task",
        42.0,
    )
    assert "session limit" in found.limit_message


def test_a_generic_429_is_not_mistaken_for_a_usage_limit(tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    path = write_transcript(tmp_path / "store", "busy", cwd=cwd, ending="generic-429")
    assert ics.parse_session(path) is None


def test_a_session_that_later_succeeded_did_not_end_at_the_limit(tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    path = write_transcript(tmp_path / "store", "resumed", cwd=cwd, ending="then-success")
    assert ics.parse_session(path) is None


def test_sidechains_and_sessions_without_limit_endings_are_skipped(tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    sidechain = write_transcript(tmp_path / "store", "subagent", cwd=cwd, sidechain=True)
    success = write_transcript(tmp_path / "store", "success", cwd=cwd, ending="success")
    assert ics.parse_session(sidechain) is None
    assert ics.parse_session(success) is None


def test_collect_and_select_take_the_recent_sessions_oldest_first(tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    store = tmp_path / "store"
    for index in range(6):
        write_transcript(store, f"s{index}", cwd=cwd, mtime=1_000.0 + index)

    selected = ics.select(ics.collect(store), 4)

    assert [session.session_id for session in selected] == ["s2", "s3", "s4", "s5"]


def test_import_artifact_copies_the_transcript_and_carries_a_safe_handoff(tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    source = write_transcript(tmp_path / "store", "limited", cwd=cwd)
    session = ics.parse_session(source)
    assert session is not None

    artifact = ics.prepare_import(session, tmp_path / "codex-imports")

    assert artifact.transcript.read_bytes() == source.read_bytes()
    prompt = artifact.prompt.read_text(encoding="utf-8")
    assert str(artifact.transcript) in prompt
    assert "continue the latest unresolved user request" in prompt.lower()
    assert "Do not just summarize" in prompt
    assert not ics.is_imported(session, tmp_path / "codex-imports")


def test_import_marker_is_idempotent_but_a_changed_source_can_be_imported_again(tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    source = write_transcript(tmp_path / "store", "limited", cwd=cwd)
    session = ics.parse_session(source)
    assert session is not None
    root = tmp_path / "codex-imports"
    artifact = ics.prepare_import(session, root)

    ics.mark_imported(session, artifact)
    assert ics.is_imported(session, root)

    source.write_text(source.read_text(encoding="utf-8") + "{}\n", encoding="utf-8")
    changed = ics.parse_session(source)
    assert changed is not None
    assert not ics.is_imported(changed, root)


def test_terminal_command_reads_the_prompt_file_without_putting_the_handoff_on_the_command_line(
    tmp_path,
):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    source = write_transcript(tmp_path / "store", "limited", cwd=cwd)
    session = ics.parse_session(source)
    assert session is not None
    artifact = ics.prepare_import(session, tmp_path / "imports")

    args = ics.wt_args([artifact])

    assert args.count("new-tab") == 1
    encoded = args[args.index("-EncodedCommand") + 1]
    command = base64.b64decode(encoded).decode("utf-16-le")
    assert "Get-Content -Raw -LiteralPath" in command
    assert str(artifact.prompt).replace("'", "''") in command
    assert "& codex $prompt" in command
    assert "work in progress" not in " ".join(args)
    assert "Source transcript copy" not in " ".join(args)


def test_list_is_read_only_and_reports_only_unimported_limit_sessions(
    tmp_path, capsys, monkeypatch
):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    store = tmp_path / "store"
    write_transcript(store, "limited", cwd=cwd)
    write_transcript(store, "normal", cwd=cwd, ending="success")
    imports = tmp_path / "imports"

    monkeypatch.setattr(
        ics.subprocess, "run", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError)
    )
    assert ics.main(["--sessions-dir", str(store), "--imports-dir", str(imports), "--list"]) == 0

    output = capsys.readouterr().out
    assert "limited" in output
    assert "normal" not in output
    assert not imports.exists()


def test_successful_launch_marks_the_session_imported(tmp_path, monkeypatch):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    store = tmp_path / "store"
    source = write_transcript(store, "limited", cwd=cwd)
    imports = tmp_path / "imports"
    monkeypatch.setattr(ics, "find_terminal", lambda: r"C:\wt.exe")
    monkeypatch.setattr(ics, "find_codex", lambda: r"C:\codex.cmd")
    monkeypatch.setattr(
        ics.subprocess, "run", lambda *_a, **_k: ics.subprocess.CompletedProcess([], 0)
    )

    assert ics.main(["--sessions-dir", str(store), "--imports-dir", str(imports)]) == 0

    session = ics.parse_session(source)
    assert session is not None
    assert ics.is_imported(session, imports)


def test_a_missing_terminal_does_not_mark_a_session_imported(tmp_path, monkeypatch):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    store = tmp_path / "store"
    source = write_transcript(store, "limited", cwd=cwd)
    imports = tmp_path / "imports"
    monkeypatch.setattr(ics, "find_terminal", lambda: "")

    assert ics.main(["--sessions-dir", str(store), "--imports-dir", str(imports)]) == 1

    session = ics.parse_session(source)
    assert session is not None
    assert not ics.is_imported(session, imports)


def test_a_missing_codex_cli_does_not_mark_or_launch(tmp_path, monkeypatch):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    store = tmp_path / "store"
    source = write_transcript(store, "limited", cwd=cwd)
    imports = tmp_path / "imports"
    monkeypatch.setattr(ics, "find_terminal", lambda: r"C:\wt.exe")
    monkeypatch.setattr(ics, "find_codex", lambda: "")
    monkeypatch.setattr(
        ics.subprocess, "run", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError)
    )

    assert ics.main(["--sessions-dir", str(store), "--imports-dir", str(imports)]) == 1

    session = ics.parse_session(source)
    assert session is not None
    assert not ics.is_imported(session, imports)


def test_config_homes_control_both_stores(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    assert ics.claude_sessions_root() == tmp_path / "claude" / "projects"
    assert ics.imports_root() == tmp_path / "codex" / "imports" / "claude"


def test_an_empty_store_fails_loudly_and_names_the_path(tmp_path, capsys):
    missing = tmp_path / "missing"
    assert ics.main(["--sessions-dir", str(missing), "--list"]) == 1
    assert str(missing) in capsys.readouterr().err


def test_a_zero_count_is_rejected(tmp_path):
    assert ics.main(["--sessions-dir", str(tmp_path), "--count", "0", "--list"]) == 2


def test_workspace_task_wires_the_importer_and_shared_count_picker():
    source = (REPO_ROOT / "workspace-tasks.jsonc").read_text(encoding="utf-8")
    task = source[source.index('"label": "Agents: Import Limited Claude Sessions"') :]
    task = task[: task.index('"problemMatcher"')]
    assert '"scripts/import-claude-sessions.py"' in task
    assert '"${input:resumeSessionCount}"' in task


def test_the_script_is_stdlib_only():
    source = (REPO_ROOT / "scripts" / "import-claude-sessions.py").read_text(encoding="utf-8")
    allowed = {
        "__future__",
        "argparse",
        "base64",
        "dataclasses",
        "json",
        "os",
        "pathlib",
        "re",
        "shutil",
        "subprocess",
        "sys",
        "time",
    }
    for line in source.splitlines():
        if line.startswith(("import ", "from ")):
            module = line.split()[1].split(".")[0]
            assert module in allowed, f"non-stdlib import: {line}"
