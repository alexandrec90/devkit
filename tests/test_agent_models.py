"""`scripts/agent_models.py`: reading two vendors' catalogues, and spelling one pick twice.

Every function here is pure except `read_catalogue`, so this suite drives them directly
and never a CLI. What it is guarding is a module whose *inputs* belong to somebody else:
both cache files are written by a vendor's client and neither is a documented interface,
so the failure this is shaped against is a shape change -- a renamed key, a level that
appears, a field that goes missing -- which must come out as "no models offered" rather
than as a traceback in a quick-pick.

The two live catalogues on this machine are deliberately not read here. A test that
asserted about `claude-opus-5` would pass today, fail the week the account's model list
changes, and be measuring the vendor rather than the code; the fixtures below are
trimmed copies of the real files' shapes, which is the part this module actually depends
on.
"""

from __future__ import annotations

import argparse
import json
import sys

from support import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import agent_models as am

# Trimmed to the members `claude_models` reads. `thinking.type` is `"effort"` on the
# three that take a level and `"none"` on Haiku, which is the distinction that decides
# whether the effort picker draws at all.
CLAUDE_CACHE = {
    "version": 2,
    "catalog": {
        "surface": "cc",
        "config": {
            "id": "cc",
            "models": [
                {
                    "id": "claude-opus-5",
                    "name": "Opus 5",
                    "description": "For complex tasks",
                    "thinking": {
                        "type": "effort",
                        "effort_options": [
                            {"id": "low", "name": "Low"},
                            {"id": "medium", "name": "Medium"},
                            {"id": "high", "name": "High", "badge": {"message": "Default"}},
                            {"id": "max", "name": "Max"},
                        ],
                    },
                },
                {
                    "id": "claude-haiku-4-5",
                    "name": "Haiku 4.5",
                    "description": "Fastest for quick answers",
                    "thinking": {"type": "none"},
                },
            ],
        },
    },
}

# Likewise for Codex: `visibility`, `priority`, and an `ultra` Claude has no equivalent
# for are the three members with behaviour hanging off them.
CODEX_CACHE = {
    "models": [
        {
            "slug": "gpt-5.5",
            "display_name": "GPT-5.5",
            "description": "Previous generation",
            "visibility": "list",
            "priority": 12,
            "default_reasoning_level": "medium",
            "supported_reasoning_levels": [{"effort": "low"}, {"effort": "medium"}],
        },
        {
            "slug": "gpt-6-astra",
            "display_name": "GPT-6-Astra",
            "description": "Most capable",
            "visibility": "list",
            "priority": 1,
            "default_reasoning_level": "low",
            "supported_reasoning_levels": [
                {"effort": "low"},
                {"effort": "medium"},
                {"effort": "ultra"},
            ],
        },
        {
            "slug": "codex-auto-review",
            "display_name": "Auto Review",
            "visibility": "hide",
            "priority": 43,
            "supported_reasoning_levels": [{"effort": "low"}],
        },
    ]
}


def write_caches(home, claude=CLAUDE_CACHE, codex=CODEX_CACHE):
    """Both cache files, where each CLI actually keeps them. Returns `home`."""
    catalog = home / ".claude" / "cache" / "model-catalog"
    catalog.mkdir(parents=True)
    (catalog / "acct-surface-cc.json").write_text(json.dumps(claude), encoding="utf-8")
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "models_cache.json").write_text(json.dumps(codex), encoding="utf-8")
    return home


# --- parsing what the vendors wrote --------------------------------------------------


def test_claudes_catalogue_becomes_models_with_their_own_effort_levels():
    found = am.claude_models(CLAUDE_CACHE)
    assert [model.id for model in found] == ["claude-opus-5", "claude-haiku-4-5"]
    opus, haiku = found
    assert opus.name == "Opus 5" and opus.efforts == ("low", "medium", "high", "max")
    assert opus.default_effort == "high", "the badged option is the vendor's own default"
    assert haiku.efforts == (), "`thinking.type: none` takes no --effort at all"


