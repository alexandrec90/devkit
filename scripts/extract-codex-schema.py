#!/usr/bin/env python3
"""Read Codex's own hook contract out of the installed binary, into a vendored snapshot.

The adapter (`scripts/hooks/codex-hook-adapter.py`) has to decide, for a hook that
exited 0, whether the response it printed means anything to Codex. Until this script
that decision was a **hand-written list** built from what the adapter itself emits and
what a live session was observed to honour -- and it was wrong on the member that
matters most: it classified `hookSpecificOutput.updatedInput` as Claude-only, while
Codex 0.149.1's `pre-tool-use.command.output` schema accepts it. Shipping that guess
would have converted `worktree-guard.py`'s re-aim into a hard deny on every Codex edit.

The contract is not something to guess at, because Codex ships it: the binary embeds a
draft-07 JSON Schema per hook event, input and output, each with a `title` like
`pre-tool-use.command.output`. This script finds them, reduces each output schema to
the set of member names it accepts (one level deep, `hookSpecificOutput` children
dotted), and writes `scripts/hooks/codex-hook-schema.json`.

Two properties of those schemas drive the whole design downstream, and neither was
guessable:

- **`additionalProperties: false`, on every one.** A member Codex does not know is a
  schema violation rather than something quietly ignored, so passing an unrecognised
  response member through risks the *whole* decision being rejected, not just that
  member being dropped.
- **The accepted set is per event, not global.** `hookSpecificOutput.permissionDecision`
  is PreToolUse's; PermissionRequest takes `hookSpecificOutput.decision` instead; Stop
  takes neither. A flat allowlist is wrong for at least one event whatever it contains.

The snapshot is committed and vendored, so the adapter and its tests need no Codex
install -- which is the point. CI has no Codex binary, and a gate that only runs where
one exists is a gate that runs nowhere that matters. What the binary *is* needed for is
noticing that Codex has changed: run `--check` on a machine with Codex installed and it
reports every member the new version added or withdrew.

Usage:
    python scripts/extract-codex-schema.py --check     # compare binary against snapshot
    python scripts/extract-codex-schema.py --write     # refresh the snapshot

Tested in `tests/test_extract_codex_schema.py`.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = Path("scripts") / "hooks" / "codex-hook-schema.json"

# The marker every embedded schema starts with. Anchored on the `$schema` member rather
# than on `title`, because the object opens before the title is reached and a scan has to
# know where the brace it is balancing began.
SCHEMA_HEAD = re.compile(rb'\{\s*"\$schema": "http://json-schema\.org/draft-07/schema#"')

# `pre-tool-use.command.output` -> `PreToolUse`. Codex names its schemas in kebab-case
# and its events in Claude's PascalCase; the wiring generator speaks the latter.
TITLE = re.compile(r"^([a-z0-9\-]+)\.command\.(input|output)$")

# A bound on one schema object, so a corrupt read walks off rather than scanning a
# hundred megabytes of binary looking for a closing brace that is not there.
MAX_SCHEMA_BYTES = 200_000


def event_name(title: str) -> tuple[str, str] | None:
    """`("PreToolUse", "output")` for a schema title, or None if it is not one."""
    found = TITLE.match(title)
    if not found:
        return None
    kebab, kind = found.groups()
    return "".join(part.capitalize() for part in kebab.split("-")), kind


def schema_at(data: bytes, start: int) -> str | None:
    """The JSON object beginning at `start`, by brace balance. None if unterminated.

    Brace counting rather than a JSON incremental parser because the surrounding bytes
    are machine code: there is no delimiter to scan to, and the schemas contain no
    string with a brace in it (they are generated from Rust types).
    """
    depth = 0
    for offset in range(start, min(len(data), start + MAX_SCHEMA_BYTES)):
        char = data[offset : offset + 1]
        if char == b"{":
            depth += 1
        elif char == b"}":
            depth -= 1
            if depth == 0:
                return data[start : offset + 1].decode("utf-8", "replace")
    return None


def embedded_schemas(data: bytes) -> dict[str, dict]:
    """Every hook schema in `data`, keyed by its title."""
    found: dict[str, dict] = {}
    for match in SCHEMA_HEAD.finditer(data):
        text = schema_at(data, match.start())
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except ValueError:
            continue
        title = parsed.get("title")
        if isinstance(title, str) and TITLE.match(title):
            found[title] = parsed
    return found


def accepted_members(schema: dict) -> list[str]:
    """The member names one output schema accepts, `hookSpecificOutput` children dotted.

    One level deep, matching `codex-hook-adapter.response_members`: that is where the
    whole contract lives -- every nested member Codex defines hangs off
    `hookSpecificOutput`, and a hook response is a decision, not a document.
    """
    definitions = schema.get("definitions", {})
    members: list[str] = []
    for name, spec in sorted(schema.get("properties", {}).items()):
        members.append(name)
        for branch in spec.get("allOf") or []:
            ref = branch.get("$ref", "").rsplit("/", 1)[-1]
            nested = definitions.get(ref, {})
            members.extend(f"{name}.{child}" for child in sorted(nested.get("properties", {})))
    return members


def contract(data: bytes, version: str) -> dict:
    """The snapshot: which members each event's output accepts, and where it came from."""
    events: dict[str, list[str]] = {}
    for title, schema in embedded_schemas(data).items():
        named = event_name(title)
        if not named or named[1] != "output":
            continue
        events[named[0]] = accepted_members(schema)
    return {
        "source": "extracted from the installed Codex binary by scripts/extract-codex-schema.py",
        "codex_version": version,
        "events": dict(sorted(events.items())),
    }


