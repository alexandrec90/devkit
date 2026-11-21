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
