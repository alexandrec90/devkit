"""The model, effort and budget every paid live-CLI smoke runs at — the only copy.

These numbers were previously three `os.environ.get` calls at the top of each live suite,
which is the natural place to put them and the wrong one. A cost default that lives in
the file that spends it can only be read by that file: the runner that launches the suite
cannot print what it is about to spend, and nothing can assert the value is still the
cheapest tier. Both of those matter more here than anywhere else in the repo, because
this is the only code that costs money to run.

So they live here, and there are three consumers:

- the two live suites, which read the resolved values and pass them to their CLI;
- `scripts/hook-tests-live.py`, which prints them in its preflight **and exports them
  into the environment it launches pytest with**, so what it printed is what is spent and
  no workstation setting can quietly substitute a pricier model;
- `tests/test_live_cost.py`, which pins the defaults to the cheapest tier so raising one
  has to be a deliberate, reviewed act rather than an edit nobody notices.

`CHEAPEST` is the allow-list that last check enforces. Adding a model to it is the
decision to spend more per live run; that is the point of it being a list in a file
rather than a habit.

Stdlib only and no import-time side effects beyond reading `os.environ`, because
`scripts/hook-tests-live.py` loads this module by path from an arbitrary checkout root.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# The cheapest tier each CLI offers, as of the last time someone checked. Anything not
# named here fails `test_the_defaults_are_the_cheapest_tier`, which is the whole point:
# a live suite silently promoted to a frontier model is a bill nobody agreed to.
CHEAPEST = {
    "claude": {"haiku"},
    "codex": {"gpt-5.6-luna"},
}

# The lowest setting each CLI's effort/reasoning knob accepts. Same reasoning.
LOWEST_EFFORT = "low"


@dataclass(frozen=True)
class LiveCost:
    """What one live suite will spend per session, already resolved against the env."""

    model: str
    effort: str
    budget_usd: str | None = None

    def summary(self) -> str:
        """One line, for a preflight that has to be read before money is spent."""
        parts = [f"model={self.model}", f"effort={self.effort}"]
        if self.budget_usd is not None:
            parts.append(f"budget=${self.budget_usd}")
        return " ".join(parts)


# The environment variable that overrides each field, per suite. The runner reads this to
# build the child environment, so adding a knob here is all it takes to make it settable
# from the task — there is no second list of names to keep in step.
ENV_VARS: dict[str, dict[str, str]] = {
    "claude": {
        "model": "CLAUDE_LIVE_HOOK_MODEL",
        "effort": "CLAUDE_LIVE_HOOK_EFFORT",
        "budget_usd": "CLAUDE_LIVE_HOOK_BUDGET_USD",
    },
    "codex": {
        "model": "CODEX_LIVE_HOOK_MODEL",
        "effort": "CODEX_LIVE_HOOK_REASONING_EFFORT",
    },
}

DEFAULTS: dict[str, LiveCost] = {
    # The budget is a ceiling, not a spend: it only stops a runaway, and it has to clear
    # *one* turn's floor, which is dominated by the one-off cache write of the CLI's own
    # system prompt and tool schemas. At 0.10 it did not: a measured run died at
    # `error_max_budget_usd` on turn 1 having spent 0.109325, failing the suite before a
    # hook was ever reached.
    "claude": LiveCost(model="haiku", effort=LOWEST_EFFORT, budget_usd="0.35"),
    # No budget flag: `codex exec` has no `--max-budget-usd` equivalent, so the bound is
    # the low reasoning effort and the single turn the prompt asks for. `None` means the
    # field is absent rather than unlimited-by-choice, and the runner prints it as absent.
    "codex": LiveCost(model="gpt-5.6-luna", effort=LOWEST_EFFORT),
}


def resolve(suite: str, env: dict[str, str] | None = None) -> LiveCost:
    """The cost this suite will actually run at, after environment overrides.

    An **empty** variable falls back to the default rather than through to the CLI. A
    task picker that contributes an empty string is the normal way that happens, and an
    empty `--model` reaches the CLI as a missing argument value, which either errors or —
    worse — silently selects whatever the settings file says. Falling back keeps the
    guarantee this module exists for.
    """
    if suite not in DEFAULTS:
        raise ValueError(f"unknown suite {suite!r}; expected one of: {', '.join(sorted(DEFAULTS))}")
    source = os.environ if env is None else env
    default = DEFAULTS[suite]
    names = ENV_VARS[suite]
    return LiveCost(
        model=source.get(names["model"], "").strip() or default.model,
        effort=source.get(names["effort"], "").strip() or default.effort,
        budget_usd=(
            source.get(names["budget_usd"], "").strip() or default.budget_usd
            if "budget_usd" in names
            else default.budget_usd
        ),
    )


def as_env(suite: str, cost: LiveCost) -> dict[str, str]:
    """The variables to export so a child process resolves to exactly this cost.

    The runner passes this to `subprocess.run`, which is what makes the preflight a
    promise rather than a guess: the suite re-resolves from these and can only land on
    the values that were printed.
    """
    names = ENV_VARS[suite]
    exported = {names["model"]: cost.model, names["effort"]: cost.effort}
    if "budget_usd" in names and cost.budget_usd is not None:
        exported[names["budget_usd"]] = cost.budget_usd
    return exported
