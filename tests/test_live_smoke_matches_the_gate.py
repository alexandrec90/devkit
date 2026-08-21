"""Free coupling between the paid Claude smoke and the gate it claims to describe.

`tests/test_claude_hooks_live.py` spends money, so it is `-m "not paid"` by default and
runs only when someone clicks the task. That made it the one test in the repo that could
go stale without anything turning red: it was written on 2026-08-16 against the
prove-every-call Bash gate, `c452755` inverted that gate on 2026-08-18 into a closed
nine-command blocklist, and the smoke went on asserting that the agent wraps a command
the blocklist does not name -- a green run would have required the agent to make the
exact mistake `.claude/rules/engineering.md` now spends a section warning against.

The tests here are the cheap half of that pair. They launch nothing and cost nothing, so
they run in every PR gate, and they fail the moment the smoke's expectation and the
shipped gate disagree again -- in either direction.
"""

import json

import test_claude_hooks_live as smoke
from support import REPO_ROOT, load_script

hook = load_script("scripts/hooks/enforce-capped-bash.py")

SMOKE_SOURCE = REPO_ROOT / "tests" / "test_claude_hooks_live.py"


def _verdict(command: str) -> int:
    raw = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    return hook.decide(raw, max_bytes=4000)[0]


def test_the_gate_allows_the_command_the_smoke_prompts_for():
    """The smoke's whole claim is "the first call is valid, not taught by rejection".
    If the gate blocks this command the claim is false before the CLI even starts, and
    the only thing the paid run would report is a denial-and-retry."""
    assert _verdict(smoke.LIVE_CHILD_COMMAND) == 0


def test_the_smoke_asserts_the_absence_of_the_wrapper():
    """Reversion check for the 2026-08-18 inversion. `python3 -c` is not on the
    blocklist, so wrapping it is the reflex the rule measured at 42% of a month's Bash
    calls -- the smoke has to demand its absence, never its presence."""
    source = SMOKE_SOURCE.read_text(encoding="utf-8")
    assert 'assert "invoke-capped.py" not in command' in source
    assert 'assert "invoke-capped.py" in command' not in source
    assert 'assert "scripts/hooks/invoke-capped.py" in command' not in source


def test_the_gate_still_blocks_something_so_the_allow_is_not_vacuous():
    """Guards the guard: a gate that allowed everything would pass the first test here
    while proving nothing. `cat` is on the closed list and is the smoke's foil."""
    assert _verdict("cat scripts/hooks/enforce-capped-bash.py") == hook.EXIT_BLOCK


def test_the_prompt_and_the_sentinel_assertion_name_the_same_file():
    """The prompt is built from `LIVE_CHILD_COMMAND` while the assertion spells the
    filename out, so editing one and not the other yields a paid run that fails on a
    missing file for a reason that has nothing to do with any hook."""
    assert "claude-allowed-sentinel.txt" in smoke.LIVE_CHILD_COMMAND
    assert "claude-allowed-sentinel.txt" in SMOKE_SOURCE.read_text(encoding="utf-8")
