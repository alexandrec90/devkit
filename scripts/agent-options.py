#!/usr/bin/env python3
"""The three dropdowns every session-spending task asks: which agent, which model, at
what effort.

One script behind all of them, and that is the point rather than a tidying. *Agent: Fix
What Is Red*, *Agent: New Worktree* and *Agents: Resume Recent Sessions* each open a paid
agent, each used to spell its own agent list in `workspace.jsonc`, and the three lists had
already drifted -- one offered a background mode, one did not, one was a checkbox. A model
list spelled three times would have drifted the same way, except that this one cannot be
maintained by hand at all: it is read from the CLI's own cache, which is what
`scripts/agent_models.py` exists for.

| verb | what it draws |
| --- | --- |
| `agents` | the CLIs a task can open, as that task offers them |
| `models` | every model the picked agent(s) currently offer, plus the default row |
| `efforts` | the levels the picked model accepts, plus the default row |

**The three chain, left to right, and the order is load-bearing.**
`augustocdias.tasks-shell-input` substitutes `${input:<id>}` inside a later input's
command from the value it recorded when that input last resolved, and VS Code resolves
inputs in the order they appear in the task's arguments. So `--agent=${input:...}` must
appear before `--model=${input:...}`, which must appear before `--effort=${input:...}`;
`tests/test_devkit_project.py` fails a task that reorders them, and
`.claude/rules/vscode-tasks.md` carries why the failure would otherwise be silent.

**A one-row list draws nothing.** Each of the three inputs sets `useSingleResult`, so a
machine with no cached catalogue is asked no model question, and a model with no effort
levels -- Haiku takes none -- is asked no effort question. That is what keeps three more
prompts from landing on a task that already asks four: they appear exactly where there is
something to choose.

Nothing here spawns anything or touches the network. Every row goes through
`picker_rows`, so stdout *is* the quick-pick and nothing else may be printed on it.

Tested in `tests/test_agent_options.py`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_models
import picker_rows

# What each task's agent question offers. Keyed by the task rather than shared, because
# the rows genuinely differ and pretending otherwise is what produced three copies:
#
#   fix       three rows, because `claude --bg` exists and `codex exec` is not the same
#             thing -- it streams into the terminal that started it and leaves nothing to
#             reattach to, so a `codex-bg` row would be a lie or a silent downgrade.
#   worktree  two, and no background row: a worktree cut to be typed into is the opposite
#             of unwatched work, and a background session in one is a tab that never opens.
#   resume    two, ticked rather than picked -- both CLIs at once is one global recency
#             slice across either store, which is the question that task asks.
AGENT_ROWS: dict[str, tuple[tuple[str, str, str, str], ...]] = {
    "fix": (
        ("claude", "Claude", "a terminal tab you can watch and steer", ""),
        (
            "claude-bg",
            "Claude (background)",
            "returns an id, read it back with `claude logs`",
            "nothing is watching it; a prompt it cannot answer waits until you look",
        ),
        ("codex", "Codex", "a terminal tab; Codex has no background session", ""),
    ),
    "worktree": (
        ("claude", "Claude", "a terminal tab you can watch and steer", ""),
        (
            "codex",
            "Codex",
            "or use `codex --worktree`, which cuts under ~/.codex/",
            "",
        ),
    ),
    "resume": (
        ("claude", "Claude", "sessions from ~/.claude/projects", ""),
        ("codex", "Codex", "sessions from ~/.codex/sessions", ""),
    ),
}

EXIT_OK = 0
EXIT_USAGE = 2


def agent_rows(task: str) -> list[str]:
    """The agent picker's lines for one task."""
    return [picker_rows.row(*row) for row in AGENT_ROWS[task]]


def selected_agents(value: str) -> tuple[str, ...]:
    """The CLIs an agent pick names, in `agent_models.AGENTS` order.

    Takes everything the three tasks can hand it: a single mode (`claude-bg`, whose CLI
    is `claude`), a checkbox list (`claude,codex`), or nothing at all -- which is the
    verb run by hand with no stage in front of it and answers with both.
    """
    picked = {part.strip().partition("-")[0] for part in (value or "").split(",") if part.strip()}
    found = tuple(agent for agent in agent_models.AGENTS if agent in picked)
    return found or agent_models.AGENTS


def draw(verb: str, agent: str, model: str) -> list[str]:
    """The rows for whichever verb was asked for. The caller's stdout is the quick-pick."""
    if verb == "agents":
        return agent_rows(agent)
    models, mtime = agent_models.available(selected_agents(agent), agent_models.home_override())
    if verb == "models":
        return agent_models.model_rows(models, mtime)
    return agent_models.effort_rows(models, model)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("verb", choices=("agents", "models", "efforts"))
    parser.add_argument(
        "--agent",
        default="",
        help=(
            "for `agents`, which task's list to draw "
            f"({', '.join(sorted(AGENT_ROWS))}); for the other two, the agent pick to "
            "scope the catalogue by"
        ),
    )
    parser.add_argument(
        "--model",
        default="",
        help="for `efforts`: the model pick, as `<agent>:<id>` or `default`",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.verb == "agents" and args.agent not in AGENT_ROWS:
        # Not through `choices=`, because `--agent` means two different things across the
        # three verbs and a parser-level constraint would reject the model verb's values.
        print(
            f"agent-options: `agents` takes --agent from {', '.join(sorted(AGENT_ROWS))}",
            file=sys.stderr,
        )
        return EXIT_USAGE
    picker_rows.emit(draw(args.verb, args.agent, args.model))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
