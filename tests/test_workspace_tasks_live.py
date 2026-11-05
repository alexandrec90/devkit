"""The workspace's VS Code tasks, driven against this workstation rather than read.

`tests/test_devkit_project.py` holds the task block to its contracts by reading it: the
argument order a chain needs, the cancel guard every picker's script carries, an
extension recommended for every picker command. Each of those is a belief about what VS
Code and its two input extensions do with the block, and every regression the quick-pick
shipped was one of those beliefs meeting a real click -- a picker whose command failed
on a machine nobody had tried, a row whose separator split it into a fifth field, an
Escape that ran the task anyway, an extension the machine did not have.

So this file does what a click does, minus the window:

- **every live picker's command is run**, through the shell the extension uses and from
  the directory it names, with each `${input:...}` it reads answered by the first row of
  the picker it chains from, as `augustocdias.tasks-shell-input` answers it. Its rows
  must parse, and arrive within `PICKER_SECONDS`, because a picker is a person watching
  an empty box;
- **every task that reads a command picker is cancelled**: its innermost script is run
  with each such input left as the `${input:<id>}` literal VS Code passes on Escape, and
  must exit 0 at once having run nothing;
- **every path a task or picker names exists**, in the checkout it names;
- **every extension a picker needs is installed**, not merely recommended.

What this cannot see is VS Code's own resolver -- the order it asks in, what it
substitutes into a chain. That needs the editor itself, and is the next tier.

`${workspaceFolder:devkit}` resolves to *this* tree, every other folder to its checkout
beside the live workspace file: a fixer in a box tests the scripts it changed against
the machine it is on. Marked `local` through `needs_live_workspace`, so CI skips it and
`scripts/e2e.py` runs it nightly.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import pytest
from support import LIVE_WORKSPACE, REPO_ROOT, devkit_jsonc, load_script, needs_live_workspace

vscode_extensions = load_script("scripts/vscode_extensions.py")
task_index = load_script("scripts/workspace-task-index.py")

CANONICAL = REPO_ROOT / "workspace.jsonc"
SHELL_INPUT = "shellCommand.execute"
# A picker is a person watching an empty quick-pick; the rule asks for a second or two
# (`.claude/rules/vscode-tasks.md`). The ceiling is wider than that ask so a cold disk
# does not fail a night, and a picker past it is a defect to fix, not a ceiling to raise.
PICKER_SECONDS = 8.0
# How many more draws a picker past that gets, and the pause before each. The fastest
# counts: the wait is the picker's only when no draw beats it. 0d70f0f6: one drew in 2.3 s
# quiet and 24.0 s under the night's load, and that verdict measured what else was running.
REDRAWS = 2
REDRAW_PAUSE = 5.0
# A cancelled task runs nothing, so anything slower than this did work it should not have.
CANCEL_SECONDS = 8.0
# `picker_rows`: value|label|description|detail. A fifth field is a separator that leaked
# out of a cell, which the extension draws as a row that cannot be picked as itself.
MAX_FIELDS = 4

VARIABLE = re.compile(r"\$\{(?P<kind>workspaceFolder|env|input):(?P<name>[^}]+)\}")
FOLDER_PATH = re.compile(r"\$\{workspaceFolder:(?P<name>[^}]+)\}(?P<rest>[/\\][^\s\"',;]*)?")


# --- resolving what VS Code resolves ------------------------------------------------------


def block(path: Path = CANONICAL) -> dict:
    return devkit_jsonc.loads(path.read_text(encoding="utf-8"))["tasks"]


def folder(name: str, root: Path = REPO_ROOT, beside: Path = LIVE_WORKSPACE) -> Path:
    """Where `${workspaceFolder:<name>}` points: this tree for devkit, else the checkout."""
    return root if name == "devkit" else beside.parent / name


def substitute(
    text: str,
    answers: Mapping[str, str],
    env: Mapping[str, str] = os.environ,
    root: Path = REPO_ROOT,
    beside: Path = LIVE_WORKSPACE,
) -> str:
    """`text` with every variable VS Code would fill in filled in. An `${input:...}`
    with no answer stays literal, which is exactly what VS Code passes on a cancel."""

    def one(match: re.Match[str]) -> str:
        kind, name = match["kind"], match["name"]
        if kind == "workspaceFolder":
            return folder(name, root, beside).as_posix()
        if kind == "env":
            return env.get(name, "")
        return answers.get(name, match[0])

    return VARIABLE.sub(one, text)


def reads(text: str) -> list[str]:
    """The input ids `text` reads, in the order it reads them."""
    return [m["name"] for m in VARIABLE.finditer(text) if m["kind"] == "input"]


def chain_order(inputs: Mapping[str, dict]) -> list[str]:
    """Every live picker, each after the live pickers its command reads."""
    live = {key: spec for key, spec in inputs.items() if spec.get("command") == SHELL_INPUT}
    done: list[str] = []

    def visit(key: str, path: tuple[str, ...]) -> None:
        if key in done:
            return
        assert key not in path, f"picker chain loops: {' -> '.join((*path, key))}"
        for upstream in reads(live[key]["args"]["command"]):
            if upstream in live:
                visit(upstream, (*path, key))
        done.append(key)

    for key in live:
        visit(key, ())
    return done


@dataclass(frozen=True)
class Drawn:
    code: int
    seconds: float
    rows: tuple[tuple[str, ...], ...]
    stderr: str


def draw(spec: dict, answers: Mapping[str, str]) -> Drawn:
    """Run one live picker the way the extension runs it: through the shell, in its cwd."""
    args = spec["args"]
    command = substitute(args["command"], answers)
    cwd = substitute(args.get("cwd", "${workspaceFolder:devkit}"), answers)
    separator = args.get("fieldSeparator", "|")
    started = time.monotonic()
    done = subprocess.run(  # noqa: S602 - the extension runs this string through a shell
        command,
        shell=True,
        cwd=cwd,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    lines = [line for line in done.stdout.splitlines() if line.strip()]
    rows = tuple(tuple(line.split(separator)) for line in lines)
    return Drawn(done.returncode, time.monotonic() - started, rows, done.stderr.strip())


def fastest(first: Drawn, again: Callable[[], Drawn], pause: float = REDRAW_PAUSE) -> Drawn:
    """`first`, or the quickest of `REDRAWS` more of a picker `first` ran past its budget:
    the budget is held to what the picker does, not to the worst moment of the machine."""
    best = first
    for _ in range(REDRAWS):
        if best.code != 0 or best.seconds <= PICKER_SECONDS:
            break
        time.sleep(pause)
        best = min(best, again(), key=lambda drawn: (drawn.code != 0, drawn.seconds))
    return best


def defaults(inputs: Mapping[str, dict]) -> dict[str, str]:
    """An answer for every input a person would type or pick from a fixed list."""
    answers: dict[str, str] = {}
    for key, spec in inputs.items():
        if spec.get("type") == "pickString":
            options = spec.get("options") or [""]
            first = options[0]
            answers[key] = str(
                spec.get("default", first.get("value") if isinstance(first, dict) else first)
            )
        elif spec.get("type") == "promptString":
            answers[key] = str(spec.get("default", "x"))
    return answers


def command_inputs(inputs: Mapping[str, dict]) -> set[str]:
    return {key for key, spec in inputs.items() if spec.get("type") == "command"}


def argv_of(task: dict) -> list[str]:
    return [str(task.get("command", "")), *(str(arg) for arg in task.get("args", []))]


# --- the pure half, which CI runs ---------------------------------------------------------


def test_variables_resolve_as_vscode_resolves_them(tmp_path):
    beside = tmp_path / "w.code-workspace"
    text = 'python "${workspaceFolder:devkit}/a.py" ${workspaceFolder:carameli} ${env:HOME} ${input:x} ${input:y}'
    got = substitute(
        text, {"x": "picked"}, env={"HOME": "/h"}, root=tmp_path / "tree", beside=beside
    )
    assert got == (
        f'python "{(tmp_path / "tree").as_posix()}/a.py" {(tmp_path / "carameli").as_posix()} '
        "/h picked ${input:y}"
    )


def test_a_chain_is_drawn_upstream_first_and_a_loop_is_refused():
    def live(command: str) -> dict:
        return {"type": "command", "command": SHELL_INPUT, "args": {"command": command}}

    inputs = {
        "effort": live("x --model=${input:model} --agent=${input:agent}"),
        "model": live("x --agent=${input:agent}"),
        "agent": live("x"),
        "native": {"type": "pickString", "options": ["a"]},
    }
    assert chain_order(inputs) == ["agent", "model", "effort"]
    with pytest.raises(AssertionError, match="loops"):
        chain_order({"a": live("${input:b}"), "b": live("${input:a}")})


def _drawn(seconds: float, code: int = 0) -> Drawn:
    return Drawn(code, seconds, (("row",),), "")


def test_a_picker_within_budget_or_failed_is_not_drawn_again():
    def again() -> Drawn:
        raise AssertionError("no second draw")

    assert fastest(_drawn(PICKER_SECONDS), again, pause=0).seconds == PICKER_SECONDS
    assert fastest(_drawn(30.0, code=1), again, pause=0).code == 1


def test_a_slow_draw_counts_only_if_no_redraw_beats_it():
    quiet = iter([_drawn(9.0), _drawn(2.3)])
    assert fastest(_drawn(24.0), lambda: next(quiet), pause=0).seconds == 2.3
    slow = iter([_drawn(20.0), _drawn(30.0)])
    assert fastest(_drawn(24.0), lambda: next(slow), pause=0).seconds == 20.0
    broke = iter([_drawn(1.0, code=1), _drawn(12.0)])
    assert fastest(_drawn(24.0), lambda: next(broke), pause=0).seconds == 12.0


def test_fixed_inputs_are_answered_with_their_default_or_first_option():
    inputs = {
        "a": {"type": "pickString", "options": ["x", "y"]},
        "b": {"type": "pickString", "options": [{"label": "L", "value": "v"}]},
        "c": {"type": "pickString", "options": ["x"], "default": "d"},
        "d": {"type": "promptString"},
        "e": {"type": "command", "command": SHELL_INPUT},
    }
    assert defaults(inputs) == {"a": "x", "b": "v", "c": "d", "d": "x"}
    assert command_inputs(inputs) == {"e"}


# --- the live half, which only this workstation can run -----------------------------------


@needs_live_workspace
def test_every_extension_a_picker_needs_is_installed_here():
    """A recommendation is a toast VS Code offers once; installed is what a click needs."""
    gone = vscode_extensions.missing(CANONICAL)
    assert gone == [], f"not installed on this machine: {gone} -- code --install-extension <id>"


@needs_live_workspace
def test_every_path_a_task_or_picker_names_exists_in_the_checkout_it_names():
    tasks = block()
    named = [(task["label"], json.dumps(task)) for task in tasks["tasks"]]
    named += [(f"input {spec['id']}", json.dumps(spec)) for spec in tasks["inputs"]]
    missing = []
    checked = 0
    for owner, text in named:
        for match in FOLDER_PATH.finditer(text.replace("\\\\", "\\")):
            checked += 1
            target = folder(match["name"]) / (match["rest"] or "").lstrip("/\\")
            if not target.exists():
                missing.append(f"{owner}: {match[0]}")
    assert checked, "no task names a path under a workspace folder; the scan is broken"
    assert not missing, "named but not on disk:\n" + "\n".join(sorted(set(missing)))


@needs_live_workspace
def test_every_live_picker_draws_parseable_rows_in_time():
    inputs = {spec["id"]: spec for spec in block()["inputs"]}
    answers = defaults(inputs)
    problems: list[str] = []
    for key in chain_order(inputs):
        drawn = fastest(draw(inputs[key], answers), partial(draw, inputs[key], answers))
        where = f"{key} ({drawn.seconds:.1f}s)"
        if drawn.code != 0:
            problems.append(f"{where}: exited {drawn.code}: {drawn.stderr[-300:]}")
            continue
        if not drawn.rows:
            problems.append(f"{where}: drew no rows, which reads as a command that failed")
            continue
        bad = [row for row in drawn.rows if not row[0].strip() or len(row) > MAX_FIELDS]
        if bad:
            problems.append(f"{where}: {len(bad)} unpickable row(s), first {bad[0]!r}")
        if drawn.seconds > PICKER_SECONDS:
            problems.append(f"{where}: slower than {PICKER_SECONDS:g}s")
        answers[key] = drawn.rows[0][0]
    assert not problems, "\n".join(problems)


@needs_live_workspace
def test_escaping_any_command_picker_runs_nothing_and_exits_clean():
    """e271e55: a command input returns nothing on Escape, VS Code records no
    substitution, and the task starts with the literal in its arguments -- 25 of 45
    tasks then ran, or failed loudly, for a run the user had called off."""
    tasks = block()
    inputs = {spec["id"]: spec for spec in tasks["inputs"]}
    answers = defaults(inputs)
    pickers = command_inputs(inputs)
    problems: list[str] = []
    cancelled = 0
    for task in tasks["tasks"]:
        argv = argv_of(task)
        if not pickers & set(reads(" ".join(argv))):
            continue
        cancelled += 1
        inner = task_index.unwrap_command([substitute(token, answers) for token in argv])
        cwd = substitute(
            str(task.get("options", {}).get("cwd", "${workspaceFolder:devkit}")), answers
        )
        started = time.monotonic()
        done = subprocess.run(
            inner,
            cwd=cwd,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
        seconds = time.monotonic() - started
        if done.returncode != 0 or seconds > CANCEL_SECONDS:
            said = (done.stderr or done.stdout).strip()[-300:]
            problems.append(f"{task['label']}: exit {done.returncode} in {seconds:.1f}s: {said}")
    assert cancelled, "no task reads a command picker; the scan is broken"
    assert not problems, "a cancelled task did something:\n" + "\n".join(problems)
