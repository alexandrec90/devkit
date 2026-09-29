"""`/go-nuts`: the unattended run, vendored so it follows the operator across machines.

Prose has no compiler, so this pins the clauses the skill exists for. Each one is a way an
unattended run has gone wrong: stopping to ask, leaving no record of what it chose,
starting on its own, or ending in a way the fix pass reads as a dead session.
"""

from __future__ import annotations

from pathlib import Path

from support import load_script

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / ".claude" / "skills" / "go-nuts" / "SKILL.md"
LOG = "logs/overnight.md"

manifest = load_script("scripts/devkit_manifest.py")


def _text() -> str:
    return SKILL.read_text(encoding="utf-8")


def _frontmatter() -> dict[str, str]:
    head = _text().split("---", 2)[1]
    return {
        key.strip(): value.strip()
        for key, _, value in (line.partition(":") for line in head.strip().splitlines())
    }


def test_the_skill_is_vendored():
    assert ".claude/skills/go-nuts/SKILL.md" in manifest.MANIFEST


def test_only_the_user_can_start_an_unattended_run():
    """A model that judged a task "looks long" must not grant itself licence to stop asking."""
    assert _frontmatter()["disable-model-invocation"] == "true"
    assert _frontmatter()["name"] == "go-nuts"


def test_the_goal_is_passed_through():
    assert "$ARGUMENTS" in _text()


def test_it_forbids_asking_and_says_what_to_do_instead():
    text = " ".join(_text().split())
    assert "Never use AskUserQuestion" in text
    assert 'When you would have written "I recommend X", do X.' in text
    assert "cheapest to undo" in text


def test_every_decision_lands_in_an_ignored_append_only_log():
    """The log is how the user reverses a call; it must never be committed or clobbered."""
    text = " ".join(_text().split())
    assert f"Record every judgement call in `{LOG}`" in text
    assert "never replace lines already there" in text
    for gitignore in (ROOT / ".gitignore", ROOT / "templates/core/dot-gitignore.tmpl"):
        assert "logs/" in gitignore.read_text(encoding="utf-8").splitlines(), gitignore


def test_it_ends_through_ship_so_the_pass_does_not_read_it_as_dead():
    text = " ".join(_text().split())
    assert "Finish with the `/ship` skill, even when only part of the goal is done" in text
    assert ".claude/rules/session-scope.md" in text


def test_a_harness_refusal_is_reported_not_routed_around():
    text = " ".join(_text().split())
    assert "not a blocker to route around" in text
    assert ".claude/rules/engineering.md" in text
