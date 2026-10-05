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

Run it on a ledger row's `evidence=` (`<transcript>#L<line>`) to read that session as
audit lines around the line: two sweeps each wrote their own reader for want of this.

Tested in `tests/test_session_friction.py`, through the detectors that read these.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

CLAUDE_ROOT = Path.home() / ".claude" / "projects"
CODEX_ROOT = Path.home() / ".codex" / "sessions"
CODEX_PREFIX = "rollout-"


@dataclass(frozen=True)
class Event:
    """One thing a session did or was told, reduced to what the detectors read."""

    kind: str  # "user", "say" (the agent's own text), "call" or "result"
    line: int
    text: str = ""  # for an edit call, what it writes
    command: str = ""
    error: bool = False
    call_id: str = ""
    tool: str = ""  # a call's tool name: `Bash`, `Edit`, `AskUserQuestion`, `shell`
    path: str = ""  # the file an edit call names: `Edit`'s `file_path`
    background: bool = False  # a call run with `run_in_background`: its wait holds no turn


@dataclass(frozen=True)
class Chunk:
    """What a transcript gained since a cursor: its rows and where the cursor goes next."""

    rows: tuple[tuple[int, dict], ...]  # (line number, record)
    offset: int


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
    speaker = "user" if spoken else "say" if row.get("type") == "assistant" else ""
    for block in _blocks(message.get("content")):
        if event := _claude_block(block, speaker, line):
            yield event


def _claude_block(block: dict, speaker: str, line: int) -> Event | None:
    """One content block as an event: text by whoever `speaker` is, a call, a result."""
    kind = block.get("type")
    if kind == "text":
        return Event(speaker, line, str(block.get("text", ""))) if speaker else None
    if kind == "tool_use":
        given = _dict(block.get("input"))
        command = str(given.get("command", ""))
        path = str(given.get("file_path") or given.get("notebook_path") or "")
        tool = str(block.get("name", ""))
        call_id = str(block.get("id", ""))
        background = given.get("run_in_background") is True
        return Event(
            "call",
            line,
            _written(given),
            command=command,
            call_id=call_id,
            tool=tool,
            path=path,
            background=background,
        )
    if kind == "tool_result":
        error = bool(block.get("is_error"))
        call_id = str(block.get("tool_use_id", ""))
        return Event("result", line, _text(block.get("content")), error=error, call_id=call_id)
    return None


def _written(given: dict) -> str:
    """The text an edit call puts in its file: `Edit`'s new string, `Write`'s content,
    each of a `MultiEdit`'s; "" for any other call."""
    listed = given.get("edits")
    edits = listed if isinstance(listed, list) else [given]
    return "\n".join(
        str(edit.get("new_string") or edit.get("content") or "")
        for edit in edits
        if isinstance(edit, dict)
    ).strip()


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
        yield Event(
            "call", line, command=command, call_id=call_id, tool=str(payload.get("name", ""))
        )
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


def read_new(path: Path, offset: int) -> Chunk:
    """The complete lines after `offset`, each numbered by its line in the file.

    A file shorter than the cursor was rewritten, and is read again from the top. The
    size is checked before anything is read, because nearly every transcript on the
    machine has not changed since the last pass. The numbers are counted from the file
    as it is now, never carried on from the last read: Claude Code rewrites a transcript
    it relocates into a worktree without shrinking it below the cursor, and a carried
    count then named line 1119 for a call on line 1003 (e1aaca09). An offset that lands
    inside a line is such a rewrite too, and the torn line is skipped.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return Chunk((), offset)
    if size < offset:
        offset = 0
    if size == offset:
        return Chunk((), offset)
    try:
        data = path.read_bytes()
    except OSError:
        return Chunk((), offset)
    line = data.count(b"\n", 0, offset)
    if offset and data[offset - 1 : offset] != b"\n":
        torn = data.find(b"\n", offset)
        offset, line = (offset, line) if torn < 0 else (torn + 1, line + 1)
    complete = data[offset : data.rfind(b"\n") + 1]
    rows = []
    # Split on `\n` alone, as the count above is: a line is what `grep -n` calls one.
    for number, raw in enumerate(complete.split(b"\n")[:-1], start=line + 1):
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append((number, row))
    return Chunk(tuple(rows), offset + len(complete))


# How much of each event a rendering keeps: an audit reads for what a session did and
# where it lost turns, and a failure's text is where the loss usually is.
RENDER_LIMITS = {"user": 3000, "say": 1500, "call": 400, "result": 300, "error": 1200}


def render(path: Path) -> str:
    """One transcript as lines a reader can audit: what was said, run, and refused.

    What `/supervise-fix-pass` hands its transcript audit, so no supervisor writes its
    own condenser first -- the first one did.
    """
    lines = []
    for event in events(path, read_new(path, 0).rows):
        kind = "error" if event.kind == "result" and event.error else event.kind
        body = event.command if event.kind == "call" else event.text
        body = " ".join(body.split())
        limit = RENDER_LIMITS[kind]
        cut = body if len(body) <= limit else f"{body[:limit]} ...[+{len(body) - limit}]"
        tool = f" {event.tool}" if event.tool else ""
        lines.append(f"L{event.line} {kind.upper()}{tool}: {cut}")
    return "\n".join(lines) + "\n"


# A ledger row's `evidence=`: a transcript, and optionally the line a finding was read at.
EVIDENCE = re.compile(r"^(?P<path>.+?)(?:#L(?P<line>\d+))?$")
RENDERED_LINE = re.compile(r"^L(\d+) ")


def window(rendered: str, line: int, around: int) -> str:
    """The rendered lines within `around` transcript lines of `line`."""
    kept = [
        text
        for text in rendered.splitlines()
        if (found := RENDERED_LINE.match(text)) and abs(int(found.group(1)) - line) <= around
    ]
    return "".join(f"{text}\n" for text in kept)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render a session transcript as audit lines.")
    parser.add_argument("evidence", help="a transcript path, or a ledger evidence= `<path>#L<n>`")
    parser.add_argument(
        "--around", type=int, default=40, help="transcript lines either side of #L<n>"
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    found = EVIDENCE.match(args.evidence.strip())
    path = Path(found.group("path")) if found else Path(args.evidence)
    if not path.is_file():
        print(f"session_transcripts: no transcript at {path}", file=sys.stderr)
        return 2
    rendered = render(path)
    line = found.group("line") if found else None
    print(window(rendered, int(line), args.around) if line else rendered, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
