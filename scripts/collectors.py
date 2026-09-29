#!/usr/bin/env python3
"""Keep the workspace's ingestion collectors running on the one machine meant to run them.

A collector is a compose service that does scheduled work with nobody connected to it --
ibkr_trader's `serve`, sports_betting's `collector`. Each keeps its own clock inside
the container (APScheduler), so what needs an owner is not the schedule but the
*container*: that it is up where it should be, down where it should not, and visibly
healthy or not. `restart: unless-stopped` covers none of that. It resurrects a container
on whatever machine last started one, and on a machine where nobody ever did it does
nothing at all.

**Which machine is not a property of the workspace**, which is shared: see
`collectors_config`. Every workstation registers this job; it acts only on the
collectors this machine was assigned, and a machine assigned none spawns nothing.

    collectors.py                       # status: what is declared, assigned and running
    collectors.py run-here [PROJECT..]  # this machine runs them; starts them now
    collectors.py stop-here [PROJECT..] # this machine must not; stops them now
    collectors.py release [PROJECT..]   # forget; neither started nor stopped from now on
    collectors.py maintain              # what `devkit-collectors` runs every 15 minutes

No names means every collector `devkit.collectors` declares.

**Started, never rebuilt.** A pass that finds the container down runs `docker compose up
-d <service>`, which builds an image only when there is none. Rebuilding for new code is
a deploy, and a deploy nobody asked for -- mid-ingest, from whatever branch the checkout
is on -- is not something a 15-minute timer should do.

`tray_rows` is the tray's half: one live `docker ps` per poll, plus the last verdict of
each project's own `health` command, which the pass records because running it takes a
process inside the container and the tray polls every two minutes.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collectors_config as config
import sweep

REPO_ROOT = Path(__file__).resolve().parents[1]

# This job's account of itself, written on every exit path -- see
# `tests/test_scheduled_jobs.py` for why a window-less job owes one.
ARTIFACT = Path("logs/collectors.log")

# Each project's last `health` verdict, keyed by project, with the container it was
# taken from so a verdict about a container since replaced is not reported against it.
HEALTH = Path("logs/collectors.health.json")

# Every spawn here is reachable from a scheduled task under `pythonw.exe`; see
# `tests/test_scheduled_jobs.py`. Zero off Windows, where the flag does not exist.
NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)

PS_FORMAT = (
    '{{.ID}}\t{{.Label "com.docker.compose.project.working_dir"}}\t'
    '{{.Label "com.docker.compose.service"}}\t{{.State}}\t{{.Status}}'
)

# Seconds. `up` is long because the first one on a machine builds the image.
PS_TIMEOUT = 30
TRAY_PS_TIMEOUT = 15
UP_TIMEOUT = 1800
STOP_TIMEOUT = 120
HEALTH_TIMEOUT = 120

# The tray's three levels, spelled as `tray_state` spells them. Not imported from there:
# `tray_state` imports this module, and `test_collectors.py` pins the two to each other.
OK, WARN, FAIL = "ok", "warn", "fail"
ROW_PREFIX = "collector: "


@dataclass(frozen=True)
class Container:
    id: str
    workdir: str
    service: str
    state: str
    status: str

    @property
    def running(self) -> bool:
        return self.state == "running"


def parse_ps(text: str) -> list[Container]:
    """`docker ps -a --format PS_FORMAT` rows; a container compose did not make is skipped."""
    found = []
    for line in text.splitlines():
        parts = [part.strip() for part in line.split("\t")]
        if len(parts) == 5 and parts[0] and parts[1] and parts[2]:
            found.append(Container(*parts))
    return found


def same_dir(left: str | Path, right: str | Path) -> bool:
    """Compose records the directory the way the CLI saw it; Windows compares caselessly."""

    def key(path: str | Path) -> str:
        return str(path).replace("\\", "/").rstrip("/").lower()

    return key(left) == key(right)


def find(containers: Sequence[Container], checkout: Path, service: str) -> Container | None:
    """The service's container for this checkout -- a running one over a stopped one.

    Matched on the working directory rather than the compose project name: a box of the
    same project runs its own stack under its own name, and it is not the collector.
    """
    mine = [c for c in containers if c.service == service and same_dir(c.workdir, checkout)]
    return next((c for c in mine if c.running), mine[0] if mine else None)


def first_line(text: str, limit: int = 100) -> str:
    line = next((raw.strip() for raw in text.splitlines() if raw.strip()), "")
    return line if len(line) <= limit else line[: limit - 1] + "…"


class Docker:
    """Everything that touches the engine: one captured, window-less spawn per call."""

    def __init__(self, ps_timeout: int = PS_TIMEOUT) -> None:
        self.ps_timeout = ps_timeout

    def run(self, argv: Sequence[str], timeout: int, cwd: Path | None = None) -> tuple[int, str]:
        """`(exit code, stdout+stderr)`. A spawn that could not happen is a code, not a raise."""
        try:
            done = subprocess.run(
                list(argv),
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
                creationflags=NO_WINDOW,
            )
        except FileNotFoundError:
            return 127, "docker is not on PATH"
        except subprocess.TimeoutExpired:
            return 124, f"timed out after {timeout}s"
        except OSError as exc:
            return 126, str(exc)
        return done.returncode, "\n".join(p for p in (done.stdout, done.stderr) if p).strip()

    def ps(self) -> list[Container] | None:
        """Every compose container, or None when the engine cannot be asked."""
        code, out = self.run(["docker", "ps", "-a", "--format", PS_FORMAT], self.ps_timeout)
        return parse_ps(out) if code == 0 else None

    def up(self, checkout: Path, service: str) -> tuple[bool, str]:
        # Naming the service activates any compose profile it sits behind, which is how
        # ibkr_trader's `app` (profile `app`) starts without this knowing the profile.
        code, out = self.run(["docker", "compose", "up", "-d", service], UP_TIMEOUT, checkout)
        return code == 0, out

    def stop(self, container: Container) -> tuple[bool, str]:
        # `stop`, never `down`: the container and its volumes survive, and a stopped
        # container is exactly what `unless-stopped` leaves alone across a reboot.
        code, out = self.run(["docker", "stop", container.id], STOP_TIMEOUT)
        return code == 0, out

    def health(self, container: Container, argv: Sequence[str]) -> tuple[int, str]:
        return self.run(["docker", "exec", container.id, *argv], HEALTH_TIMEOUT)


@dataclass
class Report:
    lines: list[str] = field(default_factory=list)
    failures: int = 0

    def say(self, line: str) -> None:
        self.lines.append(line)

    def fail(self, line: str) -> None:
        self.lines.append(line)
        self.failures += 1


@dataclass(frozen=True)
class Target:
    collector: config.Collector
    mode: str
    checkout: Path


def targets(
    collectors: Sequence[config.Collector], assignment: dict[str, str], workspace_dir: Path
) -> list[Target]:
    """The declared collectors this machine was assigned, in declaration order."""
    return [
        Target(c, assignment[c.project], workspace_dir / c.project)
        for c in collectors
        if c.project in assignment
    ]


def load_health(path: Path) -> dict[str, dict]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return (
        {str(k): v for k, v in raw.items() if isinstance(v, dict)} if isinstance(raw, dict) else {}
    )


def check_health(target: Target, box: Container, docker: Docker, report: Report) -> dict:
    """Run the project's own health command; the verdict is theirs, so it is not a failure
    of this job -- the tray shows it amber rather than turning the job red."""
    code, out = docker.health(box, target.collector.health)
    name = target.collector.project
    if code == 0:
        report.say(f"{name}: healthy")
        return {"ok": True, "summary": "healthy", "container": box.id}
    summary = f"exit {code}: {first_line(out)}"
    report.say(f"{name}: health check failing ({summary})")
    report.lines.extend(f"    {line}" for line in out.splitlines()[:40])
    return {"ok": False, "summary": summary, "container": box.id}


def keep_running(
    target: Target, containers: Sequence[Container], docker: Docker, report: Report
) -> dict | None:
    """Start the collector if it is down. Returns a health record, or None when there is
    nothing to record this pass."""
    name, service = target.collector.project, target.collector.service
    if not (target.checkout / ".git").exists():
        report.fail(f"{name}: no checkout at {target.checkout} -- nothing to start")
        return None
    box = find(containers, target.checkout, service)
    if box is None or not box.running:
        ok, out = docker.up(target.checkout, service)
        if not ok:
            report.fail(f"{name}: could not start `{service}` -- {first_line(out) or 'no output'}")
            report.lines.extend(f"    {line}" for line in out.splitlines()[-20:])
            return None
        # No health check on the pass that started it: every job in it has yet to run,
        # and a verdict of "never ran" about a container seconds old is noise.
        report.say(f"{name}: started `{service}`")
        return None
    report.say(f"{name}: `{service}` up ({box.status})")
    return check_health(target, box, docker, report) if target.collector.health else None


def keep_stopped(
    target: Target, containers: Sequence[Container], docker: Docker, report: Report
) -> None:
    name, service = target.collector.project, target.collector.service
    box = find(containers, target.checkout, service)
    if box is None or not box.running:
        report.say(f"{name}: `{service}` off on this machine")
        return
    ok, out = docker.stop(box)
    if ok:
        report.say(f"{name}: stopped `{service}` -- this machine is set to stop it")
    else:
        report.fail(f"{name}: could not stop `{service}` -- {first_line(out) or 'no output'}")


def maintain(chosen: Sequence[Target], docker: Docker, report: Report, health: dict) -> None:
    """One pass over the assigned collectors, recording health verdicts into `health`."""
    if not chosen:
        report.say("no collector is assigned to this machine -- nothing to do")
        return
    containers = docker.ps()
    if containers is None:
        if any(t.mode == config.RUN for t in chosen):
            report.fail("docker is not answering -- is Docker Desktop running?")
        else:
            report.say("docker is not answering, so nothing here can be running")
        return
    for target in chosen:
        if target.mode == config.RUN:
            record = keep_running(target, containers, docker, report)
            if record is not None:
                health[target.collector.project] = record
        else:
            keep_stopped(target, containers, docker, report)


def status(
    collectors: Sequence[config.Collector],
    assignment: dict[str, str],
    chosen: Sequence[Target],
    docker: Docker,
    report: Report,
) -> None:
    """Read-only: what is declared, what this machine was told, and what is running."""
    if not collectors:
        report.say(f"no collectors declared -- add `{config.SETTING}` to the workspace file")
    containers: list[Container] | None = docker.ps() if chosen else []
    for collector in collectors:
        mode = assignment.get(collector.project, "")
        if not mode:
            report.say(f"{collector.project}: not assigned on this machine (hands off)")
            continue
        target = next(t for t in chosen if t.collector.project == collector.project)
        box = find(containers or [], target.checkout, collector.service)
        state = (
            "docker not answering"
            if containers is None
            else (box.status if box else "no container")
        )
        report.say(f"{collector.project}: assigned `{mode}` -- `{collector.service}` {state}")


def render(lines: Sequence[str], failures: int, when: _dt.datetime) -> str:
    head = f"# collectors {when.isoformat(timespec='seconds')} -- {failures} failure(s)"
    return "\n".join([head, *lines, ""])


def write_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# --- the tray's half --------------------------------------------------------------


def row(target: Target, containers: Sequence[Container] | None, health: dict) -> tuple[str, str]:
    """`(level, detail)` for one assigned collector."""
    if target.mode == config.STOP:
        box = find(containers or [], target.checkout, target.collector.service)
        if box is not None and box.running:
            return WARN, "running here, but this machine is set to stop it"
        return OK, "off on this machine (by choice)"
    if containers is None:
        return FAIL, "docker is not answering"
    box = find(containers, target.checkout, target.collector.service)
    if box is None:
        return FAIL, "no container -- see logs/collectors.log"
    if not box.running:
        return FAIL, f"not running ({box.status})"
    verdict = health.get(target.collector.project, {})
    if verdict.get("container") == box.id and verdict.get("ok") is False:
        return WARN, f"health check failing -- {verdict.get('summary', '')}"
    return OK, f"running ({box.status})"


def tray_rows(root: Path = REPO_ROOT, docker: Docker | None = None) -> list[tuple[str, str, str]]:
    """`(row name, level, detail)` per collector assigned to this machine. Never raises.

    Empty on a machine assigned nothing, **without asking docker**: a laptop sharing the
    workspace sees no rows and pays no spawn.
    """
    base = config.home(root)
    assignment = config.load_assignment(base / config.ASSIGNMENT)
    if not assignment:
        return []
    collectors, _notes = config.declared(base)
    chosen = targets(collectors, assignment, sweep.default_workspace(base).parent)
    rows = [
        (
            ROW_PREFIX + name,
            WARN,
            f"assigned `{mode}` here, but `{config.SETTING}` no longer declares it",
        )
        for name, mode in sorted(assignment.items())
        if name not in {c.project for c in collectors}
    ]
    if chosen:
        containers = (docker or Docker(TRAY_PS_TIMEOUT)).ps()
        health = load_health(base / HEALTH)
        rows += [(ROW_PREFIX + t.collector.project, *row(t, containers, health)) for t in chosen]
    return rows


# --- the command line --------------------------------------------------------------

VERBS = {"run-here": config.RUN, "stop-here": config.STOP, "release": None}


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "mode",
        nargs="?",
        default="status",
        choices=("status", "maintain", *VERBS),
        help=(
            "status: report only (default). maintain: what the scheduler runs. run-here / "
            "stop-here: assign this machine, then act on it now. release: forget."
        ),
    )
    parser.add_argument("projects", nargs="*", help="collectors to assign (default: all)")
    parser.add_argument("--devkit", type=Path, default=REPO_ROOT, help=argparse.SUPPRESS)
    return parser.parse_args(sys.argv[1:] if argv is None else argv)


def reassign(args: argparse.Namespace, base: Path, collectors, report: Report) -> dict | None:
    """Apply a `run-here`/`stop-here`/`release`; the new assignment, or None on a typo."""
    picked, unknown = config.pick(collectors, args.projects)
    if unknown:
        report.fail(f"not declared in `{config.SETTING}`: {', '.join(unknown)}")
        return None
    path = base / config.ASSIGNMENT
    mode = VERBS[args.mode]
    updated = config.assign(config.load_assignment(path), [c.project for c in picked], mode)
    config.save_assignment(path, updated)
    for collector in picked:
        report.say(
            f"{collector.project}: {'released' if mode is None else mode + ' on this machine'}"
        )
    return updated


def main(argv: Sequence[str] | None = None, docker: Docker | None = None) -> int:
    args = parse_args(argv)
    base = config.home(args.devkit.expanduser().resolve())
    report = Report()
    now = _dt.datetime.now()

    def finish(code: int) -> int:
        text = render(report.lines, report.failures, now)
        write_file(base / ARTIFACT, text)
        print(text, end="")
        return code

    collectors, notes = config.declared(base)
    for note in notes:
        report.fail(note)
    assignment = config.load_assignment(base / config.ASSIGNMENT)
    if args.mode in VERBS:
        updated = reassign(args, base, collectors, report)
        if updated is None or VERBS[args.mode] is None:
            return finish(2 if report.failures else 0)
        assignment = {
            c.project: updated[c.project] for c in config.pick(collectors, args.projects)[0]
        }
    chosen = targets(collectors, assignment, sweep.default_workspace(base).parent)
    engine = docker or Docker()
    if args.mode == "status":
        status(collectors, assignment, chosen, engine, report)
        return finish(2 if report.failures else 0)
    health = load_health(base / HEALTH)
    maintain(chosen, engine, report, health)
    write_file(base / HEALTH, json.dumps(health, indent=2, sort_keys=True) + "\n")
    return finish(2 if report.failures else 0)


if __name__ == "__main__":
    sys.exit(main())
