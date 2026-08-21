"""Tests for native-importing usage-limited Claude sessions into Codex."""

import json
import os
from pathlib import Path

import pytest

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


def parsed_session(tmp_path, session_id="limited"):
    cwd = tmp_path / "repo"
    cwd.mkdir(exist_ok=True)
    source = write_transcript(tmp_path / "store", session_id, cwd=cwd)
    session = ics.parse_session(source)
    assert session is not None
    return session


def test_parse_accepts_the_real_claude_limit_record_shape(tmp_path):
    session = parsed_session(tmp_path)
    assert (session.session_id, session.prompt, session.mtime) == (
        "limited",
        "finish the interrupted task",
        1_000.0,
    )
    assert "session limit" in session.limit_message


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
    assert [item.session_id for item in ics.select(ics.collect(store), 4)] == [
        "s2",
        "s3",
        "s4",
        "s5",
    ]


def detected_payload(session, *extra):
    return {
        "items": [
            {"itemType": "CONFIG", "details": None},
            {
                "itemType": "SESSIONS",
                "description": "Detected Claude sessions",
                "cwd": None,
                "details": {
                    "sessions": [
                        {
                            "path": str(session.source),
                            "cwd": str(session.cwd),
                            "title": session.prompt,
                        },
                        *extra,
                    ],
                    "plugins": [],
                },
            },
        ]
    }


def test_detected_migration_payload_is_filtered_to_the_selected_sessions(tmp_path):
    session = parsed_session(tmp_path)
    item = ics.select_detected_items(
        detected_payload(
            session,
            {"path": str(tmp_path / "other.jsonl"), "cwd": str(session.cwd)},
        ),
        [session],
    )[0]
    assert item["itemType"] == "SESSIONS"
    assert item["details"]["sessions"] == [
        {"path": str(session.source), "cwd": str(session.cwd), "title": session.prompt}
    ]


def test_an_already_imported_session_can_be_absent_from_native_detection(tmp_path):
    session = parsed_session(tmp_path)
    assert ics.select_detected_items({"items": []}, [session]) == []


class FakeClient:
    def __init__(self, histories, detected, completed=None):
        self.histories = histories
        self.detected = detected
        self.completed = completed or {
            "importId": "import-1",
            "itemTypeResults": [{"itemType": "SESSIONS", "successes": [], "failures": []}],
        }
        self.requests = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def request(self, method, params):
        self.requests.append((method, params))
        if method == "externalAgentConfig/detect":
            return self.detected
        if method == "externalAgentConfig/import":
            return {"importId": "import-1"}
        assert method == "externalAgentConfig/import/readHistories"
        return self.histories

    def notification(self, method, import_id):
        assert (method, import_id) == ("externalAgentConfig/import/completed", "import-1")
        return self.completed


def test_native_import_resolves_an_existing_thread_without_importing_it_again(
    tmp_path, monkeypatch
):
    session = parsed_session(tmp_path)
    source = "\\\\?\\" + str(session.source)
    fake = FakeClient(
        {
            "data": [
                {
                    "successes": [
                        {
                            "itemType": "SESSIONS",
                            "source": source,
                            "target": "codex-thread-id",
                        }
                    ]
                }
            ]
        },
        {"items": []},
    )
    monkeypatch.setattr(ics, "AppServerClient", lambda _codex: fake)

    imported = ics.native_import("codex.cmd", [session])

    assert imported == [ics.ImportedSession(session, "codex-thread-id")]
    detect_method, detect_params = fake.requests[0]
    assert detect_method == "externalAgentConfig/detect"
    assert detect_params["migrationSource"] == "claude"
    assert [method for method, _params in fake.requests] == [
        "externalAgentConfig/detect",
        "externalAgentConfig/import/readHistories",
    ]


def test_native_import_imports_a_detected_session_through_the_claude_migration(
    tmp_path, monkeypatch
):
    session = parsed_session(tmp_path)
    completed = {
        "importId": "import-1",
        "itemTypeResults": [
            {
                "itemType": "SESSIONS",
                "successes": [
                    {
                        "itemType": "SESSIONS",
                        "source": str(session.source),
                        "target": "new-codex-thread-id",
                    }
                ],
                "failures": [],
            }
        ],
    }
    fake = FakeClient({"data": []}, detected_payload(session), completed)
    monkeypatch.setattr(ics, "AppServerClient", lambda _codex: fake)

    imported = ics.native_import("codex.cmd", [session])

    assert imported == [ics.ImportedSession(session, "new-codex-thread-id")]
    method, params = fake.requests[1]
    assert method == "externalAgentConfig/import"
    assert params["migrationSource"] == "claude"
    assert params["migrationItems"][0]["details"]["sessions"][0]["path"] == str(session.source)


