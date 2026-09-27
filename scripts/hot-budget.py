#!/usr/bin/env python3
"""Check devkit's always-loaded instruction tier against its ceiling, at commit time.

`tests/test_instruction_budget.py` holds the same ratchet, and until this ran from
`.pre-commit-config.yaml` it was first met in CI: #390, #398 and #405 each went red on
it and each took a fixer and a gate round trip to prune what one line here names. The
measuring is `instruction-budget.py`'s; this only compares its hot total with
`HOT_CEILING` and names the hot files when it is over. It writes no report.

Stdlib only. Tested in `tests/test_instruction_budget.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "precommit"))
from _loader import load_by_path

REPO_ROOT = Path(__file__).resolve().parents[1]

# devkit's hot total plus about a sentence (50 tok), never a paragraph (100). It only goes
# down: the test that reads it says why, and what to do instead of raising it.
HOT_CEILING = 5550


def verdict(docs: list, ceiling: int = HOT_CEILING) -> tuple[int, str]:
    """`(exit code, the line to print)` for the hot tier of `docs`."""
    total = sum(doc.tokens for doc in docs if doc.tier == "hot")
    line = f"instruction budget: hot {total}/{ceiling} tok"
    if total <= ceiling:
        return 0, line
    hot = sorted((d for d in docs if d.tier == "hot"), key=lambda d: -d.tokens)
    named = ", ".join(f"{d.rel} ({d.tokens})" for d in hot)
    return 1, f"{line}; move a section to a lazy tier or prune, never raise it. Hot: {named}"


def main(root: Path = REPO_ROOT) -> int:
    budget = load_by_path("instruction_budget", root / "scripts" / "instruction-budget.py")
    code, line = verdict(budget.discover(root, budget.manifest_paths(root)))
    print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
