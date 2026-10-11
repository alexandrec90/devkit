#!/usr/bin/env python3
"""Keep the workspace's ingestion collectors running on the one machine meant to run them.

A collector is ingestion that does scheduled work with nobody connected to it. Most are
a compose service -- ibkr_trader's `serve`, sports_betting's `collector` -- which keeps
its own clock inside the container (APScheduler), so what needs an owner is not the
schedule but the *container*: that it is up where it should be, down where it should
not, and visibly healthy or not. `restart: unless-stopped` covers none of that. It
resurrects a container on whatever machine last started one, and on a machine where
nobody ever did it does nothing at all.

The rest are a host command on an interval -- social-scraper drives the host's Chrome --
and there the clock *is* what needs an owner: a Scheduled Task per collector, which this
job registers where it runs and removes where it does not. `collector_tasks` holds that
half.

**Which machine is not a property of the workspace**, which is shared: see
`collectors_config`. Every workstation registers this job; it acts only on the
collectors this machine was assigned, and a machine assigned none spawns nothing.

    collectors.py                       # status: what is declared, assigned and running
    collectors.py run-here [PROJECT..]  # this machine runs them; starts them now
    collectors.py stop-here [PROJECT..] # this machine must not; stops them now
    collectors.py release [PROJECT..]   # forget; neither started nor stopped from now on
    collectors.py maintain              # what `devkit-collectors` runs every 15 minutes
    collectors.py fire NAME             # one scheduled collector, once: what its task runs
    collectors.py run-once NAME         # the same by hand, output in this terminal

No names means every collector `devkit.collectors` declares.

**Started, and redeployed onto merged code -- only merged code.** A pass that finds the
container down runs `docker compose up -d <service>`, which builds an image only when
there is none. A container whose code is older than the checkout's HEAD is rebuilt with
`up -d --build` (`redeploy`), but only from a checkout whose HEAD is on origin's default
branch and whose tracked files are clean: a deploy from whatever branch or half-edit the
checkout holds is not something a 15-minute timer should do. Without it a fix merged in
the project never reached the container its health check runs in, so ibkr_trader's
collector finding came back after every merged fix (51cca249) -- the image was a day old.

`tray_rows` is the tray's half: one live `docker ps` per poll, plus the last verdict of
each project's own `health` command, which the pass records because running it takes a
process inside the container and the tray polls every two minutes.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import json
import os
import re
import subprocess
import sys
import time as _time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collector_tasks
import collectors_config as config
import docker_memory
import machine_clock
import sweep

REPO_ROOT = Path(__file__).resolve().parents[1]

# This job's account of itself, written on every exit path -- see
# `tests/test_scheduled_jobs.py` for why a window-less job owes one.
ARTIFACT = Path("logs/collectors.log")

# Each project's last `health` verdict, keyed by project, with the container it was
# taken from so a verdict about a container since replaced is not reported against it.
# `code_at` is the newest the code in that container can be (`code_at`), which
# `fix_loop.collector_findings` weighs against the fix of the group a verdict would
# reopen; `held` says why a container behind its checkout was not redeployed;
# `started_at` is when this job last started or redeployed it (`settling`).
HEALTH = Path("logs/collectors.health.json")
CODE_AT, HELD, STARTED_AT = "code_at", "held", "started_at"

# Present while a `maintain` pass is acting on containers, `{started_at, until}`, and
# removed when it ends. A start or redeploy builds for minutes and then leaves the
# container `Created` -- or the one it replaces `Exited` -- for the seconds before compose
# starts it, so a row read then is a pass at work, not a collector down. 3cc7de43: the fix
# pass read ibkr_trader's row seven seconds into a redeploy's start and filed "not running
# (Created)" against a container that was up when the finding landed. `until` bounds a
# marker a killed pass left behind (`TARGET_BOUND`).
IN_PASS = Path("logs/collectors.pass.json")
UNTIL = "until"
# A row's state while that pass is at it.
STARTING = "being started by the collectors pass"

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
INSPECT_TIMEOUT = 30
GIT_TIMEOUT = 30
# The longest one container target can hold a pass: every spawn `keep_running` makes, at
# its timeout -- three inspects, three git calls, the build and the health check.
TARGET_BOUND = 3 * INSPECT_TIMEOUT + 3 * GIT_TIMEOUT + UP_TIMEOUT + HEALTH_TIMEOUT
# How long after the machine boots or wakes a silent engine is Docker Desktop still
# starting rather than down (`wait_for_engine`), and how often it is asked meanwhile.
# Measured start on this workstation: about three minutes from boot to running containers.
ENGINE_STARTUP = 600
ENGINE_POLL = 15
# An engine still silent past that window behind a Docker Desktop that says `running` is
# wedged, and nothing else on the machine brings it back (`revive_engine`). 2026-10-08:
# the engine answered every request with a 500 for six hours while Desktop reported
# itself running, and four ledger groups filed against it (4436a22a x14) before
# `docker desktop restart` had it answering in two and a half minutes. One restart per
# `RESTART_EVERY`, recorded in `RESTARTED`, so a restart that does not help is not
# repeated every pass.
DESKTOP_TIMEOUT = 30
DESKTOP_IMAGE = "Docker Desktop.exe"
RESTART_TIMEOUT = 600
RESTART_EVERY = 3600
RESTARTED = Path("logs/collectors.engine-restart.json")
# What a restart that left the engine silent goes on to do (`reset_vm`): stop Docker
# Desktop, stop the WSL VM its engine runs in, start Desktop again. Docker's restart asks
# the VM to shut itself down, and a VM wedged hard enough cannot: 2026-10-09 21:45 UTC the
# restart logged "init failed to shutdown the VM: ... context deadline exceeded", started
# the engine on the same frozen VM, and `wsl -d docker-desktop` still did not answer half
# an hour later (d0651b43, 4034a9e2). `wsl --shutdown` stops every distro on the machine,
# so it is used only when every running one is Docker's own (`DOCKER_DISTRO` and its
# `-data` sibling); otherwise only Docker's is terminated.
DOCKER_DISTRO = "docker-desktop"
WSL_TIMEOUT = 120
DESKTOP_STOP_TIMEOUT = 120
# Recorded in `RESTARTED` as `VM_RESET_AT`, so a VM is reset once per restart: a pass
# inside a restart's `RESTART_EVERY` that finds the engine silent still resets the VM
# when nothing has since that restart. 2026-10-09 22:45 UTC a pass from a checkout that
# predated `reset_vm` restarted the wedged engine; the 23:15 pass ran the new code, but
# the hour hold failed it without the reset (a70fa348), and so would the 23:45 pass.
VM_RESET_AT = "vm_reset_at"
# How long an engine that did not answer the pass's first question is asked again before
# it is called wedged. 2026-10-08 22:15 UTC: one `docker ps` that did not answer on a
# loaded machine -- the engine served the scrape's compose call before it and its own
# event streams after -- restarted Docker Desktop, and the restart shut down
# social-scraper's db under a scrape mid-run (6c504c34).
WEDGE_CONFIRM = 180
# How long a restart may wait for Docker Desktop's own update (`Docker.updating`) before
# the pass fails on it: one run's `FIRE_TIMEOUT`. 2026-10-09 19:15 UTC: Docker's VM froze
# for 27 minutes under a scrape, the pass held the restart and failed, and the engine was
# back by the next pass on its own (84ad65d7). A wait the next pass resolves is not a
# failure of this one.
HOLD_LIMIT = collector_tasks.FIRE_TIMEOUT
HOLD_SINCE = "hold_since"
# How long a wedged engine is left alone for scheduled collectors' runs
# (`revive_engine`'s `busy`) before it is restarted under them anyway: less than one
# pass's interval, so the second pass to find it wedged restarts it. A run's services
# live in the engine's VM, and one silent for a whole pass is not serving them either.
# 2026-10-09 20:45 UTC: the engine wedged under the 20:30 scrape, the pass held the
# restart for it, the scrape blocked on its db until its `FIRE_TIMEOUT`, and the next
# fire was already running when the hold could have ended -- fires every 30 minutes and
# passes every 15 left no pass that found nothing in flight (0c428d0e).
BUSY_HOLD = 10 * 60

# The tray's three levels, spelled as `tray_state` spells them. Not imported from there:
# `tray_state` imports this module, and `test_collectors.py` pins the two to each other.
OK, WARN, FAIL = "ok", "warn", "fail"
ROW_PREFIX = "collector: "
# A row's state when the project's own verdict is the failing part.
HEALTH_FAILING = "health check failing"
# A `run` row's state when the engine itself is silent: the machine's, not the project's.
NOT_ANSWERING = "docker is not answering"
# The failure a silent engine gets when Windows says its VM ran out of memory
# (`docker_memory.silent_vm_evidence`), filed before any restart: a restart brings the
# engine back into the same squeeze, and on 2026-10-09 eight of them named nothing else.
SHORT_OF_MEMORY = (
    "docker's engine went silent with its VM out of memory -- a container this machine "
    "keeps up needs a lower memory limit, or this machine needs fewer collectors"
)
# The line a health command may print naming its failing jobs, as ibkr_trader's does:
# `unhealthy: social, reddit`. The row carries them into the state, so each job's failure
# is its own group: without them, `social` missing boto3 read as `reddit`'s fixed failure
# recurring (6b140f4e). A command that prints none is one group per project, as before.
UNHEALTHY_LINE = re.compile(r"^\s*unhealthy:\s*(?P<jobs>.+?)\s*$", re.M | re.I)


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


def spawn(argv: Sequence[str], timeout: int, cwd: Path | None = None) -> tuple[int, str]:
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
        return 127, f"{argv[0]} is not on PATH"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except OSError as exc:
        return 126, str(exc)
    return done.returncode, "\n".join(p for p in (done.stdout, done.stderr) if p).strip()


_FRACTION = re.compile(r"\.\d+")


def parse_created(text: str) -> float | None:
    """Docker's `Created` (`2026-10-03T00:16:05.123456789Z`) as a POSIX time; None when it
    is not one. The nanoseconds go: `fromisoformat` takes at most six digits."""
    try:
        when = _dt.datetime.fromisoformat(_FRACTION.sub("", text.strip()).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when.timestamp() if when.tzinfo else None


def desktop_status(text: str) -> str | None:
    """The `Status` `docker desktop status --format json` printed, or None when it printed
    none. The JSON object is read wherever it sits: `spawn` merges stderr in, so a warning
    the CLI writes beside it must not turn "running" into an unreadable answer."""
    start, end = text.find("{"), text.rfind("}")
    try:
        said = json.loads(text[start : end + 1]) if 0 <= start < end else None
    except ValueError:
        return None
    status = said.get("Status") if isinstance(said, dict) else None
    return status if isinstance(status, str) else None


class Docker:
    """Everything that touches the engine: one captured, window-less spawn per call.
    `restarts` is where `revive_engine` records a restart of Docker Desktop; None, as
    for the tray, makes none. `holds` names the runs a restart would cut short now
    (`scheduled_in_flight`); None holds it for nothing."""

    def __init__(
        self,
        ps_timeout: int = PS_TIMEOUT,
        restarts: Path | None = None,
        holds: Callable[[], Sequence[str]] | None = None,
    ) -> None:
        self.ps_timeout = ps_timeout
        self.restarts = restarts
        self.holds = holds

    def run(self, argv: Sequence[str], timeout: int, cwd: Path | None = None) -> tuple[int, str]:
        return spawn(argv, timeout, cwd)

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

    def built(self, container: Container) -> float | None:
        """When `container`'s image was built, or None when the engine cannot say."""
        code, image = self.run(
            ["docker", "inspect", "--format", "{{.Image}}", container.id], INSPECT_TIMEOUT
        )
        if code != 0 or not image.strip():
            return None
        code, created = self.run(
            ["docker", "image", "inspect", "--format", "{{.Created}}", image.strip()],
            INSPECT_TIMEOUT,
        )
        return parse_created(created) if code == 0 else None

    def started(self, container: Container) -> float | None:
        """When `container` last started, whoever started it -- this job, or the engine
        bringing an `unless-stopped` container back after a restart -- or None when the
        engine cannot say."""
        code, out = self.run(
            ["docker", "inspect", "--format", "{{.State.StartedAt}}", container.id],
            INSPECT_TIMEOUT,
        )
        return parse_created(out) if code == 0 else None

    def deploy(self, checkout: Path, service: str) -> tuple[bool, str]:
        # Builds first and recreates only once the build succeeded, so a build that fails
        # leaves the running container as it was.
        code, out = self.run(
            ["docker", "compose", "up", "-d", "--build", service], UP_TIMEOUT, checkout
        )
        return code == 0, out

    def desktop_running(self) -> bool:
        """Whether Docker Desktop is running, whatever its engine says: its own status when
        it gives a readable one, else whether its process is up (`desktop_process`).

        A status that failed is not one saying "stopped": on 2026-10-08 the 21:31 and 21:46
        passes read Desktop as quit -- "start it" -- and stood the revive down, while the
        Desktop the 20:15 revive had started was up the whole time and its engine wedged.
        """
        code, out = self.run(["docker", "desktop", "status", "--format", "json"], DESKTOP_TIMEOUT)
        said = desktop_status(out) if code == 0 else None
        if said is not None:
            return said == "running"
        return self.desktop_process()

    def desktop_process(self) -> bool:
        """Whether `DESKTOP_IMAGE` is running on this machine; never off Windows."""
        if os.name != "nt":
            return False
        argv = ["tasklist", "/FI", f"IMAGENAME eq {DESKTOP_IMAGE}", "/FO", "CSV", "/NH"]
        code, out = self.run(argv, DESKTOP_TIMEOUT)
        return code == 0 and f'"{DESKTOP_IMAGE.lower()}"' in out.lower()

    def updating(self) -> bool:
        """Whether Docker Desktop is installing an update of itself
        (`collector_tasks.updater_listed`); never off Windows."""
        if os.name != "nt":
            return False
        code, out = self.run(collector_tasks.UPDATE_LISTING, DESKTOP_TIMEOUT)
        return code == 0 and collector_tasks.updater_listed(out)

    def restart_desktop(self) -> tuple[bool, str]:
        # Docker's own restart, not `docker-maint.py restart-engine`: that one taskkills
        # the service and runs `wsl --shutdown`, which a timer must not do to every WSL
        # distro the user has open.
        code, out = self.run(
            ["docker", "desktop", "restart", "--timeout", str(RESTART_TIMEOUT)],
            RESTART_TIMEOUT + DESKTOP_TIMEOUT,
        )
        return code == 0, out

    def reset_vm(self) -> tuple[bool, str]:
        """Stop Docker Desktop, stop the WSL VM under it, and start Desktop again
        (`DOCKER_DISTRO`); `(started, what each step said)`. Never off Windows."""
        if os.name != "nt":
            return False, "no WSL VM to reset off Windows"
        said = []
        stop = ["docker", "desktop", "stop", "--force", "--timeout", str(DESKTOP_STOP_TIMEOUT)]
        said.append(self.run(stop, DESKTOP_STOP_TIMEOUT + DESKTOP_TIMEOUT)[1])
        _, listed = self.run(["wsl", "--list", "--running", "--quiet"], WSL_TIMEOUT)
        said.append(self.run(wsl_stop(running_distros(listed)), WSL_TIMEOUT)[1])
        code, out = self.run(
            ["docker", "desktop", "start", "--timeout", str(RESTART_TIMEOUT)],
            RESTART_TIMEOUT + DESKTOP_TIMEOUT,
        )
        return code == 0, "\n".join(line for line in (*said, out) if line.strip())

    def limits(self, ids: Sequence[str]) -> dict[str, int] | None:
        """Each of `ids`' memory limit in bytes, 0 for none, keyed by full id; None when
        the engine cannot say."""
        argv = ["docker", "inspect", "--format", docker_memory.INSPECT_FORMAT, *ids]
        code, out = self.run(argv, INSPECT_TIMEOUT)
        return docker_memory.parse_limits(out) if code == 0 else None

    def vm_memory(self) -> int | None:
        """The memory of the VM the engine runs in, in bytes; None when it cannot say."""
        code, out = self.run(["docker", "info", "--format", "{{.MemTotal}}"], INSPECT_TIMEOUT)
        return docker_memory.capacity(out) if code == 0 else None

    def memory_evidence(self, since: float) -> str:
        """What Windows says about a silent engine's VM having run out of memory since
        `since` (`docker_memory.silent_vm_evidence`), "" for nothing; never off Windows.
        Asks the engine nothing, since this is read exactly when it does not answer."""
        if os.name != "nt":
            return ""
        return docker_memory.read_silent_vm(
            lambda argv: self.run(argv, DESKTOP_TIMEOUT),
            docker_memory.log_dir(),
            Path.home() / ".wslconfig",
            since,
        )

    def reclaim_cache(self) -> tuple[bool, str]:
        """Drop the VM's file cache and compact what that freed (`docker_memory.RECLAIM`);
        `(done, what wsl said)`. Nothing to do off Windows."""
        if os.name != "nt":
            return True, ""
        code, out = self.run(docker_memory.reclaim_argv(DOCKER_DISTRO), WSL_TIMEOUT)
        return code == 0, out.replace("\x00", "")


