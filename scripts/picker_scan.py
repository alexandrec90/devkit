#!/usr/bin/env python3
"""The handoff between the two stages of a live picker.

`augustocdias.tasks-shell-input` resolves `${input:<other id>}` inside an input's own
command, by recording each input's answer as it resolves and looking it up for the next
one (`UserInputContext`, and `VariableResolver`'s `input:` branch). So a live picker
*can* be two dependent stages -- "which checkouts, then which of their rows" -- which
`.claude/rules/vscode-tasks.md` spent two releases believing impossible and is the
reason four tasks lost their checkout stage.

Two conditions come with it, both from the extension's own README, and both are the
task's job rather than this module's: the stages must appear **left to right in order of
dependence** in the task's arguments, and every input in the chain must be a
`shellCommand.execute` one.

**What this module owns is the scan in between.** The first stage has to run the full
fan-out to count anything, and the second stage must not run it again -- so stage one
writes the rows it built and hands stage two a **token** naming that exact write. Which
is the whole point of the token, and worth being plain about: the failure this design
has to rule out is the one the file-backed menus died of, a reader believing rows that
some earlier pass wrote. A token that does not match is not repaired and not served
stale; it is a miss, and a miss means the caller rescans. Staleness is therefore not
unlikely here, it is unrepresentable -- the only rows anyone can read are the ones the
click before them produced.

`read` returning None is that miss, and every caller answers it the same way: scan the
ticked checkouts live. That path is *cheaper* than the scan stage one did, because it
covers what was ticked rather than the whole machine, so the fallback is a slower click
and never a wrong list.

Tested in `tests/test_picker_scan.py`.
"""

from __future__ import annotations

import datetime as _dt
import json
import secrets
from pathlib import Path

import picker_rows

# Beside the other artifacts an agent is expected to read, and written by the same
# `logs/` convention: this is a scratch file with a lifetime of one click, not state.
SCANS_DIR = Path(__file__).resolve().parent.parent / "logs"

# What separates a checkout from the token inside a stage-one row's value. An `@`
# rather than the `:` the stage-*two* values use, so a token that reached the wrong
# parser fails to split rather than parsing into a plausible wrong answer.
SEP = "@"

# What the extension joins ticked stage-one values with, and what `parse_projects`
# splits on. A comma because no checkout name and no token can contain one.
LIST_SEP = ","

# Bytes of randomness in a token. It identifies one write for one click -- it is not a
# secret and not a hash of anything, so what it has to be is unrepeatable, which six
# bytes are by a margin that needs no thought.
TOKEN_BYTES = 6


def scan_path(name: str) -> Path:
    """Where the scan called `name` is written. One file per picker, overwritten."""
    return SCANS_DIR / f"picker-scan-{name}.json"


def write(name: str, entries: list[tuple[str, str]]) -> str:
    """Record stage one's rows and return the token that names this write.

    The *rendered rows* rather than the objects behind them, and that is what keeps
    this module free of every caller's types: a row is already a string, already in the
    form stage two has to print, so there is nothing to serialise and nothing to
    reconstruct. The cost is that a row's shape is decided in stage one, which is where
    it was decided anyway.

    **A flat list in the order stage two must print, not a mapping keyed by checkout.**
    Every one of these menus is ranked across the whole machine -- newest PR first,
    trunks before branches -- and a mapping loses that the moment two checkouts are
    ticked, because nothing left in it says which of two rows came first. Grouping is
    recoverable from a list and ordering is not, so the list is what gets stored.
    """
    token = secrets.token_hex(TOKEN_BYTES)
    payload = {
        "token": token,
        "written": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
        "entries": [[str(project), str(line)] for project, line in entries],
    }
    path = scan_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return token


def read(name: str, token: str) -> list[tuple[str, str]] | None:
    """The `(checkout, row)` pairs `token` names, in order, or None if not that scan.

    Every way of not being that scan answers the same: no file, unreadable file, a
    write from an earlier click, a payload whose shape is not the one written here.
    None is not an error -- it is "rescan", and the caller has a cheaper scan to run.
    """
    if not token:
        return None
    try:
        payload = json.loads(scan_path(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("token") != token:
        return None
    entries = payload.get("entries")
    if not isinstance(entries, list):
        return None
    try:
        return [(str(project), str(line)) for project, line in entries]
    except (TypeError, ValueError):
        return None


def select(entries: list[tuple[str, str]], projects: list[str]) -> list[str]:
    """The rows belonging to `projects`, in the order they were written.

    Filtering a ranked list rather than concatenating per-checkout ones, which is the
    whole reason `write` stores a list: ticking three checkouts has to give one menu
    ranked the way the unticked menu was, not three menus end to end.
    """
    wanted = set(projects)
    return [line for project, line in entries if project in wanted]


def project_value(project: str, token: str) -> str:
    """The one token a ticked stage-one row resolves to: `<checkout>@<scan token>`."""
    return f"{project}{SEP}{token}"


def parse_projects(text: str) -> tuple[list[str], str]:
    """`"carameli@ab12,devkit@ab12"` -> `(["carameli", "devkit"], "ab12")`.

    The token comes back empty when the ticked values do not all carry the same one,
    which is the reading that matters: two tokens means the values did not come from
    one draw of the picker, so no write can be the one they name. The checkouts are
    still returned, because they are still what the reader ticked -- only the shortcut
    is refused, and the caller scans them.
    """
    projects: list[str] = []
    tokens: set[str] = set()
    for entry in str(text).split(LIST_SEP):
        project, separator, token = entry.strip().partition(SEP)
        if not separator or not project:
            continue
        if project not in projects:
            projects.append(project)
        tokens.add(token)
    return projects, tokens.pop() if len(tokens) == 1 else ""


def project_row(project: str, token: str, description: str, detail: str = "") -> str:
    """One stage-one row: a checkout, and what stage two would draw for it.

    The description is the caller's because the count is the caller's noun -- "3 broken
    PRs" and "5 refs" are the same field and not the same sentence. What is fixed here
    is the value, which is the half both stages have to agree about.
    """
    return picker_rows.row(project_value(project, token), project, description, detail)
