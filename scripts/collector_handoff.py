#!/usr/bin/env python3
"""A collector's handoff: the project's own command that carries its data to another machine.

devkit is stdlib only, so it can neither reach the store the data travels through
(ibkr_trader's is a Cloudflare R2 bucket) nor dump and restore a project's database. The
data half of `collectors.py move` is therefore the project's: an argv prefix it declares
as `handoff` beside the collector (`collectors_config.Collector.handoff`), run on the host
from the project's checkout -- where its scheduled `command` collectors run -- with one
verb appended:

    <handoff> export --from A --to B   # dump, upload, address a marker to B; devkit has
                                       # stopped the collector's service first
    <handoff> pending --host B         # the marker addressed to B and not yet imported,
                                       # as JSON on stdout, or nothing at all
    <handoff> import --host B          # download, verify, restore, mark it imported; the
                                       # service is left stopped for devkit to start

Exit 0 is success, 1 failure, and 2 "cannot tell" -- the store could not be read, which
only `pending` answers. The project writes its own account of each run to `PROJECT_LOG`
in its checkout, which is where a failure here points.

This module runs the command and reads what it said; when to run it is `collectors.py`'s.
It also keeps this machine's record of the handoffs it sent (`OUTGOING`), which is the
only way the sending machine can tell, later, that nothing runs the collector yet.

**A machine is named by `harness_events.host_name`** -- `$DEVKIT_HOST`, else the
hostname, lowercased -- the name it already files ledger events under. Both ends of a
handoff are named by that one function, so a marker one machine addresses is the one the
other finds.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collector_tasks
import collectors_config as config

sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))
import harness_events

# Every spawn here is reachable from the scheduled pass; see `tests/test_scheduled_jobs.py`.
NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)

EXPORT, PENDING, IMPORT = "export", "pending", "import"

# The contract's "cannot tell": the store could not be read.
CANNOT_TELL = 2

# The project's own account of its last run, relative to its checkout.
PROJECT_LOG = Path("logs/handoff.json")

# Seconds. `pending` runs on every pass of every machine that could receive a handoff,
# so it is one GET plus `uv run` starting in the checkout, and its bound is short: a
# timeout reads as "cannot tell", like exit 2, and the next pass asks again.
PENDING_TIMEOUT = 120
# A dump of ibkr_trader's database (about 6.5 GB on disk, about 1.8 GB compressed) and its
# upload over a home uplink: at 5 Mbit/s the upload alone is about 50 minutes. Generous,
# because an export cut short is a move that did not happen -- `move` starts the service
# again and the person is back where they began, an hour later.
EXPORT_TIMEOUT = 90 * 60
# Download is the fast direction; restoring the same 6.5 GB serially took tens of
# minutes. It runs inside the scheduled pass, whose task time limit
# (`install-collectors.TIME_LIMIT`) is held above it by `tests/test_install_collectors.py`.
IMPORT_TIMEOUT = 60 * 60

# This machine's handoffs sent and not yet seen taken over, `{project: {to, at}}`.
OUTGOING = Path("logs/collectors.handoffs.json")
TO, AT = "to", "at"

# What a `pending` answer comes to (`read_pending`).
NONE = "none"  # nothing is addressed to the host
WAITING = "waiting"  # a marker is, and has not been imported
UNREADABLE = "unreadable"  # the contract's "cannot tell", or no answer in time
BROKEN = "broken"  # any other failure, or an answer that is not a marker


def this_host() -> str:
    """This machine's name, as both ends of a handoff spell it."""
    return harness_events.host_name()


def host_key(typed: str) -> str:
    """A host as a person typed it -- any case, with or without a MagicDNS suffix
    (`desktop-ml5f3pj.tail1234.ts.net`) -- as `this_host` spells that machine. "" when
    nothing of a name is left."""
    return harness_events.host_slug(typed.strip().split(".")[0])


@dataclass(frozen=True)
class Said:
    """What one run of the handoff command said: its exit code and its two streams,
    apart. Both are empty for a run whose output went to the terminal."""

    code: int
    out: str = ""
    err: str = ""

    def summary(self) -> str:
        """`exit N`, and the first thing it said when it said anything."""
        words = next(
            (
                line.strip()
                for text in (self.err, self.out)
                for line in text.splitlines()
                if line.strip()
            ),
            "",
        )
        return f"exit {self.code}: {words[:160]}" if words else f"exit {self.code}"

    def tail(self, lines: int = 20) -> list[str]:
        return [*self.out.splitlines(), *self.err.splitlines()][-lines:]


