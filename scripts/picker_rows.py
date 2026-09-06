#!/usr/bin/env python3
"""The lines a live VS Code picker draws, and the containment they need.

`augustocdias.tasks-shell-input`'s `shellCommand.execute` runs a command when a task's
input resolves and turns its **stdout** into the quick-pick: one line per option, split
on `fieldSeparator` into `value|label|description|detail`, of which only the value comes
back to the task. That is the whole contract, and all of it is positional -- so a field
carrying a separator silently becomes two fields, and a field carrying a newline silently
becomes two rows, the second of them unpickable and holding a value nobody wrote.

Four scripts build these lists and none of them owns the format, which is why it lives
here rather than in whichever one was converted first. `.claude/rules/vscode-tasks.md`
carries when to reach for a live picker at all; this module is only the shape.

Tested in `tests/test_picker_rows.py`.
"""

from __future__ import annotations

from collections.abc import Iterable

# What the input's `fieldSeparator` is set to, in every task that uses one. A pipe
# because no checkout name, branch name or PR number can contain it -- and a PR *title*
# can, which is what `cell` is for.
FIELD_SEP = "|"

# The value a row carries when picking it should run nothing. A bare word rather than
# anything punctuated: the pick reaches its script as `--picks <value>`, and argparse
# reads a leading `-` as an option, so a sentinel with one fails the task with a usage
# error on a click that meant "never mind".
NOTHING = "none"


def cell(text: object) -> str:
    """One field: a single line, with no `FIELD_SEP` left in it.

    Applied to every field rather than to the ones thought to be risky. The fields that
    look safe are safe because of what currently generates them, which is not a property
    anything holds, and the failure is silent in both directions -- a shifted field draws
    a row whose description is its detail, and a split line draws a row whose *value* is
    somebody's prose.
    """
    return " ".join(str(text).replace(FIELD_SEP, "/").split())


def row(value: object, label: object, description: object = "", detail: object = "") -> str:
    """One quick-pick line. Only `value` comes back; the rest is what the reader sees.

    `description` renders beside the label and `detail` on its own line beneath, so a row
    reads as a heading, a qualifier and a sentence. All four are cleaned, and the order
    is positional and fixed.
    """
    return FIELD_SEP.join((cell(value), cell(label), cell(description), cell(detail)))


def nothing_row(label: str, description: str, detail: str = "picking this runs nothing") -> str:
    """The row a scan that found nothing draws.

    An empty stdout is a legitimate answer that the quick-pick cannot distinguish from a
    command that failed, a wrong path or an unauthenticated `gh` -- every one of those
    also draws no options. So "nothing to do" is stated as a row, and the receiving
    script recognises `NOTHING` and runs nothing.
    """
    return row(NOTHING, label, description, detail)


def emit(rows: Iterable[str]) -> None:
    """Write the rows to stdout, which *is* the quick-pick.

    Nothing else may be printed on this path: a status line, a warning or a progress
    message becomes an extra option a person can tick.
    """
    for line in rows:
        print(line)
