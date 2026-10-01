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
    return [row for row in rows if stoppable(row) and _key(str(row.get("cwd", ""))) in wanted]


def stoppable(row: dict) -> bool:
    """Whether the pass may stop this session: a background one with nothing in flight."""
    return row.get("kind") == "background" and row.get("status") == "idle"


def in_tree(rows: Iterable[dict], tree: object) -> list[dict]:
    """Every session working in `tree` or under it."""
    root = _key(str(tree))
    return [
        row
        for row in rows
        if (cwd := _key(str(row.get("cwd", "")))) == root or cwd.startswith(root + "/")
    ]


def working(rows: Iterable[dict], kinds: tuple[str, ...] = ()) -> frozenset[str]:
    """The directories a session of `kinds` (empty: any) is busy in right now.

    What a transcript's age cannot say: a fixer that left its intent reads as finished,
    and one kept editing after it. The pass sent #422's resolver into that tree while
    its own sweep was still mid-merge there, and the two raced one checkout (d821bd8f).
    """
    return frozenset(
        _key(str(row.get("cwd", "")))
        for row in rows
        if row.get("status") == "busy" and (not kinds or row.get("kind") in kinds)
    )


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
        if stop(ident, runner):
            stopped.append(f"{ident} in {row.get('cwd', '')}")
    return stopped


def stop(ident: str, runner: Runner) -> bool:
    """`claude stop <ident>`, which keeps the conversation; whether it answered yes."""
    try:
        done = runner(["claude", "stop", ident], check=False, capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0
