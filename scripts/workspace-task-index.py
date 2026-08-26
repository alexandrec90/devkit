#!/usr/bin/env python3
"""Render the workspace task block as a table a human can actually read.

The task block is ~40 tasks and ~40 `${input:...}` pickers spread over two thousand
lines of JSONC, and reviewing a change to it means reading the *shape* â€” which tasks
exist, what each one runs, which questions it asks before it runs â€” out of a format
that interleaves those three facts with `presentation`, `icon` and `problemMatcher`
noise. Nothing here is new information: every column below is already in the file. The
point is that a table puts the four things a reviewer wants side by side, and the file
does not.

Two decisions worth knowing:

**It reads the LIVE workspace file by default, not devkit's canonical copy.** "Which
tasks do I have" is a question about the file VS Code actually loaded, and on a task
branch the two deliberately disagree â€” see `.claude/rules/vscode-tasks.md`. `--canonical`
asks the other question, which is the one to ask when reviewing an un-merged edit.

**The parameter table is half the output, because a picker is where a one-click task
takes its blast radius from.** A task row names the inputs it asks for; the parameter
table says what each one asks, what the answers are, and which tasks reach it â€” so a
picker nothing uses shows up as `(unused)` rather than staying invisible.

This is a *report*, not a gate: it asserts nothing and can never fail a review. The
contracts on the task block live in `tests/test_devkit_project.py`.
"""

from __future__ import annotations

import argparse
import datetime as _datetime
import functools
import json
import re
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devkit_jsonc
import sweep

REPO_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_WORKSPACE = REPO_ROOT / "workspace.jsonc"
# Resolved through `sweep` for the reason `devkit_project.py` documents: this runs from
# inside an ephemeral box as a matter of course, where the naive `parent` is `.worktrees/`.
LIVE_WORKSPACE = sweep.default_workspace(REPO_ROOT)
OUTPUT_DIR = REPO_ROOT / "logs"
OUTPUT_STEM = "workspace-tasks"

INPUT_TOKEN = re.compile(r"\$\{input:([A-Za-z0-9_-]+)\}")
WORKSPACE_FOLDER = re.compile(r"\$\{workspaceFolder:[^}]*\}[\\/]")

# The two wrappers every workspace-scoped task nests around the script that does the
# work (`notify-wrap -> log-wrap -> the script`). They are a task-layer concern and say
# nothing about what the task *does*, so the "Runs" column reports what they wrap.
WRAPPERS = ("notify-wrap.py", "log-wrap.py")
# PowerShell launcher boilerplate: present on every `pwsh.exe` task, identical each time.
SHELL_NOISE = frozenset({"-NoProfile", "-ExecutionPolicy", "Bypass", "-File"})

DEFAULT_WIDTH = 120
MIN_DETAIL_WIDTH = 32
# Caps, not widths: a column shrinks to its content and never grows past these, so the
# prose column keeps whatever is left of `--width`. They are deliberately tighter than
# the longest label and the longest command: those two wrap to a second line without
# losing anything, and the detail is the column that becomes unreadable when squeezed.
TASK_COLUMN_CAPS = (28, 26, 16)
INPUT_COLUMN_CAPS = (20, 22, 30)


@dataclass(frozen=True)
class TaskRow:
    """One VS Code task, reduced to the four facts a reviewer reads it for."""

    label: str
    domain: str
    name: str
    group: str
    runs: str
    parameters: tuple[str, ...]
    detail: str


@dataclass(frozen=True)
class InputRow:
    """One `${input:...}` picker, plus the tasks that ask it."""

    ident: str
    kind: str
    question: str
    choices: tuple[str, ...]
    used_by: tuple[str, ...]


# --- reading the file ---------------------------------------------------------


