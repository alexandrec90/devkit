#!/usr/bin/env python3
"""Which agent opens, at which model, at what effort -- one choice, read from the CLIs.

Every task in this workspace that spends a session -- *Agent: Fix What Is Red*, *Agent:
New Worktree*, *Agents: Resume Recent Sessions* -- used to open its agent at whatever
`/model` or `~/.codex/config.toml` last selected, with no way to say otherwise short of
typing into the tab afterwards. That is the wrong default for a batch: a red PR sent to
Haiku and a refactor sent to Opus at `max` are different amounts of money, and the choice
belongs at the click.

**The list is not written here, and that is the whole design.** Both CLIs fetch their
model catalogue from the server and cache it on disk -- Claude Code under
`~/.claude/cache/model-catalog/`, Codex at `~/.codex/models_cache.json` -- refreshing it
every session. So the pickers read the file the CLI itself wrote: a model released this
morning is in the dropdown this afternoon with no edit here and no release of devkit, and
one that was withdrawn stops being offered. A hand-written list would be a third copy of
something two vendors already publish, wrong in whichever direction the next launch moves.

**The effort levels come from the same file, per model.** They are not a global five:
Claude's catalogue marks Haiku `thinking.type: "none"` -- no `--effort` at all -- while
Codex's frontier models carry an `ultra` Claude has no equivalent for, and each side
records its own default. So `efforts_for` asks about the *model*, which is why the effort
picker chains off the model picker.

**`Launch` is one object rather than three arguments, and that is load-bearing.** The
three travel together through four launchers, and `structure_check`'s `function_params`
limit said so out loud: `open_agent` already took six. It also puts the `claude-bg` mode
and the CLI behind it in one place, so nothing downstream repeats that mapping.

**Both parameters are optional and `DEFAULT` is the answer that passes no flag** -- not a
flag carrying the configured value, which is a different thing the moment the
configuration changes between the click and the spawn. Every picker draws that row first.

**Two CLIs, two spellings, one place that knows the difference.** Claude Code takes
`--model <id> --effort <level>`; Codex takes `-m <id>` and reads its effort out of config,
so the level arrives as `-c model_reasoning_effort="<level>"`. `Launch.flags` is the only
function in the repo that may spell either.

Everything here is pure except `read_catalogue`, and that one is total: a missing,
unreadable or malformed cache is an empty catalogue, never an exception, because its
callers draw a quick-pick somebody is watching. The CLI validates what it is handed
anyway -- this decides what is *offered*, not what is legal.

Tested in `tests/test_agent_models.py`; `scripts/agent-options.py` is the picker CLI.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import picker_rows

# The token every "leave it alone" row carries, and what `Launch` reads as "pass no
# flag". A real word rather than an empty string for the reason
# `.claude/rules/vscode-tasks.md` gives: a live picker drops a row whose value is empty
# (`filterEmptyResults`), and an empty branch reaches argparse as a stray positional.
DEFAULT = "default"

# What joins an agent to its model id in a picker's value: `claude:claude-opus-5`. The
# agent rides along because one dropdown can list both catalogues -- *Agents: Resume
# Recent Sessions* ticks both CLIs -- and a bare model id would leave the receiving
# script guessing which binary it belongs to. A colon because no model id from either
# vendor contains one, while both contain `-` and `.`.
SEP = ":"

# The CLIs this knows about, in the order every list draws them.
AGENTS = ("claude", "codex")

# Effort levels in increasing order, for sorting a list read from either catalogue into
# one that reads as a scale. A level from a catalogue that is not here still draws --
# appended, in the order the file gave it -- because a new level nobody has seen yet is
# exactly what this module refuses to be the source of truth for.
EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max", "ultra")

# How each level is written in the dropdown. `xhigh` is the one a `.title()` gets wrong,
# and a row reading "Xhigh" beside "Max" is one nobody can rank at a glance.
EFFORT_LABELS = {"xhigh": "Extra high"}

# What each level costs, in the one place a person reads before spending it. Keyed by the
# level rather than by the vendor's prose: both catalogues describe the same five in
# different words, and a row whose wording changes with the agent reads as a different
# setting.
EFFORT_NOTES = {
    "low": "fastest, lightest reasoning -- and the cheapest against your limits",
    "medium": "balances speed and depth",
    "high": "more thorough; slower, and draws down limits faster",
    "xhigh": "deeper still; for a problem that has already resisted `high`",
    "max": "the most reasoning either CLI offers; use sparingly",
    "ultra": "Codex only: maximum reasoning with automatic task delegation",
}


@dataclass(frozen=True)
class Model:
    """One model an installed CLI currently offers, as its own catalogue describes it."""

    agent: str  # "claude" or "codex"
    id: str  # what reaches `--model` / `-m`
    name: str  # how the vendor names it, for the row's label
    description: str = ""  # the vendor's one-liner, for the row's detail
    efforts: tuple[str, ...] = ()  # levels this model accepts; empty means it takes none
    default_effort: str = ""  # the level it uses when none is passed

    @property
    def token(self) -> str:
        """`<agent>:<id>` -- the value the model picker returns."""
        return f"{self.agent}{SEP}{self.id}"


# --- one launch --------------------------------------------------------------------


@dataclass(frozen=True)
class Launch:
    """Which agent, at which model, at what effort. What a click chose.

    `agent` is whatever its caller's agent question returns: a CLI name, a *mode* naming
    one (`claude-bg`), or the resume task's checkbox selection (`claude,codex`). `cli`
    resolves all three, so nothing downstream repeats the mapping.
    """

    agent: str = "claude"
    model: str = ""
    effort: str = ""

    @classmethod
    def parse(cls, agent: str = "claude", model: str = "", effort: str = "") -> Launch:
        """From the raw picker values, with `DEFAULT` and blanks meaning "pass nothing"."""
        return cls(agent or "claude", _pick(model), _pick(effort))

    @property
    def cli(self) -> str:
        """The executable behind `agent`: `claude-bg` is the Claude CLI, `-bg` is a mode."""
        return self.agent.partition("-")[0]

    def model_for(self, agent: str = "") -> str:
        """The model id to pass when opening `agent`, or "" to pass none.

        A model picked for the *other* CLI is not an error and not a substitution: it is
        silently not applied, because one click can open both -- the resume task ticks
        Claude and Codex together and reopens whichever sessions were most recent.
        "Codex -- GPT-6-Astra" there means the Codex tabs get it and the Claude tabs keep
        their own default, which is the only reading under which the pick is honest.
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
        pick: `resume-sessions.py` asks per tab which CLI that tab is.
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
        one at the default, and the terminal is the only place that choice is recorded once
        the quick-pick has closed.
        """
        stated = [self.model_for(agent), f"effort {self.effort}" if self.effort else ""]
        kept = [part for part in stated if part]
        return f" at {', '.join(kept)}" if kept else ""

    def notes(self, agents: Sequence[str]) -> str:
        """`describe` for several CLIs at once, one newline-terminated line each.

        For the caller that opens a mixed batch: a model picked for one CLI does not apply
        to the other's tabs, and one summary line would imply it did. `""` for the ordinary
        click, which answered neither question.
        """
        stated = [(agent, self.describe(agent)) for agent in agents]
        return "".join(f"  {agent} tabs open{note}\n" for agent, note in stated if note)


# A launch that chose nothing: the default for every parameter that takes one, so a
# caller with no picker in front of it opens what it opened before there was one. Frozen,
# so one shared instance is safe, and a name because ruff's B008 refuses a call there.
NOTHING_PICKED = Launch()


def _pick(value: str) -> str:
    """A picker's answer, with `DEFAULT` and whitespace both reading as "nothing chosen"."""
    text = (value or "").strip()
    return "" if text == DEFAULT else text