def test_codex_hides_what_it_marks_hidden_and_keeps_its_own_order():
    """`visibility` and `priority` are the vendor's answers to "should a person see this,
    and in what order" -- filtering by name here would be a second opinion that goes
    stale the first time it ships another internal model."""
    found = am.codex_models(CODEX_CACHE)
    assert [model.id for model in found] == ["gpt-6-astra", "gpt-5.5"]
    assert "codex-auto-review" not in {model.id for model in found}
    assert found[0].default_effort == "low"
    assert "ultra" in found[0].efforts


def test_a_model_row_carries_its_agent_so_one_list_can_hold_both():
    assert am.codex_models(CODEX_CACHE)[0].token == "codex:gpt-6-astra"
    assert am.claude_models(CLAUDE_CACHE)[0].token == "claude:claude-opus-5"


def test_every_shape_the_caches_can_take_answers_empty_rather_than_raising():
    """These files belong to two vendors and are not documented interfaces.

    A renamed key has to come out as an empty catalogue, because the caller is drawing a
    quick-pick that a person is watching and opening a paid session behind it. Empty
    collapses the picker to its default row; a traceback draws a list that cannot be told
    apart from a command that failed to run.
    """
    for payload in ({}, None, [], {"catalog": None}, {"catalog": {"config": {"models": "no"}}}):
        assert am.claude_models(payload) == ()
    for payload in ({}, None, [], {"models": {}}, {"models": [None, 3, {"no": "slug"}]}):
        assert am.codex_models(payload) == ()


def test_a_missing_unreadable_or_malformed_cache_is_an_empty_catalogue(tmp_path):
    assert am.read_catalogue("claude", tmp_path) == ((), 0.0)
    assert am.read_catalogue("codex", tmp_path) == ((), 0.0)
    assert am.read_catalogue("gemini", tmp_path) == ((), 0.0), "an agent with no parser"
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "models_cache.json").write_text("{not json", encoding="utf-8")
    assert am.read_catalogue("codex", tmp_path) == ((), 0.0)


def test_available_lists_the_named_agents_in_order_and_nothing_else(tmp_path):
    write_caches(tmp_path)
    both, mtime = am.available(("claude", "codex"), tmp_path)
    assert [model.agent for model in both] == ["claude", "claude", "codex", "codex"]
    assert mtime > 0
    claude_only, _ = am.available(("claude",), tmp_path)
    assert {model.agent for model in claude_only} == {"claude"}


# --- turning a pick into flags -------------------------------------------------------


def test_the_two_clis_spell_the_same_pick_differently():
    """Claude Code has a first-class `--effort`; Codex has none and reads its level out
    of config, so the same choice is a `-c` override there. This module is the only place
    in the repo that may know that."""
    chosen = am.Launch.parse("claude", "claude:claude-opus-5", "max")
    assert chosen.flags("claude") == ["--model", "claude-opus-5", "--effort", "max"]
    codex = am.Launch.parse("codex", "codex:gpt-6-astra", "high")
    assert codex.flags("codex") == ["-m", "gpt-6-astra", "-c", 'model_reasoning_effort="high"']


def test_the_default_token_and_a_blank_both_mean_pass_no_flag():
    """ "Leave it alone" is not "pass the configured value": the two differ the moment the
    configuration changes between the click and the spawn, and only the first is what the
    picker's first row says."""
    for model, effort in (("default", "default"), ("", ""), ("  ", " default ")):
        chosen = am.Launch.parse("claude", model, effort)
        assert chosen.flags("claude") == [] and chosen.flags("codex") == []


def test_a_model_picked_for_one_cli_does_not_reach_the_other():
    """One click can open both -- the resume task ticks two CLIs -- and `-m
    claude-opus-5` would fail the Codex half. Not applying it is the only honest reading;
    the effort is agent-neutral and still applies."""
    chosen = am.Launch.parse("claude", "claude:claude-opus-5", "high")
    assert chosen.model_for("codex") == ""
    assert chosen.flags("codex") == ["-c", 'model_reasoning_effort="high"']


