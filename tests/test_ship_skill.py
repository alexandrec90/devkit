"""`/ship`: the top-level session ships, a subagent never does.

The skill stays model-invocable: a fixer, `/go-nuts` and `/supervise-fix-pass` have
nobody to type `/ship` for them, and their prompts send them through the skill. What
`session-scope.md` rules out is a subagent -- one of many fanned out by a workflow --
shipping its own slice, which would leave an intent describing part of the change, or
several racing to write the one file the pass reads.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import after_merge
import ship_intent

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / ".claude" / "skills" / "ship" / "SKILL.md"
SCOPE = ROOT / ".claude" / "rules" / "session-scope.md"
QUEUE_SECTION = "## Never end by telling the person to do something"


def _frontmatter() -> dict[str, str]:
    head = SKILL.read_text(encoding="utf-8").split("---", 2)[1]
    return {
        key.strip(): value.strip()
        for key, _, value in (line.partition(":") for line in head.strip().splitlines())
    }


def test_an_unattended_session_can_invoke_ship():
    assert _frontmatter()["disable-model-invocation"] == "false"
    assert _frontmatter()["name"] == "ship"


def test_ship_auto_writes_the_trailer_the_pass_labels_automerge():
    """`/ship auto` reaches the skill as `$ARGUMENTS`, and the line it says to write is
    the one `ship_intent.asks_automerge` reads; a drift on either side is a PR that
    quietly waits for the review the person said to skip."""
    text = SKILL.read_text(encoding="utf-8")
    assert "$ARGUMENTS" in text
    assert "auto" in _frontmatter()["argument-hint"]
    assert "`Automerge: yes`" in text
    assert ship_intent.asks_automerge("Because.\n\nAutomerge: yes")
    assert "never add the line on your own judgement" in " ".join(text.split())


def test_ship_hold_writes_the_trailer_that_keeps_the_merge_label_off():
    """The direct flow merges what nobody held, so a drift here is a PR that merges past
    the person who said to wait."""
    text = " ".join(SKILL.read_text(encoding="utf-8").split())
    assert "hold" in _frontmatter()["argument-hint"]
    assert "`Hold: yes`" in text
    assert ship_intent.asks_hold("Because.\n\nHold: yes")
    assert "The same rule: never add the line on your own judgement" in text
    assert "standing alone on a line of its own" in text


def _section() -> str:
    """The skill from `QUEUE_SECTION` on."""
    return SKILL.read_text(encoding="utf-8").split(QUEUE_SECTION, 1)[1]


def test_never_leaving_the_person_a_job_is_the_skills_last_section():
    """The instruction the operator cares most about, and the last thing a session reads
    before it stops. Last so an edit to the flow above it cannot bury it, and held here so
    one cannot drop it: a session that loses this section goes back to ending on "you
    need to", which is exactly what this test fails on."""
    headings = re.findall(r"^## .*$", SKILL.read_text(encoding="utf-8"), re.M)
    assert headings[-1] == QUEUE_SECTION


def test_the_section_says_each_thing_a_session_has_to_be_told():
    text = " ".join(_section().split())
    for clause in (
        "a session NEVER ends with",
        "Anything that must happen later goes in a task file, in the same change",
        "Anything that does not fit, a case nobody thought of, is a plain `agent` task",
        "written for a session with no memory of this one",
        "A session with no code change still commits that one file",
        "Only what truly needs the human, a payment or a login, reaches them",
        "through a task the agent attempts first",
        "an edit never runs a finished one again",
        "to run something again, add a new file",
    ):
        assert clause in text, clause


def test_the_example_is_a_task_the_queue_accepts():
    """An example the parser refuses would teach every session to write a malformed file."""
    example = re.search(r"```toml\n(.*?)```", _section(), re.S)
    assert example is not None
    task = after_merge.parse(
        "project", f"{after_merge.QUEUE_DIR}/name.toml", "a" * 40, example.group(1)
    )
    assert not task.malformed, task.malformed
    assert (task.when, task.on) == (after_merge.GREEN, after_merge.ANY), (
        "the section calls these the defaults"
    )


def test_the_section_names_the_elevated_verbs_the_queue_takes():
    for verb in after_merge.ELEVATED_VERBS:
        assert f"`{verb}`" in _section()
    assert f"{after_merge.QUEUE_DIR}/<name>.toml" in _section()


def test_the_scope_rule_keeps_subagents_from_shipping():
    scope = " ".join(SCOPE.read_text(encoding="utf-8").split())
    assert (
        "only the top-level session ships; a subagent never invokes `/ship` or writes "
        "`logs/ship-intent.md`" in scope
    )
