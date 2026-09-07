#!/usr/bin/env python3
"""The machine half of `reap-stale.py`: the process table, and what in it is a leftover.

`reap-stale.py` is the pass the scheduler names and the one that decides; this is
everything that reads the machine or reads one row of it, as pure functions over a table
of `Process` rows so every decision can be exercised against a hand-written table.

What an agent session leaves behind when it ends is not one thing, and each kind has a
different owner of record:

- **A spawned Remote Control session** -- `claude --print --sdk-url ... --session-id
  <id>`, started by an `rc-servers.py` server when a phone opened a conversation. The
  server holds it for as long as the server lives, by `remote-control`'s design, so a
  conversation left open at breakfast is still a 300 MB process at dinner. Its transcript
  is the record of use: `session_activity` reads the newest mtime for that session.
- **A stray Remote Control server** -- `claude remote-control --name <project>` for a
  project the workspace serves, that `rc-servers.py`'s state file does not know about.
  That job tracks servers only by the pids it wrote down, so a server it started under an
  earlier configuration outlives the config change and keeps accepting sessions beside
  the one that replaced it. Six servers with a capacity of eight is how a 16 GB desk came
  to hold forty-eight potential sessions.
- **An orphaned dev server** -- `vite`, `vitest`, `npm run dev` started in the
  background by an agent's shell tool, whose session has since ended. The shell survives
  the session on Windows, so the server's ancestry ends in a process that exists and a
  parent that does not. `is_hosted` asks the only question that separates that from a
  server a person started: does the chain reach something a person is sitting in front
  of?

**Unknown is busy**, as in `rc_machine`: a table that cannot be read is `None` and the
pass reaps nothing; a transcript store that cannot be read makes every session active;
an ancestry that reaches a living host is owned however old it is.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_clis
import rc_machine

WINDOWS = os.name == "nt"

# Reading the whole process table through PowerShell takes a few seconds on a loaded
# machine; `tasklist` would be quicker but reports neither parent pids nor command lines,
# and both are the whole question here.
QUICK_TIMEOUT = 60

# Process names that mean "a person is on the other end of this": a chain that reaches
# one of them alive is owned, whatever it is doing and however old it is. Shells are
# deliberately absent -- `bash`, `pwsh`, `cmd` -- because a shell whose owner has gone is
# exactly the shape an orphan takes. `svchost` and `services` cover the scheduler's own
# children on Windows, so a preview server a scheduled task started is not a stray.
HOSTS = frozenset(
    {
        "claude",
        "codex",
        "code",
        "cursor",
        "windowsterminal",
        "explorer",
        "svchost",
        "services",
        "tmux",
        "sshd",
        "systemd",
        "launchd",
        "init",
    }
)

# Only these images are ever a dev-server candidate. The pattern below is matched
# against a command line, and a command line is also where an editor names the file it
# has open -- `Code.exe ... vite.config.ts` would match `vite` -- so the image is checked
# first and the pattern second.
DEV_SERVER_NAMES = frozenset({"node", "npm", "npx", "bun", "deno"})

DEV_SERVER_PATTERN = (
    r"\bvite\b|\bvitest\b|\bwebpack\b|\bnext(\.js)?\"?\s+dev\b"
    r"|\bnpm(-cli\.js|\.exe|\.cmd)?\"?\s+run\s+dev\b"
)

# Seconds between the polite `taskkill` and the `/F`. Shorter than `rc_machine`'s: a
# leftover has, by definition, nobody waiting on what it writes on the way out.
STOP_GRACE_SECONDS = 5

PS_QUERY = (
    "Get-CimInstance Win32_Process | "
    "Select-Object ProcessId,ParentProcessId,Name,CommandLine | ConvertTo-Json -Compress"
)

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class Process:
    """One row of the table. `name` is the bare lowercase stem, `claude` not `claude.EXE`."""

    pid: int
    ppid: int
    name: str
    cmdline: str


@dataclass(frozen=True)
class Session:
    """A spawned Remote Control session: its row, its id, and the server's project."""

    row: Process
    session_id: str
    project: str


def run_command(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        check=False,
        timeout=QUICK_TIMEOUT,
        creationflags=rc_machine.NO_WINDOW,
    )


# --- the table -----------------------------------------------------------------


def table_argv(windows: bool = WINDOWS) -> list[str]:
    """The lister that reports parent pids and command lines on this platform."""
    if windows:
        return ["powershell", "-NoProfile", "-NonInteractive", "-Command", PS_QUERY]
    return ["ps", "-eo", "pid=,ppid=,comm=,args="]


