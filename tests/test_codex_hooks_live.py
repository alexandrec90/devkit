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

LIVE_MODEL = os.environ.get("CODEX_LIVE_HOOK_MODEL", "gpt-5.6-luna")
LIVE_REASONING_EFFORT = os.environ.get("CODEX_LIVE_HOOK_REASONING_EFFORT", "low")


def _codex_or_skip() -> str:
    codex = shutil.which("codex")
    if codex is None:
        pytest.skip("codex CLI is not installed")
    return codex


def _initialize_repo(tmp_path: Path) -> Path:
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
    return scripts_dir


def _write_generated_hooks(tmp_path: Path, claude_settings: dict) -> dict:
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
    return generated


def _isolated_codex_env(tmp_path: Path) -> dict[str, str]:
    """Keep workstation settings out while retaining the authentication under test.

    The smoke's cheap model settings belong to this disposable Codex home. Putting
    them here makes their lifetime and scope explicit: normal sessions continue to
    read the operator's own config, and the Codex command needs no preference
    overrides that could be mistaken for human-session defaults.
    """
    source_codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    isolated_codex_home = tmp_path / ".codex-home"
    isolated_codex_home.mkdir()
    project_key = str(tmp_path).lower()
    (isolated_codex_home / "config.toml").write_text(
        f"model = {json.dumps(LIVE_MODEL)}\n"
        f"model_reasoning_effort = {json.dumps(LIVE_REASONING_EFFORT)}\n"
        'model_reasoning_summary = "none"\n'
        'model_verbosity = "low"\n\n'
        f"[projects.'{project_key}']\n"
        'trust_level = "trusted"\n',
        encoding="utf-8",
        newline="",
    )
    auth_file = source_codex_home / "auth.json"
    if auth_file.is_file():
        shutil.copyfile(auth_file, isolated_codex_home / "auth.json")
    return {**os.environ, "CODEX_HOME": str(isolated_codex_home)}


def _run_codex(codex: str, tmp_path: Path, clean_env: dict[str, str], prompt: str):
    return subprocess.run(
        [
            codex,
            "exec",
            "--dangerously-bypass-hook-trust",
            "--approve-for-me",
            "--enable",
            "hooks",
            "--ephemeral",
            "--ignore-rules",
            "--color",
            "never",
            prompt,
        ],
        cwd=tmp_path,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env=clean_env,
        timeout=240,
        check=False,
    )


def test_project_hooks_are_discovered_and_block_a_real_tool_call(tmp_path):
    """Exercise repo discovery, generation, adapter execution, and denial together."""
    codex = _codex_or_skip()
    _initialize_repo(tmp_path)
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
    _write_generated_hooks(tmp_path, claude_settings)

    # `--ignore-user-config` does not isolate ~/.codex/hooks.json. Without a clean
    # CODEX_HOME, a workstation guard can block the sentinel and make this project
    # hook test look green even though its recorder never ran. Carry authentication
    # only; the temporary home owns the smoke's model preferences, while project
    # config and hooks still come from tmp_path.
    clean_env = _isolated_codex_env(tmp_path)
    result = _run_codex(
        codex,
        tmp_path,
        clean_env,
        (
            "Use the shell tool exactly once to run a command that creates a file named "
            "blocked-sentinel.txt in the current directory. Do not use apply_patch or any "
            "other tool. The repository hook is expected to deny the shell call. After it "
            "is denied, do not retry; reply with exactly CODEX_HOOK_BLOCKED_OK."
        ),
    )
    diagnostic = f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"

    assert result.returncode == 0, diagnostic
    assert "CODEX_HOOK_BLOCKED_OK" in result.stdout, diagnostic
    assert "Expected smoke-test denial" in result.stderr, diagnostic
    assert not (tmp_path / "blocked-sentinel.txt").exists(), diagnostic
    events_path = tmp_path / "hook-events.jsonl"
    assert events_path.is_file(), diagnostic
    records = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    events = {record["event"] for record in records}
    assert {"SessionStart", "UserPromptSubmit", "PreToolUse", "Stop"} <= events, diagnostic
    assert any(
        record["event"] == "PreToolUse" and record["tool_name"] == "Bash" for record in records
    ), diagnostic


def test_claude_bash_cap_cannot_trigger_a_codex_retry_loop(tmp_path):
    """One direct command must remain one call when Claude's cap is in the source."""
    codex = _codex_or_skip()
    scripts_dir = _initialize_repo(tmp_path)
    for name in ("enforce-capped-bash.py", "harness_config.py", "invoke-capped.py"):
        shutil.copyfile(REPO_ROOT / "scripts/hooks" / name, scripts_dir / name)

    (tmp_path / "pretool-recorder.py").write_text(
        textwrap.dedent(
            """\
            import json
            import sys
            from pathlib import Path

            payload = json.load(sys.stdin)
            with (Path(__file__).parent / "shell-calls.jsonl").open(
                "a", encoding="utf-8", newline=""
            ) as stream:
                stream.write(
                    json.dumps(
                        {
                            "tool_name": payload.get("tool_name"),
                            "tool_input": payload.get("tool_input"),
                        }
                    )
                    + "\\n"
                )
            print("{}")
            """
        ),
        encoding="utf-8",
        newline="",
    )
    claude_settings = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {
                            "type": "command",
                            "command": ('python3 "${CLAUDE_PROJECT_DIR:-.}/pretool-recorder.py"'),
                        },
                        {
                            "type": "command",
                            "command": (
                                'python3 "${CLAUDE_PROJECT_DIR:-.}/scripts/hooks/'
                                'enforce-capped-bash.py"'
                            ),
                        },
                    ],
                }
            ]
        }
    }
    generated = _write_generated_hooks(tmp_path, claude_settings)
    clean_env = _isolated_codex_env(tmp_path)
    result = _run_codex(
        codex,
        tmp_path,
        clean_env,
        (
            "Use the shell tool exactly once. Run this exact command and no other command: "
            'python3 -c "from pathlib import Path; '
            "Path('allowed-sentinel.txt').write_text('ALLOWED', encoding='utf-8')\". "
            "Do not use apply_patch or another tool. After it succeeds, reply with exactly "
            "CODEX_DIRECT_SHELL_OK."
        ),
    )
    diagnostic = f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"

    assert result.returncode == 0, diagnostic
    assert "CODEX_DIRECT_SHELL_OK" in result.stdout, diagnostic
    sentinel = tmp_path / "allowed-sentinel.txt"
    assert sentinel.is_file(), diagnostic
    assert sentinel.read_text(encoding="utf-8") == "ALLOWED", diagnostic

    records_path = tmp_path / "shell-calls.jsonl"
    assert records_path.is_file(), diagnostic
    records = [json.loads(line) for line in records_path.read_text(encoding="utf-8").splitlines()]
    shell_calls = [record for record in records if record["tool_name"] == "Bash"]
    assert len(shell_calls) == 1, diagnostic

    evidence = json.dumps({"generated": generated, "calls": shell_calls, "output": diagnostic})
    assert "enforce-capped-bash.py" not in evidence
    assert "invoke-capped.py" not in evidence
