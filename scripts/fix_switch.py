#!/usr/bin/env python3
"""What `devkit.fixPass` says on this machine, for a job that must not import the pass.

Standard library and nothing else, because `fix-pass-watchdog.py` reads it and that file
imports nothing the pass does: it has to run when the pass cannot be imported. So the
setting is found by pattern in the text, with no JSON or JSONC parser.
`fix_cycle.mode_here` is the pass's own reading, which also refuses a misspelt value;
`tests/test_fix_switch.py` pins the spellings the two share.

The switch is this PC's, in the machine file beside the live workspace file
(`project_registry.MACHINE_FILE_NAME`, spelled here rather than imported). A machine
file that does not set it -- or is not there -- leaves the workspace file to answer, as
`project_registry.setting` does.

Tested in `tests/test_fix_switch.py`.
"""

from __future__ import annotations

import re
from pathlib import Path

OFF = "off"
DISPATCH = "dispatch"
MODE = re.compile(r'"devkit\.fixPass"\s*:\s*"(\w+)"')
MACHINE_FILE_NAME = ".devkit-machine.json"


def _found(path: Path) -> str | None:
    """The switch as `path` spells it, or None for a file that cannot be read or lacks it."""
    try:
        found = MODE.search(path.read_text(encoding="utf-8"))
    except OSError:
        return None
    return found.group(1) if found else None


def workspace_mode(workspace: Path) -> str:
    """The switch for the machine whose live workspace file is `workspace`: the machine
    file beside it, else `workspace` itself, else `OFF`."""
    for path in (workspace.parent / MACHINE_FILE_NAME, workspace):
        if (mode := _found(path)) is not None:
            return mode
    return OFF