def running_distros(text: str) -> list[str]:
    """The distro names `wsl --list --running --quiet` printed. `wsl.exe` writes UTF-16,
    which `spawn`'s UTF-8 decoding leaves as the name with a NUL after every letter."""
    return [line.strip() for line in text.replace("\x00", "").splitlines() if line.strip()]


def wsl_stop(running: Sequence[str]) -> list[str]:
    """The `wsl` call that stops Docker's VM: `--shutdown` when every running distro is
    Docker's own, so nobody's shell goes with it; else `--terminate` of Docker's alone."""
    if all(name.lower().startswith(DOCKER_DISTRO) for name in running):
        return ["wsl", "--shutdown"]
    return ["wsl", "--terminate", DOCKER_DISTRO]


class Git:
    """The checkout's half of "is the container behind": what HEAD is, and whether it is
    merged code a timer may deploy."""

    def run(self, checkout: Path, *args: str) -> tuple[int, str]:
        return spawn(["git", *args], GIT_TIMEOUT, checkout)

    def head(self, checkout: Path) -> tuple[str, float] | None:
        """`(short sha, commit time)` of HEAD; None when git cannot say."""
        code, out = self.run(checkout, "log", "-1", "--format=%h %ct", "HEAD")
        sha, _, when = out.strip().partition(" ")
        try:
            return (sha, float(when)) if code == 0 and sha else None
        except ValueError:
            return None

    def held(self, checkout: Path) -> str:
        """Why HEAD must not be deployed by a timer, or "" when it may: it is on origin's
        default branch (at its tip or behind it) and no tracked file is edited."""
        code, _out = self.run(
            checkout, "merge-base", "--is-ancestor", "HEAD", "refs/remotes/origin/HEAD"
        )
        if code == 1:
            return "HEAD is not on origin's default branch"
        if code != 0:
            return "origin's default branch is unknown here"
        code, out = self.run(checkout, "status", "--porcelain", "--untracked-files=no")
        if code != 0:
            return "git status failed"
        return "the checkout has uncommitted edits" if out.strip() else ""