def parse_model(token: str) -> tuple[str, str]:
    """A model picker's value as `(agent, model id)`; `("", "")` for the default row.

    A bare id with no `<agent>:` prefix -- what someone typing the flag by hand writes --
    answers `("", <id>)`: this model, whichever agent is opening.
    """
    text = _pick(token)
    agent, sep, model = text.partition(SEP)
    if sep and agent in AGENTS:
        return agent, model.strip()
    return "", text


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Declare `--model` and `--effort` on a parser, identically everywhere.

    Four scripts between a click and a tab accept this pair and none interprets it. Both
    default to `""`, which `Launch.parse` reads as `DEFAULT` does -- pass no flag -- so a
    caller that never mentions either gets exactly the behaviour it had before.
    """
    parser.add_argument(
        "--model",
        default="",
        help=f"`<agent>{SEP}<model id>` from the model picker, a bare id, or `{DEFAULT}`",
    )
    parser.add_argument(
        "--effort", default="", help=f"one of {', '.join(EFFORT_ORDER)}, or `{DEFAULT}`"
    )


# --- reading the two catalogues -----------------------------------------------------


def catalogue_paths(agent: str, home: Path | None = None) -> list[Path]:
    """Every file `agent`'s catalogue could be at on this machine, newest first.

    Claude Code keys its cache file by account and surface, so the directory holds one
    file whose name nothing here can predict -- hence a glob. Codex writes one fixed name.
    """
    root = Path(home) if home is not None else Path.home()
    if agent == "claude":
        found = root / ".claude" / "cache" / "model-catalog"
        try:
            return sorted(found.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return []
    return [root / ".codex" / "models_cache.json"] if agent == "codex" else []


def claude_models(payload: object) -> tuple[Model, ...]:
    """Claude Code's cached catalogue, as models.

    The shape is `{"catalog": {"config": {"models": [...]}}}`. Effort levels live under
    `thinking.effort_options` and exist only when `thinking.type` is `"effort"` -- Haiku's
    is `"none"`, and offering it a level would be offering a flag the CLI rejects. The
    default level is the option the vendor badges `Default`.
    """
    config = _dig(payload, "catalog", "config")
    models = config.get("models") if isinstance(config, dict) else None
    found: list[Model] = []
    for entry in models if isinstance(models, list) else ():
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            continue
        levels, default = _claude_efforts(entry.get("thinking"))
        found.append(
            Model(
                "claude",
                entry["id"],
                _text(entry.get("name")) or entry["id"],
                _text(entry.get("description")),
                order_efforts(levels),
                default,
            )
        )
    return tuple(found)


def _claude_efforts(thinking: object) -> tuple[list[str], str]:
    """One model's `(levels, its own default)` out of its `thinking` block."""
    options = thinking.get("effort_options") if isinstance(thinking, dict) else None
    levels: list[str] = []
    default = ""
    for option in options if isinstance(options, list) else ():
        if not isinstance(option, dict) or not isinstance(option.get("id"), str):
            continue
        levels.append(option["id"])
        badge = option.get("badge")
        if isinstance(badge, dict) and badge.get("message") == "Default":
            default = option["id"]
    return levels, default