def resolve_workspace(explicit: Path | None, canonical: bool) -> tuple[Path, str]:
    """The workspace file to read, and a one-word name for which copy it is.

    Falls back to the canonical copy when the live file is absent rather than failing:
    the live file sits *beside* the checkouts and so does not exist in a CI clone, and a
    report that refuses to run there would be a report nobody could run in review.
    """
    if explicit is not None:
        return explicit, "given"
    if canonical:
        return CANONICAL_WORKSPACE, "canonical"
    if LIVE_WORKSPACE.is_file():
        return LIVE_WORKSPACE, "live"
    return CANONICAL_WORKSPACE, "canonical"


def load_task_block(path: Path) -> dict:
    """The `tasks` object out of a workspace file: `{"version", "tasks", "inputs"}`.

    A `.code-workspace` nests it under a top-level `tasks` key; a bare `tasks.json`
    carries it at the root. Both are accepted so the script can be aimed at either.
    """
    payload = devkit_jsonc.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} is not a JSON object")
    block = payload.get("tasks", payload)
    if isinstance(block, list):
        # A bare `tasks.json`: the root *is* the block, and its own `tasks` key is the
        # list of tasks rather than a nested block.
        block = payload
    if not isinstance(block, dict):
        raise ValueError(f"{path.name} has no task block")
    return block


# --- one task -----------------------------------------------------------------


def unwrap_command(tokens: list[str]) -> list[str]:
    """Strip `notify-wrap.py` / `log-wrap.py` layers, returning the wrapped command.

    Each wrapper's argv is `python <wrapper> <label> -- <the rest>`, so the wrapped
    command begins after the first `--`. Nesting is two deep in practice; the loop does
    not care how deep it is.
    """
    while True:
        head = tokens[:2]
        if not any(token.endswith(wrapper) for token in head for wrapper in WRAPPERS):
            return tokens
        if "--" not in tokens:
            return tokens
        tokens = tokens[tokens.index("--") + 1 :]


def describe_command(task: dict) -> str:
    """What the task runs, with the noise every task carries identically removed.

    `${input:x}` becomes `<x>` so the command reads as a signature, and lines up with
    the parameter table underneath.
    """
    tokens = [str(task.get("command", ""))] + [str(arg) for arg in task.get("args", ())]
    tokens = [token for token in unwrap_command(tokens) if token and token not in SHELL_NOISE]
    condensed = []
    for token in tokens:
        token = WORKSPACE_FOLDER.sub("", token)
        token = token.removeprefix("scripts/").removeprefix("scripts\\")
        condensed.append(INPUT_TOKEN.sub(r"<\1>", token))
    return " ".join(condensed)


def task_group(task: dict) -> str:
    """`build` / `test`, with `*` marking the one VS Code runs from Run Build/Test Task."""
    group = task.get("group")
    if isinstance(group, dict):
        kind = str(group.get("kind", ""))
        return f"{kind}*" if group.get("isDefault") else kind
    return str(group or "")


def task_parameters(task: dict) -> tuple[str, ...]:
    """Every `${input:...}` the task names, in the order it names them.

    Read off the serialised task rather than off `args` alone: an input can reach a task
    through `options.cwd` or `options.env` too, and a parameter table that missed those
    would be quietly wrong in exactly the tasks with the most going on.
    """
    seen: dict[str, None] = {}
    for match in INPUT_TOKEN.finditer(json.dumps(task)):
        seen.setdefault(match.group(1), None)
    return tuple(seen)


def task_rows(block: dict) -> list[TaskRow]:
    """Every task in the block, in file order."""
    rows = []
    for task in block.get("tasks", ()):
        label = str(task.get("label", "<unlabelled>"))
        domain, _, name = label.partition(": ")
        detail = (
            str(task.get("detail", "")).strip()
            or "(no detail â€” see .claude/rules/vscode-tasks.md)"
        )
        limit = (task.get("runOptions") or {}).get("instanceLimit")
        if isinstance(limit, int) and limit > 1:
            detail = f"{detail} [up to {limit} runs at once]"
        rows.append(
            TaskRow(
                label=label,
                domain=domain if name else "Other",
                name=name or label,
                group=task_group(task),
                runs=describe_command(task),
                parameters=task_parameters(task),
                detail=detail,
            )
        )
    return rows