def test_a_bare_model_id_applies_to_whichever_cli_is_opening():
    """What somebody typing the flag by hand writes. The picker always prefixes."""
    chosen = am.Launch.parse("claude", "gpt-6-astra")
    assert chosen.model_for("codex") == "gpt-6-astra"
    assert chosen.model_for("claude") == "gpt-6-astra"


def test_the_launcher_line_states_the_pick_and_says_nothing_about_a_default():
    """The terminal is the only place the choice is recorded once the quick-pick closes,
    and a session at `max` costs several times one at the default."""
    chosen = am.Launch.parse("claude", "claude:claude-opus-5", "max")
    assert chosen.describe("claude") == " at claude-opus-5, effort max"
    # The Codex tabs of a mixed batch take the level and not the model, and the line has
    # to say exactly that rather than repeating the Claude one.
    assert chosen.describe("codex") == " at effort max"
    assert am.Launch().describe("claude") == ""


# --- the rows ------------------------------------------------------------------------


def values(rows):
    """The first field of each row -- the only one that comes back from a quick-pick."""
    return [row.split("|")[0] for row in rows]


def test_the_model_list_always_leads_with_the_row_that_changes_nothing(tmp_path):
    models, mtime = am.available(("claude",), write_caches(tmp_path))
    rows = am.model_rows(models, mtime)
    assert values(rows)[0] == am.DEFAULT
    assert values(rows)[1:] == ["claude:claude-opus-5", "claude:claude-haiku-4-5"]


def test_a_machine_with_no_catalogue_draws_one_row_so_the_picker_never_opens(tmp_path):
    """`useSingleResult` takes a one-row list without drawing it, which is what keeps a
    fresh machine -- or a CLI nobody has run yet -- from being asked a question with one
    possible answer."""
    models, mtime = am.available(("claude", "codex"), tmp_path)
    assert len(am.model_rows(models, mtime)) == 1
    assert "no catalogue cached here yet" in am.model_rows(models, mtime)[0]


def test_a_model_that_takes_no_effort_asks_nothing(tmp_path):
    models, _ = am.available(("claude",), write_caches(tmp_path))
    assert len(am.effort_rows(models, "claude:claude-haiku-4-5")) == 1
    assert values(am.effort_rows(models, "claude:claude-opus-5")) == [
        am.DEFAULT,
        "low",
        "medium",
        "high",
        "max",
    ]


def test_the_default_model_offers_only_levels_every_ticked_cli_can_take(tmp_path):
    """The union would be wrong in the one place both CLIs are ticked at once. Codex's
    `ultra` has no Claude equivalent, so offering it on the resume task would open the
    Claude half of the batch on a flag its CLI rejects."""
    home = write_caches(tmp_path)
    both, _ = am.available(("claude", "codex"), home)
    assert "ultra" not in values(am.effort_rows(both, am.DEFAULT))
    codex_only, _ = am.available(("codex",), home)
    assert "ultra" in values(am.effort_rows(codex_only, am.DEFAULT))


def test_the_effort_default_row_names_the_models_own_default_when_there_is_one(tmp_path):
    models, _ = am.available(("claude",), write_caches(tmp_path))
    assert "own default is high" in am.effort_rows(models, "claude:claude-opus-5")[0]
    assert "no --effort flag" in am.effort_rows(models, am.DEFAULT)[0]


def test_a_level_no_release_has_heard_of_still_draws_last():
    """This module is not the authority on what the vendors ship. A level it cannot rank
    keeps the position the catalogue gave it rather than disappearing from the list."""
    assert am.order_efforts(["cosmic", "high", "low"]) == ("low", "high", "cosmic")


def test_the_age_note_says_how_old_the_list_is_and_when_to_refresh_it():
    """A cached list is only as alive as its writer and nothing in a dropdown says so --
    the broken-PR menu spent two days a day stale on exactly that. Here the writer is the
    CLI itself, so the honest claim is when it last ran."""
    assert am.age_note(0) == ""
    assert "within the hour" in am.age_note(1_000_000, now=1_001_000)
    assert "5h old" in am.age_note(1_000_000, now=1_000_000 + 5 * 3600)
    assert "run the CLI once" in am.age_note(1_000_000, now=1_000_000 + 4 * 86_400)


