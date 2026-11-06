#!/usr/bin/env python3
"""What `devkit.fixPass` says on this machine, for a job that must not import the pass.

Standard library and nothing else, because `fix-pass-watchdog.py` reads it and that file
imports nothing the pass does: it has to run when the pass cannot be imported. So the
setting is found by pattern in the text, with no JSONC parser. `fix_cycle.mode_from_workspace`
is the pass's own reading, which also refuses a misspelt value; `tests/test_fix_switch.py`
pins the spellings the two share.

Tested in `tests/test_fix_switch.py`.
"""

from __future__ import annotations

import re
from pathlib import Path

OFF = "off"
DISPATCH = "dispatch"
MODE = re.compile(r'"devkit\.fixPass"\s*:\s*"(\w+)"')


def workspace_mode(workspace: Path) -> str:
    """The switch in `workspace`, or `OFF` for a file that cannot be read or lacks it."""
    try:
        found = MODE.search(workspace.read_text(encoding="utf-8"))
    except OSError:
        return OFF
    return found.group(1) if found else OFF