def parse_windows_table(text: str) -> list[Process]:
    """`ConvertTo-Json` output: a list of objects, or one bare object for one row.

    `CommandLine` is `null` for the system processes and for any the caller may not
    inspect, and those rows are kept with an empty command line: they are still parents,
    and an ancestry that dropped them would end early and read as orphaned.
    """
    try:
        payload = json.loads(text or "")
    except json.JSONDecodeError:
        return []
    rows = payload if isinstance(payload, list) else [payload]
    table = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        pid, ppid = row.get("ProcessId"), row.get("ParentProcessId")
        if isinstance(pid, bool) or not isinstance(pid, int) or not isinstance(ppid, int):
            continue
        name, cmdline = row.get("Name"), row.get("CommandLine")
        table.append(
            Process(
                pid,
                ppid,
                agent_clis.normalise_process(name if isinstance(name, str) else ""),
                cmdline if isinstance(cmdline, str) else "",
            )
        )
    return table


def parse_posix_table(text: str) -> list[Process]:
    """`ps -eo pid=,ppid=,comm=,args=`: four whitespace columns, the last one free text."""
    table = []
    for line in text.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 3 or not (parts[0].isdigit() and parts[1].isdigit()):
            continue
        args = parts[3] if len(parts) == 4 else ""
        table.append(
            Process(int(parts[0]), int(parts[1]), agent_clis.normalise_process(parts[2]), args)
        )
    return table


def process_table(run: Runner = run_command, windows: bool = WINDOWS) -> list[Process] | None:
    """Every process on the machine, or `None` when the machine could not be asked.

    An empty table is reported as `None` too: this process is in any table that was
    actually read, so empty means the lister answered with something the parser could
    not use, and a pass that trusted it would find nothing owned and reap everything.
    """
    try:
        result = run(table_argv(windows))
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    table = parse_windows_table(result.stdout) if windows else parse_posix_table(result.stdout)
    return table or None


def argv_of(cmdline: str) -> list[str]:
    """A command line as tokens, quotes kept.

    `posix=False` so a Windows path's backslashes survive; the tokens compared here --
    `remote-control`, `--name`, `--session-id` -- are never quoted, so keeping the quotes
    on the ones that are costs nothing.
    """
    try:
        return shlex.split(cmdline, posix=False)
    except ValueError:
        return []


# --- ancestry ------------------------------------------------------------------


def ancestors(table: Sequence[Process], pid: int) -> list[Process]:
    """The chain above `pid`, nearest first, ending where a parent is not in the table.

    A parent pid that names nothing is the ordinary end of an orphan's chain: Windows
    keeps the dead parent's pid on the child forever. The seen-set bounds it -- pid 0 is
    its own parent, and a recycled pid can close a loop.
    """
    rows = {row.pid: row for row in table}
    chain: list[Process] = []
    seen = {pid}
    current = rows.get(pid)
    while current is not None and current.ppid not in seen:
        parent = rows.get(current.ppid)
        if parent is None:
            break
        chain.append(parent)
        seen.add(parent.pid)
        current = parent
    return chain


def is_hosted(table: Sequence[Process], pid: int, hosts: frozenset[str] = HOSTS) -> bool:
    """Whether something a person sits in front of is still above `pid`."""
    return any(row.name in hosts for row in ancestors(table, pid))


# --- Remote Control rows -------------------------------------------------------


def rc_daemon_name(row: Process) -> str:
    """The `--name` of a `claude remote-control` server, or "" for anything else.

    The mode must be the first argument, not merely present: a session whose prompt
    mentions `remote-control` carries the word on its command line too. A server started
    without `--name` is not devkit's -- `rc_machine.launch_argv` always passes one -- and
    is left alone under whatever name it generated for itself.
    """
    if row.name != "claude":
        return ""
    argv = argv_of(row.cmdline)
    if argv[1:2] != ["remote-control"]:
        return ""
    try:
        return argv[argv.index("--name") + 1]
    except (ValueError, IndexError):
        return ""


def spawned_session_id(row: Process) -> str:
    """The `--session-id` of a session a server spawned, or "" for any other `claude`.

    `--sdk-url` is the marker: an interactive session has no such flag, and the one
    thing this must never return an id for is a session with a person typing into it.
    """
    if row.name != "claude":
        return ""
    argv = argv_of(row.cmdline)
    if "--sdk-url" not in argv:
        return ""
    try:
        return argv[argv.index("--session-id") + 1]
    except (ValueError, IndexError):
        return ""


