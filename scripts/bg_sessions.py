#!/usr/bin/env python3
"""Stop the background sessions the fix pass sent once they have finished.

A `claude --bg` session does not exit when its work is done: it goes idle and stays a
process. After the first two supervised runs, fourteen were alive -- every fixer the
pass had ever sent, each holding memory, and each listed by `claude agents` as though
it were still in flight. The pass knows when one has finished (its tree carries an
intent or a report, `fix_reports.session_state`), so it stops it: `claude stop` keeps
the conversation, and `claude attach <id>` reopens it.

Only a background session that is idle, in a tree whose stamped session is finished or
dead, is stopped -- never an interactive one, and never one still working. The same
listing says which trees a session is busy in (`working`), which the pass holds a
dispatch off.

Tested in `tests/test_bg_sessions.py`.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Iterable

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def listed(runner: Runner) -> list[dict]:
    """Every session `claude agents --json` reports; empty when it cannot say."""
    try:
        done = runner(["claude", "agents", "--json"], check=False, capture_output=True, text=True)
    except OSError:
        return []
    if done.returncode != 0:
        return []
    try:
        rows = json.loads(done.stdout or "[]")
    except ValueError:
        return []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def finished_in(rows: Iterable[dict], trees: Iterable[str]) -> list[dict]:
    """The idle background sessions whose working directory is one of `trees`."""
    wanted = {_key(tree) for tree in trees}
    return [
        row
        for row in rows
        if row.get("kind") == "background"
        and row.get("status") == "idle"
        and _key(str(row.get("cwd", ""))) in wanted
    ]


def working(rows: Iterable[dict]) -> frozenset[str]:
    """The directories some session -- background or interactive -- is busy in right now.

    What a transcript's age cannot say: a fixer that left its intent reads as finished,
    and one kept editing after it. The pass sent #422's resolver into that tree while
    its own sweep was still mid-merge there, and the two raced one checkout (d821bd8f).
    """
    return frozenset(_key(str(row.get("cwd", ""))) for row in rows if row.get("status") == "busy")


def busy_in(dirs: frozenset[str], tree: object) -> bool:
    """Whether `tree` is one of the directories `working` returned."""
    return _key(str(tree)) in dirs


def _key(path: str) -> str:
    """A path as text that compares equal across slashes and case, on any OS: `claude`
    reports Windows paths, and on the Linux gate `Path` keeps a backslash as a letter."""
    return path.replace("\\", "/").rstrip("/").lower()


def stop_finished(trees: Iterable[str], runner: Runner) -> list[str]:
    """Stop each finished session in `trees`; `(id in tree)` for each one stopped."""
    stopped = []
    for row in finished_in(listed(runner), trees):
        ident = str(row.get("id", ""))
        done = runner(["claude", "stop", ident], check=False, capture_output=True, text=True)
        if done.returncode == 0:
            stopped.append(f"{ident} in {row.get('cwd', '')}")
    return stopped
