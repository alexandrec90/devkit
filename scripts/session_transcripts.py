#!/usr/bin/env python3
"""Read Claude Code and Codex session transcripts as one stream of events, incrementally.

`session_friction.py` judges what sessions did; this is the reading, split out so the
two formats and the byte-offset bookkeeping live apart from the detectors.

- Claude Code writes `~/.claude/projects/<slug>/<session>.jsonl`, one record per content
  block, each carrying the session's `cwd`.
- Codex writes `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`, and names its `cwd` once,
  in the first (`session_meta`) record -- which is why a cursor keeps it.

Both become `Event`s: what the user said, what command a tool call ran, and whether a
tool result was a failure. Everything else in a transcript is ignored.

Tested in `tests/test_session_friction.py`, through the detectors that read these.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

CLAUDE_ROOT = Path.home() / ".claude" / "projects"
CODEX_ROOT = Path.home() / ".codex" / "sessions"
CODEX_PREFIX = "rollout-"


@dataclass(frozen=True)
class Event:
    """One thing a session did or was told, reduced to what the detectors read."""

    kind: str  # "user", "call" or "result"
    line: int
    text: str = ""
    command: str = ""
    error: bool = False
    call_id: str = ""


@dataclass(frozen=True)
class Chunk:
    """What a transcript gained since a cursor: its rows and where the cursor goes next."""

    rows: tuple[tuple[int, dict], ...]  # (line number, record)
    offset: int
    line: int


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _blocks(content: object) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _text(content: object) -> str:
    if isinstance(content, list):
        return " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
    return str(content or "")


def claude_events(row: dict, line: int) -> Iterator[Event]:
    message = _dict(row.get("message"))
    # A compaction summary and a harness-injected row are typed `user` but are not the user.
    spoken = row.get("type") == "user" and not (row.get("isMeta") or row.get("isCompactSummary"))
    for block in _blocks(message.get("content")):
        kind = block.get("type")
        if spoken and kind == "text":
            yield Event("user", line, str(block.get("text", "")))
        elif kind == "tool_use":
            command = str(_dict(block.get("input")).get("command", ""))
            yield Event("call", line, command=command, call_id=str(block.get("id", "")))
        elif kind == "tool_result":
            error = bool(block.get("is_error"))
            call_id = str(block.get("tool_use_id", ""))
            yield Event("result", line, _text(block.get("content")), error=error, call_id=call_id)


def codex_events(row: dict, line: int) -> Iterator[Event]:
    payload = _dict(row.get("payload"))
    kind = payload.get("type")
    call_id = str(payload.get("call_id", ""))
    if row.get("type") == "event_msg" and kind == "user_message":
        yield Event("user", line, str(payload.get("message", "")))
    elif kind == "function_call":
        try:
            args = json.loads(payload.get("arguments") or "{}")
        except ValueError:
            args = {}
        command = _dict(args).get("command", "")
        command = " ".join(command) if isinstance(command, list) else str(command)
        yield Event("call", line, command=command, call_id=call_id)
    elif kind == "function_call_output":
        text = _text(payload.get("output"))
        failed = bool(re.search(r"exit code:?\s*[1-9]", text[:200], re.I))
        yield Event("result", line, text, error=failed, call_id=call_id)


def events(path: Path, rows: tuple[tuple[int, dict], ...]) -> list[Event]:
    reader = codex_events if is_codex(path) else claude_events
    return [event for number, row in rows for event in reader(row, number)]


def is_codex(path: Path) -> bool:
    return path.name.startswith(CODEX_PREFIX)


def cwd_of(row: dict) -> str:
    """The working directory a record names, in either format; "" when it names none."""
    if isinstance(row.get("cwd"), str):
        return row["cwd"]
    payload = row.get("payload")
    if row.get("type") == "session_meta" and isinstance(payload, dict):
        return str(payload.get("cwd", ""))
    return ""


def transcripts(claude_root: Path = CLAUDE_ROOT, codex_root: Path = CODEX_ROOT) -> list[Path]:
    found: list[Path] = []
    for root, pattern in ((claude_root, "*.jsonl"), (codex_root, f"{CODEX_PREFIX}*.jsonl")):
        if root.is_dir():
            found.extend(root.rglob(pattern))
    return sorted(found)


def read_new(path: Path, offset: int, line: int) -> Chunk:
    """The complete lines after `offset`, numbered on from `line`.

    A file shorter than the cursor was rewritten, and is read again from the top. The
    size is checked before anything is read, because nearly every transcript on the
    machine has not changed since the last pass.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return Chunk((), offset, line)
    if size < offset:
        offset, line = 0, 0
    if size == offset:
        return Chunk((), offset, line)
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read()
    except OSError:
        return Chunk((), offset, line)
    complete = data[: data.rfind(b"\n") + 1]
    rows = []
    for number, raw in enumerate(complete.splitlines(), start=line + 1):
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append((number, row))
    return Chunk(tuple(rows), offset + len(complete), line + complete.count(b"\n"))
