#!/usr/bin/env python3
"""What `devkit.fixPass` says on this machine, for a job that must not import the pass.

Standard library and nothing else, because `fix-pass-watchdog.py` reads it and that file
imports nothing the pass does: it has to run when the pass cannot be imported. So the
setting is found by pattern in the text, with no JSON parser.
`fix_cycle.mode_here` is the pass's own reading, which also refuses a misspelt value;
`tests/test_fix_switch.py` pins the spellings the two share.

The switch is this PC's, in the machine file at the projects root
(`project_registry.MACHINE_FILE_NAME`, spelled here rather than imported), and nowhere
else: a machine file that does not set it -- or is not there -- is `OFF`, as
`project_registry.setting` reads it. The retired live workspace file is not consulted.

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


def root_mode(root: Path) -> str:
    """The switch for the machine whose projects root is `root`: its machine file's
    value, else `OFF`."""
    return _found(root / MACHINE_FILE_NAME) or OFF
