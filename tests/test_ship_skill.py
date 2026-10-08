"""`/ship`: the top-level session ships, a subagent never does.

The skill stays model-invocable: a fixer, `/go-nuts` and `/supervise-fix-pass` have
nobody to type `/ship` for them, and their prompts send them through the skill. What
`session-scope.md` rules out is a subagent -- one of many fanned out by a workflow --
shipping its own slice, which would leave an intent describing part of the change, or
several racing to write the one file the pass reads.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / ".claude" / "skills" / "ship" / "SKILL.md"
SCOPE = ROOT / ".claude" / "rules" / "session-scope.md"


def _frontmatter() -> dict[str, str]:
    head = SKILL.read_text(encoding="utf-8").split("---", 2)[1]
    return {
        key.strip(): value.strip()
        for key, _, value in (line.partition(":") for line in head.strip().splitlines())
    }


def test_an_unattended_session_can_invoke_ship():
    assert _frontmatter()["disable-model-invocation"] == "false"
    assert _frontmatter()["name"] == "ship"


def test_the_scope_rule_keeps_subagents_from_shipping():
    scope = " ".join(SCOPE.read_text(encoding="utf-8").split())
    assert (
        "only the top-level session ships; a subagent never invokes `/ship` or writes "
        "`logs/ship-intent.md`" in scope
    )
