"""How long each reconcile pass ran, and which step it was in, kept past the next pass.

`logs/reconcile.log` belongs to whichever pass wrote last and stamped only its end, so
nothing on disk said when a pass started, how long it took, or where its minutes went.
2026-11-07 the 00:30 pass was still going at 00:45, and the sweep sent at it (3fa66640)
could read neither its duration nor its step anywhere (1f44005c). Three records fix that:

- the log's header gains `started=` and `took=`, and a line of step offsets;
- `HISTORY` keeps one JSON line per pass, the last `KEEP` of them, so an overlap reads
  as one pass's `started` + `took` running past the next one's `started`;
- `RUNNING` holds the step a pass is in *now*. A pass that never finishes -- killed by
  the scheduler, or still going when the next one starts -- leaves it behind, and the
  next pass files it in `HISTORY` as `unfinished`, so a pass that wrote no log is still
  on the record with the step it was last in.

Every write is best-effort: a reconcile that did its work must not fail over a log.
"""

from __future__ import annotations

import contextlib
import contextvars
import datetime as _dt
import json
import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

RUNNING = "logs/reconcile.running.json"
HISTORY = "logs/reconcile.history.jsonl"
# Two days of passes at the job's fifteen-minute interval (`install-reconcile-task.py`).
KEEP = 192

_current: contextvars.ContextVar[Journal | None] = contextvars.ContextVar(
    "reconcile_journal", default=None
)


def _iso(when: _dt.datetime) -> str:
    return when.isoformat(timespec="seconds")


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.UTC)


@dataclass
class Journal:
    """One pass's timing: when it started, and each step's offset in seconds."""

    root: Path
    started: _dt.datetime
    clock: Callable[[], float] = time.monotonic
    pid: int = field(default_factory=os.getpid)
    steps: list[tuple[str, int]] = field(default_factory=list)
    begun: float = field(init=False)

    def __post_init__(self) -> None:
        self.begun = self.clock()

    def elapsed(self) -> int:
        return int(self.clock() - self.begun)

    def marker(self) -> dict:
        """What `RUNNING` says while this pass is under way."""
        return {
            "started": _iso(self.started),
            "pid": self.pid,
            "step": self.steps[-1][0] if self.steps else "start",
            "steps": [list(s) for s in self.steps],
        }

    def step(self, name: str) -> None:
        """Note that the pass has reached `name`, in `RUNNING` as well as in memory."""
        self.steps.append((name, self.elapsed()))
        _write(self.root / RUNNING, json.dumps(self.marker()))

    def header(self, code: int, ended: _dt.datetime) -> str:
        """The log's header: its end, exit, start and duration, then where the time went."""
        head = f"# {_iso(ended)}  exit={code}  started={_iso(self.started)}  took={self.elapsed()}s"
        if not self.steps:
            return head
        return head + "\n# steps  " + "  ".join(f"{name}@{at}s" for name, at in self.steps)

    def entry(self, code: int, ended: _dt.datetime) -> dict:
        """This pass as one `HISTORY` line."""
        return {
            "started": _iso(self.started),
            "ended": _iso(ended),
            "took": self.elapsed(),
            "exit": code,
            "pid": self.pid,
            "steps": [list(s) for s in self.steps],
        }


def _write(path: Path, text: str) -> None:
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8", newline="\n")


def append_history(root: Path, row: dict, keep: int = KEEP) -> None:
    """Add `row` to `HISTORY`, keeping only the newest `keep` lines."""
    path = root / HISTORY
    try:
        lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    except OSError:
        lines = []
    lines = [*lines, json.dumps(row)][-keep:]
    _write(path, "\n".join(lines))


def read_marker(root: Path) -> dict | None:
    """The `RUNNING` marker left in `root`, or None when there is none to read."""
    try:
        found = json.loads((root / RUNNING).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return found if isinstance(found, dict) else None


def begin(
    root: Path, now: _dt.datetime | None = None, clock: Callable[[], float] = time.monotonic
) -> Journal:
    """Start a pass's journal, first filing a marker an earlier pass left as `unfinished`."""
    now = now or _now()
    if (left := read_marker(root)) is not None:
        append_history(root, {"unfinished": True, **left, "seen": _iso(now)})
    journal = Journal(root, now, clock)
    journal.step("start")
    return journal


def finish(journal: Journal, code: int, ended: _dt.datetime | None = None) -> None:
    """File the pass in `HISTORY`, and clear `RUNNING` if it is still this pass's."""
    append_history(journal.root, journal.entry(code, ended or _now()))
    left = read_marker(journal.root)
    if left and left.get("started") == _iso(journal.started) and left.get("pid") == journal.pid:
        with contextlib.suppress(OSError):
            (journal.root / RUNNING).unlink()


@contextlib.contextmanager
def journaled(root: Path, enabled: bool = True) -> Iterator[Journal | None]:
    """The pass's journal, current for `step` inside the block; None when not `enabled`
    -- a dry run, which writes no log either (`worktree.write_reconcile_log`)."""
    if not enabled:
        yield None
        return
    journal = begin(root)
    token = _current.set(journal)
    try:
        yield journal
    finally:
        _current.reset(token)


def step(name: str) -> None:
    """Mark the current pass as at `name`; nothing outside a `journaled` block."""
    if (journal := _current.get()) is not None:
        journal.step(name)
