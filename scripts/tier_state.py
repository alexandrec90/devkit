#!/usr/bin/env python3
"""What the medium tier remembers about a project it has no verdict for.

`result.json` is the last verdict and nothing else, so a run that judged nothing cannot
go there without hiding a red. This is the other record, one file beside it, deleted by
the next verdict.

**Could not judge is not the job failing.** No tree, no toolchain, a command that will not
start: a network blip or a project's own broken lock, and every ledger row bills the devkit
session. So the job exits 0, the project waits out a backoff doubling from an hour to a day
(each attempt cost up to 25 minutes, every fire), and only a run of them lasting a day is
filed, once. A project's own defect (`own`) is never filed on devkit: dependencies that do
not resolve fail its own gate too.

**Short of memory is not a finding either,** because no session can fix it, and it is why
`schedule_health` reads this job healthy while it judges nothing. So the skips are counted,
and a day of them is said where a person reads: the pass's record and `status`.

Tested in `tests/test_tier_state.py`.
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import e2e_results

STATE_FILE = "state.json"

FIRST_BACKOFF_HOURS = 1
MAX_BACKOFF_HOURS = 24
# How long a run of could-not-judge lasts before it is worth anyone's session.
FILE_AFTER = _dt.timedelta(hours=24)
# How long a project goes unjudged for memory before the record says so.
STARVED_AFTER = _dt.timedelta(hours=24)


@dataclass(frozen=True)
class State:
    """Times are ISO, this machine's clock. `failures` counts could-not-judge in a row."""

    failures: int = 0
    since: str = ""
    next_try: str = ""
    reason: str = ""
    own: bool = False
    filed: bool = False
    memory_since: str = ""
    memory_skips: int = 0


def path_for(project: str, root: Path) -> Path:
    return e2e_results.result_dir(project, root, e2e_results.MEDIUM) / STATE_FILE


def read(project: str, root: Path) -> State:
    """The project's record; a plain `State` when there is none or it cannot be read, which
    at worst means one more attempt."""
    try:
        record = json.loads(path_for(project, root).read_text(encoding="utf-8"))
        known = {f.name for f in fields(State)}
        state = State(**{key: value for key, value in record.items() if key in known})
        int(state.failures), int(state.memory_skips)
    except (OSError, ValueError, TypeError, AttributeError):
        return State()
    return state


def write(project: str, state: State, root: Path) -> None:
    path = path_for(project, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(state), indent=2) + "\n", encoding="utf-8")


def clear(project: str, root: Path) -> None:
    """A verdict ends both waits."""
    path_for(project, root).unlink(missing_ok=True)


def _stamp(at: _dt.datetime) -> str:
    return at.isoformat(timespec="seconds")


def _when(stamp: str) -> _dt.datetime | None:
    try:
        return _dt.datetime.fromisoformat(stamp)
    except ValueError:
        return None


def could_not_judge(state: State, now: _dt.datetime, why: str, own: bool) -> State:
    """`state` after one more attempt that judged nothing."""
    failures = int(state.failures) + 1
    hours = min(FIRST_BACKOFF_HOURS * 2 ** (failures - 1), MAX_BACKOFF_HOURS)
    return replace(
        state,
        failures=failures,
        since=state.since or _stamp(now),
        next_try=_stamp(now + _dt.timedelta(hours=hours)),
        reason=why,
        own=own,
    )


def due(state: State, now: _dt.datetime) -> bool:
    """Whether the project's backoff has run. An unreadable time has."""
    at = _when(state.next_try)
    return at is None or now >= at


def owes_filing(state: State, now: _dt.datetime) -> bool:
    """A run of could-not-judge that has lasted a day, is devkit's, and is not yet filed."""
    since = _when(state.since)
    return since is not None and not state.own and not state.filed and now - since >= FILE_AFTER


def filed(state: State) -> State:
    return replace(state, filed=True)


def skipped_for_memory(state: State, now: _dt.datetime) -> State:
    return replace(
        state,
        memory_since=state.memory_since or _stamp(now),
        memory_skips=int(state.memory_skips) + 1,
    )


def notes(project: str, state: State, now: _dt.datetime) -> list[str]:
    """What a person should read about the wait: the record's lines, and `status`'s."""
    lines: list[str] = []
    if state.failures:
        whose = " (the project's own)" if state.own else ""
        lines.append(
            f"{project}: could not judge since {state.since}, next try {state.next_try}"
            f"{whose} -- {state.reason}"
        )
    since = _when(state.memory_since)
    if since is not None and now - since >= STARVED_AFTER:
        hours = int((now - since).total_seconds() // 3600)
        lines.append(
            f"{project}: unjudged for {hours} hours: short of memory, {state.memory_skips} skip(s)"
        )
    return lines
