#!/usr/bin/env python3
"""Flag a directory an elevated process wrote into an agent tree, once, for investigation.

Until 2026-09-29 the operator started sessions from an elevated Windows Terminal (its
default profile carried `"elevate": true`, `wt_profile.py`). pytest run elevated in a tree
leaves a `.pytest_cache` that only Administrators may open, so the unelevated scheduled
reap can never remove that tree; `session_trees.admin_only` names each such tree with the
elevated command that clears it. Seven were cut between 2026-09-15 and 2026-09-29, and
cleared by hand on 2026-10-02.

Since then nothing runs elevated, so one created after `UNELEVATED_SINCE` is not routine:
something started elevated again. A launcher, a scheduled task, a terminal profile -- and
naming the tree alone would leave the cause standing. So each new one is filed as an
`elevated-write` finding on the harness-defect ledger, and the fix pass sends the devkit
session to find what ran elevated and stop it. Each is filed once: `SEEN` records what was
filed, because clearing the directory takes an administrator, which no fixer is, and a
finding re-filed on every run would send a fixer at it every run.

Only each tree's top-level directories are opened. That is where pytest's cache lands,
and a walk through every tree's `.venv` and `node_modules` every fifteen minutes would cost
more than the job it rides in. A file or directory an elevated process made deeper
inherits the tree's ACL and stays openable, so it blocks nothing.

Run by `reap-stale.py`. Tested in `tests/test_elevated_writes.py`.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))
import fix_findings
import harness_triage as triage
import sweep
import worktree_tiers as wt

# After the newest of the seven elevated writes (2026-09-29 16:16 EDT), and before the
# operator's sessions all ran unelevated.
UNELEVATED_SINCE = _dt.datetime(2026, 9, 30, tzinfo=_dt.UTC)
KIND = "elevated-write"
# The directories already filed, beside the artifact the job writes.
SEEN = Path("logs") / "elevated-writes.json"


@dataclass(frozen=True)
class Sighting:
    """One top-level directory of an agent tree that this process may not open."""

    project: str
    tree: Path
    path: Path
    created: _dt.datetime

    def finding(self) -> fix_findings.Finding:
        stamp = self.created.astimezone(_dt.UTC).strftime("%Y-%m-%d %H:%M UTC")
        detail = (
            f"{KIND}: {self.project} {self.tree.name}: {self.path.name} was made by an "
            f"elevated process at {stamp}, after every session moved to unelevated "
            f"({UNELEVATED_SINCE:%Y-%m-%d}) -- find what ran elevated and stop it"
        )
        return fix_findings.Finding(KIND, self.project, detail, evidence=str(self.path))


def trees(root: Path, names: Iterable[str]) -> list[tuple[str, Path]]:
    """`(project, tree)` for every session tree and box under the workspace `root`."""
    found: list[tuple[str, Path]] = []
    for name in names:
        found += [(name, tree) for tree in _children(wt.default_root(root / name))]
    boxes = _children(root / wt.BOXES_DIR_NAME)
    return found + [(box.name.split("--", 1)[0], box) for box in boxes]


def _children(path: Path) -> list[Path]:
    try:
        return sorted(entry for entry in path.iterdir() if entry.is_dir())
    except OSError:
        return []


def _opens(path: Path) -> bool:
    try:
        with os.scandir(path):
            return True
    except PermissionError:
        return False
    except OSError:
        return True  # gone, or not a directory: nothing an elevated process left


def _born(entry: os.DirEntry[str]) -> _dt.datetime | None:
    """When `entry` was made, from its parent's listing: the directory itself may not
    open. `st_birthtime` where the platform keeps it, Windows' `st_ctime` otherwise."""
    try:
        stat = entry.stat(follow_symlinks=False)
    except OSError:
        return None
    seconds = getattr(stat, "st_birthtime", None) or stat.st_ctime
    return _dt.datetime.fromtimestamp(seconds, _dt.UTC)


def sightings(
    found: Iterable[tuple[str, Path]],
    since: _dt.datetime = UNELEVATED_SINCE,
    opens: Callable[[Path], bool] = _opens,
    born: Callable[[os.DirEntry[str]], _dt.datetime | None] = _born,
) -> list[Sighting]:
    """Each top-level directory of each tree that will not open and was made after `since`."""
    seen: list[Sighting] = []
    for project, tree in found:
        try:
            entries = [entry for entry in os.scandir(tree) if entry.is_dir(follow_symlinks=False)]
        except OSError:
            continue
        for entry in entries:
            path = Path(entry.path)
            if opens(path):
                continue
            created = born(entry)
            if created is not None and created > since:
                seen.append(Sighting(project, tree, path, created))
    return seen


def read_seen(path: Path) -> set[str]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {str(item) for item in loaded} if isinstance(loaded, list) else set()


def write_seen(path: Path, paths: Iterable[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(paths), indent=1) + "\n", encoding="utf-8")


def check(
    workspace: Path,
    devkit: Path,
    apply: bool,
    say: Callable[[str], None],
    scan: Callable[[list[tuple[str, Path]]], list[Sighting]] = sightings,
    record: Callable[[list[fix_findings.Finding], Path], object] | None = None,
) -> int:
    """File each new sighting (with `apply`), once; the count of new ones.

    A path stays in `SEEN` while it exists, so a tree cleared and cut again under the
    same name is flagged afresh.
    """
    try:
        names = sweep.parse_workspace(workspace.read_text(encoding="utf-8"))
    except OSError:
        return 0
    found = scan(trees(workspace.parent, names))
    seen_path = devkit / SEEN
    seen = {item for item in read_seen(seen_path) if Path(item).exists()}
    new = [one for one in found if str(one.path) not in seen]
    for one in new:
        verb = "filed for investigation" if apply else "would file for investigation"
        say(f"ELEVATED {one.project}:{one.tree.name}: {one.path.name} -- {verb}")
    if apply:
        if new:
            (record or _record)([one.finding() for one in new], devkit)
        write_seen(seen_path, seen | {str(one.path) for one in new})
    return len(new)


def _record(findings: list[fix_findings.Finding], devkit: Path) -> object:
    return fix_findings.record_all(findings, triage.load(devkit), devkit)