# --- one input ----------------------------------------------------------------


def input_kind(spec: dict) -> str:
    """How the picker asks: free text, one answer, or several.

    A `command` input is the `command-variable` extension, whose whole reason for being
    here is multi-select and remembering â€” so say which of those this one uses rather
    than printing the extension's command id, which is the same string 10 times over.
    """
    kind = str(spec.get("type", ""))
    if kind == "promptString":
        return "free text"
    if kind == "pickString":
        return "pick one"
    if kind != "command":
        return kind or "?"
    args = spec.get("args") or {}
    parts = ["pick many" if args.get("multiPick") else "pick one", "remembered"]
    if args.get("fileName"):
        parts.append("from a file")
    if args.get("pickStringRemember"):
        parts.append("after a first pick")
    return ", ".join(parts)


def input_question(spec: dict) -> str:
    """The prompt the picker shows. A `command` input carries it inside `args`."""
    question = spec.get("description") or (spec.get("args") or {}).get("description") or ""
    return str(question).strip()


def _option_text(option: object, default: object) -> str:
    """One option row: `label = value`, with `*` on the one that is pre-selected."""
    if isinstance(option, dict):
        value = option.get("value", "")
        label = str(option.get("label", value))
        text = label if str(value) == label else f"{label} = {value!r}"
    else:
        value = option
        text = str(option)
    return f"* {text}" if default is not None and value == default else f"  {text}"


def input_choices(spec: dict) -> tuple[str, ...]:
    """The answers on offer, as display lines. `*` marks the default.

    Three shapes reach here and all three matter to a reviewer: a literal option list, a
    list the `command-variable` extension reads out of a JSON file at pick time (so the
    menu is only as fresh as whatever last wrote that file), and a nested
    `pickStringRemember`, which is a *second* question asked before this one.
    """
    default = spec.get("default")
    lines: list[str] = []
    for option in spec.get("options", ()):
        lines.append(_option_text(option, default))
    args = spec.get("args") or {}
    for group in args.get("optionGroups", ()):
        for option in group.get("options", ()):
            lines.append(_option_text(option, default))
    if args.get("fileName"):
        source = WORKSPACE_FOLDER.sub("", str(args["fileName"]))
        lines.append(f"  read at pick time from {source}")
    for key, nested in (args.get("pickStringRemember") or {}).items():
        asked = str(nested.get("description", "")).strip()
        lines.append(f"  asks {key} first: {asked}")
    if spec.get("type") == "promptString":
        blank = default in ("", None)
        lines.append("  (blank)" if blank else f"  default {default!r}")
    if not lines:
        lines.append("  â€”")
    return tuple(lines)


def input_rows(block: dict, tasks: list[TaskRow]) -> list[InputRow]:
    """Every declared input, with the tasks that ask it.

    `used_by` is what makes an orphan picker visible: an input no task names is dead
    weight that still shows up in the file, and there is nothing else in the workspace
    that would ever point at it.
    """
    rows = []
    for spec in block.get("inputs", ()):
        ident = str(spec.get("id", "<no id>"))
        used_by = tuple(task.label for task in tasks if ident in task.parameters)
        rows.append(
            InputRow(
                ident=ident,
                kind=input_kind(spec),
                question=input_question(spec) or "â€”",
                choices=input_choices(spec),
                used_by=used_by,
            )
        )
    return rows


def domains(tasks: list[TaskRow]) -> list[str]:
    """The label prefixes, in the order the file introduces them."""
    seen: dict[str, None] = {}
    for task in tasks:
        seen.setdefault(task.domain, None)
    return list(seen)


# --- text tables --------------------------------------------------------------