def test_native_import_fails_if_no_resumable_thread_is_returned(tmp_path, monkeypatch):
    session = parsed_session(tmp_path)
    monkeypatch.setattr(
        ics,
        "AppServerClient",
        lambda _codex: FakeClient({"data": []}, detected_payload(session)),
    )
    with pytest.raises(ics.AppServerError, match="no resumable Codex thread"):
        ics.native_import("codex.cmd", [session])


def test_native_import_surfaces_importer_failures(tmp_path, monkeypatch):
    session = parsed_session(tmp_path)
    completed = {
        "importId": "import-1",
        "itemTypeResults": [
            {
                "itemType": "SESSIONS",
                "successes": [],
                "failures": [{"message": "unsupported transcript"}],
            }
        ],
    }
    monkeypatch.setattr(
        ics,
        "AppServerClient",
        lambda _codex: FakeClient({"data": []}, detected_payload(session), completed),
    )
    with pytest.raises(ics.AppServerError, match="unsupported transcript"):
        ics.native_import("codex.cmd", [session])


def test_terminal_tabs_resume_the_native_codex_thread_ids(tmp_path):
    session = parsed_session(tmp_path)
    args = ics.wt_args([ics.ImportedSession(session, "codex-thread-id")])
    assert args.count("new-tab") == 1
    assert args[args.index("-Command") + 1 :] == [
        "codex",
        "resume",
        "codex-thread-id",
        ";",
        "focus-tab",
        "-t",
        "0",
    ]
    assert "Continue the interrupted" not in " ".join(args)


def test_list_is_read_only_and_reports_only_limit_sessions(tmp_path, capsys, monkeypatch):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    store = tmp_path / "store"
    write_transcript(store, "limited", cwd=cwd)
    write_transcript(store, "normal", cwd=cwd, ending="success")
    monkeypatch.setattr(ics, "native_import", lambda *_a: (_ for _ in ()).throw(AssertionError))
    assert ics.main(["--sessions-dir", str(store), "--list"]) == 0
    output = capsys.readouterr().out
    assert "limited" in output
    assert "normal" not in output


def test_successful_native_import_launches_resume_tabs(tmp_path, monkeypatch):
    parsed_session(tmp_path)
    launches = []
    monkeypatch.setattr(ics, "find_terminal", lambda: r"C:\wt.exe")
    monkeypatch.setattr(ics, "find_codex", lambda: r"C:\codex.cmd")
    monkeypatch.setattr(
        ics, "native_import", lambda codex, sessions: [ics.ImportedSession(sessions[0], "t1")]
    )
    monkeypatch.setattr(
        ics.subprocess,
        "run",
        lambda args, **_kwargs: launches.append(args) or ics.subprocess.CompletedProcess(args, 0),
    )
    assert ics.main(["--sessions-dir", str(tmp_path / "store")]) == 0
    assert launches[0][0] == r"C:\wt.exe"
    assert launches[0][-7:-4] == ["codex", "resume", "t1"]


def test_missing_tools_do_not_attempt_a_native_import(tmp_path, monkeypatch):
    parsed_session(tmp_path)
    monkeypatch.setattr(ics, "find_terminal", lambda: "")
    monkeypatch.setattr(ics, "native_import", lambda *_a: (_ for _ in ()).throw(AssertionError))
    assert ics.main(["--sessions-dir", str(tmp_path / "store")]) == 1


def test_claude_config_home_controls_the_source_store(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    assert ics.claude_sessions_root() == tmp_path / "claude" / "projects"


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
        "dataclasses",
        "json",
        "os",
        "pathlib",
        "queue",
        "re",
        "shutil",
        "subprocess",
        "sys",
        "threading",
        "time",
    }
    for line in source.splitlines():
        if line.startswith(("import ", "from ")):
            module = line.split()[1].split(".")[0]
            assert module in allowed, f"non-stdlib import: {line}"
