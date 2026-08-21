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
# A ceiling, not a spend: the run costs what it costs and this only stops a runaway. It
# has to clear *one* turn's floor, which is dominated by the one-off cache write of the
# CLI's own system prompt and tool schemas -- ~53k tokens, and nothing this test
# controls. At 0.10 it did not: a measured run died at `error_max_budget_usd` on turn 1
# having spent 0.109325, so the ceiling failed the suite before a hook was ever reached.
LIVE_BUDGET_USD = os.environ.get("CLAUDE_LIVE_HOOK_BUDGET_USD", "0.35")

# The child command the prompt asks for, hoisted to a constant because a *free* test
# feeds this exact string to `enforce-capped-bash.decide` -- see
# `tests/test_live_smoke_matches_the_gate.py`. The two have to agree about whether the
# gate blocks it, and spending money is the wrong way to find out that they don't.
LIVE_CHILD_COMMAND = (
    'python3 -c "from pathlib import Path; '
    "Path('claude-allowed-sentinel.txt').write_text('ALLOWED', encoding='utf-8')\""
)


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


def test_claude_runs_an_unlisted_command_bare_without_a_denial_retry(tmp_path):
    """The shared rule should make the first Bash call valid, not teach by rejection.

    `python3 -c` is deliberately absent from `NOISY_COMMANDS`, so the *correct* first
    call is the bare one. This asserted the opposite until 2026-08-21 -- it was written
    on 2026-08-16 against the prove-every-call gate, `c452755` inverted that gate on
    2026-08-18 into a nine-command blocklist, and this test was not part of the
    inversion. It has demanded the wrapper on a command nothing blocks ever since, which
    is the reflex `engineering.md` measured at 42% of one month's Bash calls: the suite
    would only have gone green by the agent doing the thing the rule now calls a mistake.

    So the prompt still says "in the way the project instructions require" -- the point
    is to tempt a wrap and watch the rule prevent it. A prompt that said "run it exactly"
    would prove nothing about the rule.
    """
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
                f"way the project instructions require: {LIVE_CHILD_COMMAND}. Use Bash "
                "exactly once and no other tool. Then reply with exactly "
                "CLAUDE_CAPPED_SHELL_OK."
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
    # Not a skip: the run proved nothing, and a ceiling below one turn's floor is a
    # defect in the ceiling. Read off the JSON here because the raw `diagnostic` buries
    # it -- the failure this replaces was a wall of usage counters whose only actionable
    # token was `"subtype":"error_max_budget_usd"`.
    if response.get("subtype") == "error_max_budget_usd":
        pytest.fail(
            f"the run hit its ${LIVE_BUDGET_USD} ceiling after "
            f"{response.get('num_turns')} turn(s), having spent "
            f"${response.get('total_cost_usd')}, so no hook was exercised. The ceiling is "
            "a cost guard and not an assertion: raise LIVE_BUDGET_USD (or set "
            "CLAUDE_LIVE_HOOK_BUDGET_USD) above one turn's floor, which is dominated by "
            "the one-off cache write of the CLI's own system prompt.\n\n" + diagnostic
        )
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
    # Bare, both ways round: the gate did not block it (no `enforce-capped-bash` anywhere
    # in the transcript, so there was no denial-and-retry), and the agent did not wrap it
    # anyway. The second half is the one with teeth -- a reflex wrap is invisible from
    # the exit code, costs a subprocess per call, and nothing else in the suite sees it.
    assert "invoke-capped.py" not in command, diagnostic
    assert "enforce-capped-bash" not in diagnostic
