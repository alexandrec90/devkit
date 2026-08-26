"""Recognising a VS Code task input that was dismissed rather than answered.

**Escape cancels a task only when the input is one of VS Code's own.** A
`promptString` or `pickString` that is dismissed aborts the run before anything starts
-- which is why *DB: New Migration (Autogenerate)* stops dead when its message box is
escaped. A `command` input cannot do that: the extension returns `undefined`, VS Code
records no substitution for it, and the task launches anyway with the literal
`${input:dbCheckout}` still sitting in the argument list. Every multi-select picker in
`workspace.jsonc` is a command input, so for those tasks Escape is indistinguishable
from a click unless the receiving script says otherwise.

The extension's `checkEscapedUI` is not the missing piece, and reading it as one is the
mistake this module exists to close off. It aborts no launch. It suppresses the
*remaining* prompts of a compound task once one has been escaped, through a sticky bit
in the extension's remember store that nothing clears but a successful opted-in pick --
which cannot happen while the bit is set. `workspace.jsonc` carries the full account on
`previewRow`; the short version is that opting in makes the *next* click silent too,
and never makes this one stop.

So the cancel is recognised here instead, at the entry point that would otherwise
interpret the literal. Left alone it reaches `resolve_project` as
`unknown project '${input:project}'`: a red task, a failure toast and a log artifact,
all describing a run the user cancelled.

Pure and stdlib-only; every decision is an importable function tested in
`tests/test_task_input.py`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

# What VS Code leaves in an argument when it has no value to put there. The id class is
# deliberately narrow -- `workspace.jsonc` spells every input id in it, and a wider one
# would start claiming interpolation that belongs to something else.
UNRESOLVED_INPUT = re.compile(r"\$\{input:([A-Za-z0-9_-]+)\}")


def cancelled_inputs(argv: Sequence[str]) -> tuple[str, ...]:
    """The ids of the prompts that were dismissed, in the order they appear.

    De-duplicated: a task may spell the same input twice, and the line printed for it
    should name each prompt once.
    """
    found = (match.group(1) for arg in argv for match in UNRESOLVED_INPUT.finditer(str(arg)))
    return tuple(dict.fromkeys(found))


def cancel_report(prog: str, ids: Sequence[str]) -> str:
    """The single line a cancelled task prints before exiting 0.

    It names the prompts because a compound task has several, and which one was
    dismissed is the only thing the terminal can still tell you -- the quick-pick that
    never opened left no other trace. Exiting 0 is the other half: a cancel is not a
    failure, and reporting one would put a red icon and a toast on the user's own
    decision not to run the task.
    """
    return f"{prog}: cancelled -- nothing ran (dismissed: {', '.join(ids)})"