def codex_models(payload: object) -> tuple[Model, ...]:
    """Codex's cached catalogue, as models.

    `visibility` is the vendor's own answer to "should a person be offered this": the
    internal entries (`gpt-reserve`, `codex-auto-review`) are marked `hide` and are left
    out rather than filtered by name here. `priority` is the order its own picker draws,
    so it is the order this one draws too.
    """
    models = payload.get("models") if isinstance(payload, dict) else None
    rows: list[tuple[int, Model]] = []
    for entry in models if isinstance(models, list) else ():
        if not isinstance(entry, dict) or not isinstance(entry.get("slug"), str):
            continue
        if entry.get("visibility") == "hide":
            continue
        levels = [
            level["effort"]
            for level in (entry.get("supported_reasoning_levels") or [])
            if isinstance(level, dict) and isinstance(level.get("effort"), str)
        ]
        priority = entry.get("priority")
        model = Model(
            "codex",
            entry["slug"],
            _text(entry.get("display_name")) or entry["slug"],
            _text(entry.get("description")),
            order_efforts(levels),
            _text(entry.get("default_reasoning_level")),
        )
        rows.append((priority if isinstance(priority, int) else 10**6, model))
    return tuple(model for _priority, model in sorted(rows, key=lambda row: row[0]))


PARSERS = {"claude": claude_models, "codex": codex_models}


def read_catalogue(agent: str, home: Path | None = None) -> tuple[tuple[Model, ...], float]:
    """`(models, mtime)` for one agent, or `((), 0.0)` when this machine has no cache.

    Total by construction: a file that is missing, unreadable, not JSON or not the shape
    the parser expects all answer empty. The caller is a quick-pick, and a traceback there
    draws an empty dropdown that cannot be told from a command that never ran.
    """
    parse = PARSERS.get(agent)
    if parse is None:  # an agent nothing here knows how to read
        return (), 0.0
    for path in catalogue_paths(agent, home):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            mtime = path.stat().st_mtime
        except (OSError, ValueError):
            continue
        models = parse(payload)
        if models:
            return models, mtime
    return (), 0.0


def available(agents: Sequence[str], home: Path | None = None) -> tuple[tuple[Model, ...], float]:
    """Every model the named agents offer, in `AGENTS` order, with the oldest cache time.

    The oldest rather than the newest: the age is reported to say how stale the list a
    person is reading might be, and a list is only as fresh as its stalest half.
    """
    models: list[Model] = []
    ages: list[float] = []
    for agent in AGENTS:
        if agent not in agents:
            continue
        found, mtime = read_catalogue(agent, home)
        models += found
        if mtime:
            ages.append(mtime)
    return tuple(models), min(ages) if ages else 0.0


def home_override() -> Path | None:
    """`$DEVKIT_AGENT_HOME`, for a test or a machine whose caches live elsewhere."""
    found = os.environ.get("DEVKIT_AGENT_HOME", "").strip()
    return Path(found) if found else None


