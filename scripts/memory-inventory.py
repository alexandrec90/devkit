#!/usr/bin/env python3
"""What is eating this machine's memory, on one screen, without an agent.

Backs the "Machine: What Is Eating Memory" task. `reclaim.py` answers "why is this
machine slow" for the disk and the Docker daemon and says of memory only that it cannot
free any; `reap-stale.py` frees the part agents leave behind but reports only what it
would touch. Neither shows the picture a person wants before deciding what to close:
who holds the commit charge, which `claude` processes are sessions someone is in and
which are phone sessions nobody has touched since breakfast, which are servers, what the
MCP server every session starts adds up to, and which dev servers still have an owner.
On 2026-09-07 that picture took an agent forty tool calls to assemble. This is the same
picture in one.

**Read-only, always.** The one action it points at is `reap-stale.py reap`, and the last
section is that script's own `status`, run as a subprocess so the two can never disagree
about what would go. Written to `logs/memory-inventory.log` as well as the terminal, for
the reason every devkit pass leaves a file: the terminal a task opened is gone by the
time the question is asked again.

Stdlib only, and every section is an importable function tested in
`tests/test_memory_inventory.py`.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import subprocess
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rc_machine
import reap_machine
import reclaim
import sweep

REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = Path("logs/memory-inventory.log")

# Read, never written: the servers `rc-servers.py` owns. Same file `reap-stale.py` reads.
RC_STATE = Path("logs/rc-servers.state.json")

MB = 1024 * 1024
TOP = 12

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def run_command(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), capture_output=True, text=True, check=False, timeout=120)


# --- the sections -------------------------------------------------------------------


def holders(table: Sequence[reap_machine.Process], limit: int = TOP) -> list[tuple[str, int, int]]:
    """`(image, count, private bytes)` for the images holding the most, largest first.

    Private bytes rather than working set: the working set is what is resident *now*,
    and on a machine that is paging the number that explains the paging is what each
    process has committed, half of which may already be on disk.
    """
    totals: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for row in table:
        totals[row.name][0] += 1
        totals[row.name][1] += row.private
    ranked = sorted(totals.items(), key=lambda item: (-item[1][1], item[0]))
    return [(name, count, private) for name, (count, private) in ranked[:limit] if private]


def age(created: float, now: float) -> str:
    """`3h 20m`, `2d 4h`, or `?` when the start time is unknown."""
    if created <= 0:
        return "?"
    minutes = max(0, int((now - created) // 60))
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours:02d}h"


def session_role(row: reap_machine.Process, servers: dict[int, str]) -> str:
    """What one `claude` process is, in the words a person sorts them by."""
    name = reap_machine.rc_daemon_name(row)
    argv = reap_machine.argv_of(row.cmdline)
    if name:
        spawn = argv[argv.index("--spawn") + 1] if "--spawn" in argv[:-1] else "?"
        return f"server {name} ({spawn})"
    session_id = reap_machine.spawned_session_id(row)
    if session_id:
        return f"phone session {session_id[:12]}... in {servers.get(row.ppid, '?')}"
    if "-w" in argv or "--worktree" in argv:
        return "interactive (worktree)"
    if "--print" in argv or "-p" in argv:
        return "print"
    return "interactive"


def sessions_section(
    table: Sequence[reap_machine.Process],
    store: Path,
    root: Path | None,
    known: frozenset[int],
    now: float,
) -> list[str]:
    """One line per `claude` process, largest first: role, age, memory, and the fact
    that decides its fate -- idle time for a phone session, ownership for a server."""
    servers = reap_machine.daemons(table)
    lines = []
    for row in sorted((row for row in table if row.name == "claude"), key=lambda row: -row.private):
        role = session_role(row, servers)
        note = ""
        session_id = reap_machine.spawned_session_id(row)
        if session_id and row.ppid in servers:
            project = root / servers[row.ppid] if root else None
            latest = reap_machine.session_activity(session_id, project, store)
            note = (
                "  activity unknown"
                if latest is None
                else f"  idle {int((now - latest) // 60)} min"
            )
        elif reap_machine.rc_daemon_name(row) and row.pid not in known:
            note = "  NOT in rc-servers state"
        lines.append(
            f"  pid {row.pid:<6} {row.private // MB:>5} MB  up {age(row.created, now):<8} {role}{note}"
        )
    return lines or ["  none"]


def mcp_section(table: Sequence[reap_machine.Process]) -> str:
    """The MCP servers, as one line: every session starts its own set at launch."""
    rows = [row for row in table if row.name == "node" and "mcp" in row.cmdline.lower()]
    if not rows:
        return "  none"
    total = sum(row.private for row in rows) // MB
    return f"  {len(rows)} node processes, {total} MB -- one set per session, started with it"


def dev_servers_section(
    table: Sequence[reap_machine.Process], pattern: str = reap_machine.DEV_SERVER_PATTERN
) -> list[str]:
    """Every dev server and who, if anyone, still owns it."""
    lines = []
    orphans = {row.pid for row in reap_machine.dev_servers(table, pattern)}
    for row in reap_machine.dev_servers(table, pattern, hosts=frozenset()):
        chain = reap_machine.ancestors(table, row.pid)
        host = next((parent.name for parent in chain if parent.name in reap_machine.HOSTS), "")
        owner = f"owned by {host}" if host else "NO LIVING OWNER"
        if row.pid in orphans or not host:
            owner = "NO LIVING OWNER -- reap-stale would stop it"
        shown = " ".join(
            token.strip('"').replace("\\", "/").rsplit("/", 1)[-1]
            for token in reap_machine.argv_of(row.cmdline)[:4]
        )
        lines.append(f"  pid {row.pid:<6} {row.private // MB:>5} MB  {owner}  `{shown}`")
    return lines or ["  none"]


def totals_line(snap: reclaim.Snapshot) -> str:
    if snap.limit_gb < 0 or snap.committed_gb < 0:
        return "memory: could not be read"
    used = snap.committed_gb / snap.limit_gb if snap.limit_gb else 0.0
    verdict = (
        "commit headroom available"
        if used < 0.85
        else "HIGH COMMIT -- nearing the allocation limit"
    )
    return (
        f"available {snap.avail_gb:.1f} GB; commit {snap.committed_gb:.1f} / "
        f"{snap.limit_gb:.1f} GB ({used:.0%}) -- {verdict}"
    )


def reap_status(
    root: Path, workspace: Path | None, run: Runner = run_command, python: str = ""
) -> list[str]:
    """`reap-stale.py status`'s own verdict lines, so this never paraphrases them."""
    argv = [
        python or sweep.console_python(),
        str(REPO_ROOT / "scripts" / "reap-stale.py"),
        "status",
    ]
    argv += ["--devkit", str(root)]
    if workspace:
        argv += ["--workspace", str(workspace)]
    try:
        result = run(argv)
    except (OSError, subprocess.SubprocessError) as error:
        return [f"  reap-stale could not be asked: {error}"]
    lines = [line for line in result.stdout.splitlines() if not line.startswith("#")]
    if result.returncode != 0:
        lines.append(f"(reap-stale exited {result.returncode})")
    return [f"  {line}" for line in lines] or ["  nothing left behind"]