@dataclass
class Report:
    lines: list[str] = field(default_factory=list)
    failures: int = 0
    # The first `fail` line, which `render` repeats last as the run's `error:` line.
    cause: str = ""

    def say(self, line: str) -> None:
        self.lines.append(line)

    def fail(self, line: str) -> None:
        self.lines.append(line)
        self.failures += 1
        self.cause = self.cause or line


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
    report.say(f"{name}: {HEALTH_FAILING} ({summary})")
    report.lines.extend(f"    {line}" for line in out.splitlines()[:40])
    verdict = {"ok": False, "summary": summary, "container": box.id}
    if jobs := unhealthy_jobs(out):
        verdict["unhealthy"] = jobs
    return verdict


def unhealthy_jobs(out: str) -> list[str]:
    """The jobs a health command's `UNHEALTHY_LINE` names, sorted so the order the project
    printed them in makes no new group; [] when it prints none."""
    return sorted(
        {job.strip() for found in UNHEALTHY_LINE.finditer(out) for job in found["jobs"].split(",")}
        - {""}
    )


def code_at(box: Container, docker: Docker, last: dict) -> float | None:
    """The newest the code `box` runs can be: its image's build time, or the commit this
    job last redeployed it onto, whichever is later. The second is what stops a rebuild
    that changed nothing -- a docs-only merge, every layer cached, the image's own date
    unmoved -- from being redeployed again on every pass."""
    times = [t for t in (docker.built(box), last.get(CODE_AT)) if isinstance(t, (int, float))]
    return max(times) if times else None