# --- the rows the pickers draw ------------------------------------------------------


def order_efforts(levels: Iterable[str]) -> tuple[str, ...]:
    """The levels a catalogue listed, de-duplicated and sorted into a readable scale.

    A level `EFFORT_ORDER` has never heard of keeps its position at the end rather than
    being dropped: this module is not the authority on what the vendors ship.
    """
    seen = list(dict.fromkeys(level for level in levels if level))
    known = [level for level in EFFORT_ORDER if level in seen]
    return tuple(known + [level for level in seen if level not in EFFORT_ORDER])


def age_note(mtime: float, now: float | None = None) -> str:
    """How old the catalogue behind a list is, in words, or "" when there is none.

    Stated on the default row because `.claude/rules/vscode-tasks.md` earns it: a cached
    list is only as alive as its writer, and nothing in a dropdown says so. Here the
    writer is the CLI itself on its own session start, so the honest thing to report is
    when it last ran rather than a promise that the list is current.
    """
    if not mtime:
        return ""
    hours = max((now if now is not None else time.time()) - mtime, 0) / 3600
    if hours < 1:
        return "the CLI's own catalogue, refreshed within the hour"
    if hours < 48:
        return f"the CLI's own catalogue, {int(hours)}h old"
    return f"the CLI's own catalogue, {int(hours // 24)}d old -- run the CLI once to refresh it"


def model_rows(models: Sequence[Model], mtime: float = 0.0, now: float | None = None) -> list[str]:
    """The model picker's lines: the default first, then one per model on offer.

    Always at least one row. With no catalogue on this machine the list is the default
    alone, and the task's `useSingleResult` then takes it without drawing anything -- a
    question with one answer is not a question.
    """
    rows = [
        picker_rows.row(
            DEFAULT,
            "Default model",
            "whatever the CLI is already configured with",
            age_note(mtime, now) or "no catalogue cached here yet; run the CLI once",
        )
    ]
    return rows + [
        picker_rows.row(m.token, f"{m.agent.title()} -- {m.name}", m.id, m.description)
        for m in models
    ]


def efforts_for(models: Sequence[Model], token: str) -> tuple[str, ...]:
    """The levels the effort picker should offer for a chosen model token.

    For a named model that is exactly its own list -- Haiku's is empty, so the picker
    collapses to the default row and never draws.

    For the *default* model row there is no one model to ask, so the answer is the union
    within each agent and the **intersection across them**: a level every ticked CLI can
    take somewhere. Union alone would be wrong in the one place both are ticked at once --
    *Agents: Resume Recent Sessions* -- because Codex's `ultra` has no Claude equivalent,
    so offering it there would open the Claude half of the batch on a rejected flag.
    """
    _agent, chosen = parse_model(token)
    if chosen:
        picked = next((model for model in models if model.id == chosen), None)
        return picked.efforts if picked else ()
    per_agent = [
        {level for model in models if model.agent == agent for level in model.efforts}
        for agent in dict.fromkeys(model.agent for model in models)
    ]
    return order_efforts(set.intersection(*per_agent)) if per_agent else ()


def default_level(models: Sequence[Model], token: str) -> str:
    """The level the chosen model uses when none is passed, or "" when that is not one
    model's question. Only ever rendered as a note on the default row."""
    _agent, chosen = parse_model(token)
    picked = next((model for model in models if model.id == chosen), None) if chosen else None
    return picked.default_effort if picked else ""


def effort_rows(models: Sequence[Model], token: str) -> list[str]:
    """The effort picker's lines, scoped to whichever model the stage above returned."""
    stated = default_level(models, token)
    rows = [
        picker_rows.row(
            DEFAULT,
            "Default effort",
            "whatever the CLI is already configured with",
            f"this model's own default is {stated}" if stated else "no --effort flag is passed",
        )
    ]
    return rows + [
        picker_rows.row(
            level, EFFORT_LABELS.get(level, level.title()), "", EFFORT_NOTES.get(level, "")
        )
        for level in efforts_for(models, token)
    ]


# --- small shared helpers ------------------------------------------------------------


def _dig(payload: object, *keys: str) -> object:
    """Walk nested dicts, answering `{}` the moment the shape stops matching."""
    found = payload
    for key in keys:
        if not isinstance(found, dict):
            return {}
        found = found.get(key)
    return found if found is not None else {}


def _text(value: object) -> str:
    """A catalogue field as a string, and "" for every way it can be absent."""
    return value.strip() if isinstance(value, str) else ""
