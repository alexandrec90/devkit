#!/usr/bin/env python3
"""Docker stack lifecycle and daemon maintenance, callable from any workspace.

Backs every "Docker: ..." task in `alex-projects.code-workspace`. They reach this
script through `devkit_project.py`, which sets cwd to the checkout picked in the task's
project prompt.

Two scopes live here, and the difference is worth keeping straight:

  - `up` / `down` are STACK-scoped -- one project's compose topology.
  - `restart-engine` / `fix` / `prune` / `stop-idle` are DAEMON-scoped -- one Docker
    Desktop per machine, so they are about the VM rather than about any repo.
    `stop-idle` walks every running compose stack at once, which is why it cannot be
    stack-scoped: the stack it stops is by definition not the one anyone is in.

Both are defined once at workspace level rather than copy-pasted into every repo's
.vscode/tasks.json. `up`/`down` arrived here last, from carameli's "Start: Full Stack"
and two different "Stop: Docker Stack" tasks that shared a label and did different
things -- which is the drift this arrangement exists to remove.

Delegation: a repo that ships its own, better-informed version of a mode wins, and
extra arguments are forwarded to it. Several modes need project knowledge no generic
fallback has -- carameli's `up` waits on healthchecks, ibkr_trader's `down` has to
name its `ibkr`/`app` profiles, and `prune` must know the compose file and named
volumes -- so those repos keep their scripts and this one finds and runs them rather
than duplicating the logic. The generic fallbacks below run only when the workspace
ships no such script: a scratch folder, a non-Docker repo, or a freshly generated
project whose plain `docker compose` stack the fallback handles correctly.

Delegate paths are `scripts/<name>.py` and nothing else. Every consuming project keeps
its scripts there, so a candidate list per mode is no longer needed -- see DELEGATES.

Usage:  python docker-maint.py {up|down|stop-idle|restart-engine|fix|prune}
        [--generic] [args...]
        (run with cwd set to the workspace folder; --generic skips delegation)

`prune --idle-only` is the unattended spelling: it does nothing while containers are
running, because the half that actually returns disk to Windows needs
`wsl --shutdown`. See `generic_prune`. `scripts/install-docker-prune.py` is what
schedules it, and it passes that flag -- the hand-registered task it replaced did not,
because nothing in this repo owned that task or checked what it ran.

`stop-idle` is the other unattended mode: it stops every running compose stack that
has opted in (`[docker] auto_stop = true` in the project's own `.devkit.toml`) and
shows no sign of being used. `scripts/install-docker-stop-idle.py` schedules it; see
`generic_stop_idle` for what "idle" means and why every ambiguity reads as "in use".

**This script writes no artifact of its own, deliberately**: most of its callers are
interactive, and a `logs/` file per click is noise. The scheduled caller is the
exception and gets one from the outside, by being wrapped in `log-wrap.py --always`.
Anything else that runs this unattended has to do the same -- under `pythonw.exe` these
`print`s go nowhere at all.

The daemon modes are Windows-only by nature -- they drive Docker Desktop and compact
the WSL2 VHDX. `up`/`down` are portable.

Never passes --volumes to any prune or any `down`: named volumes hold real dev
databases, and losing one costs a re-ingest measured in hours.
"""

import re
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

MODES = ("up", "down", "stop-idle", "restart-engine", "fix", "prune")