@dataclass(frozen=True)
class Pending:
    """`read_pending`'s reading of one `pending` run."""

    state: str
    marker: dict = field(default_factory=dict)
    said: Said = Said(0)

    @property
    def source(self) -> str:
        """The machine the marker says the handoff came from, or "another machine"."""
        source = self.marker.get("from")
        return source if isinstance(source, str) and source else "another machine"


def read_pending(said: Said) -> Pending:
    """What a `pending` run comes to. Its stdout is the answer; stderr is `uv`'s and the
    project's chatter, and is never read as a marker."""
    if said.code in (CANNOT_TELL, collector_tasks.TIMED_OUT):
        return Pending(UNREADABLE, said=said)
    if said.code != 0:
        return Pending(BROKEN, said=said)
    if not said.out.strip():
        return Pending(NONE, said=said)
    try:
        marker = json.loads(said.out)
    except ValueError:
        return Pending(BROKEN, said=said)
    return (
        Pending(WAITING, marker, said) if isinstance(marker, dict) else Pending(BROKEN, said=said)
    )


def argv(collector: config.Collector, verb: str, *flags: str) -> list[str]:
    return [*collector.handoff, verb, *flags]


def run(command: Sequence[str], cwd: Path, timeout: int, echo: bool = False) -> Said:
    """Run `command` from `cwd` under `collector_tasks.command_env`; a spawn that could
    not happen is a code, not a raise.

    stdout is kept apart from stderr, because `pending`'s stdout *is* its answer and
    `uv run` writes what it built and installed to stderr. `echo` hands the child this
    terminal's streams instead (`collector_tasks.inherited_streams`), for the export a
    person watches for up to an hour; nothing is captured then. On a timeout the whole
    tree goes (`collector_tasks.kill_tree`): `uv`'s child is the one holding the dump.
    """
    streams = (
        collector_tasks.inherited_streams()
        if echo
        else {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
    )
    try:
        process = subprocess.Popen(
            collector_tasks.resolve(command),
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=NO_WINDOW,
            env=collector_tasks.command_env(),
            **streams,
        )
    except FileNotFoundError:
        return Said(127, err=f"{command[0]} is not on PATH")
    except OSError as exc:
        return Said(126, err=str(exc))
    try:
        out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        stuck = collector_tasks.kill_tree(process.pid)
        process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.communicate(timeout=collector_tasks.REAP_TIMEOUT)
        ended = f"ended it, but not its children: {stuck}" if stuck else "ended it and its children"
        return Said(collector_tasks.TIMED_OUT, err=f"timed out after {timeout}s; {ended}")
    return Said(process.returncode, out or "", err or "")


class Handoff:
    """The three verbs, each from the collector's checkout under its own bound."""

    def run(self, command: Sequence[str], cwd: Path, timeout: int, echo: bool = False) -> Said:
        return run(command, cwd, timeout, echo)

    def export(self, collector: config.Collector, checkout: Path, source: str, dest: str) -> Said:
        command = argv(collector, EXPORT, "--from", source, "--to", dest)
        return self.run(command, checkout, EXPORT_TIMEOUT, echo=True)

    def pending(self, collector: config.Collector, checkout: Path, host: str) -> Pending:
        said = self.run(argv(collector, PENDING, "--host", host), checkout, PENDING_TIMEOUT)
        return read_pending(said)

    def restore(self, collector: config.Collector, checkout: Path, host: str) -> Said:
        return self.run(argv(collector, IMPORT, "--host", host), checkout, IMPORT_TIMEOUT)


def load_outgoing(path: Path) -> dict[str, dict]:
    """`{project: {to, at}}` from `path`; {} when there is none or it is unreadable, and
    any record without a host and a time is dropped."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(project): record
        for project, record in raw.items()
        if isinstance(record, dict)
        and isinstance(record.get(TO), str)
        and isinstance(record.get(AT), (int, float))
    }


def save_outgoing(path: Path, records: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def record_outgoing(path: Path, project: str, dest: str, clock: float) -> None:
    """Note that `project` was handed to `dest` at `clock`, replacing any earlier note."""
    save_outgoing(path, {**load_outgoing(path), project: {TO: dest, AT: clock}})


def forget_outgoing(path: Path, project: str) -> None:
    records = load_outgoing(path)
    if project in records:
        records.pop(project)
        save_outgoing(path, records)
