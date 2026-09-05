#!/usr/bin/env python3
"""What the workspace file asks the Remote Control job to serve, and when.

Split out of `rc-servers.py` rather than living in it, along the seam the imports
already showed: everything here is a decision read off a file, with no process and no
filesystem behind it. `rc_machine.py` is the machine, and `rc-servers.py` is the pass
that drives both.

The opt-in lives beside `sweep.ON_HOLD_SETTING` in the workspace file, for that
setting's reason: the workspace file is already the registry every tool in this repo
reads the checkouts from, so "which of them get a server" belongs there rather than in
a second file nothing else knows about.
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devkit_jsonc
import sweep

# The workspace-file setting naming the projects that get a server. A list of checkout
# names, or an object carrying the same list under `projects` plus the knobs below.
RC_SETTING = "devkit.remoteControl"

# `same-dir` is `remote-control`'s own default and is kept as this job's default
# deliberately -- but it is a default, not a prohibition, and `"spawn": "worktree"` is a
# reasonable thing for a machine to ask for: a phone has no VS Code tasks, so spawning
# in place is the only isolation a mobile session gets otherwise.
#
# What the opt-in costs, and why it is not the default: Claude Code cuts its worktrees
# under `<repo>/.claude/worktrees/<name>`, *inside* the checkout, while `worktree.py`
# owns a separate tier at `<workspace>/.worktrees/`. The two do not know about each
# other, so a box cut by the phone gets no `provision`, no port lease, no
# `COMPOSE_PROJECT_NAME`, and no `reconcile` reap -- and `sweep`, `reconcile` and
# `workspace-status` cannot see it to report it stranded. Removing one is a hand job.
# The consuming project must also gitignore `.claude/worktrees/`, or the nested worktree
# leaves the static checkout dirty and `sweep.classify` stops syncing it.
DEFAULT_SPAWN = "same-dir"

# Empty means "pass no `--permission-mode`", so a server inherits whatever the project's
# own settings say. Not defaulted even though a phone is the one client that cannot
# escalate for itself -- a session spawned from the mobile app opens in `auto` and the UI
# offers no switch, so `bypassPermissions` set here is the only route to one. That is a
# standing grant on an unattended machine reachable from the internet, which is exactly
# why the opt-in belongs to the person whose machine it is rather than to this default.
DEFAULT_PERMISSION_MODE = ""

# Minutes of transcript silence before a project's server may be restarted. Twenty is
# longer than a slow turn and shorter than a coffee break; the cost of it being too
# small is an interrupted turn, and of too large, an update deferred to tomorrow.
DEFAULT_IDLE_MINUTES = 20

# Earliest wall-clock time the daily update-and-restart may run, in the local zone.
#
# **After `devkit-global-tools` at 04:30, deliberately.** That is the pass that owns the
# agent CLIs; this one is not trying to replace it, only to do the part it structurally
# cannot -- with servers up, its exact-name check finds `claude` running and skips, every
# night. Running first would invert that into this job racing a pass that was about to
# succeed on its own. Ordered second, a desk with no servers configured behaves exactly
# as it does today and this reports "current".
DEFAULT_UPDATE_AT = "04:45"

# Concurrent sessions one server will serve. `remote-control`'s own default is 32, and
# **this is where a server's memory actually goes** -- the process baseline is one cost,
# paid once, but every session the phone spawns is held for as long as the server lives.
# Eight is more phone conversations than a project accumulates between nightly restarts,
# and the cost of the bound being too low is one refused session with a clear reason,
# against an unbounded server that grows all week.
DEFAULT_CAPACITY = 8

# Whether to stop the servers while someone is sitting at the machine.
#
# **Off by default, and it is a real trade-off rather than a free saving.** Restarting
# brings sessions back only inside about four hours; a working day at the desk is longer
# than that, so the conversations are gone rather than paused. What it buys is the whole
# per-server footprint back during the hours you are using the desktop for something
# else. Worth it for a machine that is tight on memory, wrong for one where you want to
# pick up this morning's phone conversation after lunch.
DEFAULT_POWER_SAVING = False

# Minutes of no keyboard or mouse input before the desk counts as empty. Fifteen is
# longer than reading a diff and shorter than a meeting.
DEFAULT_AWAY_MINUTES = 15


@dataclass(frozen=True)
class Config:
    """What the workspace file asks this job to serve."""

    projects: tuple[str, ...] = ()
    spawn: str = DEFAULT_SPAWN
    permission_mode: str = DEFAULT_PERMISSION_MODE
    update_at: str = DEFAULT_UPDATE_AT
    idle_minutes: int = DEFAULT_IDLE_MINUTES
    capacity: int = DEFAULT_CAPACITY
    power_saving: bool = DEFAULT_POWER_SAVING
    away_minutes: int = DEFAULT_AWAY_MINUTES


def _text(value: object, fallback: str) -> str:
    return value if isinstance(value, str) and value else fallback


def _positive_int(value: object, fallback: int) -> int:
    """Ints only, and only useful ones.

    `isinstance(True, int)` is true, so a `bool` slipping through here would set an idle
    window of one minute from a setting someone wrote as `true`.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return fallback
    return value