def redeploy(
    target: Target, box: Container, docker: Docker, git: Git, report: Report, last: dict
) -> dict:
    """Rebuild `box` onto the checkout's HEAD when its code is older and HEAD is merged
    code -- the newest HEAD among the checkout and those it `builds_from`, each of which
    must be merged code. Returns what to record: `code_at`, `held` when a redeploy was
    owed and refused, and `deployed` when this pass redeployed (and so must skip the
    health check)."""
    name, service = target.collector.project, target.collector.service
    known = code_at(box, docker, last)
    if known is None:
        return {}
    heads = source_heads(target, git)
    newest = max(heads, key=lambda pair: pair[1][1], default=None)
    if newest is None or newest[1][1] <= known:
        return {CODE_AT: known}
    where, (sha, committed) = newest
    sha = sha if where == target.checkout else f"{where.name}@{sha}"
    # Every checkout the image copies is baked into it, so each must be merged code.
    held = next((_held_in(target, w, why) for w, _head in heads if (why := git.held(w))), "")
    if held:
        report.say(
            f"{name}: `{service}` runs code older than {sha} and was not redeployed -- {held}"
        )
        return {CODE_AT: known, HELD: held}
    ok, out = docker.deploy(target.checkout, service)
    if not ok:
        report.fail(
            f"{name}: could not redeploy `{service}` onto {sha} -- {first_line(out) or 'no output'}"
        )
        report.lines.extend(f"    {line}" for line in out.splitlines()[-20:])
        return {CODE_AT: known}
    report.say(f"{name}: redeployed `{service}` onto {sha} -- its code was older")
    return {CODE_AT: committed, "deployed": True}


def source_heads(target: Target, git: Git) -> list[tuple[Path, tuple[str, float]]]:
    """`(checkout, its HEAD)` for the target's checkout and each it `builds_from`, leaving
    out any git cannot read."""
    roots = [target.checkout, *(target.checkout.parent / s for s in target.collector.builds_from)]
    return [(root, head) for root in roots if (head := git.head(root)) is not None]


def _held_in(target: Target, where: Path, why: str) -> str:
    """`Git.held`'s answer for `where`, naming it when it is a checkout built from."""
    return why if where == target.checkout else f"{where.name}: {why}"


def keep_running(
    target: Target,
    containers: Sequence[Container],
    docker: Docker,
    report: Report,
    last: dict | None = None,
    git: Git | None = None,
) -> dict | None:
    """Start the collector if it is down, redeploy it if it is behind its checkout.
    Returns a health record, or None when there is nothing to record this pass. `last`
    is the project's previous record. A container this job or the engine (re)started is
    not judged until it has settled (`settling`, `last_start`)."""
    name, service = target.collector.project, target.collector.service
    clock = _clock()
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
        # No health check until it has settled: every job in it has yet to run, and a
        # verdict about a container seconds old is noise (`settling`).
        report.say(f"{name}: started `{service}`")
        return {STARTED_AT: clock}
    report.say(f"{name}: `{service}` up ({box.status})")
    last = last or {}
    code = redeploy(target, box, docker, git or Git(), report, last)
    if code.pop("deployed", False):
        # As on a start: the container is seconds old and its verdict would be noise. The
        # record holds no `ok`, so the tray shows the row green until it has settled.
        return {**code, STARTED_AT: clock}
    if not target.collector.health:
        return code or None
    started = last_start(last.get(STARTED_AT), docker.started(box))
    if started is not None and settling(started, clock, target.collector.settle):
        report.say(
            f"{name}: health check deferred -- `{service}` was (re)started "
            f"{int((clock - started) // 60)} min ago, inside its {target.collector.settle}-minute "
            "settle"
        )
        return {**code, STARTED_AT: started}
    return {**check_health(target, box, docker, report), **code}


def last_start(recorded: object, engine: float | None) -> float | None:
    """The later of the start this job recorded and the one the engine reports, or None.

    The engine's counts because this job is not the only thing that starts a container:
    after a Docker Desktop restart -- `revive_engine`'s, or the VM coming back from a
    freeze -- the engine starts every `unless-stopped` one itself. 2026-10-09 22:24 UTC:
    ibkr_trader's `app` came back that way after a three-hour wedge, and the pass three
    minutes later judged `social` stale by the runs the wedge had cost (3517acbe), before
    its catch-up run could finish.
    """
    times = [t for t in (recorded, engine) if isinstance(t, (int, float))]
    return max(times) if times else None


def _clock() -> float:
    """Now, as a POSIX time: what `STARTED_AT` records and `settling` measures from."""
    return _dt.datetime.now().timestamp()


def settling(started: float, clock: float, settle: int) -> bool:
    """Whether a container this job started or redeployed at `started` is still too new
    for its health verdict to be about the code it runs.

    A project's scheduler persists each job's outcome across a restart, and an interval
    job first fires one interval after it. So for that long the verdict is the *old*
    code's: ibkr_trader's `reddit` read 45 failures from before #79 for the half hour
    after the redeploy that carried #79, and only resolutions that happened to post-date
    #79's merge kept the fix pass from refiling the group (221f3b05) as "did not hold".
    A `started` in the future is a clock that moved, not a container still settling.
    """
    return 0 <= clock - started < settle * 60


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


def maintain_scheduled(
    chosen: Sequence[Target],
    base: Path,
    report: Report,
    run: collector_tasks.Runner = collector_tasks.run_argv,
    python: str | None = None,
) -> None:
    """Register each scheduled collector's task on a `run` machine; remove it otherwise."""
    if not chosen:
        return
    interpreter = python or collector_tasks.interpreter()
    for target in chosen:
        if target.mode == config.RUN:
            collector_tasks.keep_registered(target.collector, base, interpreter, run, report)
        else:
            collector_tasks.keep_removed(collector_tasks.task_name(target.collector), run, report)


