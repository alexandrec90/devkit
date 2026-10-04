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

# devkit's own scheduled jobs' namespace (`schedule_health.PREFIX`), spelled here because
# this module spawns nothing and imports nothing that does.
DEVKIT_PREFIX = "devkit-"

# Machine-local by construction: `logs/` is ignored in every devkit checkout.
ASSIGNMENT = Path("logs/collectors.machine.json")

# Minutes a started or redeployed container goes unjudged (`Collector.settle`). An hour
# covers ibkr_trader's half-hourly `reddit` and hourly `sentiment` with a pass to spare.
DEFAULT_SETTLE = 60


@dataclass(frozen=True)
class Collector:
    """One piece of ingestion that does scheduled work with nobody connected to it.

    Two kinds, told apart by which field is set:

    - **a container** (`service`): a compose service keeping its own clock inside, which
      this job keeps up. `health` is a command run *inside* it, whose exit code is the
      project's own verdict on whether ingestion is actually pulling data. Empty means the
      project offers none, and only "is the container up" is reported.
    - **a scheduled command** (`command`): argv run on the host from the project's
      checkout every `minutes`, by a Windows Scheduled Task of its own named after the
      collector (`collector_tasks`). For work that cannot live in a container --
      social-scraper drives the host's Chrome. `needs` are compose services started
      before each fire. The command's exit code is the verdict, so 0 has to cover "ran
      and deliberately did nothing".

    `settle` is how many minutes after this job starts or redeploys a container its
    `health` is not run (`collectors.settling`): until each job inside has fired on the
    new code, the verdict is the old code's. Set it to the longest interval of a job
    whose failure would otherwise outlive the fix that cured it.
    """

    project: str
    service: str = ""
    health: tuple[str, ...] = ()
    command: tuple[str, ...] = ()
    minutes: int = 0
    needs: tuple[str, ...] = ()
    settle: int = DEFAULT_SETTLE

    @property
    def scheduled(self) -> bool:
        return bool(self.command)


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


def _argv(raw: object) -> tuple[str, ...] | None:
    """A JSON list of non-empty strings as a tuple; None for anything else."""
    if not isinstance(raw, list) or not all(isinstance(part, str) and part for part in raw):
        return None
    return tuple(raw)


def _scheduled_entry(project: str, raw: dict) -> tuple[Collector | None, str]:
    """A `command` entry, or `None` and the reason it was refused."""
    if "service" in raw:
        return None, f"{project}: has both `service` and `command` -- a collector is one kind"
    command = _argv(raw["command"])
    if not command:
        return None, f"{project}: `command` must be a non-empty list of strings (argv)"
    minutes = raw.get("minutes")
    # `bool` is an `int`: `"minutes": true` would register a one-minute task.
    if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < 1:
        return None, f"{project}: `minutes` must be a positive whole number of minutes"
    needs = _argv(raw.get("needs", []))
    if needs is None:
        return None, f"{project}: `needs` must be a list of compose service names"
    if project.startswith(DEVKIT_PREFIX):
        # The task is named after the collector, and `devkit-` is devkit's own jobs'
        # namespace: `schedule_health` and the fix pass would report it as a devkit job.
        return None, f"{project}: a collector's name may not start with `{DEVKIT_PREFIX}`"
    return Collector(project, command=command, minutes=minutes, needs=needs), ""


def _entry(project: str, raw: object) -> tuple[Collector | None, str]:
    """One setting entry as a `Collector`, or `None` and the reason it was refused."""
    if not isinstance(raw, dict):
        return None, (
            f"{project}: expected an object with a `service` or a `command`, "
            f"got {type(raw).__name__}"
        )
    if "command" in raw:
        return _scheduled_entry(project, raw)
    service = raw.get("service")
    if not isinstance(service, str) or not service:
        return None, f"{project}: no `service` -- which compose service is the collector?"
    health = raw.get("health", [])
    if not isinstance(health, list) or not all(isinstance(part, str) and part for part in health):
        return None, f"{project}: `health` must be a list of strings (argv, not a shell line)"
    settle = raw.get("settle", DEFAULT_SETTLE)
    # `bool` is an `int`, as for `minutes`.
    if isinstance(settle, bool) or not isinstance(settle, int) or settle < 0:
        return None, f"{project}: `settle` must be a whole number of minutes, 0 or more"
    return Collector(project, service, tuple(health), settle=settle), ""


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
