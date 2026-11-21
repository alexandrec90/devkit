"""What a workspace task's command became, written instead of running it.

`tests/test_vscode_tasks_e2e.py` renders the workspace with every task's command replaced
by this file, so a real VS Code resolves every `${input:...}` exactly as a click would and
nothing the task names actually runs -- `Agent: Delete Worktrees` included.

    python receipt.py <receipts dir> <label> -- <the task's own command and arguments>

Appends one JSON line per run to `<receipts dir>/<slug of label>.jsonl`. Stdlib only:
it runs under whatever `python` the task's terminal finds.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def slug(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", label).strip("_")


def main(argv: list[str]) -> int:
    where, label, sep, *command = argv
    if sep != "--":
        print(f"receipt.py: expected -- after the label, got {sep!r}", file=sys.stderr)
        return 2
    target = Path(where) / f"{slug(label)}.jsonl"
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"label": label, "argv": command}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