def scheduled_in_flight(chosen: Sequence[Target], run: collector_tasks.Runner) -> list[str]:
    """The scheduled collectors this machine runs whose task is running right now."""
    return [
        t.collector.project
        for t in chosen
        if t.collector.scheduled
        and t.mode == config.RUN
        and collector_tasks.running(collector_tasks.task_name(t.collector), run)
    ]


def wait_for_engine(
    docker: Docker,
    awake: float | None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> list[Container] | None:
    """`docker.ps()`, asked again every `ENGINE_POLL` seconds while the machine has been
    up (`awake` seconds, from `machine_clock`) for less than `ENGINE_STARTUP`.

    The job fires at logon, which is exactly when Docker Desktop is still starting: the
    2026-10-07 logon fire ran 108 seconds after boot, found the engine silent and failed
    (aea4ccfa), and the containers were up a minute later on their own restart policy.
    Past the window -- or where the machine cannot say when it came up -- one answer is
    the answer.
    """
    containers = docker.ps()
    if containers is not None or awake is None or awake >= ENGINE_STARTUP:
        return containers
    clock, sleep = clock or _time.monotonic, sleep or _time.sleep
    deadline = clock() + ENGINE_STARTUP - awake
    while containers is None and (left := deadline - clock()) > 0:
        sleep(min(ENGINE_POLL, left))
        containers = docker.ps()
    return containers


def restart_record(path: Path) -> dict:
    """`revive_engine`'s record: when it last restarted (`STARTED_AT`) and since when a
    restart has been held (`HOLD_SINCE`). {} when there is none or it is unreadable."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def last_restart(path: Path) -> float | None:
    """When `revive_engine` last restarted Docker Desktop, or None when it has not."""
    when = restart_record(path).get(STARTED_AT)
    return when if isinstance(when, (int, float)) else None


def hold_restart(path: Path, clock: float) -> float:
    """Since when a restart has been held, recording `clock` as the start of a new hold."""
    record = restart_record(path)
    since = record.get(HOLD_SINCE)
    if isinstance(since, (int, float)) and since <= clock:
        return since
    write_file(path, json.dumps({**record, HOLD_SINCE: clock}) + "\n")
    return clock


def release_hold(path: Path) -> None:
    """End a held restart: the engine answered, so there is nothing left to restart."""
    record = restart_record(path)
    if HOLD_SINCE in record:
        record.pop(HOLD_SINCE)
        write_file(path, json.dumps(record) + "\n")


def held(report: Report, restarts: Path, clock: float, why: str, cause: str) -> tuple[None, str]:
    """`revive_engine`'s answer for a restart it is holding: `why`, with how long it has
    been held, and no failure until that is `HOLD_LIMIT` -- then `cause`."""
    minutes = int((clock - hold_restart(restarts, clock)) // 60)
    report.say(f"{why} (held {minutes} min)")
    return None, "" if minutes * 60 < HOLD_LIMIT else cause


def revive_vm(docker: Docker, report: Report) -> list[Container] | None:
    """The containers once `Docker.reset_vm` has the engine answering, or None. The step
    after a `docker desktop restart` that left the engine silent: the VM under it was too
    wedged to shut itself down (`DOCKER_DISTRO`). The engine is asked for `WEDGE_CONFIRM`
    after the start, since a cold one re-mounts its disk before it answers."""
    ok, out = docker.reset_vm()
    containers = wait_for_engine(docker, ENGINE_STARTUP - WEDGE_CONFIRM) if ok else None
    if containers is None:
        report.say(
            f"reset Docker's WSL VM, and its engine still did not answer: {first_line(out) or 'no output'}"
        )
    else:
        report.say("the restart left the engine silent, so its WSL VM was reset as well")
    return containers


def vm_reset_since(path: Path, restarted: float) -> bool:
    """Whether `revive_vm` has run since the restart made at `restarted`."""
    when = restart_record(path).get(VM_RESET_AT)
    return isinstance(when, (int, float)) and when >= restarted


def revive_vm_once(
    docker: Docker, report: Report, restarts: Path, clock: float
) -> list[Container] | None:
    """`revive_vm`, recorded as `VM_RESET_AT` first, so a reset that does not help is not
    repeated before the next restart."""
    write_file(restarts, json.dumps({**restart_record(restarts), VM_RESET_AT: clock}) + "\n")
    return revive_vm(docker, report)


def revive_engine(
    docker: Docker, report: Report, restarts: Path, clock: float, busy: Sequence[str] = ()
) -> tuple[list[Container] | None, str]:
    """`(containers, why not)` for an engine `wait_for_engine` has already given up on.

    Only a *wedged* engine is restarted: Docker Desktop says `running` and its engine
    does not answer for `WEDGE_CONFIRM` more seconds -- one that answers in that time was
    slow, not wedged. A Desktop that is not running was most likely quit by a person, and
    a timer does not start it behind their back; the answer then says so. A restart is
    made once per `RESTART_EVERY`; `clock` is now, as a POSIX time.

    Nor is it restarted while a scheduled collector's run is in flight (`busy`, from
    `scheduled_in_flight`) and the wedge is new: its services may outlive a silent engine
    API, and the restart is what would stop them under it (6c504c34). That wait is no
    failure -- `(None, "")`. A wedge held for `BUSY_HOLD` is restarted under the run: it
    is starving the run too, and waiting for a run to end is waiting forever when the
    next one has started by then (0c428d0e).

    Nor while Docker Desktop installs an update of itself (`Docker.updating`): its engine
    is down by design until the installer starts the new app, a restart would cut the
    install short, and the installer may have closed the app it reads as quit. That is
    held, asked first, and fails nothing until it has lasted `HOLD_LIMIT` (3012d246).

    A restart that leaves the engine silent goes on to reset the VM under it
    (`revive_vm`), in the same pass and under the same `RESTART_EVERY` record. A pass
    inside that hour that finds it silent again resets the VM if nothing has since the
    restart (`VM_RESET_AT`): a restart that came back and wedged again, or one made by
    code without the reset, is not left to fail every pass until the hour is up.
    """
    if docker.updating():
        why = (
            "Docker Desktop is installing an update, and its engine is down until it is "
            "done -- not restarting it under the installer"
        )
        cause = f"{NOT_ANSWERING} while Docker Desktop installs an update"
        return held(report, restarts, clock, why, cause)
    if not docker.desktop_running():
        return None, "docker is not answering and Docker Desktop is not running -- start it"
    containers = wait_for_engine(docker, ENGINE_STARTUP - WEDGE_CONFIRM)
    if containers is not None:
        report.say("docker was slow to answer, not wedged -- it answered when asked again")
        return containers, ""
    wedged = "docker is not answering though Docker Desktop says it is running"
    last = last_restart(restarts)
    if last is not None and 0 <= clock - last < RESTART_EVERY:
        report.say(
            f"Docker Desktop was restarted {int((clock - last) // 60)} min ago; "
            f"not again within {RESTART_EVERY // 60} minutes of that"
        )
        if not vm_reset_since(restarts, last):
            containers = revive_vm_once(docker, report, restarts, clock)
            if containers is not None:
                return containers, ""
        return None, f"{wedged}, and a restart did not bring its engine back"
    if busy:
        runs = f"{', '.join(busy)}'s scheduled run"
        silent = clock - hold_restart(restarts, clock)
        if silent < BUSY_HOLD:
            why = f"not restarting Docker Desktop under {runs}: the restart would stop the services it is using"
            return held(report, restarts, clock, why, "")
        report.say(
            f"restarting Docker Desktop under {runs}: its engine has not answered for "
            f"{int(silent // 60)} min, so the run is not being served either"
        )
    write_file(restarts, json.dumps({STARTED_AT: clock}) + "\n")
    ok, out = docker.restart_desktop()
    containers = docker.ps() if ok else None
    if containers is None:
        report.say(f"`docker desktop restart`: {first_line(out) or 'no output'}")
        containers = revive_vm_once(docker, report, restarts, clock)
    if containers is None:
        return None, f"{wedged}, and a restart did not bring its engine back"
    report.say("the docker engine was wedged behind a running Docker Desktop -- restarted it")
    return containers, ""


def kept_up(
    chosen: Sequence[Target], containers: Sequence[Container]
) -> list[tuple[str, Container]]:
    """`(project, container)` for every running container this machine keeps up: one
    whose compose working directory is the checkout of a collector it runs -- the
    collector's own service and whatever it brings up beside it, such as its db."""
    runs = [t for t in chosen if t.mode == config.RUN]
    return [
        (t.collector.project, c)
        for c in containers
        if c.running
        for t in runs
        if same_dir(c.workdir, t.checkout)
    ]