def test_every_field_of_every_row_is_one_line_with_no_separator_in_it(tmp_path):
    """`picker_rows` is positional: a separator in a vendor's description would silently
    make a fifth field, and a newline a second, unpickable row whose *value* is prose."""
    models, mtime = am.available(("claude", "codex"), write_caches(tmp_path))
    rows = am.model_rows(models, mtime) + am.effort_rows(models, am.DEFAULT)
    for row in rows:
        assert len(row.split("|")) == 4
        assert "\n" not in row


# --- the surface the pickers and the launchers reach for ------------------------------


def test_a_model_is_a_record_of_what_the_vendor_said_and_nothing_this_repo_decided():
    """`Model` carries the catalogue's own fields. A default invented here would be a
    claim about somebody else's product with no owner and no way to notice it go stale."""
    bare = am.Model(agent="claude", id="claude-x", name="X")
    assert (bare.description, bare.efforts, bare.default_effort) == ("", (), "")
    assert bare.token == "claude:claude-x"


def test_catalogue_paths_looks_where_each_cli_actually_writes(tmp_path):
    """Claude Code keys its file by account and surface, so the name cannot be predicted
    and the directory is globbed; Codex writes one fixed name. An agent with no parser
    has no path at all rather than a guess."""
    write_caches(tmp_path)
    claude = am.catalogue_paths("claude", tmp_path)
    assert claude and claude[0].parent == tmp_path / ".claude" / "cache" / "model-catalog"
    assert am.catalogue_paths("codex", tmp_path) == [tmp_path / ".codex" / "models_cache.json"]
    assert am.catalogue_paths("gemini", tmp_path) == []


def test_a_model_token_is_read_back_into_its_agent_and_its_id():
    """The value the model picker returns, and the only thing that tells a receiving
    script which binary a model belongs to."""
    assert am.parse_model("codex:gpt-6-astra") == ("codex", "gpt-6-astra")
    assert am.parse_model(am.DEFAULT) == ("", "")
    assert am.parse_model("") == ("", "")
    assert am.parse_model("weird:thing") == ("", "weird:thing"), "an unknown prefix is not one"


def test_efforts_for_and_default_level_answer_about_the_model_not_the_agent(tmp_path):
    models, _ = am.available(("claude",), write_caches(tmp_path))
    assert am.efforts_for(models, "claude:claude-opus-5") == ("low", "medium", "high", "max")
    assert am.default_level(models, "claude:claude-opus-5") == "high"
    assert am.efforts_for(models, "claude:gone") == (), "a model the catalogue dropped"
    assert am.default_level(models, am.DEFAULT) == "", "no one model to ask"


def test_both_flags_are_declared_in_one_place_so_four_scripts_cannot_disagree():
    """`add_arguments` is the whole CLI contract between a picker and a launcher, and both
    flags default to "pass nothing" -- so a caller that never mentions either keeps the
    behaviour it had before any of this existed."""
    parser = argparse.ArgumentParser()
    am.add_arguments(parser)
    args = parser.parse_args([])
    assert (args.model, args.effort) == ("", "")
    assert am.Launch.parse("claude", args.model, args.effort).flags() == []
    assert parser.parse_args(["--model=claude:x", "--effort=max"]).effort == "max"


def test_the_catalogue_home_can_be_pointed_elsewhere(monkeypatch, tmp_path):
    """The seam this suite reads its fixture caches through, and the escape hatch for a
    machine whose config homes are not under `~`."""
    monkeypatch.delenv("DEVKIT_AGENT_HOME", raising=False)
    assert am.home_override() is None
    monkeypatch.setenv("DEVKIT_AGENT_HOME", str(tmp_path))
    assert am.home_override() == tmp_path