# --- the page ---------------------------------------------------------------------------


def render(when: _dt.datetime, totals: str, sections: Sequence[tuple[str, Sequence[str]]]) -> str:
    lines = [f"# memory-inventory {when.isoformat(timespec='seconds')}", totals, ""]
    for heading, body in sections:
        lines.append(heading)
        lines += body
        lines.append("")
    return "\n".join(lines)


def write_artifact(text: str, root: Path = REPO_ROOT) -> Path:
    path = root / ARTIFACT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument("--devkit", type=Path, default=REPO_ROOT)
    parser.add_argument(
        "--top", type=int, default=TOP, help="images to list (default: %(default)s)"
    )
    return parser.parse_args(sys.argv[1:] if argv is None else argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.devkit.expanduser().resolve()
    workspace = args.workspace or sweep.default_workspace(root)
    now = time.time()
    when = _dt.datetime.now()

    table = reap_machine.process_table()
    if table is None:
        text = render(when, "the process table could not be read", [])
        print(text, end="")
        write_artifact(text, root)
        return 2

    known = frozenset(rc_machine.State.load(root / RC_STATE).servers.values())
    holder_lines = [
        f"  {name:<24} {count:>3} proc  {private // MB:>6} MB"
        for name, count, private in holders(table, args.top)
    ]
    sections = [
        (f"Top {args.top} holders (private bytes):", holder_lines or ["  none"]),
        (
            "claude processes:",
            sessions_section(
                table,
                rc_machine.sessions_store(),
                workspace.parent if workspace else None,
                known,
                now,
            ),
        ),
        ("MCP servers:", [mcp_section(table)]),
        ("Dev servers:", dev_servers_section(table)),
        ("What `reap-stale.py reap` would stop:", reap_status(root, workspace)),
    ]
    text = render(when, totals_line(reclaim.snapshot()), sections)
    print(text, end="")
    path = write_artifact(text, root)
    print(f"(written to {path})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