def check_memory(
    chosen: Sequence[Target], containers: Sequence[Container], docker: Docker, report: Report
) -> None:
    """Fail the pass for every container kept up without a memory limit, and for limits
    that together do not fit the VM (`docker_memory.budget`). A limit the engine would
    not say is said, not failed: the pass cannot tell it from a fine one."""
    mine = kept_up(chosen, containers)
    if not mine:
        return
    limits = docker.limits([c.id for _, c in mine])
    if limits is None:
        report.say("memory limits could not be read -- the VM's budget is unchecked this pass")
        return
    capped = [
        (f"{project}: `{c.service}`", docker_memory.limit_of(c.id, limits)) for project, c in mine
    ]
    for problem in docker_memory.budget(capped, docker.vm_memory()):
        if problem.detail:
            report.say(f"  {problem.detail}")
        report.fail(problem.line)


def reclaim_vm_memory(docker: Docker, report: Report) -> None:
    """Hand the memory the VM holds only as file cache back to Windows. A drop that did
    not run is said, not failed: the containers are no worse off for it."""
    done, out = docker.reclaim_cache()
    if not done:
        report.say(f"the VM's file cache was not dropped: {first_line(out) or 'no output'}")


def maintain(
    chosen: Sequence[Target],
    docker: Docker,
    report: Report,
    health: dict,
    git: Git | None = None,
    awake: float | None = None,
) -> None:
    """One pass over the assigned *container* collectors, recording health verdicts into
    `health`. The scheduled ones are `maintain_scheduled`'s, and need no docker. `awake`
    is how long the machine has been up, which `wait_for_engine` gives Docker to start;
    an engine silent past that is `revive_engine`'s where `docker.restarts` is set, held
    for whatever `docker.holds` names for up to `BUSY_HOLD`. A held restart fails nothing until `HOLD_LIMIT`.

    Memory is checked on both sides (`docker_memory`): a silent engine whose VM Windows
    says ran out fails as `SHORT_OF_MEMORY` before it is restarted, and an answering one
    has the limits of what it keeps up checked (`check_memory`). An answering engine's
    VM also has its file cache dropped (`reclaim_vm_memory`), by the scheduled pass
    alone -- the one that carries `docker.restarts`."""
    if not chosen:
        return
    containers = wait_for_engine(docker, awake)
    running = any(t.mode == config.RUN for t in chosen)
    why = "docker is not answering -- is Docker Desktop running?"
    if containers is None and running and docker.restarts is not None:
        short = docker.memory_evidence(_clock() - docker_memory.OOM_LOOKBACK)
        if short:
            report.say(f"  {short}")
            report.fail(SHORT_OF_MEMORY)
        busy = docker.holds() if docker.holds else ()
        containers, why = revive_engine(docker, report, docker.restarts, _clock(), busy)
    if containers is None:
        if not running:
            report.say("docker is not answering, so nothing here can be running")
        elif why:
            report.fail(why)
        return
    if docker.restarts is not None:
        release_hold(docker.restarts)
        reclaim_vm_memory(docker, report)
    check_memory(chosen, containers, docker, report)
    for target in chosen:
        if target.mode == config.RUN:
            last = health.get(target.collector.project, {})
            record = keep_running(target, containers, docker, report, last, git)
            if record is not None:
                health[target.collector.project] = record
        else:
            keep_stopped(target, containers, docker, report)


