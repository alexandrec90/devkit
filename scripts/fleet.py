#!/usr/bin/env python3
"""See every PC in one place, and move a collector from whichever one you are sitting at.

`collectors.py` keeps a collector running on the machine assigned it, and `move` hands one
to another machine, but it sees one machine and has to be typed on the one that runs it.
This is the layer above that: it asks every PC on the tailnet what it has free and what it
runs, so adding a PC means joining the tailnet and accepting this machine's SSH key, with
no list of machines to maintain.

    fleet.py                            # every PC: memory, collectors, what is wrong
    fleet.py move PROJECT --to HOST     # from any PC, whichever one runs it
    fleet.py rebalance                  # which move would relieve the shortest PC
    fleet.py rebalance --apply          # make that one move
    fleet.py probe                      # this PC as JSON: what the others read over SSH

**Nothing here moves data or keeps state.** A move is `collectors.py move` run on the
machine that owns the collector, over SSH when that is not this one, and the destination
picks it up on its own next pass as it always has. So a PC that cannot be reached costs a
row saying so, and the collectors keep running exactly as they were.

**One move at a time, and only what opted in.** A collector may be picked by `rebalance`
only if it declares `"rebalance": true`, because some moves are not free: ibkr_trader's
carries a 35 GB database and is down for the dump, and social-scraper's browser logins
belong to one machine. A named `move` is not limited this way: it needs only a `handoff`.

**A rebalance has to help.** It moves nothing unless the destination stays above
`FLOOR_GB + MARGIN_GB` afterwards, so a move cannot simply transfer the shortage, and it
says when the memory is not held by anything it may move. The largest user of memory on a
workstation is usually not a collector.

A collector's footprint is the larger of what its containers use now and the `memoryGb` it
declares. The declaration covers what `docker stats` cannot see: a scheduled collector's
Chrome exists only during a run.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collector_handoff as handoff
import collectors_config as config
import host_memory

REPO_ROOT = Path(__file__).resolve().parents[1]

# Below this a machine is short of memory. Two gigabytes is about where the pool's gateway
# started missing its keepalives on a 16 GB PC.
FLOOR_GB = 2.0
# What a destination keeps above the floor after a move, so one busy agent session there
# does not make it the next machine a rebalance moves away from.
MARGIN_GB = 2.0

# Peers that can run a collector, as Tailscale spells their OS: a phone on the tailnet is
# a peer too, and is not a machine to report as unreachable.
WORKSTATIONS = frozenset({"windows", "linux", "macOS"})

SSH = (
    "ssh",
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=8",
    # The long pause of a dump says nothing on the wire, so keepalives tell it from a hang.
    "-o",
    "ServerAliveInterval=30",
    # OpenSSH's post-quantum notice is a warning on stderr: not a failure to report.
    "-o",
    "LogLevel=ERROR",
)
TAILSCALE_TIMEOUT = 15
PROBE_TIMEOUT = 60
DOCKER_TIMEOUT = 30

Runner = Callable[[Sequence[str], int], tuple[int, str]]
Mover = Callable[[Sequence[str]], int]


def spawn(argv: Sequence[str], timeout: int) -> tuple[int, str]:
    """Exit code and stdout. Stderr joins it only on failure, where it is the reason."""
    try:
        done = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 124, f"{type(exc).__name__}: {exc}"
    return done.returncode, done.stdout if done.returncode == 0 else done.stderr or done.stdout


def inherit(argv: Sequence[str]) -> int:
    """Run a move with its output on this terminal: it can take an hour, and says why."""
    return subprocess.run(list(argv), check=False).returncode


@dataclass(frozen=True)
class Machine:
    """One PC as it answered, or as far as it could be asked (`error`)."""

    host: str
    available_gb: float = 0.0
    assignment: Mapping[str, str] = field(default_factory=dict)
    measured_gb: Mapping[str, float] = field(default_factory=dict)
    ready: frozenset[str] = frozenset()
    error: str = ""

    @property
    def runs(self) -> list[str]:
        return sorted(p for p, mode in self.assignment.items() if mode == config.RUN)


@dataclass(frozen=True)
class Move:
    """One move a rebalance chose, and how much it takes off the source."""

    project: str
    source: str
    dest: str
    frees_gb: float


def tailnet_hosts(status_json: str) -> tuple[list[str], list[str]]:
    """The workstations `tailscale status --json` shows online, and those it shows offline,
    spelled as `collector_handoff.host_key` spells a machine."""
    status = json.loads(status_json)
    peers = [status.get("Self") or {}, *(status.get("Peer") or {}).values()]
    online: list[str] = []
    offline: list[str] = []
    for peer in peers:
        name = handoff.host_key(peer.get("HostName") or "")
        if name and peer.get("OS") in WORKSTATIONS:
            (online if peer.get("Online") else offline).append(name)
    return sorted(online), sorted(offline)


def container_gb(stats: str, projects: Sequence[str]) -> dict[str, float]:
    """Gigabytes per declared project from `docker stats` lines of `<name> <used>`.

    A container is `<project>-<service>-<n>`, and either part may hold a hyphen
    (`ibkr_trader-ib-gateway-1`, `social-scraper-db-1`), so the name is matched against
    the projects rather than split. Anything else on the machine is not a collector."""
    units = {"KiB": 1 / 2**20, "MiB": 1 / 2**10, "GiB": 1.0}
    totals: dict[str, float] = {}
    for line in stats.splitlines():
        parts = line.split()
        owner_of = [p for p in projects if parts and parts[0].startswith(f"{p}-")]
        if len(parts) < 2 or not owner_of:
            continue
        for unit, scale in units.items():
            if parts[1].endswith(unit):
                used = float(parts[1].removesuffix(unit)) * scale
                totals[owner_of[0]] = totals.get(owner_of[0], 0.0) + used
    return totals


def probe(root: Path = REPO_ROOT, run: Runner = spawn) -> dict[str, Any]:
    """This machine, as `fleet.py probe` prints it for the machine asking."""
    base = config.home(root)
    collectors, _ = config.declared(base)
    workspace = base.parent
    code, out = run(
        ["docker", "stats", "--no-stream", "--format", "{{.Name}} {{.MemUsage}}"], DOCKER_TIMEOUT
    )
    available = host_memory.available_mb()
    return {
        "host": handoff.this_host(),
        "available_gb": round((available or 0) / 1024, 2),
        "assignment": config.load_assignment(base / config.ASSIGNMENT),
        "measured_gb": container_gb(out, [c.project for c in collectors]) if code == 0 else {},
        "ready": [
            c.project
            for c in collectors
            if (workspace / c.project / ".git").exists()
            and (workspace / c.project / ".env").exists()
        ],
    }


def machine_from(payload: Mapping[str, Any]) -> Machine:
    """A `Machine` from what a `probe` printed."""
    return Machine(
        host=str(payload["host"]),
        available_gb=float(payload["available_gb"]),
        assignment=dict(payload["assignment"]),
        measured_gb=dict(payload["measured_gb"]),
        ready=frozenset(payload["ready"]),
    )


def remote_script(host: str, root: Path, script: str, *args: str) -> list[str]:
    """Run a devkit script on `host`, from the checkout at the same place under its home."""
    relative = (config.home(root) / "scripts" / script).relative_to(Path.home())
    return [*SSH, host, "python", relative.as_posix(), *args]


def ask(host: str, here: str, root: Path, run: Runner) -> Machine:
    """One machine's `probe`, or the machine with the reason it did not answer."""
    if host == here:
        return machine_from(probe(root, run))
    code, out = run(remote_script(host, root, "fleet.py", "probe"), PROBE_TIMEOUT)
    if code != 0:
        lines = [line for line in out.splitlines() if line.strip()]
        return Machine(host, error=(lines[-1] if lines else f"ssh exited {code}")[:120])
    try:
        return machine_from(json.loads(out))
    except (ValueError, KeyError, TypeError) as exc:
        return Machine(host, error=f"answered oddly: {exc}"[:120])