def column_widths(
    headers: tuple[str, ...], rows: list[tuple[str, ...]], caps: tuple[int, ...], width: int
) -> list[int]:
    """Fixed columns shrink to their content and stop at their cap; the last one takes
    whatever `width` has left, because it holds the prose."""
    fixed = []
    for index, cap in enumerate(caps):
        longest = max([len(headers[index])] + [len(row[index]) for row in rows], default=0)
        fixed.append(max(min(longest, cap), len(headers[index])))
    remaining = width - sum(fixed) - 2 * len(headers[: len(caps)])
    return [*fixed, max(remaining, MIN_DETAIL_WIDTH)]


def wrap_cell(cell: str, width: int) -> list[str]:
    """A cell's display lines.

    Newlines inside a cell are honoured rather than collapsed — `textwrap.wrap` treats
    one as ordinary whitespace, which ran a 13-option picker's answers together into a
    single paragraph: the exact illegibility this script exists to undo.
    """
    wrap = functools.partial(textwrap.wrap, width=width, break_on_hyphens=False)
    return [wrapped for line in cell.split("\n") for wrapped in (wrap(line) or [""])] or [""]


def render_row(cells: tuple[str, ...], widths: list[int]) -> list[str]:
    """One table row, wrapped inside its columns so nothing spills into its neighbour."""
    wrapped = [wrap_cell(cell, width) for cell, width in zip(cells, widths, strict=True)]
    height = max(len(column) for column in wrapped)
    lines = []
    for index in range(height):
        line = "  ".join(
            (column[index] if index < len(column) else "").ljust(width)
            for column, width in zip(wrapped, widths, strict=True)
        )
        lines.append(line.rstrip())
    return lines


def render_table(
    headers: tuple[str, ...], rows: list[tuple[str, ...]], caps: tuple[int, ...], width: int
) -> list[str]:
    """A wrapped fixed-width table, one blank line between rows.

    The blank line is not decoration: a detail column runs to a dozen wrapped lines, and
    without a separator two adjacent rows read as one paragraph.
    """
    widths = column_widths(headers, rows, caps, width)
    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True)).rstrip()]
    lines.append("  ".join("-" * w for w in widths))
    for row in rows:
        lines.extend(render_row(row, widths))
        lines.append("")
    return lines


def _multiline(values: tuple[str, ...], empty: str) -> str:
    return ", ".join(values) if values else empty


def render_text(tasks: list[TaskRow], inputs: list[InputRow], header: list[str], width: int) -> str:
    """The default format: a section per label domain, then the parameter table."""
    lines = [*header, ""]
    for domain in domains(tasks):
        members = [task for task in tasks if task.domain == domain]
        lines.append(f"=== {domain} ({len(members)}) ".ljust(width, "="))
        lines.append("")
        lines.extend(
            render_table(
                ("Task", "Runs", "Asks for", "What it does"),
                [(t.name, t.runs, _multiline(t.parameters, "â€”"), t.detail) for t in members],
                TASK_COLUMN_CAPS,
                width,
            )
        )
    lines.append(f"=== Parameters ({len(inputs)}) ".ljust(width, "="))
    lines.append("")
    lines.extend(
        render_table(
            ("Parameter", "Kind", "Question", "Answers  (* = default)"),
            [(i.ident, i.kind, i.question, "\n".join(i.choices)) for i in inputs],
            INPUT_COLUMN_CAPS,
            width,
        )
    )
    lines.extend(
        render_table(
            ("Parameter", "Asked by"),
            [(i.ident, _multiline(i.used_by, "(unused)")) for i in inputs],
            (INPUT_COLUMN_CAPS[0] + 2,),
            width,
        )
    )
    return "\n".join(lines).rstrip() + "\n"


# --- markdown -----------------------------------------------------------------


def md_cell(text: str) -> str:
    """A table cell for Markdown: pipes escaped, newlines folded to `<br>`."""
    return text.replace("|", "\\|").replace("\n", "<br>").strip()


def md_table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(" --- " for _ in headers) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(md_cell(cell) for cell in row) + " |")
    lines.append("")
    return lines


