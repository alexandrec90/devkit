"""`scripts/agent_models.py`: spelling one choice of model and effort for two CLIs.

Every function here is pure, so this suite drives them directly and never a CLI.
"""

from __future__ import annotations

import argparse
import sys

from support import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import agent_models as am

# --- turning a choice into flags -----------------------------------------------------


def test_the_two_clis_spell_the_same_choice_differently():
    """Claude Code has a first-class `--effort`; Codex has none and reads its level out
    of config, so the same choice is a `-c` override there. This module is the only place
    in the repo that may know that."""
    chosen = am.Launch.parse("claude", "claude:claude-opus-5", "max")
    assert chosen.flags("claude") == ["--model", "claude-opus-5", "--effort", "max"]
    codex = am.Launch.parse("codex", "codex:gpt-6-astra", "high")
    assert codex.flags("codex") == ["-m", "gpt-6-astra", "-c", 'model_reasoning_effort="high"']


def test_the_default_token_and_a_blank_both_mean_pass_no_flag():
    """ "Leave it alone" is not "pass the configured value": the two differ the moment the
    configuration changes between the choice and the spawn."""
    for model, effort in (("default", "default"), ("", ""), ("  ", " default ")):
        chosen = am.Launch.parse("claude", model, effort)
        assert chosen.flags("claude") == [] and chosen.flags("codex") == []


def test_a_model_chosen_for_one_cli_does_not_reach_the_other():
    """One run can open both -- the resume command reopens two CLIs -- and `-m
    claude-opus-5` would fail the Codex half. Not applying it is the only honest reading;
    the effort is agent-neutral and still applies."""
    chosen = am.Launch.parse("claude", "claude:claude-opus-5", "high")
    assert chosen.model_for("codex") == ""
    assert chosen.flags("codex") == ["-c", 'model_reasoning_effort="high"']


def test_a_bare_model_id_applies_to_whichever_cli_is_opening():
    chosen = am.Launch.parse("claude", "gpt-6-astra")
    assert chosen.model_for("codex") == "gpt-6-astra"
    assert chosen.model_for("claude") == "gpt-6-astra"


def test_the_launcher_line_states_the_choice_and_says_nothing_about_a_default():
    """The terminal is the only place the choice is recorded, and a session at `max`
    costs several times one at the default."""
    chosen = am.Launch.parse("claude", "claude:claude-opus-5", "max")
    assert chosen.describe("claude") == " at claude-opus-5, effort max"
    # The Codex tabs of a mixed batch take the level and not the model, and the line has
    # to say exactly that rather than repeating the Claude one.
    assert chosen.describe("codex") == " at effort max"
    assert am.Launch().describe("claude") == ""


def test_notes_name_each_cli_whose_tabs_open_differently():
    chosen = am.Launch.parse("claude", "claude:claude-opus-5", "max")
    assert chosen.notes(["claude", "codex"]) == (
        "  claude tabs open at claude-opus-5, effort max\n  codex tabs open at effort max\n"
    )
    assert am.NOTHING_PICKED.notes(["claude", "codex"]) == ""


def test_the_cli_behind_a_mode_is_its_prefix():
    assert am.Launch("claude-bg").cli == "claude"
    assert am.Launch("codex").cli == "codex"
    assert am.Launch.parse("").agent == "claude"


def test_a_model_token_is_read_back_into_its_agent_and_its_id():
    """The only thing that tells a receiving script which binary a model belongs to."""
    assert am.parse_model("codex:gpt-6-astra") == ("codex", "gpt-6-astra")
    assert am.parse_model(am.DEFAULT) == ("", "")
    assert am.parse_model("") == ("", "")
    assert am.parse_model("weird:thing") == ("", "weird:thing"), "an unknown prefix is not one"


def test_both_flags_are_declared_in_one_place_so_four_scripts_cannot_disagree():
    """`add_arguments` is the whole CLI contract between a caller and a launcher, and both
    flags default to "pass nothing"."""
    parser = argparse.ArgumentParser()
    am.add_arguments(parser)
    args = parser.parse_args([])
    assert (args.model, args.effort) == ("", "")
    assert am.Launch.parse("claude", args.model, args.effort).flags() == []
    assert parser.parse_args(["--model=claude:x", "--effort=max"]).effort == "max"
