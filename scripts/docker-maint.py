#!/usr/bin/env python3
"""Docker stack lifecycle and daemon maintenance, callable from any workspace.

Backs every "Docker: ..." task in `alex-projects.code-workspace`. They reach this
script through `devkit_project.py`, which sets cwd to the checkout picked in the task's
project prompt.

Two scopes live here, and the difference is worth keeping straight:

  - `up` / `down` are STACK-scoped -- one project's compose topology.
  - `restart-engine` / `fix` / `prune` are DAEMON-scoped -- one Docker Desktop per
    machine, so they are about the VM rather than about any repo.

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

Usage:  python docker-maint.py {up|down|restart-engine|fix|prune} [--generic] [args...]
        (run with cwd set to the workspace folder; --generic skips delegation)

`prune --idle-only` is the unattended spelling: it does nothing while containers are
running, because the half that actually returns disk to Windows needs
`wsl --shutdown`. See `generic_prune`. That is what the scheduled task passes.

The daemon modes are Windows-only by nature -- they drive Docker Desktop and compact
the WSL2 VHDX. `up`/`down` are portable.

Never passes --volumes to any prune or any `down`: named volumes hold real dev
databases, and losing one costs a re-ingest measured in hours.
"""

import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

MODES = ("up", "down", "restart-engine", "fix", "prune")

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


def run(cmd, check: bool = False, timeout: int = 300) -> int:
    """Run `cmd`, streaming output. Returns the exit code (127 if not found)."""
    print(f"  $ {' '.join(str(c) for c in cmd)}")
    try:
        code = subprocess.run(cmd, timeout=timeout).returncode
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
            subprocess.run(["docker", "info"], capture_output=True, timeout=timeout).returncode == 0
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
    subprocess.Popen([str(DOCKER_DESKTOP_EXE)])


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
            ["docker", "ps", "-q"], capture_output=True, text=True, timeout=30, check=False
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


# The stack modes take the forwarded arguments; the daemon ones are parameterless by
# nature (there is one Docker Desktop and nothing to aim it at), so they are adapted to
# the same signature rather than dispatched differently. Annotated because a dict of
# mixed function shapes infers as `object`, and `GENERIC[mode](forwarded)` then fails
# mypy with "Cannot call function of unknown type".
GENERIC: dict[str, Callable[[list[str]], int]] = {
    "up": generic_up,
    "down": generic_down,
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
            return subprocess.run([sys.executable, str(delegate), *forwarded]).returncode

    return GENERIC[mode](forwarded)


if __name__ == "__main__":
    sys.exit(main())