def render_markdown(tasks: list[TaskRow], inputs: list[InputRow], header: list[str]) -> str:
    """The same tables, for reading in VS Code's Markdown preview rather than a terminal."""
    lines = ["# VS Code workspace tasks", "", *[f"- {line}" for line in header], ""]
    for domain in domains(tasks):
        members = [task for task in tasks if task.domain == domain]
        lines.extend([f"## {domain} ({len(members)})", ""])
        lines.extend(
            md_table(
                ("Task", "Runs", "Asks for", "What it does"),
                [
                    (t.name, f"`{t.runs}`", _multiline(t.parameters, "â€”"), t.detail)
                    for t in members
                ],
            )
        )
    lines.extend([f"## Parameters ({len(inputs)})", ""])
    lines.extend(
        md_table(
            ("Parameter", "Kind", "Question", "Answers (* = default)", "Asked by"),
            [
                (
                    i.ident,
                    i.kind,
                    i.question,
                    "\n".join(i.choices),
                    _multiline(i.used_by, "_(unused)_"),
                )
                for i in inputs
            ],
        )
    )
    return "\n".join(lines).rstrip() + "\n"


# --- assembly -----------------------------------------------------------------


def header_lines(path: Path, which: str, tasks: list[TaskRow], inputs: list[InputRow]) -> list[str]:
    """Provenance, first, because the two copies of this file disagree on purpose."""
    stamp = _datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    note = {
        "live": "the file VS Code loaded â€” devkit's workspace.jsonc is the reviewable copy",
        "canonical": "devkit's reviewable copy â€” the live file may not have this yet",
        "given": "named on the command line",
    }[which]
    return [
        f"source:    {path}  ({which}: {note})",
        f"generated: {stamp}",
        f"contents:  {len(tasks)} tasks, {len(inputs)} parameters",
    ]


def build_report(path: Path, which: str, fmt: str, width: int) -> str:
    """Read the workspace file and render it. The one function a caller needs."""
    block = load_task_block(path)
    tasks = task_rows(block)
    inputs = input_rows(block, tasks)
    header = header_lines(path, which, tasks, inputs)
    if fmt == "markdown":
        return render_markdown(tasks, inputs, header)
    return render_text(tasks, inputs, header, width)


def output_path(fmt: str, explicit: Path | None) -> Path:
    """Where the report is persisted. Under `logs/`, which is git-ignored: this is a
    generated view of a file that is already in the repo, so committing it would be a
    second copy with nothing keeping it honest."""
    if explicit is not None:
        return explicit
    return OUTPUT_DIR / f"{OUTPUT_STEM}.{'md' if fmt == 'markdown' else 'txt'}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workspace", type=Path, help="read this workspace file instead")
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--canonical",
        action="store_true",
        help="read devkit's workspace.jsonc rather than the live .code-workspace",
    )
    # Redundant against the default, and deliberately so: the VS Code task picks the
    # source from a `${input:...}`, and a picker feeding a script directly must supply
    # one real token in every branch — an empty one reaches argparse as a stray
    # positional. `.claude/rules/vscode-tasks.md` names the rule and the two other
    # flags that exist for it.
    source.add_argument(
        "--live",
        action="store_true",
        help="read the live .code-workspace (the default; spelled out for the task picker)",
    )
    parser.add_argument("--format", choices=("text", "markdown"), default="text")
    parser.add_argument(
        "--out", type=Path, help=f"where to write it (default: logs/{OUTPUT_STEM}.*)"
    )
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH, help="text-format table width")
    args = parser.parse_args(argv)

    # Details carry en-dashes and arrows; a Windows console is cp1252 and would raise
    # UnicodeEncodeError partway through the report rather than printing it.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    path, which = resolve_workspace(args.workspace, args.canonical)
    if not path.is_file():
        print(f"workspace-task-index: no workspace file at {path}", file=sys.stderr)
        return 2
    try:
        report = build_report(path, which, args.format, args.width)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"workspace-task-index: cannot read {path}: {exc}", file=sys.stderr)
        return 2

    destination = output_path(args.format, args.out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(report, encoding="utf-8", newline="\n")
    print(report)
    print(f"written to {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
