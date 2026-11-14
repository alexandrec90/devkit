#!/usr/bin/env python3
"""Keep pre-commit's shared cache usable by a process that cannot read all of it.

pre-commit clones each hook repository into its store (`~/.cache/pre-commit`) through
`tempfile.mkdtemp`, and on Windows CPython gives that directory a *protected* ACL:
SYSTEM, Administrators and OWNER RIGHTS, nothing inherited. A clone made by an elevated
process is owned by `BUILTIN\\Administrators`, so OWNER RIGHTS names that group too, and
an unelevated process -- every scheduled job, whose token holds Administrators for deny
only -- is refused the whole directory. pre-commit reads that refusal as a missing
manifest (`InvalidManifestError: ...\\.pre-commit-hooks.yaml is not a file`), and the
commit fails before any hook runs. It cost the release job v0.11.26 (2026-09-26) and the
v0.11.32 adoption in two projects (2026-09-28), because fixer sessions run elevated on
this machine and cloned three of the store's repositories on 2026-09-24.

A process that cannot read a directory cannot repair its ACL either, so the unreadable
row is dropped from the store's database instead: pre-commit then clones that
repository afresh into a directory this process owns. The old directory stays, as
nothing unelevated can delete it; `heal` names it so an elevated shell can. A hook
environment an elevated process installs into a clone later is refused the same way,
which is why every directory directly under a clone is listed too.

Tested in `tests/test_precommit_store.py`.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import closing
from pathlib import Path

DB_NAME = "db.db"


def store_dir(env: Mapping[str, str] | None = None) -> Path:
    """Where pre-commit keeps its store, by pre-commit's own rule."""
    env = os.environ if env is None else env
    if env.get("PRE_COMMIT_HOME"):
        return Path(env["PRE_COMMIT_HOME"])
    cache = env.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(cache) / "pre-commit"


def readable(path: Path) -> bool:
    """`path` and every directory directly inside it can be listed by this process."""
    try:
        with os.scandir(path) as entries:
            children = [entry.path for entry in entries if entry.is_dir(follow_symlinks=False)]
        for child in children:
            os.listdir(child)
    except OSError:
        return False
    return True


def heal(store: Path | None = None, can_read: Callable[[Path], bool] = readable) -> list[str]:
    """Drop every store row whose clone this process cannot read; what was dropped.

    Nothing to do, and nothing raised, when there is no store or no database yet: the
    commit that follows is what creates them.
    """
    store = store_dir() if store is None else store
    db = store / DB_NAME
    if not db.is_file():
        return []
    try:
        with closing(sqlite3.connect(db, timeout=30)) as conn, conn:
            rows = conn.execute("SELECT repo, ref, path FROM repos").fetchall()
            dead = [(repo, ref, path) for repo, ref, path in rows if not can_read(Path(path))]
            conn.executemany(
                "DELETE FROM repos WHERE repo = ? AND ref = ?", [(r, f) for r, f, _ in dead]
            )
    except sqlite3.Error:
        return []
    return [f"{repo}@{ref} ({path})" for repo, ref, path in dead]


def say_healed(dropped: list[str]) -> str:
    """One line for a job's log about what `heal` dropped; "" when nothing was."""
    if not dropped:
        return ""
    return (
        "pre-commit store: re-cloning what this process cannot read (an elevated process "
        f"made it; an elevated shell may delete the old directory): {', '.join(dropped)}"
    )


if __name__ == "__main__":
    print(say_healed(heal()) or "pre-commit store: every clone is readable")
