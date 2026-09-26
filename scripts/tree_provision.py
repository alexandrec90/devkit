#!/usr/bin/env python3
"""Install a worktree's toolchain before a session opens in it.

A fresh worktree checks out tracked files only, so it has no `.venv` and no
`node_modules`. Fixers kept opening in trees like that -- four of five sessions in the
first fix-pass audit, and ibkr_trader's and devkit's in the first supervised run -- and
spent their opening turns on the bootstrap a script can do. `worktree.py provision
--yes` is the one verb that provisions any tree; without `--yes` it only prints the
plan, which is how the first session to follow "run the provisioning command" was left
without a `.venv`.

Split out of `fix-prs.py`, which calls it on every tree it opens a session in.

Tested in `tests/test_tree_provision.py`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sweep

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKTREE = REPO_ROOT / "scripts" / "worktree.py"


def argv(tree: Path) -> list[str]:
    return [sweep.console_python(), str(WORKTREE), "provision", str(tree), "--yes"]


def provision(tree: Path, runner=subprocess.run) -> bool:
    """Whether the tree's toolchain is installed. A failure is printed and the session
    still goes: it may not need the toolchain, and if it does, its transcript files the
    friction."""
    done = runner(argv(tree), check=False, capture_output=True, text=True)
    if done.returncode != 0:
        tail = " ".join(((done.stderr or "") + (done.stdout or "")).split())[-200:]
        print(f"  provisioning {tree} failed: {tail}", file=sys.stderr)
        return False
    return True