@contextlib.contextmanager
def acting(path: Path, chosen: Sequence[Target]) -> Iterator[None]:
    """`IN_PASS` written for as long as the block runs, when any of `chosen` is a `run`
    target -- a pass that only stops containers starts none -- and removed however it
    ends. `until` allows each such target its whole `TARGET_BOUND`."""
    started = [t for t in chosen if t.mode == config.RUN]
    if not started:
        yield
        return
    clock = _clock()
    until = clock + len(started) * TARGET_BOUND + PS_TIMEOUT
    write_file(path, json.dumps({STARTED_AT: clock, UNTIL: until}) + "\n")
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def in_pass(path: Path, clock: float) -> bool:
    """Whether `acting`'s marker at `path` says a pass is at work at `clock`. A marker
    past its `until` is one a killed pass left; one starting after `clock` is a clock
    that moved. Either, or one unreadable, is no pass."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(raw, dict):
        return False
    started, until = raw.get(STARTED_AT), raw.get(UNTIL)
    if not (isinstance(started, (int, float)) and isinstance(until, (int, float))):
        return False
    return started <= clock < until


def status(
    collectors: Sequence[config.Collector],
    chosen: Sequence[Target],
    docker: Docker,
    report: Report,
    base: Path = REPO_ROOT,
    run: collector_tasks.Runner = collector_tasks.run_argv,
) -> None:
    """Read-only: what is declared, what this machine was told (`chosen`, the targets
    `targets` drew from the assignment), and what is running."""
    if not collectors:
        report.say(f"no collectors declared -- add `{config.SETTING}` to the workspace file")
    in_containers = any(not t.collector.scheduled for t in chosen)
    containers: list[Container] | None = docker.ps() if in_containers else []
    python = collector_tasks.interpreter() if any(t.collector.scheduled for t in chosen) else ""
    by_project = {t.collector.project: t for t in chosen}
    for collector in collectors:
        target = by_project.get(collector.project)
        if target is None:
            report.say(f"{collector.project}: not assigned on this machine (hands off)")
            continue
        mode = target.mode
        if collector.scheduled:
            state = collector_tasks.describe(collector, base, python, run)
            report.say(f"{collector.project}: assigned `{mode}` -- {state}")
            continue
        box = find(containers or [], target.checkout, collector.service)
        state = (
            "docker not answering"
            if containers is None
            else (box.status if box else "no container")
        )
        report.say(f"{collector.project}: assigned `{mode}` -- `{collector.service}` {state}")


def render(lines: Sequence[str], failures: int, when: _dt.datetime, cause: str = "") -> str:
    """The artifact: a head with the failure count, the lines, and a failed run's `cause`
    last as an `error:` line. `log-wrap.py` files a failed run's cause from that line, and
    with none it took the run's last line: f783d741 filed `sports_betting: healthy` as the
    cause of a failure some other line had named."""
    head = f"# collectors {when.isoformat(timespec='seconds')} -- {failures} failure(s)"
    tail = [f"error: {cause}"] if failures and cause else []
    return "\n".join([head, *lines, *tail, ""])


def write_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# --- the tray's half --------------------------------------------------------------


def row(
    target: Target, containers: Sequence[Container] | None, health: dict, busy: bool = False
) -> tuple[str, str]:
    """`(level, detail)` for one assigned collector. `busy` is a `maintain` pass at work
    (`in_pass`), which is starting whatever it finds down: green until it is done, and a
    start that fails fails that pass."""
    if target.mode == config.STOP:
        box = find(containers or [], target.checkout, target.collector.service)
        if box is not None and box.running:
            return WARN, "running here, but this machine is set to stop it"
        return OK, "off on this machine (by choice)"
    if containers is None:
        return FAIL, NOT_ANSWERING
    box = find(containers, target.checkout, target.collector.service)
    if box is None or not box.running:
        return down_row(box, busy)
    verdict = health.get(target.collector.project, {})
    if verdict.get("container") == box.id and verdict.get("ok") is False:
        jobs = verdict.get("unhealthy")
        named = f": {', '.join(map(str, jobs))}" if isinstance(jobs, list) and jobs else ""
        return WARN, f"{HEALTH_FAILING}{named} -- {verdict.get('summary', '')}"
    return OK, f"running ({box.status})"


def down_row(box: Container | None, busy: bool) -> tuple[str, str]:
    """`row`'s answer for a `run` collector with no running container: red, unless a
    pass is at work (`busy`), which is starting it."""
    if busy:
        return OK, f"{STARTING} ({box.status if box else 'no container yet'})"
    if box is None:
        return FAIL, "no container -- see logs/collectors.log"
    return FAIL, f"not running ({box.status})"


def assigned(root: Path = REPO_ROOT) -> tuple[list[config.Collector], list[Target]]:
    """`(declared, assigned here)`; nothing read past the assignment on a machine with none."""
    base = config.home(root)
    assignment = config.load_assignment(base / config.ASSIGNMENT)
    if not assignment:
        return [], []
    collectors, _notes = config.declared(base)
    return collectors, targets(collectors, assignment, sweep.default_workspace(base).parent)


def scheduled_tasks(root: Path = REPO_ROOT) -> dict[str, str]:
    """`{task name: its log}` for each scheduled collector this machine runs.

    The tray asks the scheduler about these beside devkit's own jobs, in the same query,
    and judges them the same way (`schedule_health.problems`). A `stop` one is not here:
    its task is deleted, and `tray_rows` reports it.
    """
    _collectors, chosen = assigned(root)
    return {
        collector_tasks.task_name(t.collector): collector_tasks.log_path(
            collector_tasks.task_name(t.collector)
        ).as_posix()
        for t in chosen
        if t.collector.scheduled and t.mode == config.RUN
    }


def tray_rows(root: Path = REPO_ROOT, docker: Docker | None = None) -> list[tuple[str, str, str]]:
    """`(row name, level, detail)` per collector assigned to this machine, except the
    scheduled ones it runs, which are scheduler rows (`scheduled_tasks`). Never raises.

    Empty on a machine assigned nothing, **without asking docker**: a laptop sharing the
    workspace sees no rows and pays no spawn.
    """
    base = config.home(root)
    assignment = config.load_assignment(base / config.ASSIGNMENT)
    if not assignment:
        return []
    collectors, chosen = assigned(root)
    scheduled = [t for t in chosen if t.collector.scheduled]
    chosen = [t for t in chosen if not t.collector.scheduled]
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
        busy = in_pass(base / IN_PASS, _clock())
        rows += [
            (ROW_PREFIX + t.collector.project, *row(t, containers, health, busy)) for t in chosen
        ]
    rows += [
        (ROW_PREFIX + t.collector.project, OK, "off on this machine (by choice)")
        for t in scheduled
        if t.mode == config.STOP
    ]
    return rows


# --- the command line --------------------------------------------------------------

VERBS = {"run-here": config.RUN, "stop-here": config.STOP, "release": None}

# The value of the picker row that runs nothing (`picker_rows.NOTHING`): drawn when no
# collector is declared, so a click on it has to be a quiet no-op, not a usage error.
NOTHING = "none"

# One scheduled collector, run by hand in this terminal. Not one of `VERBS`: it assigns
# nothing, so the picker offers it per collector and never for "all".
ONCE = "run-once"

# The verbs that act on exactly one collector, named.
SINGLE = ("fire", ONCE)


def split_pick(argv: Sequence[str]) -> list[str]:
    """`run-here:ibkr_trader` -> `run-here ibkr_trader`.

    The VS Code task's picker hands its pick over as one argument, and one argument is
    all the dispatcher can pass through; typed at a terminal the two words arrive apart
    and nothing changes.
    """
    if not argv or ":" not in argv[0]:
        return list(argv)
    verb, _, project = argv[0].partition(":")
    return [verb, *([project] if project else []), *argv[1:]]


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "mode",
        nargs="?",
        default="status",
        choices=("status", "maintain", *SINGLE, *VERBS, NOTHING),
        help=(
            "status: report only (default). maintain: what the scheduler runs. run-here / "
            "stop-here: assign this machine, then act on it now. release: forget. "
            "fire NAME: run one scheduled collector once (what its own task runs). "
            "run-once NAME: the same, by hand, its output in this terminal. "
            "Also takes `<mode>:<project>`, the VS Code picker's spelling."
        ),
    )
    parser.add_argument("projects", nargs="*", help="collectors to assign (default: all)")
    parser.add_argument("--devkit", type=Path, default=REPO_ROOT, help=argparse.SUPPRESS)
    return parser.parse_args(split_pick(sys.argv[1:] if argv is None else argv))


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


def fire(
    name: str, base: Path, now: _dt.datetime, spawner: collector_tasks.Spawner | None = None
) -> int:
    """`fire <name>`: what a scheduled collector's task runs. Exits with the command's code.

    Writes `logs/collector-<name>.log` and never `ARTIFACT`, which belongs to the pass, and
    records a run's start before its command runs (`collector_tasks.start_path`). A
    task left behind on a machine no longer assigned `run` -- the pass that deletes it has
    not come round yet -- does nothing and says so, rather than running a second writer.
    """
    collectors, _notes = config.declared(base)
    collector = next((c for c in collectors if c.project == name and c.scheduled), None)
    assignment = config.load_assignment(base / config.ASSIGNMENT)
    if collector is None:
        code, lines = 2, [f"`{config.SETTING}` declares no scheduled collector `{name}`"]
    elif assignment.get(name) != config.RUN:
        code, lines = 0, ["not assigned `run` on this machine -- nothing run"]
    else:
        checkout = sweep.default_workspace(base).parent / collector.project
        start = now.astimezone().isoformat(timespec="seconds")
        write_file(base / collector_tasks.start_path(name), start + "\n")
        code, lines = collector_tasks.fire(
            collector, checkout, spawner or collector_tasks.spawn, fired=now
        )
    text = collector_tasks.render(name, code, lines, now)
    write_file(base / collector_tasks.log_path(name), text)
    print(text, end="")
    return code


def run_once(
    name: str,
    base: Path,
    run: collector_tasks.Runner,
    streamer: collector_tasks.Streamer | None = None,
) -> int:
    """`run-once <name>`: the VS Code task's "run it now", whatever this machine's
    assignment -- a first run before `run-here` is the case it exists for.

    Refused while the scheduled task is running, for the reason `collector_tasks.running`
    gives; the scheduler cannot refuse it, because a run by hand is not one of its fires.
    """
    collectors, _notes = config.declared(base)
    collector = next((c for c in collectors if c.project == name and c.scheduled), None)
    if collector is None:
        print(f"collectors: `{config.SETTING}` declares no scheduled collector `{name}`")
        return 2
    task = collector_tasks.task_name(collector)
    if collector_tasks.running(task, run):
        print(
            f"collectors: {task}'s scheduled run is going right now -- a second run would "
            f"share its browser profile. Wait for it, or end it with `schtasks /End /TN {task}`."
        )
        return 2
    checkout = sweep.default_workspace(base).parent / collector.project
    code = collector_tasks.run_once(collector, checkout, streamer or collector_tasks.stream)
    print(f"collectors: {name} exited {code}")
    return code


def single(
    args: argparse.Namespace,
    base: Path,
    now: _dt.datetime,
    run: collector_tasks.Runner,
    spawner: collector_tasks.Spawner | None,
    streamer: collector_tasks.Streamer | None,
) -> int:
    """`fire` and `run-once`, the verbs that take exactly one collector and no `Report`."""
    if len(args.projects) != 1:
        print(f"collectors: `{args.mode}` takes exactly one collector name", file=sys.stderr)
        return 2
    if args.mode == ONCE:
        return run_once(args.projects[0], base, run, streamer)
    return fire(args.projects[0], base, now, spawner)


def apply_verb(
    args: argparse.Namespace,
    base: Path,
    collectors: list[config.Collector],
    run: collector_tasks.Runner,
    report: Report,
) -> dict[str, str] | None:
    """`run-here`/`stop-here`/`release`: the assignment this run then acts on, or None
    when there is nothing left to act on -- a typo, or a release."""
    updated = reassign(args, base, collectors, report)
    if updated is None:
        return None
    picked = config.pick(collectors, args.projects)[0]
    if VERBS[args.mode] is not None:
        return {c.project: updated[c.project] for c in picked}
    # Released: hands off the container, but a task exists only because this
    # job registered it, so it goes rather than firing with nobody assigned.
    for collector in picked:
        if collector.scheduled:
            collector_tasks.keep_removed(collector_tasks.task_name(collector), run, report)
    return None


def main(
    argv: Sequence[str] | None = None,
    docker: Docker | None = None,
    run: collector_tasks.Runner = collector_tasks.run_argv,
    spawner: collector_tasks.Spawner | None = None,
    streamer: collector_tasks.Streamer | None = None,
) -> int:
    args = parse_args(argv)
    if args.mode == NOTHING:
        print("collectors: nothing picked -- no collector is declared, so nothing was done")
        return 0
    base = config.home(args.devkit.expanduser().resolve())
    now = _dt.datetime.now()
    if args.mode in SINGLE:
        return single(args, base, now, run, spawner, streamer)
    report = Report()

    def finish() -> int:
        """Write the artifact; 2 when anything in it failed, a typo included."""
        text = render(report.lines, report.failures, now, report.cause)
        write_file(base / ARTIFACT, text)
        print(text, end="")
        return 2 if report.failures else 0

    collectors, notes = config.declared(base)
    for note in notes:
        report.fail(note)
    assignment = config.load_assignment(base / config.ASSIGNMENT)
    if args.mode in VERBS:
        acted_on = apply_verb(args, base, collectors, run, report)
        if acted_on is None:
            return finish()
        assignment = acted_on
    chosen = targets(collectors, assignment, sweep.default_workspace(base).parent)
    engine = docker or Docker(
        restarts=base / RESTARTED, holds=lambda: scheduled_in_flight(chosen, run)
    )
    if args.mode == "status":
        status(collectors, chosen, engine, report, base, run)
        return finish()
    if not chosen:
        report.say("no collector is assigned to this machine -- nothing to do")
    maintain_scheduled([t for t in chosen if t.collector.scheduled], base, report, run)
    health = load_health(base / HEALTH)
    in_containers = [t for t in chosen if not t.collector.scheduled]
    since = machine_clock.awake_since()
    awake = None if since is None else _time.time() - since
    with acting(base / IN_PASS, in_containers):
        maintain(in_containers, engine, report, health, awake=awake)
    write_file(base / HEALTH, json.dumps(health, indent=2, sort_keys=True) + "\n")
    return finish()


if __name__ == "__main__":
    sys.exit(main())
