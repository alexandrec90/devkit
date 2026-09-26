#!/usr/bin/env python3
"""The channels back from a session: the stamp a dispatch leaves, and what came of it.

The fix pass learned a session's outcome only from the gate. A fixer that stopped with
"cannot be done", or a background session that died on a permission prompt, left a
ledger entry and nothing else, and the record read "already dispatched" for as long as
the entry stood. Files under the worktree's ignored `logs/` close that:

- `logs/fix-dispatch.json`, written by the pass when it opens a session in a worktree:
  the ledger key and problem the dispatch was recorded under, so anything from that
  tree can be matched back to the decision it answers, whatever branch the tree is on.
- `logs/fix-blocked.md`, written by the fixer instead of an intent when it cannot
  finish. The pass marks the ledger entry blocked and escalates it as a finding
  (`fix_budget.budget`), then files the report away so it is read once.
- `logs/friction.md`, which any session may write: one line per thing the harness cost
  it turns. The pass files each line on the harness-defect ledger.
- No outcome at all: a stamped tree with no intent, no report and a transcript gone
  quiet (`session_state`) is a session that died, which frees its dispatch at once.

`agent_trees` is the walk both this and `ship_intent.find_intents` make: every
worktree of every registered checkout, through `git worktree list`, so a box, a
`--worktree` checkout and the static checkout on a task branch are all found the same
way. Stdlib plus this repo's modules. Tested in `tests/test_fix_reports.py`.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_worktrees as aw
import sweep

STAMP_FILE = Path("logs") / "fix-dispatch.json"
BLOCKED_FILE = Path("logs") / "fix-blocked.md"
FRICTION_FILE = Path("logs") / "friction.md"
# The message a refused intent was being shipped with, set aside by the pass at
# dispatch (`ship_intent.set_aside`) where the fixer it sent can reuse it.
REFUSED_FILE = Path("logs") / "ship-intent.refused.md"
# What a session leaves when it is done one way or another. The intent names are
# `ship_intent`'s; spelled here because that module imports this one.
OUTCOME_FILES = (
    Path("logs") / "ship-intent.md",
    Path("logs") / "ship-intent.shipped.md",
    Path("logs") / "ship-state.json",
    BLOCKED_FILE,
    Path("logs") / "fix-blocked.filed.md",  # the report once the pass has read it
)

# How much of a report reaches the record: one line's worth, not the essay.
REASON_LIMIT = 400

# A dispatched session with no transcript this long after its stamp never started; one
# whose transcript has been quiet this long, with nothing left behind, has ended. Both
# well past a permission prompt a person might still answer in a tab.
START_GRACE = _dt.timedelta(minutes=30)
QUIET_AFTER = _dt.timedelta(minutes=90)
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"

GitFor = Callable[[Path], Callable[..., object]]


def stamp(
    tree: Path,
    key: str,
    what: str,
    now: _dt.datetime | None = None,
    problem: str = "",
    agent: str = "claude",
) -> Path:
    """Mark the tree with the key this dispatch is recorded under.

    A report left by an earlier session in the same tree is cleared with it: read
    against the new key, it would mark this dispatch blocked before its session had
    started.
    """
    when = (now or _dt.datetime.now(_dt.UTC)).isoformat(timespec="seconds")
    path = tree / STAMP_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    (tree / BLOCKED_FILE).unlink(missing_ok=True)
    payload = {"key": key, "what": what, "when": when, "problem": problem, "agent": agent}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def note_on_stamp(tree: Path, field: str, value: str) -> None:
    """Add `field` to the tree's stamp, so a verdict about this dispatch is reached once."""
    payload = read_stamp(tree)
    if payload:
        payload[field] = value
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        (tree / STAMP_FILE).write_text(text, encoding="utf-8")


def file_away(tree: Path, relative: Path) -> None:
    """`logs/x.md` -> `logs/x.filed.md`: read once, kept for whoever looks at the tree."""
    source = tree / relative
    if source.is_file():
        source.replace(source.with_name(f"{source.stem}.filed{source.suffix}"))