def codex_binary(which=None) -> Path | None:
    """The installed Codex executable, or None. `which` injected so this is testable."""
    probe = which or shutil.which
    found = probe("codex")
    return Path(found) if found else None


def codex_version(binary: Path) -> str:
    """`codex --version`, trimmed. Unknown rather than fatal: the members are the point."""
    try:
        result = subprocess.run(
            [str(binary), "--version"], capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return result.stdout.strip().splitlines()[0].strip() if result.stdout.strip() else "unknown"


def load_snapshot(root: Path | None = None) -> dict:
    """The committed contract. Empty when it has not been extracted yet."""
    path = (root or REPO_ROOT) / SNAPSHOT
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def differences(snapshot: dict, extracted: dict) -> list[str]:
    """Every member the binary added or withdrew, as lines a human can act on."""
    old, new = snapshot.get("events", {}), extracted.get("events", {})
    lines = []
    for event in sorted(set(old) | set(new)):
        before, after = set(old.get(event, [])), set(new.get(event, []))
        for member in sorted(after - before):
            lines.append(f"+ {event}.{member}")
        for member in sorted(before - after):
            lines.append(f"- {event}.{member}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract Codex's hook output contract.")
    parser.add_argument("--check", action="store_true", help="compare the binary to the snapshot")
    parser.add_argument("--write", action="store_true", help="refresh the snapshot")
    parser.add_argument("--root", type=Path, default=None, help="repo root (default: devkit's)")
    args = parser.parse_args(argv)
    root = args.root or REPO_ROOT

    binary = codex_binary()
    if binary is None:
        # Exit 0, deliberately, and on the same reasoning `sync-devkit.py` applies to an
        # unset `$DEVKIT_DIR`: with nothing to compare against there is no finding to
        # report. The snapshot is committed precisely so the adapter and its tests never
        # depend on this branch.
        print("extract-codex-schema: no codex on PATH — nothing to extract from.")
        return 0

    extracted = contract(binary.read_bytes(), codex_version(binary))
    if not extracted["events"]:
        print(
            f"extract-codex-schema: found no hook schemas in {binary}. Codex may have "
            "stopped embedding them; the snapshot is now unverifiable, not merely stale.",
            file=sys.stderr,
        )
        return 1

    if args.write:
        path = root / SNAPSHOT
        path.write_text(json.dumps(extracted, indent=2) + "\n", encoding="utf-8")
        print(
            f"extract-codex-schema: wrote {SNAPSHOT.as_posix()} from {extracted['codex_version']}"
        )
        return 0

    drift = differences(load_snapshot(root), extracted)
    if drift:
        print(
            f"extract-codex-schema: {extracted['codex_version']} differs from the snapshot:",
            file=sys.stderr,
        )
        print("\n".join(f"  {line}" for line in drift), file=sys.stderr)
        print("  refresh with: python scripts/extract-codex-schema.py --write", file=sys.stderr)
        return 1
    print(f"extract-codex-schema: snapshot matches {extracted['codex_version']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
