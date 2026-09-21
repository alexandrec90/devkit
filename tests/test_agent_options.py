"""`scripts/agent-options.py`: the three dropdowns, and the wiring that chains them.

The catalogue reading is tested next door in `tests/test_agent_models.py`. What is here
is the picker CLI over it -- that stdout is the quick-pick and nothing else may reach it,
that the agent rows the three tasks share still differ where they have to, and that the
workspace file resolves the three stages in the order the extension needs.
"""

from __future__ import annotations

import json
import re
import sys

from support import REPO_ROOT, load_script

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import agent_models as am
import devkit_project
from test_agent_models import write_caches

options = load_script("scripts/agent-options.py")


def run(argv, capsys, home=None, monkeypatch=None):
    """One verb, with the catalogues pointed at a fixture home. Returns the rows."""
    if home is not None:
        monkeypatch.setenv("DEVKIT_AGENT_HOME", str(home))
    assert options.main(argv) == 0
    return [line for line in capsys.readouterr().out.splitlines() if line]


def values(rows):
    return [row.split("|")[0] for row in rows]


# --- the agent question --------------------------------------------------------------


def test_the_three_tasks_share_one_agent_list_and_still_differ_where_they_must():
    """One script draws all three, which is the point: the lists were spelled three times
    in the workspace file and had already drifted. What may differ is what is genuinely
    different -- only the fix pass can send work nobody is watching, so only it offers a
    background mode, and `codex-bg` is not a row at all because Codex has no such thing.
    """
    assert values(options.agent_rows("fix")) == ["claude", "claude-bg", "codex"]
    assert values(options.agent_rows("worktree")) == ["claude", "codex"]
    assert values(options.agent_rows("resume")) == ["claude", "codex"]
    assert "claude-bg" not in values(options.agent_rows("worktree"))


def test_the_background_row_states_that_nothing_is_watching_it():
    """The cost belongs on the row: a permission prompt a background session cannot
    answer waits silently until `claude logs <id>` is read."""
    row = next(r for r in options.agent_rows("fix") if r.startswith("claude-bg|"))
    assert "nothing is watching it" in row


def test_an_unknown_task_is_a_usage_error_rather_than_an_empty_dropdown(capsys):
    assert options.main(["agents", "--agent=nonesuch"]) == options.EXIT_USAGE
    assert capsys.readouterr().out == "", "nothing may reach the quick-pick's stdout"


# --- the agent pick scoping the catalogue --------------------------------------------


def test_a_background_mode_is_still_the_claude_catalogue():
    """`claude-bg` is a mode, not a CLI: the model list must be Claude's whether the
    session lands in a tab or in the background."""
    assert options.selected_agents("claude-bg") == ("claude",)
    assert options.selected_agents("codex") == ("codex",)


def test_a_checkbox_selection_reaches_both_catalogues_and_an_empty_one_means_both():
    """Empty is the verb run by hand with no stage in front of it; answering with nothing
    would make a typed command look like a machine with no CLIs installed."""
    assert options.selected_agents("claude,codex") == ("claude", "codex")
    assert options.selected_agents("codex,claude") == ("claude", "codex"), "canonical order"
    assert options.selected_agents("") == am.AGENTS


def test_the_model_list_is_scoped_to_the_agent_that_was_picked(tmp_path, capsys, monkeypatch):
    home = write_caches(tmp_path)
    claude = run(["models", "--agent=claude-bg"], capsys, home, monkeypatch)
    assert all(value.startswith(("claude:", am.DEFAULT)) for value in values(claude))
    both = run(["models", "--agent=claude,codex"], capsys, home, monkeypatch)
    assert any(value.startswith("codex:") for value in values(both))


def test_the_effort_list_is_scoped_to_the_model_that_was_picked(tmp_path, capsys, monkeypatch):
    home = write_caches(tmp_path)
    haiku = run(
        ["efforts", "--agent=claude", "--model=claude:claude-haiku-4-5"], capsys, home, monkeypatch
    )
    assert values(haiku) == [am.DEFAULT], "a model with no levels asks nothing"
    opus = run(
        ["efforts", "--agent=claude", "--model=claude:claude-opus-5"], capsys, home, monkeypatch
    )
    assert values(opus)[1:] == ["low", "medium", "high", "max"]