def read_stamp(tree: Path) -> dict:
    try:
        loaded = json.loads((tree / STAMP_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def blocked_reason(tree: Path) -> str:
    """The report's text as one line, cut to `REASON_LIMIT`; "" when there is none."""
    try:
        text = (tree / BLOCKED_FILE).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    words = " ".join(line.strip().lstrip("#").strip() for line in text.splitlines()).split()
    return " ".join(words)[:REASON_LIMIT]


def agent_trees(
    root: Path, projects: list[str], git_for: GitFor = sweep.git_for
) -> Iterator[tuple[str, Path, str]]:
    """`(project, tree, branch)` for every worktree of every checkout on disk.

    A detached tree yields an empty branch. A checkout git cannot list is passed over:
    a missing or broken one is somebody else's report.
    """
    for project in projects:
        project_dir = root / project
        if not project_dir.is_dir():
            continue
        listed = git_for(project_dir)("worktree", "list", "--porcelain")
        if getattr(listed, "returncode", 1) != 0:
            continue
        for path, branch in aw.parse_worktree_list(str(getattr(listed, "stdout", "") or "")):
            yield project, Path(path), branch


# --- what the trees say beyond a report ---------------------------------------------------


@dataclass(frozen=True)
class Tree:
    """One agent worktree and what it carries for the pass."""

    project: str
    path: Path
    branch: str
    stamp: dict
    friction: tuple[str, ...]


def friction_lines(tree: Path) -> tuple[str, ...]:
    """The tree's `logs/friction.md`, one entry per line: headings and blanks dropped,
    list markers stripped."""
    try:
        text = (tree / FRICTION_FILE).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ()
    kept = (line.strip() for line in text.splitlines())
    lines = (line.lstrip("-*0123456789. ").strip() for line in kept if not line.startswith("#"))
    return tuple(line[:REASON_LIMIT] for line in lines if line)


def read_trees(root: Path, projects: list[str], git_for: GitFor = sweep.git_for) -> list[Tree]:
    """Every agent worktree with its stamp and friction, walked once for the whole pass."""
    return [
        Tree(project, tree, branch, read_stamp(tree), friction_lines(tree))
        for project, tree, branch in agent_trees(root, projects, git_for)
    ]


def transcript_dir(tree: Path, projects_root: Path | None = None) -> Path:
    """Where Claude Code keeps the transcripts of sessions started in `tree`."""
    return (projects_root or CLAUDE_PROJECTS) / re.sub(r"[^A-Za-z0-9]", "-", str(tree))


def _mtime(path: Path) -> _dt.datetime | None:
    try:
        return _dt.datetime.fromtimestamp(path.stat().st_mtime, _dt.UTC)
    except OSError:
        return None


def newest_transcript(tree: Path, projects_root: Path | None = None) -> Path | None:
    dated = [
        (when, path)
        for path in transcript_dir(tree, projects_root).glob("*.jsonl")
        if (when := _mtime(path))
    ]
    return max(dated)[1] if dated else None


WORKING = "working"
DONE = "done"
NEVER_STARTED = "never started"
NO_OUTCOME = "ended without an outcome"
DEAD = (NEVER_STARTED, NO_OUTCOME)


def session_state(
    tree: Path, now: _dt.datetime, projects_root: Path | None = None
) -> tuple[str, str]:
    """`(state, transcript)` of the session the tree's stamp sent.

    `DONE` once it left an intent or a report, `WORKING` while its transcript moves (or
    it is still inside `START_GRACE`), one of `DEAD` otherwise. `""` for a tree with no
    stamp, one already judged dead, or a session this cannot see -- only Claude Code
    keeps a transcript here, and a Codex tab is watched by the person who opened it.
    """
    payload = read_stamp(tree)
    if (
        not payload
        or payload.get("dead")
        or not str(payload.get("agent", "claude")).startswith("claude")
    ):
        return "", ""
    try:
        sent = _dt.datetime.fromisoformat(str(payload.get("when", "")))
    except ValueError:
        return "", ""
    if any((_mtime(tree / name) or sent) > sent for name in OUTCOME_FILES):
        return DONE, ""
    transcript = newest_transcript(tree, projects_root)
    touched = _mtime(transcript) if transcript else None
    if touched is None or touched < sent:
        return (NEVER_STARTED, "") if now - sent > START_GRACE else (WORKING, "")
    if now - touched > QUIET_AFTER:
        return NO_OUTCOME, str(transcript)
    return WORKING, str(transcript)
