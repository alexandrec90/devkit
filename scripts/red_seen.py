#!/usr/bin/env python3
"""When a pass first saw a red: the clock every grace on a default branch runs from.

A commit's time says when the work merged, not when anyone looked. A PC asleep through
those hours would wake to a grace already spent, and its first pass would send a fixer and
give up on that fixer at once. So a grace runs from the first pass that saw the red, and
what that pass saw is kept here, in one small file per project under `logs/`.

`kind` names the clock and `key` names the red: a new key is a new red, and its clock
starts now. Anything unreadable is "first seen now", the side that waits. A read-only pass
(`write=False`) starts no clock.

Tested in `tests/test_red_seen.py`.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

STATE_DIR = Path("logs") / "red-seen"

# The revert clock's two marks, kept here rather than in `fix_revert.py` because the fix
# trigger reads them every two minutes and must not import the pass to do it.
#
# A direct-flow default branch red this long, from the first pass that saw it, has its
# commits reverted. The trigger starts that pass within two minutes of the red, and it
# sends the fixer; the reds that hold a branch -- a lint finding, an untested symbol, a
# structure limit -- take that fixer about ten minutes to land, and its merge's gate
# under ten more to judge. Twenty is that, with nothing to spare: on 2026-11-26 the old
# two hours let one red main gather a red per merge for over an hour, and held a release
# that five projects were waiting on.
REVERT_GRACE = _dt.timedelta(minutes=20)
# A fixer still at that red (`fix_ledger.fixer_at_branch`) holds the revert past the grace
# to here, and no further: `fix_ledger.BRANCH_SETTLE`, the window that already counts a
# sent fixer as working, so a fixer that has not landed by then is one the pass would send
# again anyway.
REVERT_CEILING = _dt.timedelta(minutes=45)
# The clock `fix_revert` keeps under this name, which the trigger reads.
REVERT = "revert"


def _read(path: Path) -> dict:
    try:
        held = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return held if isinstance(held, dict) else {}


def _seen_at(record: object, key: str) -> _dt.datetime | None:
    """When `record` says `key` was first seen; None when it is another red or unreadable."""
    if not isinstance(record, dict) or record.get("key") != key:
        return None
    try:
        when = _dt.datetime.fromisoformat(str(record.get("seen")))
    except ValueError:
        return None
    return when if when.tzinfo is not None else None


def first_seen(
    devkit_dir: Path, project: str, kind: str, key: str, now: _dt.datetime, write: bool = True
) -> _dt.datetime:
    """When a pass first saw this red: the stored time, or `now`, recorded when `write`."""
    path = devkit_dir / STATE_DIR / f"{project}.json"
    held = _read(path)
    if (seen := _seen_at(held.get(kind), key)) is not None:
        return seen
    if write:
        held[kind] = {"key": key, "seen": now.isoformat(timespec="seconds")}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(held, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        except OSError:
            pass  # unwritten, the next pass sees it first again: the side that waits
    return now


def lapsed(
    devkit_dir: Path, kind: str, now: _dt.datetime, after: _dt.timedelta
) -> list[tuple[str, str]]:
    """`(project, key)` for each `kind` clock that has run `after` or longer by `now`.

    What the fix trigger turns into a pass: a red nothing new is landing on raises no other
    signal, and the half-hourly backstop would add up to thirty minutes to a grace of
    twenty. A record nobody refreshed -- the red already gone -- costs one pass that does
    nothing. Unreadable is nothing."""
    try:
        paths = sorted((devkit_dir / STATE_DIR).glob("*.json"))
    except OSError:
        return []
    found = []
    for path in paths:
        record = _read(path).get(kind)
        key = str(record.get("key", "")) if isinstance(record, dict) else ""
        seen = _seen_at(record, key) if key else None
        if seen is not None and now - seen >= after:
            found.append((path.stem, key))
    return found