def parse_config(text: str) -> Config:
    """Read `RC_SETTING` out of a workspace file.

    Two accepted shapes, because the list is what almost every machine wants and an
    object that exists only to hold one key is a tax on the common case:

        "devkit.remoteControl": ["devkit", "carameli"]
        "devkit.remoteControl": {"projects": ["devkit"], "idleMinutes": 30}

    Malformed input yields an empty `Config`, matching `sweep.parse_workspace` and
    `sweep.on_hold`: this job's failure mode for a broken workspace file is to serve
    nothing, which is visible in the artifact, rather than to crash a scheduled task
    whose stdout goes nowhere.
    """
    try:
        payload = devkit_jsonc.loads(text)
    except (json.JSONDecodeError, TypeError):
        return Config()
    if not isinstance(payload, dict):
        return Config()
    settings = payload.get("settings")
    if not isinstance(settings, dict):
        return Config()
    raw = settings.get(RC_SETTING)
    if isinstance(raw, list):
        raw = {"projects": raw}
    if not isinstance(raw, dict):
        return Config()

    names = raw.get("projects")
    projects = (
        tuple(name for name in names if isinstance(name, str) and name)
        if isinstance(names, list)
        else ()
    )
    return Config(
        projects=projects,
        spawn=_text(raw.get("spawn"), DEFAULT_SPAWN),
        permission_mode=_text(raw.get("permissionMode"), DEFAULT_PERMISSION_MODE),
        update_at=_text(raw.get("updateAt"), DEFAULT_UPDATE_AT),
        idle_minutes=_positive_int(raw.get("idleMinutes"), DEFAULT_IDLE_MINUTES),
        capacity=_positive_int(raw.get("capacity"), DEFAULT_CAPACITY),
        # The one setting where a bare `bool` is the right type, so unlike the ints this
        # accepts nothing else: a truthy string ("false", say) meaning True is how a
        # power-saving mode nobody asked for starts destroying sessions.
        power_saving=raw.get("powerSaving") is True,
        away_minutes=_positive_int(raw.get("awayMinutes"), DEFAULT_AWAY_MINUTES),
    )


def selected(
    config: Config, known: Sequence[str], held: frozenset[str]
) -> tuple[list[str], list[str]]:
    """`(serve, notes)` -- the projects to serve, and a line for each one refused.

    A name that is not in the workspace is reported rather than passed over: the setting
    is hand-edited, and a typo that silently serves nothing looks exactly like a machine
    where the job is working.

    A project on hold is refused for the reason `upgrade-project.py` refuses it -- a
    paused project is one nothing should be in flight for, and a phone-reachable server
    is an invitation to start something.
    """
    serve: list[str] = []
    notes: list[str] = []
    for name in config.projects:
        if name not in known:
            notes.append(f"{name}: not a checkout in the workspace file -- skipped")
        elif name in held:
            notes.append(f"{name}: on hold (workspace `{sweep.ON_HOLD_SETTING}`) -- skipped")
        else:
            serve.append(name)
    return serve, notes


def valid_time(at: str) -> bool:
    """`HH:MM`, 24-hour. Same check and same reason as `install-upgrade-schedule`."""
    hours, _, minutes = at.partition(":")
    if not (hours.isdigit() and minutes.isdigit()) or len(hours) != 2 or len(minutes) != 2:
        return False
    return 0 <= int(hours) <= 23 and 0 <= int(minutes) <= 59


def due_for_update(last_update: str, now: _dt.datetime, update_at: str) -> bool:
    """Whether today's update-and-restart is still owed.

    Compared against the *date*, not against an interval, so a machine that was off at
    `update_at` runs it on the first tick after it comes back rather than waiting for
    tomorrow. That is `StartWhenAvailable` expressed in the one place the scheduler's
    version of it cannot reach -- the scheduler catches up a missed *fire*, and every
    fire happens in the runner, so only this predicate knows a whole day was missed.
    """
    if not valid_time(update_at):
        return False
    if last_update == now.date().isoformat():
        return False
    return now.strftime("%H:%M") >= update_at
