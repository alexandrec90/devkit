#!/usr/bin/env python3
"""The two quick-picks the *Machine: Scheduled Jobs* task draws, and the one of them
that declines to be asked.

Both lists are static, so neither is here for freshness -- `rioj7.command-variable` could
template them out of the workspace file and did. They run a command for the one thing a
templated list cannot do: **VS Code resolves every `${input:...}` a task's arguments name,
unconditionally and in the order they appear**, and there are no conditional inputs. So
"apply an uninstall?" was asked after a `status` that removes nothing -- the question a
person answers wrongly once and then stops reading.

`augustocdias.tasks-shell-input` substitutes an earlier input's answer into a later
input's command, so `apply_rows` can be handed the verb; with `useSingleResult` a one-row
answer is taken without drawing a quick-pick, and the prompt is simply not asked. The
task's own comments carry the wiring and `.claude/rules/vscode-tasks.md` the convention.

**Separate from `installers.py` because the gate said so.** Adding these to it put the
module at 22 definitions and 558 lines against limits of 20 and 500 --
`structure_check`'s answer to which is the split, never the raised baseline. The seam is
real either way: nothing here reads the machine, spawns an installer or writes an
artifact, and `installers.py` never draws anything.

Stdlib only, like everything the task path touches, and every decision is an importable
function tested in `tests/test_installer_picker.py`.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import picker_rows

# The verb a row's value becomes: `installers.py`'s `mode`, so a value here that its
# parser does not take is a usage error on a click that looked legitimate.
UNINSTALL = "uninstall"

# The safe token. A row with an empty value is dropped by the picker's
# `filterEmptyResults`, and a quick-pick left with no options is an error rather than a
# default -- so the branch that asks nothing still has to supply a real one.
DRY_RUN = "--dry-run"


def verb_rows() -> list[str]:
    """What should happen to the ticked jobs. `status` first: the read-only verb is the
    one wanted most often, and a mis-click on the first row must change nothing.

    **The label says what the verb does; the value is still the `mode` argparse takes.**
    The three CLI words were the whole quick-pick until 2026-09-17, and `maintain` in
    particular told a reader nothing -- least of all that it is the verb that *installs*,
    which is what a machine missing a job needs and what the fresh-PC bootstrap runs at
    its last step. A picker that assumes you already know the vocabulary is a picker for
    the person who did not need it.
    """
    return [
        picker_rows.row(
            "status",
            "Check only -- report, change nothing",
            "status",
            "Asks every installer whether what it registers is on this machine, and says so. "
            "Registers nothing, removes nothing.",
        ),
        picker_rows.row(
            "maintain",
            "Install or repair -- register whatever is missing",
            "maintain",
            "Runs every installer that reports itself missing or out of date, and leaves the "
            "rest alone. This is how a job gets onto a machine, fresh PC included; the "
            "devkit-installers job runs the same pass daily and at logon.",
        ),
        picker_rows.row(
            UNINSTALL,
            "Remove -- take them off this machine",
            UNINSTALL,
            "Unregisters what the installers registered. Touches no checkout and no logs, "
            "and asks next whether to print the plan or actually do it.",
        ),
    ]


def apply_rows(verb: str) -> list[str]:
    """Whether an `uninstall` applies -- and, for every other verb, one row rather than a
    question.

    A single row is how this picker declines to ask: the task sets `useSingleResult`, so
    a list of one is taken without drawing a quick-pick.

    Anything that is not `uninstall` takes the quiet branch, the unresolved literal an
    escaped verb leaves behind included: nothing is removed on that path, and
    `devkit_project` recognises the literal and runs nothing at all.
    """
    if verb.strip() != UNINSTALL:
        return [
            picker_rows.row(
                DRY_RUN,
                "not asked",
                "nothing is being removed",
                "This row is taken without prompting; only uninstall has a second answer.",
            )
        ]
    return [
        picker_rows.row(
            DRY_RUN,
            "Dry run -- print the plan, remove nothing",
            DRY_RUN,
            "Lists what would be unregistered, in the order it would come off.",
        ),
        picker_rows.row(
            "--yes",
            "Remove them -- this one applies",
            "--yes",
            "The maintainer comes off first; see installers.MAINTAINER for why that ordering "
            "matters. Getting them back is this task again with Install or repair.",
        ),
    ]


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "rows",
        choices=("verb", "apply"),
        help="which picker to draw, one `value|label|description|detail` line per option",
    )
    parser.add_argument(
        "--verb",
        default="",
        help=(
            "the verb `apply` is choosing for; anything but `uninstall` -- an escaped "
            "picker's unresolved literal included -- draws the single quiet row"
        ),
    )
    return parser.parse_args(sys.argv[1:] if argv is None else argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Nothing but rows on stdout: a picker's stdout *is* the quick-pick, so a status
    line, a warning or a progress message here is an extra option a person can tick."""
    args = parse_args(argv)
    picker_rows.emit(verb_rows() if args.rows == "verb" else apply_rows(args.verb))
    return 0


if __name__ == "__main__":
    sys.exit(main())
