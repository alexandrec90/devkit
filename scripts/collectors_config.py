#!/usr/bin/env python3
"""What the collectors job manages, and what *this machine* was told to do with each.

Two answers from two places, and the split between them is the whole design:

- **Which collectors exist** is the workspace file's `devkit.collectors`, beside
  `devkit.remoteControl`. That file is devkit's canonical `workspace.jsonc`, the same on
  every workstation, so declaring a collector there declares it everywhere -- once, with a
  branch and a review, and without an edit to the project that owns it.
- **Whether this machine runs one** is a file under this checkout's `logs/`, which git
  ignores and nothing syncs. That is what keeps a laptop that shares the workspace from
  starting an ingestion job just because the desktop runs one: two collectors against
  one provider quota, or two writers into one Parquet archive, is the failure this exists
  to prevent. **Absent means hands off** -- neither started nor stopped -- so a new
  workstation does nothing until someone assigns it.

Moving a collector between machines is `collectors.py stop-here` on the one and
`run-here` on the other. The mechanism is in `collectors.py`; this module only reads
and writes the two answers, and spawns nothing.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devkit_jsonc
import sweep

REPO_ROOT = Path(__file__).resolve().parents[1]

SETTING = "devkit.collectors"

# The two things a machine can be told. Anything else in the file is dropped on read,
# which lands on "hands off" -- the direction that neither starts a second writer nor
# stops the only one.
RUN = "run"
STOP = "stop"
MODES = frozenset({RUN, STOP})

# Machine-local by construction: `logs/` is ignored in every devkit checkout.
ASSIGNMENT = Path("logs/collectors.machine.json")


@dataclass(frozen=True)
class Collector:
    """One compose service that does scheduled work with nobody connected to it.

    `health` is a command run *inside* the running container, whose exit code is the
    project's own verdict on whether ingestion is actually pulling data. Empty means the
    project offers none, and only "is the container up" is reported.
    """

    project: str
    service: str
    health: tuple[str, ...] = ()


def home(root: Path = REPO_ROOT) -> Path:
    """The checkout whose `logs/` holds this machine's answer.

    `$DEVKIT_DIR` first, then the static checkout this one belongs to -- so `run-here`
    typed in a box or an agent's worktree lands where the scheduled job reads, rather
    than in a directory deleted when that tree's PR merges.
    """
    named = (os.environ.get("DEVKIT_DIR") or "").strip()
    if named and Path(named).is_dir():
        return Path(named)
    return sweep.source_checkout(root)


def _entry(project: str, raw: object) -> tuple[Collector | None, str]:
    """One setting entry as a `Collector`, or `None` and the reason it was refused."""
    if not isinstance(raw, dict):
        return None, f"{project}: expected an object with a `service`, got {type(raw).__name__}"
    service = raw.get("service")
    if not isinstance(service, str) or not service:
        return None, f"{project}: no `service` -- which compose service is the collector?"
    health = raw.get("health", [])
    if not isinstance(health, list) or not all(isinstance(part, str) and part for part in health):
        return None, f"{project}: `health` must be a list of strings (argv, not a shell line)"
    return Collector(project, service, tuple(health)), ""


def parse_setting(text: str) -> tuple[list[Collector], list[str]]:
    """`(collectors, notes)` out of a workspace file's text. Never raises.

    The setting is hand-edited, so a malformed entry is a note rather than silence: an
    entry that quietly declared nothing would read, on the machine meant to run it,
    exactly like a collector nobody had asked for.
    """
    try:
        payload = devkit_jsonc.loads(text)
    except (json.JSONDecodeError, TypeError):
        return [], ["the workspace file does not parse"]
    settings = payload.get("settings") if isinstance(payload, dict) else None
    raw = settings.get(SETTING) if isinstance(settings, dict) else None
    if raw is None:
        return [], []
    if not isinstance(raw, dict):
        return [], [f"`{SETTING}` must be an object keyed by project name"]
    found: list[Collector] = []
    notes: list[str] = []
    for project, entry in raw.items():
        collector, note = _entry(project, entry)
        if collector is None:
            notes.append(note)
        else:
            found.append(collector)
    return found, notes


def declared(root: Path) -> tuple[list[Collector], list[str]]:
    """The collectors the workspace file beside `root` declares, with any notes."""
    workspace = sweep.default_workspace(root)
    try:
        text = workspace.read_text(encoding="utf-8")
    except OSError:
        return [], [f"no workspace file at {workspace}"]
    return parse_setting(text)


def load_assignment(path: Path) -> dict[str, str]:
    """`{project: mode}` for this machine. Never raises; unreadable is "hands off"."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(name): mode for name, mode in raw.items() if isinstance(mode, str) and mode in MODES
    }


def save_assignment(path: Path, assignment: Mapping[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(sorted(assignment.items())), indent=2) + "\n", encoding="utf-8")


def assign(
    assignment: Mapping[str, str], projects: Iterable[str], mode: str | None
) -> dict[str, str]:
    """A new assignment with `projects` set to `mode`, or released when `mode` is None."""
    if mode is not None and mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; expected one of {sorted(MODES)}")
    updated = dict(assignment)
    for project in projects:
        if mode is None:
            updated.pop(project, None)
        else:
            updated[project] = mode
    return updated


def pick(collectors: list[Collector], names: list[str]) -> tuple[list[Collector], list[str]]:
    """`(chosen, unknown)`: every declared collector when `names` is empty.

    A name the setting does not declare is returned rather than dropped, because the
    caller is a person who typed it and a typo that assigned nothing looks like success.
    """
    if not names:
        return list(collectors), []
    by_name = {collector.project: collector for collector in collectors}
    return [by_name[name] for name in names if name in by_name], [
        name for name in names if name not in by_name
    ]