def test_every_verb_prints_rows_and_only_rows(tmp_path, capsys, monkeypatch):
    """A status line, a warning or a progress message on this stdout becomes an extra
    option somebody can tick."""
    home = write_caches(tmp_path)
    for argv in (
        ["agents", "--agent=fix"],
        ["models", "--agent=claude"],
        ["efforts", "--agent=claude", "--model=default"],
    ):
        for row in run(argv, capsys, home, monkeypatch):
            assert len(row.split("|")) == 4


# --- the workspace wiring ------------------------------------------------------------


def canonical():
    """The task block as devkit's copy defines it. `canonical_tasks` parses the JSONC --
    the file carries comments, so `json.loads` on its text does not."""
    return devkit_project.canonical_tasks()


CHAINS = {
    "Agent: Fix What Is Red": ("fixAgent", "fixModel", "fixEffort"),
    "Agent: New Worktree": ("worktreeAgent", "worktreeModel", "worktreeEffort"),
    "Agents: Resume Recent Sessions": ("resumeAgents", "resumeModel", "resumeEffort"),
}


def test_every_task_that_spends_a_session_asks_all_three_in_dependence_order():
    """The extension resolves a task's inputs left to right and substitutes a later one's
    `${input:<id>}` from what the earlier one recorded, so a wrong order draws a list
    from some previous click rather than failing. `tests/test_devkit_project.py` holds
    that property for every task in the file; this one names the three chains, so a task
    that quietly loses its model stage is a failure here rather than a silence there.
    """
    tasks = {task["label"]: task for task in canonical()["tasks"]}
    for label, (agent, model, effort) in CHAINS.items():
        args = " ".join(str(arg) for arg in tasks[label]["args"])
        assert args.index(f"${{input:{agent}}}") < args.index(f"${{input:{model}}}")
        assert args.index(f"${{input:{model}}}") < args.index(f"${{input:{effort}}}")


def test_every_stage_is_a_live_picker_that_declines_to_ask_when_there_is_one_answer():
    """Two properties that have to hold together, and neither is optional.

    `shellCommand.execute` is the only input type that extension records an answer for,
    so a `pickString` anywhere in a chain makes the stage below it substitute empty and
    draw an unscoped list. `useSingleResult` is what stops the two new stages becoming
    two more prompts on tasks that already ask several: a machine with no catalogue, or a
    model that takes no effort level, returns one row and is never shown.
    """
    inputs = {spec["id"]: spec for spec in canonical()["inputs"]}
    for chain in CHAINS.values():
        for input_id in chain:
            spec = inputs[input_id]
            assert spec["command"] == "shellCommand.execute", input_id
            assert spec["args"].get("useSingleResult") or spec["args"].get("multiselect"), input_id


def test_every_stage_runs_this_script_rather_than_listing_models_in_the_workspace_file():
    """The list is read from the CLIs' own caches, so nothing in the workspace file may
    name a model. A hand-written list is a third copy of what two vendors publish, wrong
    in whichever direction the next launch moves -- and this file has no test run and no
    reviewer for the week it stays wrong."""
    inputs = {spec["id"]: spec for spec in canonical()["inputs"]}
    for chain in CHAINS.values():
        for input_id in chain:
            assert "agent-options.py" in inputs[input_id]["args"]["command"]
    spelled = re.findall(r"claude-(?:opus|sonnet|haiku|fable)-\d|gpt-\d", json.dumps(canonical()))
    assert not spelled, f"the workspace file names models: {sorted(set(spelled))}"


def test_the_parser_takes_the_three_verbs_and_the_two_picks_the_chain_hands_down():
    """`build_parser` is the seam the workspace file writes against: a verb, the agent
    pick that scopes the catalogue, and the model pick that scopes the levels."""
    parser = options.build_parser()
    args = parser.parse_args(["efforts", "--agent=claude,codex", "--model=claude:x"])
    assert (args.verb, args.agent, args.model) == ("efforts", "claude,codex", "claude:x")
    assert parser.parse_args(["models"]).model == "", "both picks are optional on the CLI"


def test_draw_dispatches_on_the_verb_and_nothing_else(tmp_path, monkeypatch):
    """One function for three lists, because "which half of the catalogue" is the only
    thing that varies across them -- and the caller's stdout IS the quick-pick."""
    monkeypatch.setenv("DEVKIT_AGENT_HOME", str(write_caches(tmp_path)))
    assert values(options.draw("agents", "worktree", "")) == ["claude", "codex"]
    assert values(options.draw("models", "claude", ""))[0] == am.DEFAULT
    assert values(options.draw("efforts", "claude", "claude:claude-haiku-4-5")) == [am.DEFAULT]
