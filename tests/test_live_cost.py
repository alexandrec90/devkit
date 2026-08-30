"""Tests for `tests/live_cost.py` — the cost table the paid live smokes run at.

The headline test here is `test_the_defaults_are_the_cheapest_tier`. Everything else in
this repo can be checked by reading it; a model default cannot, because the consequence
of getting it wrong is a bill rather than a failure. So it gets a ratchet.
"""

import live_cost as cost
import pytest
from support import REPO_ROOT, load_script

live = load_script("scripts/hook-tests-live.py")


# --- the ratchet -------------------------------------------------------------


@pytest.mark.parametrize("suite", sorted(cost.DEFAULTS))
def test_the_defaults_are_the_cheapest_tier(suite):
    """Raising a live suite onto a pricier model must be a deliberate, reviewed act.

    Not a tautology restating the constant: `CHEAPEST` is the reviewed allow-list and
    `DEFAULTS` is what runs. Editing one to match the other is the moment someone has to
    justify the spend, and it shows up in a diff as a change to a list of model names
    rather than as one word inside an `os.environ.get` call.
    """
    assert cost.DEFAULTS[suite].model in cost.CHEAPEST[suite], (
        f"{suite} defaults to {cost.DEFAULTS[suite].model!r}, which is not in CHEAPEST. "
        "If that model really is the cheapest tier now, add it there in the same change."
    )


@pytest.mark.parametrize("suite", sorted(cost.DEFAULTS))
def test_every_default_asks_for_the_lowest_effort(suite):
    assert cost.DEFAULTS[suite].effort == cost.LOWEST_EFFORT


def test_no_default_names_a_frontier_model():
    """The allow-list above is the real gate; this is the same check from the other side,
    so a careless edit that widened `CHEAPEST` still trips something."""
    expensive = {"sonnet", "opus", "gpt-5.6", "gpt-5.6-pro", "claude-opus-5", "claude-sonnet-5"}
    for suite, default in cost.DEFAULTS.items():
        assert default.model not in expensive, suite


def test_the_claude_default_carries_a_budget_ceiling():
    """The Claude CLI takes `--max-budget-usd`; a live suite that does not pass one is a
    session with no upper bound on a bad day.

    The cap here started at 0.25 and was raised once, deliberately: a measured run died
    at `error_max_budget_usd` on turn 1 having spent 0.109325 — one turn's floor is
    dominated by the cache write of the CLI's own system prompt, so a ceiling that tight
    fails the suite before a hook is ever reached. 0.50 still bounds a runaway.
    """
    assert cost.DEFAULTS["claude"].budget_usd is not None
    assert float(cost.DEFAULTS["claude"].budget_usd) <= 0.50


# --- resolution --------------------------------------------------------------


def test_an_override_wins_over_the_default():
    resolved = cost.resolve("claude", {"CLAUDE_LIVE_HOOK_MODEL": "sonnet"})
    assert resolved.model == "sonnet"
    assert resolved.effort == cost.LOWEST_EFFORT


def test_an_empty_override_falls_back_rather_than_through():
    """A VS Code picker contributing an empty string is the normal way this happens, and
    an empty `--model` either errors or lets the settings file choose — which is the whole
    thing the explicit model exists to prevent."""
    resolved = cost.resolve(
        "claude", {"CLAUDE_LIVE_HOOK_MODEL": "", "CLAUDE_LIVE_HOOK_EFFORT": " "}
    )
    assert resolved.model == cost.DEFAULTS["claude"].model
    assert resolved.effort == cost.LOWEST_EFFORT


def test_an_unknown_suite_names_the_real_ones():
    with pytest.raises(ValueError, match="claude"):
        cost.resolve("gemini", {})


def test_the_codex_default_has_no_budget_field_to_export():
    """`codex exec` has no budget flag, so `as_env` must not invent a variable for it."""
    exported = cost.as_env("codex", cost.resolve("codex", {}))
    assert set(exported) == {"CODEX_LIVE_HOOK_MODEL", "CODEX_LIVE_HOOK_REASONING_EFFORT"}


def test_as_env_round_trips_through_resolve():
    """What the runner exports is what the suite reads back — the property the preflight's
    honesty rests on."""
    original = cost.LiveCost(model="haiku", effort="high", budget_usd="0.05")
    assert cost.resolve("claude", cost.as_env("claude", original)) == original


def test_the_summary_line_names_every_field_that_costs_money():
    summary = cost.LiveCost(model="haiku", effort="low", budget_usd="0.10").summary()
    assert "haiku" in summary and "low" in summary and "0.10" in summary
    assert "budget" not in cost.LiveCost(model="m", effort="low").summary()


# --- agreement with the runner and the suites --------------------------------


def test_every_suite_the_runner_knows_has_a_cost_entry():
    assert set(live.SUITES) == set(cost.DEFAULTS) == set(cost.ENV_VARS) == set(cost.CHEAPEST)


@pytest.mark.parametrize("suite", sorted(cost.DEFAULTS))
def test_the_live_suites_read_their_cost_from_this_module(suite):
    """No literal model name may survive in a suite file.

    This is what keeps the ratchet meaningful: a default pinned here is worth nothing if
    the file that spends the money quietly carries its own copy.
    """
    source = (REPO_ROOT / live.SUITES[suite].path).read_text(encoding="utf-8")
    assert "from live_cost import" in source
    for model in cost.CHEAPEST[suite]:
        assert model not in source, f"{live.SUITES[suite].path} still spells {model!r}"
    for name in cost.ENV_VARS[suite].values():
        assert name not in source, f"{live.SUITES[suite].path} still reads {name} directly"
