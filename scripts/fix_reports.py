#!/usr/bin/env python3
"""The one channel back from a fixer: the stamp a dispatch leaves, the report it may write.

The fix pass learned a session's outcome only from the gate. A fixer that stopped with
"cannot be done", or a background session that died on a permission prompt, left a
ledger entry and nothing else, and the record read "already dispatched" for as long as
the entry stood. Two files under the worktree's ignored `logs/` close that:

- `logs/fix-dispatch.json`, written by the pass when it opens a session in a worktree:
  the ledger key the dispatch was recorded under, so a report from that tree can be
  matched back to the decision it answers, whatever branch the tree is on.
- `logs/fix-blocked.md`, written by the fixer instead of an intent when it cannot
  finish: what is in the way, in its own words. The pass reads it, marks the ledger
  entry blocked (`fix_ledger.mark_blocked`) so no second session is spent, and puts the
  reason on the record where a person reads it.

`agent_trees` is the walk both this and `ship_intent.find_intents` make: every
worktree of every registered checkout, through `git worktree list`, so a box, a
`--worktree` checkout and the static checkout on a task branch are all found the same
way. Stdlib plus this repo's modules. Tested in `tests/test_fix_reports.py`.
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_worktrees as aw
import sweep

STAMP_FILE = Path("logs") / "fix-dispatch.json"
BLOCKED_FILE = Path("logs") / "fix-blocked.md"
# The message a refused intent was being shipped with, set aside by the pass at
# dispatch (`ship_intent.set_aside`) where the fixer it sent can reuse it.
REFUSED_FILE = Path("logs") / "ship-intent.refused.md"

# How much of a report reaches the record: one line's worth, not the essay.
REASON_LIMIT = 400

GitFor = Callable[[Path], Callable[..., object]]


@dataclass(frozen=True)
class Blocked:
    project: str
    tree: Path
    branch: str
    key: str  # the stamp's ledger key, or "" when the tree carries no stamp
    reason: str


def stamp(
    tree: Path,
    key: str,
    what: str,
    now: _dt.datetime | None = None,
    owns_branch: bool | None = None,
) -> Path:
    """Mark the tree with the key this dispatch is recorded under.

    A report left by an earlier session in the same tree is cleared with it: read
    against the new key, it would mark this dispatch blocked before its session had
    started, and a blocked entry never expires.

    `owns_branch` says the pass cut the branch for this fixer, so the PR shipped from it
    is the pass's own and carries `automerge` (`fixer_owns_branch`). None keeps what an
    earlier stamp said: a fixer sent back at a fixer's red PR or refused commit is still
    on the pass's branch, and one sent at a feature session's is still on the feature's.
    """
    when = (now or _dt.datetime.now(_dt.UTC)).isoformat(timespec="seconds")
    path = tree / STAMP_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    (tree / BLOCKED_FILE).unlink(missing_ok=True)
    if owns_branch is None:
        owns_branch = fixer_owns_branch(tree)
    payload = {"key": key, "what": what, "when": when, "owns_branch": owns_branch}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def read_stamp(tree: Path) -> dict:
    try:
        loaded = json.loads((tree / STAMP_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def fixer_owns_branch(tree: Path) -> bool:
    """Whether the tree's branch was cut by the pass for a fixer; False when unstamped.

    Only `True` counts: a stamp from before the field existed, or a hand-picked tree,
    reads as a feature session's branch, which is the side that waits for a person.
    """
    return read_stamp(tree).get("owns_branch") is True


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


def find_blocked(root: Path, projects: list[str], git_for: GitFor = sweep.git_for) -> list[Blocked]:
    """Every worktree carrying a blocked report, with the key it was dispatched under."""
    found: list[Blocked] = []
    for project, tree, branch in agent_trees(root, projects, git_for):
        reason = blocked_reason(tree)
        if reason:
            key = str(read_stamp(tree).get("key", ""))
            found.append(Blocked(project, tree, branch, key, reason))
    return found