def daemons(table: Sequence[Process]) -> dict[int, str]:
    """`pid -> project` for every named Remote Control server in the table."""
    return {row.pid: name for row in table if (name := rc_daemon_name(row))}


def sessions(table: Sequence[Process]) -> list[Session]:
    """Every spawned session whose parent is a named server, oldest pid first.

    Only children of a server: a `--sdk-url` process with some other parent was started
    by something this job knows nothing about, and "knows nothing about" is not a reason
    to kill it.
    """
    servers = daemons(table)
    found = [
        Session(row, session_id, servers[row.ppid])
        for row in table
        if row.ppid in servers and (session_id := spawned_session_id(row))
    ]
    return sorted(found, key=lambda session: session.row.pid)


def newest_transcript(directory: Path) -> float:
    """Newest `*.jsonl` mtime under one store directory, `0.0` for none."""
    mtimes = []
    for path in directory.glob("*.jsonl"):
        try:
            mtimes.append(path.stat().st_mtime)
        except OSError:
            continue
    return max(mtimes) if mtimes else 0.0


def session_activity(session_id: str, project: Path | None, store: Path) -> float | None:
    """When one spawned session last wrote its transcript; `None` when that is unknowable.

    A session spawned into a worktree runs in `.claude/worktrees/bridge-<id>`, and the
    store files that directory under a name carrying the id, so the id finds it. One
    spawned in place shares the project's own directory with every other session there,
    and the newest transcript in it may be an interactive session's -- which errs
    towards "active", the right direction. No project directory and no id match is
    `None`, and `None` is kept.
    """
    if not store.is_dir():
        return None
    key = rc_machine.slug(session_id)
    try:
        matched = [path for path in store.iterdir() if key and key in path.name and path.is_dir()]
    except OSError:
        return None
    if matched:
        return max(newest_transcript(path) for path in matched)
    if project is None:
        return None
    return rc_machine.last_activity(project, store)


# --- dev servers ---------------------------------------------------------------


def dev_servers(
    table: Sequence[Process],
    pattern: str = DEV_SERVER_PATTERN,
    hosts: frozenset[str] = HOSTS,
) -> list[Process]:
    """Dev servers with no living owner -- the top of each orphaned tree only.

    `taskkill /T` takes a tree from the pid it is given, so of `npm run dev` and the
    `vite` it spawned only the `npm` is returned; naming both would kill the second one
    twice and report a failure for the one that was already gone.
    """
    regex = re.compile(pattern, re.IGNORECASE)
    matched = {
        row.pid: row for row in table if row.name in DEV_SERVER_NAMES and regex.search(row.cmdline)
    }
    orphans = []
    for row in matched.values():
        if is_hosted(table, row.pid, hosts):
            continue
        if any(parent.pid in matched for parent in ancestors(table, row.pid)):
            continue
        orphans.append(row)
    return sorted(orphans, key=lambda row: row.pid)


# --- stopping ------------------------------------------------------------------


def pid_alive(pid: int, run: Runner = run_command, windows: bool = WINDOWS) -> bool:
    """Whether anything is running as `pid`. Unknown is alive, so a failed probe escalates
    to the `/F` a dead process cannot mind."""
    if not windows:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True
        return True
    try:
        result = run(rc_machine.pid_query_argv(pid))
    except (OSError, subprocess.SubprocessError):
        return True
    if result.returncode != 0:
        return True
    return bool(rc_machine.parse_pid_image(result.stdout or ""))


def stop_tree(
    pid: int,
    run: Runner = run_command,
    sleep: Callable[[float], None] = time.sleep,
    windows: bool = WINDOWS,
) -> str:
    """Stop `pid` and everything under it. "" on success, else the reason.

    `rc_machine.stop_argv`'s `/T`, for its reason: a session spawns MCP servers and a
    dev server spawns workers, and leaving those behind leaks the memory the reap was
    for. Polite first, `/F` only for what is still there after the grace. Off Windows
    this reports rather than pretends: the pass is read-only there.
    """
    if not windows:
        return "stopping processes is only implemented on Windows"
    try:
        run(rc_machine.stop_argv(pid))
        sleep(STOP_GRACE_SECONDS)
        if not pid_alive(pid, run, windows):
            return ""
        result = run(rc_machine.stop_argv(pid, force=True))
    except (OSError, subprocess.SubprocessError) as error:
        return str(error)
    if result.returncode != 0:
        return (result.stderr or result.stdout or "taskkill failed").strip()
    return ""
