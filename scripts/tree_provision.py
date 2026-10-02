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

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))  # the vendored tier
import fix_reports
import harness_config
import sweep
import worktree_env

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKTREE = REPO_ROOT / "scripts" / "worktree.py"


def argv(tree: Path) -> list[str]:
    return [sweep.console_python(), str(WORKTREE), "provision", str(tree), "--yes"]


def frontend_missing(tree: Path) -> bool:
    """Whether the tree's frontend tier is on and its `node_modules` holds no finished
    install. That covers a missing directory and also a linked one: a `claude --worktree`
    tree's `node_modules` linked to an empty directory in its checkout, and `vite` could
    not start (fcf111b9)."""
    frontend = harness_config.load(tree).frontend
    return frontend.enabled and not worktree_env.node_modules_installed(
        tree / frontend.dir / "node_modules"
    )


def needs_provision(tree: Path) -> bool:
    """Whether the git hook left any of the tree's toolchain to install. The hook installs
    Python alone and marks the tree provisioned anyway, so its mark proves the `.venv` and
    nothing about `node_modules`: a roguelike tree came up with vitest unable to run."""
    if not (tree / worktree_env.PROVISIONED).is_file():
        return True
    return frontend_missing(tree)


def provision(tree: Path, runner=sweep.run_windowless) -> bool:
    """Whether the tree's toolchain is installed. The session still goes on a failure --
    it may not need the toolchain -- and the failure is left in the tree's friction
    file, which the next pass files on the ledger: printed alone, it reached nobody.

    Read as UTF-8: the console code page could not decode a byte of `uv`'s output, and
    the reader thread's traceback in the pass's output read as the pass crashing."""
    done = runner(
        argv(tree),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if done.returncode != 0:
        tail = " ".join(((done.stderr or "") + (done.stdout or "")).split())[-200:]
        print(f"  provisioning {tree} failed: {tail}", file=sys.stderr)
        friction = tree / fix_reports.FRICTION_FILE
        friction.parent.mkdir(parents=True, exist_ok=True)
        with friction.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"- provisioning this tree failed before the session opened: {tail}\n")
        return False
    return True