# Windows only. The scheduled prune reaches this script under `pythonw.exe`, which has no
# console, and Windows answers a console-less parent by giving each console child a brand
# new console **window** -- so an unflagged `docker` here is a window flashing on the
# desktop for every command the prune runs. The flagged child gets a window-less console
# instead, and passes it down to its own children.
#
# Applied to the interactive spawns too, where it costs nothing: the child still inherits
# this process's stdout and stderr handles, so a click still sees the output in its own
# terminal. Uniformity is the point -- `tests/test_scheduled_jobs.py` checks every spawn
# in a script the scheduler can reach, because one unflagged site restores the flicker.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Per-mode delegation targets, most-specific first. Relative to the workspace cwd.
DELEGATES = {
    "up": ("scripts/docker-up.py",),
    "down": ("scripts/docker-down.py",),
    "restart-engine": ("scripts/docker-restart-engine.py",),
    "fix": ("scripts/docker-fix.py",),
    # One path each, deliberately. `prune` used to carry a second candidate,
    # `.vscode/docker_prune.py`, for ibkr_trader specifically — a shared script naming
    # one repo's private layout. That project's scripts now live in `scripts/` like
    # everyone else's, so the special case is gone rather than merely unused.
    "prune": ("scripts/docker-prune.py",),
    # No delegate on purpose: `stop-idle` acts on every project's stack at once, so no
    # single repo is better informed about it than the generic pass.
    "stop-idle": (),
}

# Any of these in the cwd means there is a stack to act on. Checked before running a
# compose command so "this repo has no Docker tier" is a sentence rather than
# compose's own error about a file it cannot find.
COMPOSE_FILES = (
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
)

DOCKER_PROCESSES = [
    "Docker Desktop",
    "com.docker.backend",
    "com.docker.service",
    "com.docker.proxy",
]
DOCKER_DESKTOP_EXE = Path(r"C:\Program Files\Docker\Docker\Docker Desktop.exe")
POLL_TIMEOUT = 90
POLL_INTERVAL = 5


def banner(text: str) -> str:
    return f"\n{'=' * 60}\n  {text}\n{'=' * 60}\n"


def inherited_streams() -> dict:
    """The stream arguments that keep a window-less child's output where a reader is.

    `NO_WINDOW` gives the child a console of its own, and **a child that was not told
    otherwise writes to that console rather than to the handles it inherited**. For a
    spawn that captures nothing the flag alone is therefore not free: it empties
    `logs/scheduled-docker-prune.log` of everything docker said, leaving an artifact
    that reports an exit code and nothing to diagnose it with -- which is the failure
    that made this job's artifact mandatory in the first place. Naming the streams is
    what puts the output back.

    Both fallbacks mean the same thing -- there is no stream here to hand down, so
    inherit and let the child do what it would have done. `sys.stdout` is None under a
    bare `pythonw.exe`, and under pytest's capture it is an object with no real
    `fileno` (`io.UnsupportedOperation` is both a ValueError and an OSError).
    """
    streams = {}
    for key, stream in (("stdout", sys.stdout), ("stderr", sys.stderr)):
        try:
            stream.fileno()
        except (AttributeError, OSError, ValueError):
            continue
        streams[key] = stream
    return streams


def run(cmd, check: bool = False, timeout: int = 300) -> int:
    """Run `cmd`, streaming output. Returns the exit code (127 if not found)."""
    print(f"  $ {' '.join(str(c) for c in cmd)}")
    try:
        code = subprocess.run(
            cmd, timeout=timeout, creationflags=NO_WINDOW, **inherited_streams()
        ).returncode
    except FileNotFoundError:
        print(f"  [skip] {cmd[0]} not on PATH")
        return 127
    except subprocess.TimeoutExpired:
        print(f"  [timeout] after {timeout}s")
        return 124
    if code and check:
        print(f"  [warn] exit {code}")
    return code