def survey(root: Path = REPO_ROOT, run: Runner = spawn) -> list[Machine]:
    """Every workstation on the tailnet. Without Tailscale, just this machine."""
    here = handoff.this_host()
    code, out = run(["tailscale", "status", "--json"], TAILSCALE_TIMEOUT)
    online, offline = tailnet_hosts(out) if code == 0 else ([here], [])
    if here not in online:
        online.append(here)
    return [ask(host, here, root, run) for host in online] + [
        Machine(host, error="offline") for host in offline
    ]


def footprint(collector: config.Collector, machine: Machine | None = None) -> float:
    """Gigabytes this collector costs a machine: see the module docstring."""
    measured = machine.measured_gb.get(collector.project, 0.0) if machine else 0.0
    return max(measured, collector.memory_gb)


def anomalies(machines: Sequence[Machine], collectors: Sequence[config.Collector]) -> list[str]:
    """What is wrong with the placement. Nothing is said to run nowhere while a machine
    that might run it did not answer: that would be a guess."""
    found = []
    blind = any(m.error for m in machines)
    for collector in collectors:
        runners = [m.host for m in machines if collector.project in m.runs]
        if len(runners) > 1:
            found.append(f"{collector.project} is assigned to {', '.join(runners)}: two writers")
        elif not runners and not blind:
            found.append(f"{collector.project} runs nowhere")
    return found


def plan(
    machines: Sequence[Machine],
    collectors: Sequence[config.Collector],
    floor: float = FLOOR_GB,
    margin: float = MARGIN_GB,
) -> tuple[Move | None, list[str]]:
    """The one move that best relieves the shortest machine, and why not when there is none."""
    up = [m for m in machines if not m.error]
    short = sorted((m for m in up if m.available_gb < floor), key=lambda m: m.available_gb)
    if not short:
        return None, [f"no machine is below {floor:g} GB"]
    notes: list[str] = []
    for source in short:
        movable = [c for c in collectors if c.rebalance and c.handoff and c.project in source.runs]
        if not movable:
            notes.append(f"{source.host} is short, but runs nothing `rebalance` may move")
            continue
        for collector in sorted(movable, key=lambda c: -footprint(c, source)):
            cost = footprint(collector, source)
            room = [
                m
                for m in up
                if m.host != source.host
                and collector.project in m.ready
                and m.available_gb - cost >= floor + margin
            ]
            if room:
                best = max(room, key=lambda m: m.available_gb)
                return Move(collector.project, source.host, best.host, cost), notes
            notes.append(
                f"{collector.project} ({cost:.1f} GB) has nowhere to go from {source.host}: "
                f"no machine with its checkout would keep {floor + margin:g} GB"
            )
    return None, notes


