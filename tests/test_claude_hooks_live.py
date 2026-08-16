"""Opt-in black-box smoke tests for the capped-Bash policy in Claude Code."""

import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
from support import REPO_ROOT

pytestmark = [
    pytest.mark.claude_live,
    pytest.mark.paid,
]

LIVE_MODEL = os.environ.get("CLAUDE_LIVE_HOOK_MODEL", "haiku")
LIVE_EFFORT = os.environ.get("CLAUDE_LIVE_HOOK_EFFORT", "low")
LIVE_BUDGET_USD = os.environ.get("CLAUDE_LIVE_HOOK_BUDGET_USD", "0.10")


def _claude_or_skip() -> str:
    claude = shutil.which("claude")
    if claude is None:
        pytest.skip("Claude Code CLI is not installed")
    return claude


def _initialize_project(tmp_path: Path) -> None:
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    hooks_dir = tmp_path / "scripts/hooks"
    hooks_dir.mkdir(parents=True)
    for name in ("enforce-capped-bash.py", "harness_config.py", "invoke-capped.py"):
        shutil.copyfile(REPO_ROOT / "scripts/hooks" / name, hooks_dir / name)

    rules_dir = tmp_path / ".claude/rules"
    rules_dir.mkdir(parents=True)
    shutil.copyfile(
        REPO_ROOT / ".claude/rules/engineering.md",
        rules_dir / "engineering.md",
    )
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
                stream.write(json.dumps(payload) + "\\n")
            """
        ),
        encoding="utf-8",
        newline="",
    )
    (tmp_path / ".claude/settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "^Bash$",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": (
                                        'python3 "${CLAUDE_PROJECT_DIR:-.}/pretool-recorder.py"'
                                    ),
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
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
        newline="",
    )


def test_claude_uses_the_cap_wrapper_without_a_denial_retry(tmp_path):
    """The shared rule should make the first Bash call valid, not teach by rejection."""
    claude = _claude_or_skip()
    _initialize_project(tmp_path)
    result = subprocess.run(
        [
            claude,
            "--print",
            "--output-format",
            "json",
            "--model",
            LIVE_MODEL,
            "--effort",
            LIVE_EFFORT,
            "--max-budget-usd",
            LIVE_BUDGET_USD,
            "--max-turns",
            "3",
            "--no-session-persistence",
            "--setting-sources",
            "project",
            "--dangerously-skip-permissions",
            "--disable-slash-commands",
            "--no-chrome",
            "--tools=Bash",
            (
                "Create claude-allowed-sentinel.txt by running this child command in the "
                'way the project instructions require: python3 -c "from pathlib import '
                "Path; Path('claude-allowed-sentinel.txt').write_text('ALLOWED', "
                "encoding='utf-8')\". Use Bash exactly once and no other tool. Then reply "
                "with exactly CLAUDE_CAPPED_SHELL_OK."
            ),
        ],
        cwd=tmp_path,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env=os.environ.copy(),
        timeout=240,
        check=False,
    )
    diagnostic = f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"

    response = json.loads(result.stdout)
    if response.get("terminal_reason") == "api_error" and response.get("api_error_status") == 429:
        pytest.skip(f"Claude capacity unavailable: {response.get('result')}")
    assert result.returncode == 0, diagnostic
    assert response["result"] == "CLAUDE_CAPPED_SHELL_OK", diagnostic

    sentinel = tmp_path / "claude-allowed-sentinel.txt"
    assert sentinel.is_file(), diagnostic
    assert sentinel.read_text(encoding="utf-8") == "ALLOWED", diagnostic

    records_path = tmp_path / "shell-calls.jsonl"
    assert records_path.is_file(), diagnostic
    records = [json.loads(line) for line in records_path.read_text(encoding="utf-8").splitlines()]
    shell_calls = [record for record in records if record.get("tool_name") == "Bash"]
    assert len(shell_calls) == 1, diagnostic
    command = shell_calls[0]["tool_input"]["command"]
    assert "scripts/hooks/invoke-capped.py" in command
    assert "enforce-capped-bash" not in diagnostic
