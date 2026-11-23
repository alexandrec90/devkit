"""`/ship` is Claude Code's built-in, and nothing here may stand in its way.

The owner's decision (2026-11-23): a person ships by typing `/ship`, it behaves the way
anyone who knows Claude Code expects, and every project is in the direct flow, so the PR
it opens merges as it opens. What this file holds is everything that could quietly undo
that -- a skill taking the name back, a hook gating the commit or the push, a template
generating a gated project, a merger that waits for a label the built-in never applies.
The merger's half is in `scripts/hooks/tests/test_merge_dependabot_prs.py`, the hook's
in `tests/test_git_policy.py`; the cross-project half needs this workstation and is the
`local` tier below.

A session nobody can type `/ship` for -- a fixer, `/go-nuts` -- finishes with the intent
file `.claude/ship-intent.md` describes, and that file's promises are held here too.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from support import (  # also what puts `scripts/` on the path for the imports below
    generate,
    load_script,
    needs_live_workspace,
)

import after_merge
import delivery
import devkit_manifest
import git_policy
import ship_intent
import sweep

ROOT = Path(__file__).resolve().parents[1]
new_project = load_script("scripts/new-project.py")
DOC = ROOT / ".claude" / "ship-intent.md"
SCOPE = ROOT / ".claude" / "rules" / "session-scope.md"
OLD_SKILL = ".claude/skills/ship/SKILL.md"
QUEUE_SECTION = "## Never end by telling the person to do something"


def _flat(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


# --- nothing takes the name ------------------------------------------------------


def _skill_files() -> list[Path]:
    found: list[Path] = []
    for tier in (".claude/skills", ".agents/skills", "templates"):
        found.extend((ROOT / tier).rglob("SKILL.md*"))
    return found


def test_no_skill_here_is_called_ship():
    """A repository skill named `ship` shadows the built-in: the session then sees it as
    `ship-local`, and `/ship` does whatever this repo last wrote instead."""
    for skill in _skill_files():
        assert skill.parent.name != "ship", skill
        head = skill.read_text(encoding="utf-8").split("---", 2)
        name = re.search(r"^name:\s*(\S+)", head[1], re.M) if len(head) > 2 else None
        assert not (name and name.group(1).strip("'\"") == "ship"), skill


def test_no_vendored_path_brings_a_ship_skill_back():
    shipped = [path for path in devkit_manifest.MANIFEST if "/skills/ship/" in path]
    assert shipped == []


def test_every_consumer_loses_its_copy_on_the_next_pull():
    """`--pull` deletes only what the retired list names, and a consumer's stale copy
    would keep shadowing the built-in there."""
    assert OLD_SKILL in devkit_manifest.RETIRED_PATHS
    assert OLD_SKILL.replace(".claude/", ".agents/", 1) in devkit_manifest.RETIRED_PATHS


def test_no_prompt_or_instruction_sends_a_session_to_the_ship_skill():
    """A fixer told to "ship it with the ship skill" now meets the built-in, which only a
    person may start, and stalls on it."""
    instructions = (ROOT / ".claude").rglob("*.md")
    mine = [path for path in instructions if "worktrees" not in path.relative_to(ROOT).parts]
    for path in [*(ROOT / "scripts").glob("*.py"), *mine]:
        assert "ship skill" not in path.read_text(encoding="utf-8"), path


# --- nothing gates it ------------------------------------------------------------


@pytest.mark.parametrize(
    "features",
    [
        pytest.param({}, id="bare"),
        pytest.param({f: True for f in new_project.STACK_FEATURES}, id="everything"),
        pytest.param({f: True for f in new_project.DESKTOP_FEATURES}, id="desktop"),
    ],
)
def test_a_generated_project_is_in_the_direct_flow(tmp_path, features):
    """Rendered by the generator itself, so a template branch cannot hide a gated one."""
    root = generate(tmp_path, features)
    assert delivery.is_direct(root), (root / ".devkit.toml").read_text(encoding="utf-8")


def test_devkit_itself_is_in_the_direct_flow():
    assert delivery.is_direct(ROOT)


@pytest.mark.parametrize(
    "text",
    [
        '[delivery]\nflow = "direct"\n',
        '[delivery]\nflow = "gated"\n',
        "[delivery]\n",
        'flow = "direct"\n',
        "[delivery\n",
        "",
    ],
)
def test_the_hook_and_the_pass_read_the_flow_alike(tmp_path, text):
    """The hook decides whether a commit is gated, the pass whether it ships `merge-now`.
    Read apart, one could call a project direct while the other gates it."""
    (tmp_path / ".devkit.toml").write_text(text, encoding="utf-8")
    assert git_policy.framework.flows_directly(tmp_path) == delivery.is_direct(tmp_path)


def test_a_held_intent_writes_the_line_the_merger_reads():
    """`/ship hold` is gone with the skill, but a held intent still keeps its PR open:
    the merger reads the same line out of the PR body."""
    spec = importlib.util.spec_from_file_location(
        "merger", ROOT / "scripts" / "merge-dependabot-prs.py"
    )
    assert spec is not None and spec.loader is not None
    merger = importlib.util.module_from_spec(spec)
    sys.modules["merger"] = merger
    spec.loader.exec_module(merger)
    body = "Because.\n\nHold: yes\nCloses #3"
    assert ship_intent.asks_hold(body)
    assert merger.held({"body": body, "labels": []})


# --- the unattended finish -------------------------------------------------------


def test_the_reference_is_vendored_and_cited_where_sessions_finish():
    assert ".claude/ship-intent.md" in devkit_manifest.MANIFEST
    for path in (
        ROOT / ".claude" / "fixer.md",
        ROOT / ".claude" / "skills" / "go-nuts" / "SKILL.md",
        SCOPE,
    ):
        assert "ship-intent.md" in path.read_text(encoding="utf-8"), path


FORBIDS_SHIPPING = re.compile(
    r"\b(no|never|not|don't|do not)\b[^.\n]{0,40}"
    r"(git commit|git push|gh pr create|commit, push|open a PR)",
    re.I,
)


def _instruction_files() -> list[Path]:
    found = [ROOT / "CLAUDE.md", *(ROOT / "scripts").glob("CLAUDE.md")]
    for path in (ROOT / ".claude").rglob("*.md"):
        if "worktrees" not in path.relative_to(ROOT).parts:
            found.append(path)
    found.extend((ROOT / "templates").rglob("CLAUDE.md*"))
    return found


def test_no_instruction_forbids_committing_pushing_or_opening_a_pr():
    """The owner's call (2026-11-23): shipping is Claude Code's own business. A rule
    against it is read as authority and fights the built-in `/ship` every time."""
    for path in _instruction_files():
        text = " ".join(path.read_text(encoding="utf-8").split())
        assert not FORBIDS_SHIPPING.search(text), (path, FORBIDS_SHIPPING.search(text))


def test_the_scope_rule_leaves_shipping_to_claude_code():
    assert "Shipping is Claude Code's own business" in _flat(SCOPE)


def test_a_hold_is_the_persons_call_alone():
    text = _flat(DOC)
    assert "`Hold: yes`" in text
    assert "standing alone on a line of its own" in text
    assert "Never add the line on your own judgement" in text
    assert "Automerge: yes" not in text


def _section() -> str:
    return DOC.read_text(encoding="utf-8").split(QUEUE_SECTION, 1)[1]


def test_never_leaving_the_person_a_job_is_the_last_section():
    """The instruction the operator cares most about, and the last thing a session reads
    before it stops: last, so an edit above it cannot bury it."""
    headings = re.findall(r"^## .*$", DOC.read_text(encoding="utf-8"), re.M)
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
    assert (task.when, task.on) == (after_merge.GREEN, after_merge.ANY)


def test_the_section_names_the_elevated_verbs_the_queue_takes():
    for verb in after_merge.ELEVATED_VERBS:
        assert f"`{verb}`" in _section()
    assert f"{after_merge.QUEUE_DIR}/<name>.toml" in _section()


# --- every project on this machine (the local tier) ------------------------------


def _projects() -> list[tuple[str, Path]]:
    workspace = sweep.default_workspace(ROOT)
    names = sweep.parse_workspace(workspace.read_text(encoding="utf-8"))
    return [(name, workspace.parent / name) for name in names]


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)


@needs_live_workspace
def test_every_project_is_direct_on_its_default_branch():
    """Read off origin, not the checkout: a flow edited and never merged gates nothing
    out of anyone's way, and that is how every project sat gated on 2026-11-23."""
    gated = []
    for name, root in _projects():
        head = _run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], root)
        shown = _run(["git", "show", f"{head.stdout.strip()}:.devkit.toml"], root)
        flow = tomllib.loads(shown.stdout).get("delivery", {}) if shown.returncode == 0 else {}
        if flow.get("flow") != delivery.DIRECT:
            gated.append(name)
    assert gated == [], f"not in the direct flow on origin: {gated}"


@needs_live_workspace
def test_no_project_requires_a_check_before_a_merge():
    """Branch protection or a ruleset that requires a check would make GitHub refuse the
    merger's merge, and every `/ship` would sit open behind CI after all."""
    blocked = []
    for name, root in _projects():
        url = _run(["git", "remote", "get-url", "origin"], root).stdout.strip()
        slug = re.sub(r"^.*github\.com[:/]|\.git$", "", url)
        head = _run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], root)
        branch = head.stdout.strip().removeprefix("origin/")
        rules = _run(["gh", "api", f"repos/{slug}/rules/branches/{branch}"], root)
        assert rules.returncode == 0, f"{name}: {rules.stderr.strip()}"
        kinds = {rule.get("type") for rule in json.loads(rules.stdout or "[]")}
        protection = _run(["gh", "api", f"repos/{slug}/branches/{branch}/protection"], root)
        if kinds & {"required_status_checks", "pull_request"} or protection.returncode == 0:
            blocked.append(name)
    assert blocked == [], f"a rule stands between a PR and its merge: {blocked}"
