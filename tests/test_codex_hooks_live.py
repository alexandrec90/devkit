"""Opt-in black-box smoke test for generated hooks in the real Codex CLI."""

import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
from support import REPO_ROOT, load_script

hook = load_script("scripts/sync-codex-hooks.py")

pytestmark = [
    pytest.mark.codex_live,
    pytest.mark.paid,
]


def test_project_hooks_are_discovered_and_block_a_real_tool_call(tmp_path):
    """Exercise repo discovery, generation, adapter execution, and denial together."""
    codex = shutil.which("codex")
    if codex is None:
        pytest.skip("codex CLI is not installed")

    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    scripts_dir = tmp_path / "scripts/hooks"
    scripts_dir.mkdir(parents=True)
    shutil.copyfile(
        REPO_ROOT / "scripts/hooks/codex-hook-adapter.py",
        scripts_dir / "codex-hook-adapter.py",
    )
    (tmp_path / "hook-recorder.py").write_text(
        textwrap.dedent(
            """\
            import json
            import sys
            from pathlib import Path

            payload = json.load(sys.stdin)
            event = payload.get("hook_event_name") or payload.get("hookEventName")
            with (Path(__file__).parent / "hook-events.jsonl").open(
                "a", encoding="utf-8", newline=""
            ) as stream:
                stream.write(
                    json.dumps(
                        {
                            "event": event,
                            "tool_name": payload.get("tool_name"),
                            "tool_input": payload.get("tool_input"),
                        }
                    )
                    + "\\n"
                )

            if event == "PreToolUse":
                print(
                    json.dumps(
                        {
                            "hookSpecificOutput": {
                                "hookEventName": "PreToolUse",
                                "permissionDecision": "deny",
                                "permissionDecisionReason": (
                                    "Expected smoke-test denial. Do not retry with another tool."
                                ),
                            }
                        }
                    )
                )
            else:
                print("{}")
            """
        ),
        encoding="utf-8",
        newline="",
    )
    handler = {
        "hooks": [
            {
                "type": "command",
                "command": 'python3 "${CLAUDE_PROJECT_DIR:-.}/hook-recorder.py"',
            }
        ]
    }
    claude_settings = {
        "hooks": {
            **{event: [handler] for event in ("SessionStart", "UserPromptSubmit", "Stop")},
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {
                            "type": "command",
                            "command": 'python3 "${CLAUDE_PROJECT_DIR:-.}/hook-recorder.py"',
                        }
                    ],
                }
            ],
        }
    }
    generated = hook.to_codex_hooks(claude_settings)
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    (codex_dir / "hooks.json").write_text(
        json.dumps(generated, indent=2) + "\n",
        encoding="utf-8",
        newline="",
    )
    for groups in generated["hooks"].values():
        for group in groups:
            for command_hook in group["hooks"]:
                assert "commandWindows" in command_hook

    # `--ignore-user-config` does not isolate ~/.codex/hooks.json. Without a clean
    # CODEX_HOME, a workstation guard can block the sentinel and make this project
    # hook test look green even though its recorder never ran. Carry authentication
    # only; project config and hooks still come from tmp_path.
    source_codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    isolated_codex_home = tmp_path / ".codex-home"
    isolated_codex_home.mkdir()
    project_key = str(tmp_path).lower()
    (isolated_codex_home / "config.toml").write_text(
        f"[projects.'{project_key}']\ntrust_level = \"trusted\"\n",
        encoding="utf-8",
        newline="",
    )
    auth_file = source_codex_home / "auth.json"
    if auth_file.is_file():
        shutil.copyfile(auth_file, isolated_codex_home / "auth.json")
    clean_env = {**os.environ, "CODEX_HOME": str(isolated_codex_home)}

    result = subprocess.run(
        [
            codex,
            "exec",
            "--dangerously-bypass-hook-trust",
            "--enable",
            "hooks",
            "--ephemeral",
            "--ignore-rules",
            "--sandbox",
            "workspace-write",
            "--color",
            "never",
            (
                "Use the shell tool exactly once to run a command that creates a file named "
                "blocked-sentinel.txt in the current directory. Do not use apply_patch or any "
                "other tool. The repository hook is expected to deny the shell call. After it "
                "is denied, do not retry; reply with exactly CODEX_HOOK_BLOCKED_OK."
            ),
        ],
        cwd=tmp_path,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env=clean_env,
        timeout=240,
        check=False,
    )
    diagnostic = f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"

    assert result.returncode == 0, diagnostic
    assert "CODEX_HOOK_BLOCKED_OK" in result.stdout, diagnostic
    assert not (tmp_path / "blocked-sentinel.txt").exists(), diagnostic
    events_path = tmp_path / "hook-events.jsonl"
    assert events_path.is_file(), diagnostic
    records = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    events = {record["event"] for record in records}
    assert {"SessionStart", "UserPromptSubmit", "PreToolUse", "Stop"} <= events, diagnostic
    assert any(
        record["event"] == "PreToolUse" and record["tool_name"] == "Bash" for record in records
    ), diagnostic