def docker_info_ok(timeout: int = 15) -> bool:
    try:
        return (
            subprocess.run(
                ["docker", "info"],
                capture_output=True,
                timeout=timeout,
                creationflags=NO_WINDOW,
            ).returncode
            == 0
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def poll_engine(timeout: int = POLL_TIMEOUT) -> bool:
    """Block until `docker info` succeeds. Guards against reporting success on a
    wedged 'Starting the Docker Engine'."""
    print(f"  Waiting up to {timeout}s for the engine ...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if docker_info_ok():
            return True
        time.sleep(POLL_INTERVAL)
    return False


def stop_docker() -> None:
    for name in DOCKER_PROCESSES:
        run(["taskkill", "/F", "/IM", f"{name}.exe", "/T"], timeout=30)
    run(["wsl", "--shutdown"], timeout=60)


def start_docker() -> None:
    if not DOCKER_DESKTOP_EXE.is_file():
        print(f"  [skip] {DOCKER_DESKTOP_EXE} not found -- start Docker Desktop manually")
        return
    subprocess.Popen([str(DOCKER_DESKTOP_EXE)], creationflags=NO_WINDOW)


def find_delegate(mode: str) -> Path | None:
    for rel in DELEGATES[mode]:
        candidate = Path.cwd() / rel
        if candidate.is_file():
            return candidate
    return None


# --- generic fallbacks (used only when the workspace ships no script) ---------


def compose_file(root: Path | None = None) -> Path | None:
    """The stack definition in `root`, or None when the repo has no Docker tier."""
    base = root or Path.cwd()
    return next((base / name for name in COMPOSE_FILES if (base / name).is_file()), None)


def _no_stack_here() -> int:
    """Say so by name. Returns 2 -- a usage answer, not a failed operation.

    devkit and a `bare` preset genuinely have no stack, and the workspace task is a
    single picker over every checkout, so landing on one is an ordinary mistake rather
    than a broken setup. Exiting 0 would be the silent-skip this harness treats as its
    worst failure mode; letting compose report it prints a path-not-found error that
    reads like a misconfiguration.
    """
    print(
        f"  {Path.cwd().name} has no compose file ({', '.join(COMPOSE_FILES)}) — "
        f"it has no Docker stack to act on.",
        file=sys.stderr,
    )
    return 2


def generic_up(extra: list[str] | None = None) -> int:
    """`compose up -d`, plus whatever the task passed (the action supplies --build).

    No healthcheck polling: a project that wants to block until its stack is actually
    serving ships its own `scripts/docker-up.py` (carameli does, and it writes an
    artifact of the unhealthy services' logs). Detached-and-report is the honest
    generic behaviour -- pretending to verify readiness would be worse than not.
    """
    if not compose_file():
        return _no_stack_here()
    print(banner("Docker Compose Up (generic)"))
    return run(["docker", "compose", "up", "-d", *(extra or [])], timeout=900)


def generic_down(extra: list[str] | None = None) -> int:
    """`compose down` -- containers only.

    Named volumes and the data in them survive. `-v`/`--volumes` is never added here
    and must never be: this runs from a one-click task over a project picker, which is
    the last place a database should be destroyable by choosing the wrong entry.
    """
    if not compose_file():
        return _no_stack_here()
    print(banner("Docker Compose Down (generic)"))
    return run(["docker", "compose", "down", *(extra or [])], timeout=300)


def generic_restart_engine() -> int:
    print(banner("Docker Engine Restart (generic)"))
    stop_docker()
    start_docker()
    if poll_engine():
        print(banner("DOCKER ENGINE READY"))
        return 0
    print(banner("ENGINE STILL NOT RESPONDING"))
    print("  Try: check the Docker Desktop UI, rerun from an Admin terminal, or reboot.\n")
    return 1


def generic_fix() -> int:
    """More aggressive than restart: two stop/start rounds with a longer poll."""
    print(banner("Docker Desktop Fix (generic, aggressive)"))
    stop_docker()
    time.sleep(5)
    stop_docker()  # second pass catches processes respawned by the first
    start_docker()
    if poll_engine(timeout=POLL_TIMEOUT * 2):
        print(banner("DOCKER ENGINE READY"))
        return 0
    print(banner("DOCKER STILL WEDGED"))
    print("  Next: 'Troubleshoot -> Reset to factory defaults' in Docker Desktop, or reboot.\n")
    return 1


def running_containers() -> int:
    """How many containers are up; -1 when the engine cannot be asked.

    -1 rather than 0 for "cannot tell", so `--idle-only` fails toward *not* pruning.
    An unreadable engine is the one state where guessing zero would license the
    disruptive half against a machine that might be mid-run.
    """
    try:
        result = subprocess.run(
            ["docker", "ps", "-q"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return -1
    if result.returncode != 0:
        return -1
    return len([line for line in result.stdout.splitlines() if line.strip()])


def generic_prune(idle_only: bool = False) -> int:
    """Reclaim image/build-cache space, then hand the freed space back to Windows.

    No --volumes and no `docker volume prune`, ever: named volumes are where dev
    databases live. Compose is left alone here -- a workspace that needs its stack
    torn down and brought back should ship its own docker-prune.py (see DELEGATES).

    **The two halves have very different blast radii, and only the second reclaims
    anything Windows can see.** `docker system prune` frees space *inside* the VM; the
    VHDX is a dynamically-expanding file that does not shrink when its contents do, so
    a prune on its own returns exactly zero bytes to the host. `Optimize-VHD` is what
    returns them, and it needs exclusive access -- which means `wsl --shutdown`, which
    kills every running container and every other WSL distro with them.

    `idle_only` is that distinction made operable, and it exists for the scheduled
    caller. A prune that runs while twelve containers are up would stop them at 4am to
    reclaim disk, which is not a trade anything should make unattended. Interactive
    callers leave it off: a human choosing this from the task list has already decided.
    """
    if idle_only:
        running = running_containers()
        if running != 0:
            where = "the engine could not be asked" if running < 0 else f"{running} container(s) up"
            print(banner(f"SKIPPED -- {where}"))
            print("  Compacting needs `wsl --shutdown`, which would stop them.")
            print("  Run without --idle-only to prune and compact anyway.")
            return 0
    print(banner("Docker Prune + Compact VHDX (generic)"))
    if not docker_info_ok():
        print("  Docker is not responding; starting it first.")
        start_docker()
        if not poll_engine():
            print(banner("ENGINE UNAVAILABLE -- nothing pruned"))
            return 1

    run(["docker", "system", "prune", "-af"], timeout=600)
    run(["docker", "builder", "prune", "-af"], timeout=600)

    print("\n  Stopping Docker for exclusive VHDX access ...")
    stop_docker()

    vhdx = Path.home() / "AppData/Local/Docker/wsl/disk/docker_data.vhdx"
    if not vhdx.is_file():  # older layouts kept it under wsl/data
        vhdx = Path.home() / "AppData/Local/Docker/wsl/data/ext4.vhdx"
    if vhdx.is_file():
        print(f"  Compacting {vhdx}")
        code = run(
            ["powershell", "-NoProfile", "-Command", f"Optimize-VHD -Path '{vhdx}' -Mode Full"],
            timeout=900,
        )
        if code:
            print("  [warn] Optimize-VHD failed -- it needs an ELEVATED shell and Hyper-V tools.")
            print("         The prune above still freed space inside the VM.")
    else:
        print("  [skip] No Docker WSL VHDX found at the expected paths.")

    start_docker()
    if poll_engine():
        print(banner("PRUNE COMPLETE -- ENGINE READY"))
        return 0
    print(banner("PRUNE DONE, BUT ENGINE DID NOT COME BACK"))
    return 1


# --- stop-idle: the unattended stack half -------------------------------------

# A stack an agent brought up minutes ago has no connections during the edit half of
# its loop; the window says "recent enough that something probably still wants it"
# without having to watch anything.
GRACE_HOURS = 2.0

# One row per container: id, compose project, that project's source directory, and the
# published ports -- everything the verdict needs, from one `docker ps`.
PS_FORMAT = (
    '{{.ID}}\t{{.Label "com.docker.compose.project"}}\t'
    '{{.Label "com.docker.compose.project.working_dir"}}\t{{.Ports}}'
)


def _capture(cmd: list[str], timeout: int = 60) -> str | None:
    """stdout of `cmd`, or None when it cannot be asked -- absent, failed, timed out.

    None rather than "" for the same reason `running_containers` returns -1: every
    caller here treats "cannot tell" as "leave the stack alone", and an empty answer
    would read as "nothing running / no connections", which licenses the stop.
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def published_ports(ports_field: str) -> set[int]:
    """Host ports out of a `docker ps` Ports column ('127.0.0.1:5433->5432/tcp, ...')."""
    return {int(port) for port in re.findall(r":(\d+)->", ports_field)}


def compose_stacks(ps_output: str) -> dict[str, dict]:
    """Group `docker ps` rows (PS_FORMAT) by compose project. Pure.

    A container with no compose project label -- an ad-hoc `docker run`, an MCP
    server -- is not a stack and is left alone.
    """
    stacks: dict[str, dict] = {}
    for line in ps_output.splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        cid, project, workdir, ports = (part.strip() for part in parts)
        if not cid or not project:
            continue
        stack = stacks.setdefault(project, {"ids": [], "workdir": workdir, "ports": set()})
        stack["ids"].append(cid)
        stack["ports"] |= published_ports(ports)
    return stacks


def established_ports(netstat_output: str) -> set[int]:
    """Both ends of every ESTABLISHED row.

    The stack's published port and its client's ephemeral port land on opposite sides
    depending on which row of a loopback pair netstat prints, so only the union is a
    reliable "someone is connected to something".
    """
    ports: set[int] = set()
    for line in netstat_output.splitlines():
        tokens = line.split()
        if len(tokens) < 4 or not tokens[0].upper().startswith("TCP"):
            continue
        if "ESTABLISHED" not in (token.upper() for token in tokens):
            continue
        for addr in tokens[1:3]:
            _host, sep, port = addr.rpartition(":")
            if sep and port.isdigit():
                ports.add(int(port))
    return ports


def auto_stop_enabled(workdir: str) -> bool:
    """True only when the project's own `.devkit.toml` says `[docker] auto_stop = true`.

    Opt-in, so a collector-style stack -- one doing scheduled work with no client
    connected, which no connection check can tell apart from an idle one -- is safe by
    *default* rather than by being remembered. Every failure (no label, no file, no
    `tomllib`, a parse error) reads as "not opted in".
    """
    if not workdir:
        return False
    try:
        import tomllib  # stdlib 3.11+; guarded the way harness_config guards it
    except ModuleNotFoundError:
        return False
    try:
        with (Path(workdir) / ".devkit.toml").open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError):  # TOMLDecodeError is a ValueError
        return False
    docker = data.get("docker")
    return isinstance(docker, dict) and docker.get("auto_stop") is True


def youngest_start(inspect_output: str) -> datetime | None:
    """The newest `State.StartedAt` in an inspect listing; None when none parse.

    Docker prints nanoseconds and `fromisoformat` takes at most six digits, so the
    fraction is trimmed rather than parsed.
    """
    newest = None
    for line in inspect_output.splitlines():
        text = re.sub(r"\.(\d{1,6})\d*", r".\1", line.strip()).replace("Z", "+00:00")
        try:
            started = datetime.fromisoformat(text)
        except ValueError:
            continue
        if newest is None or started > newest:
            newest = started
    return newest


def keep_reason(stack: dict, busy_ports: set[int] | None, now: datetime) -> str | None:
    """Why this stack is left alone, or None when it is safe to stop.

    Ordered cheapest-first, and every ambiguity keeps the stack up: an unreadable
    netstat or an unparseable start time is exactly the state in which this could
    stop a database out from under someone.
    """
    if not auto_stop_enabled(stack["workdir"]):
        return "not opted in (`[docker] auto_stop = true` in its .devkit.toml)"
    if busy_ports is None:
        return "netstat could not be read, so idleness cannot be verified"
    used = sorted(stack["ports"] & busy_ports)
    if used:
        return f"established connection(s) on port(s) {used}"
    listing = _capture(
        ["docker", "inspect", "--format", "{{.State.StartedAt}}", *stack["ids"]], timeout=30
    )
    newest = youngest_start(listing) if listing is not None else None
    if newest is None:
        return "start time unreadable, so the grace window cannot be checked"
    age_hours = (now - newest).total_seconds() / 3600
    if age_hours < GRACE_HOURS:
        return f"started {age_hours:.1f}h ago (grace window {GRACE_HOURS:g}h)"
    return None


def generic_stop_idle() -> int:
    """Stop the compose stacks that opted in and show no sign of being used.

    The counterpart to `restart: unless-stopped`, which resurrects on every boot
    whatever was running at shutdown -- so a stack someone brought up once runs around
    the clock whether or not anyone touches that project again. This pass turns "left
    up" back into "up on demand": `docker stop`, never `down`, so containers and named
    volumes survive, the restart (`docker-maint.py up`, or the stop hook's
    `*_STOP_TESTS_AUTOSTART` tier bringing up just db+redis) costs seconds, and a
    manual stop is exactly the state `unless-stopped` respects across reboots.

    Exit 0 covers "nothing eligible" -- most runs, and the correct outcome; only a
    `docker stop` that actually failed reports 1.
    """
    print(banner("Docker Stop Idle Stacks (generic)"))
    listing = _capture(["docker", "ps", "--format", PS_FORMAT], timeout=30)
    if listing is None:
        print("  [skip] the engine could not be asked; nothing to stop.")
        return 0
    stacks = compose_stacks(listing)
    if not stacks:
        print("  No compose stacks are running.")
        return 0
    netstat = _capture(["netstat", "-n"], timeout=60)
    busy_ports = established_ports(netstat) if netstat is not None else None
    now = datetime.now(timezone.utc)
    failures = 0
    for project, stack in sorted(stacks.items()):
        reason = keep_reason(stack, busy_ports, now)
        if reason:
            print(f"  [keep] {project}: {reason}")
            continue
        print(f"  [stop] {project}: opted in, no connections, past the grace window")
        if run(["docker", "stop", *stack["ids"]], timeout=300):
            failures += 1
    return 1 if failures else 0


# The stack modes take the forwarded arguments; the daemon ones are parameterless by
# nature (there is one Docker Desktop and nothing to aim it at), so they are adapted to
# the same signature rather than dispatched differently. Annotated because a dict of
# mixed function shapes infers as `object`, and `GENERIC[mode](forwarded)` then fails
# mypy with "Cannot call function of unknown type".
GENERIC: dict[str, Callable[[list[str]], int]] = {
    "up": generic_up,
    "down": generic_down,
    "stop-idle": lambda extra: generic_stop_idle(),
    "restart-engine": lambda extra: generic_restart_engine(),
    "fix": lambda extra: generic_fix(),
    "prune": lambda extra: generic_prune(idle_only="--idle-only" in extra),
}


def split_args(args: list[str]) -> tuple[str | None, list[str], bool]:
    """`(mode, forwarded, generic_only)` — pure, so the parsing is unit-testable.

    `--generic` is consumed here; everything else is forwarded to the delegate or the
    fallback. Forwarding is what lets one action carry `--build`: before this, the
    delegate was spawned with no arguments at all, so a hoisted "Docker: Start Stack"
    would have silently started a stale image in every project that ships its own
    `docker-up.py` — the exact class of failure that looks like success.
    """
    generic_only = "--generic" in args
    rest = [a for a in args if a != "--generic"]
    mode = rest[0] if rest and rest[0] in MODES else None
    return mode, rest[1:], generic_only


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    mode, forwarded, generic_only = split_args(args)

    if mode is None:
        print(
            f"usage: docker-maint.py {{{'|'.join(MODES)}}} [--generic] [args...]",
            file=sys.stderr,
        )
        return 2

    if not generic_only:
        delegate = find_delegate(mode)
        if delegate:
            print(f"Delegating to this workspace's own script: {delegate}\n")
            return subprocess.run(
                [sys.executable, str(delegate), *forwarded],
                creationflags=NO_WINDOW,
                **inherited_streams(),
            ).returncode

    return GENERIC[mode](forwarded)


if __name__ == "__main__":
    sys.exit(main())
