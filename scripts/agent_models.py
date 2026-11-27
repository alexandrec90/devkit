#!/usr/bin/env python3
"""Which agent opens, at which model, at what effort -- one choice, spelled for each CLI.

Every command that spends a session -- `fix-prs.py`, `agent-worktree.py new`,
`resume-sessions.py`, `agent-box.py spawn`, the fix pass -- takes `--model` and
`--effort` and hands them here. A red PR sent to Haiku and a refactor sent to Opus at
`max` are different amounts of money, so the choice belongs to whoever starts the run.

The model list is not written here: the CLI validates what it is handed, so this decides
only how a choice is spelled, never what is legal.

**`Launch` is one object rather than three arguments, and that is load-bearing.** The
three travel together through four launchers, and `structure_check`'s `function_params`
limit said so out loud: `open_agent` already took six. It also puts the `claude-bg` mode
and the CLI behind it in one place, so nothing downstream repeats that mapping.

**Both parameters are optional and `DEFAULT` is the answer that passes no flag** -- not a
flag carrying the configured value, which is a different thing the moment the
configuration changes between the choice and the spawn.

**Two CLIs, two spellings, one place that knows the difference.** Claude Code takes
`--model <id> --effort <level>`; Codex takes `-m <id>` and reads its effort out of config,
so the level arrives as `-c model_reasoning_effort="<level>"`. `Launch.flags` is the only
function in the repo that may spell either.

Pure; tested in `tests/test_agent_models.py`.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass

# The token that means "pass no flag", like a blank. A real word so it can be typed.
DEFAULT = "default"

# What joins an agent to its model id: `claude:claude-opus-5`. The agent rides along
# because one run can open both CLIs -- `resume-sessions.py --agent any` -- and a bare
# model id would leave the receiving script guessing which binary it belongs to. A colon
# because no model id from either vendor contains one, while both contain `-` and `.`.
SEP = ":"

# The CLIs this knows about.
AGENTS = ("claude", "codex")

# Effort levels in increasing order, for the `--effort` help. The CLI is the authority on
# which a model accepts; a level not listed here is still passed through.
EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max", "ultra")


# --- one launch --------------------------------------------------------------------


@dataclass(frozen=True)
class Launch:
    """Which agent, at which model, at what effort. What the caller chose.

    `agent` is whatever its caller's agent argument holds: a CLI name, a *mode* naming
    one (`claude-bg`), or the resume command's list (`claude,codex`). `cli` resolves all
    three, so nothing downstream repeats the mapping.
    """

    agent: str = "claude"
    model: str = ""
    effort: str = ""

    @classmethod
    def parse(cls, agent: str = "claude", model: str = "", effort: str = "") -> Launch:
        """From the raw argument values, with `DEFAULT` and blanks meaning "pass nothing"."""
        return cls(agent or "claude", _pick(model), _pick(effort))

    @property
    def cli(self) -> str:
        """The executable behind `agent`: `claude-bg` is the Claude CLI, `-bg` is a mode."""
        return self.agent.partition("-")[0]

    def model_for(self, agent: str = "") -> str:
        """The model id to pass when opening `agent`, or "" to pass none.

        A model chosen for the *other* CLI is not an error and not a substitution: it is
        silently not applied, because one run can open both -- the resume command reopens
        Claude and Codex sessions together. `codex:gpt-6-astra` there means the Codex tabs
        get it and the Claude tabs keep their own default, which is the only reading under
        which the choice is honest.
        """
        owner, model = parse_model(self.model)
        return "" if not model or (owner and owner != (agent or self.cli)) else model

    def flags(self, agent: str = "") -> list[str]:
        """The arguments that open `agent` this way. Empty for a launch that chose neither.

        The two CLIs disagree about effort and this is the only place that may know it.
        Claude Code has a first-class `--effort`; Codex has none, and its level is a config
        key -- `-c model_reasoning_effort="high"`, quoted because the value half of a `-c`
        override is parsed as TOML and a bare word is not. Codex does fall back to the raw
        string, but relying on a parse *failure* to mean the right thing is no contract.

        `agent` overrides `self.cli`, for the one caller that opens a mixed batch from one
        choice: `resume-sessions.py` asks per tab which CLI that tab is.
        """
        codex = (agent or self.cli) == "codex"
        model = self.model_for(agent)
        argv = ["-m" if codex else "--model", model] if model else []
        if not self.effort:
            return argv
        level = f'model_reasoning_effort="{self.effort}"' if codex else self.effort
        return [*argv, "-c" if codex else "--effort", level]

    def describe(self, agent: str = "") -> str:
        """What a launcher prints beside the tab it is opening, or "" for a plain default.

        Printed rather than left to the tab because a session at `max` costs several times
        one at the default, and the terminal is the only place that choice is recorded.
        """
        stated = [self.model_for(agent), f"effort {self.effort}" if self.effort else ""]
        kept = [part for part in stated if part]
        return f" at {', '.join(kept)}" if kept else ""

    def notes(self, agents: Sequence[str]) -> str:
        """`describe` for several CLIs at once, one newline-terminated line each.

        For the caller that opens a mixed batch: a model chosen for one CLI does not apply
        to the other's tabs, and one summary line would imply it did. `""` for the ordinary
        run, which chose neither.
        """
        stated = [(agent, self.describe(agent)) for agent in agents]
        return "".join(f"  {agent} tabs open{note}\n" for agent, note in stated if note)


# A launch that chose nothing: the default for every parameter that takes one, so a
# caller that passes no choice opens what the CLI is configured with. Frozen, so one
# shared instance is safe, and a name because ruff's B008 refuses a call there.
NOTHING_PICKED = Launch()


def _pick(value: str) -> str:
    """An argument's value, with `DEFAULT` and whitespace both reading as "nothing chosen"."""
    text = (value or "").strip()
    return "" if text == DEFAULT else text


def parse_model(token: str) -> tuple[str, str]:
    """A `--model` value as `(agent, model id)`; `("", "")` for the default.

    A bare id with no `<agent>:` prefix answers `("", <id>)`: this model, whichever agent
    is opening.
    """
    text = _pick(token)
    agent, sep, model = text.partition(SEP)
    if sep and agent in AGENTS:
        return agent, model.strip()
    return "", text


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Declare `--model` and `--effort` on a parser, identically everywhere.

    Four scripts between a command and a tab accept this pair and none interprets it.
    Both default to `""`, which `Launch.parse` reads as `DEFAULT` does -- pass no flag --
    so a caller that never mentions either gets the CLI's own configuration.
    """
    parser.add_argument(
        "--model",
        default="",
        help=f"`<agent>{SEP}<model id>`, a bare id, or `{DEFAULT}`",
    )
    parser.add_argument(
        "--effort", default="", help=f"one of {', '.join(EFFORT_ORDER)}, or `{DEFAULT}`"
    )