def move_argv(project: str, source: str, dest: str, here: str, root: Path) -> list[str]:
    """`collectors.py move` as it has to run: on the machine that owns the collector."""
    args = ("move", project, "--to", dest)
    if source == here:
        return [sys.executable, str(config.home(root) / "scripts" / "collectors.py"), *args]
    return remote_script(source, root, "collectors.py", *args)


def owner(project: str, machines: Sequence[Machine]) -> tuple[str, str]:
    """The machine running `project`, or "" and why no single one can be named."""
    runners = [m.host for m in machines if project in m.runs]
    if len(runners) == 1:
        return runners[0], ""
    if runners:
        return "", f"{project} is assigned to {', '.join(runners)}: settle that first"
    silent = [m.host for m in machines if m.error]
    if silent:
        return "", f"no machine that answered runs {project}; not heard from: {', '.join(silent)}"
    return "", f"no machine runs {project}"


def refusal(project: str, dest: str, machines: Sequence[Machine]) -> str:
    """Why `dest` cannot take `project`, or "". The checkout and its `.env` are looked for
    here because `collectors.py move` stops the collector before the destination is asked
    anything, and a pickup that cannot run leaves it stopped."""
    target = next((m for m in machines if m.host == dest), None)
    if target is None:
        return f"{dest} is not on the tailnet"
    if target.error:
        return f"{dest} cannot take it: {target.error}"
    if project not in target.ready:
        return f"{dest} has no checkout of {project} with a .env"
    return ""


def move(
    project: str,
    to: str,
    machines: Sequence[Machine],
    root: Path = REPO_ROOT,
    mover: Mover = inherit,
) -> int:
    """Hand `project` to `to`, from whichever machine this is."""
    dest = handoff.host_key(to)
    source, why = owner(project, machines)
    if source == dest:
        why = f"{dest} already runs {project}"
    why = why or refusal(project, dest, machines)
    if why:
        print(f"fleet: not moved -- {why}", file=sys.stderr)
        return 1
    return mover(move_argv(project, source, dest, handoff.this_host(), root))


def rebalance(
    machines: Sequence[Machine],
    collectors: Sequence[config.Collector],
    apply: bool,
    root: Path = REPO_ROOT,
    mover: Mover = inherit,
) -> int:
    """Say the move a rebalance would make, and make it with `apply`."""
    chosen, notes = plan(machines, collectors)
    for note in notes:
        print(f"fleet: {note}")
    if chosen is None:
        print("fleet: nothing to move")
        return 0
    print(
        f"fleet: {chosen.project} from {chosen.source} to {chosen.dest} "
        f"frees about {chosen.frees_gb:.1f} GB"
    )
    if not apply:
        print("fleet: nothing done; `--apply` makes this move")
        return 0
    return mover(move_argv(chosen.project, chosen.source, chosen.dest, handoff.this_host(), root))


def render(machines: Sequence[Machine], collectors: Sequence[config.Collector]) -> list[str]:
    """The table `fleet.py` prints, shortest machine first, the silent ones last."""
    lines = []
    for machine in sorted(machines, key=lambda m: (bool(m.error), m.available_gb)):
        if machine.error:
            lines.append(f"{machine.host:<20} unreachable: {machine.error}")
            continue
        runs = ", ".join(machine.runs) or "no collectors"
        flag = "  SHORT" if machine.available_gb < FLOOR_GB else ""
        lines.append(f"{machine.host:<20} {machine.available_gb:>5.1f} GB free  {runs}{flag}")
    return lines + [f"problem: {problem}" for problem in anomalies(machines, collectors)]


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    verbs = parser.add_subparsers(dest="verb")
    verbs.add_parser("probe", help="this machine as JSON")
    moving = verbs.add_parser("move", help="hand a collector to a machine, from any machine")
    moving.add_argument("project")
    moving.add_argument("--to", required=True)
    balancing = verbs.add_parser("rebalance", help="the move that relieves the shortest machine")
    balancing.add_argument("--apply", action="store_true", help="make the move")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.verb == "probe":
        print(json.dumps(probe()))
        return 0
    collectors, errors = config.declared(config.home(REPO_ROOT))
    for error in errors:
        print(f"fleet: {error}", file=sys.stderr)
    machines = survey()
    if args.verb == "move":
        return move(args.project, args.to, machines)
    if args.verb == "rebalance":
        return rebalance(machines, collectors, args.apply)
    print("\n".join(render(machines, collectors)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
