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
    collectors.py move PROJECT.. --to HOST  # hand them, data and all, to HOST
    collectors.py maintain              # what `devkit-collectors` runs every 15 minutes
    collectors.py fire NAME             # one scheduled collector, once: what its task runs
    collectors.py run-once NAME         # the same by hand, output in this terminal

No names means every collector `devkit.collectors` declares -- except for `move`, which
takes them by name.

**Moving one is a command on one machine** (`move`), for a collector that declares a
`handoff` (`collector_handoff` owns the contract). Typed on the machine that runs it, it
stops the collector's service, has the project export its data addressed to HOST, sets
this machine to `stop` and stops the project's other containers. HOST takes it over on
its next pass, with nobody at it (`pick_up`): it imports the data, sets itself to `run`
and starts it as `run-here` would. Without a `handoff` a move is `stop-here` on one
machine and `run-here` on the other, and the data stays where it was. The sending machine
keeps asking whether HOST has taken it over, and fails its pass once that is overdue
(`follow_handoffs`): until then nothing runs the collector, and no row on either machine
would otherwise say so.

**One actor at a time** (`PASS_LOCK`). `move` holds the lock a pass holds, for its whole
export, so a scheduled pass meanwhile stands down rather than restarting the service
`move` stopped for the dump; and the pickup runs inside `maintain`, under that pass's lock,
whose bound allows for the restore (`pass_bound`).

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
import tempfile
import time as _time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collector_build
import collector_handoff as handoff
import collector_tasks
import collectors_config as config
import docker_memory
import machine_clock
import sweep
import worktree_env

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
# Held, `{started_at, until}`, by every pass that starts or stops containers -- `maintain`
# and the verbs -- for as long as it acts. Nothing serialised them, so a `run-here` at
# 22:14:38 and the scheduled `maintain` at 22:15:01 both ran `docker compose up -d` for the
# same newly assigned collectors, and each exited 2 on compose's "container name is already
# in use" Conflict over the container the other had just created (3c3e36f2). A scheduled
# pass that finds it held stands down; a verb waits, since it was typed to act now. `until`
# breaks a lock a killed pass left, as it bounds `IN_PASS`.
PASS_LOCK = Path("logs/collectors.lock")
# Seconds a lock may stay unreadable: its holder has created it and not yet written it.
LOCK_WRITING = 10
# Seconds between a waiting verb's looks at the lock.
LOCK_POLL = 5
STOOD_DOWN = (
    "another collectors pass is acting right now -- this one stands down; the next "
    "scheduled pass acts on whatever that one did not"
)
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
# One `git archive` of a checkout into a staged build context (`collector_build`).
EXPORT_TIMEOUT = 120
# The longest one container target can hold a pass: every spawn `keep_running` makes, at
# its timeout -- three inspects; three git calls for the checkout and three for a sibling
# it `builds_from` (resolve the pin, read it, check it is merged); an export of each; the
# build and the health check.
TARGET_BOUND = (
    3 * INSPECT_TIMEOUT + 6 * GIT_TIMEOUT + 2 * EXPORT_TIMEOUT + UP_TIMEOUT + HEALTH_TIMEOUT
)
# The longest one handoff pickup can hold a pass (`take_over`): asking, stopping the
# service, the restore. A pass's `PASS_LOCK` allows each it may make this long
# (`pass_bound`), so no stale-lock takeover starts a second pass mid-restore; and the
# scheduled task's time limit covers one plus a target's `TARGET_BOUND`
# (`tests/test_install_collectors.py`), since a pass killed mid-restore would leave the
# marker unimported for every later pass to start the restore again.
PICKUP_BOUND = handoff.PENDING_TIMEOUT + PS_TIMEOUT + STOP_TIMEOUT + handoff.IMPORT_TIMEOUT
# How long `move` holds `PASS_LOCK` for each collector it hands off (`move_one`): stopping
# it, the export, and either starting it again or stopping the rest.
MOVE_BOUND = PS_TIMEOUT + STOP_TIMEOUT + handoff.EXPORT_TIMEOUT + UP_TIMEOUT + 2 * PS_TIMEOUT
# How long after a `move` the receiving machine may take before the sender's pass fails on
# it (`follow_handoffs`): two of its passes, 15 minutes apart, and the restore.
HANDOFF_GRACE = 2 * 15 * 60 + PICKUP_BOUND
# What `move` writes into `PASS_LOCK` beside `{started_at, until}`: who holds it and for
# which collectors, so the tray keeps those rows green for the hour a move stops them
# (`move_holding`). A pass writes neither; `IN_PASS` speaks for it.
HOLDER, PROJECTS = "holder", "projects"
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
# What Docker Desktop's stop reason says of an engine that died starting rather than one a
# person stopped (`Docker.stop_reason`). 2026-10-12: status `stopped`, reason `engine
# linux/wsl failed to start: ... wsl.exe -l -v --all: ... CommandTimedOut` -- read as
# "quit by a person", so nothing revived it under social-scraper's fires (08ec2a47).
FAILED_TO_START = "failed to start"
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
# `-data` sibling); otherwise only Docker's is terminated. A listing that fails is read as
# none running: a `wsl.exe` that does not answer serves no distro to spare. 2026-10-12
# `wsl.exe -l -v` hung for good under Docker's failed start (930844fd), and the spawn's
# `timed out after` read as a distro name would have earned it a `--terminate` instead.
DOCKER_DISTRO = "docker-desktop"
WSL_TIMEOUT = 120
# What leaves every revive above powerless: WSL's own service stuck between states
# (`Docker.wsl_service`). 2026-10-13 from 23:45 local (9e7dfa6f x3): `WSLService` sat in
# STOP_PENDING, so `wsl.exe -l -v` and `wsl --shutdown` never answered, Docker's restart
# and `reset_vm` changed nothing, and every pass for the next hour said only "a restart
# did not bring its engine back". The service grants an unelevated user neither start nor
# stop (`sc sdshow WSLService`) and a timer may not reboot the machine, so a pass that
# finds it stuck restarts nothing and names the one remedy, which needs a person. The
# query and its parse are `collector_tasks`', whose fire asks the same of a silent engine.
WSL_SERVICE = collector_tasks.WSL_QUERY[-1]
WSL_STUCK = ("STOP_PENDING", "START_PENDING")
# How long `wsl --list` may take before WSL itself is called hung (`Docker.wsl_answers`),
# which no restart of Desktop or reset of its VM reaches: every one of them goes through
# the WSL service. 2026-10-12 23:45 local: `WslService` sat in `StopPending`, the pass
# waited out the engine, a restart and both `wsl` calls of `reset_vm` at their full
# timeouts, and ran 35 minutes on a 15-minute schedule, so the 00:00 fire was skipped
# (5b71506f) -- to fail on "a restart did not bring its engine back" at the end.
WSL_PROBE_TIMEOUT = 60
WSL_HUNG = (
    f"docker is not answering: WSL itself does not answer (`wsl --list` ran past "
    f"{WSL_PROBE_TIMEOUT}s), and no restart of Docker Desktop or its VM reaches a hung WSL "
    f"-- restart WslService from an elevated prompt, or reboot"
)
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


TIMED_OUT = collector_tasks.TIMED_OUT


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
        return TIMED_OUT, f"timed out after {timeout}s"
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


def desktop_process(run: Callable[[Sequence[str], int], tuple[int, str]]) -> bool:
    """Whether `DESKTOP_IMAGE` is running on this machine, asked through `run`
    (`Docker.run`); never off Windows."""
    if os.name != "nt":
        return False
    argv = ["tasklist", "/FI", f"IMAGENAME eq {DESKTOP_IMAGE}", "/FO", "CSV", "/NH"]
    code, out = run(argv, DESKTOP_TIMEOUT)
    return code == 0 and f'"{DESKTOP_IMAGE.lower()}"' in out.lower()


class WslProbes:
    """What `Docker` asks of WSL under its engine, which no restart of Docker Desktop
    reaches: a service stuck between states (`wsl_service`), or a WSL that does not
    answer at all (`wsl_answers`). Both go through the subclass's `run`."""

    def run(self, argv: Sequence[str], timeout: int, cwd: Path | None = None) -> tuple[int, str]:
        raise NotImplementedError

    def wsl_service(self) -> str:
        """`WSL_SERVICE`'s state as `sc query` names it (`RUNNING`, `STOP_PENDING`); ""
        when it cannot say, and off Windows. Asks the service manager, not `wsl.exe`,
        which is exactly what does not answer while the service is stuck."""
        if os.name != "nt":
            return ""
        code, out = self.run(list(collector_tasks.WSL_QUERY), DESKTOP_TIMEOUT)
        return collector_tasks.service_state(out) if code == 0 else ""

    def wsl_answers(self) -> bool:
        """Whether WSL lists its distros within `WSL_PROBE_TIMEOUT`; always off Windows.
        Only a timeout is silence: an exit code is WSL answering."""
        if os.name != "nt":
            return True
        code, out = self.run(["wsl", "--list", "--quiet"], WSL_PROBE_TIMEOUT)
        return not (code == TIMED_OUT and out.startswith("timed out"))


class Docker(WslProbes):
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

    def deploy(self, checkout: Path, service: str, files: Sequence[Path] = ()) -> tuple[bool, str]:
        # Builds first and recreates only once the build succeeded, so a build that fails
        # leaves the running container as it was. `files` are compose files to read in
        # place of the checkout's own lookup (`collector_build.stage`); the project
        # directory stays the first one's, so the project name and paths do not move.
        named = [arg for f in files for arg in ("-f", str(f))]
        code, out = self.run(
            ["docker", "compose", *named, "up", "-d", "--build", service], UP_TIMEOUT, checkout
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
        return desktop_process(self.run)

    def stop_reason(self) -> str:
        """Docker Desktop's own reason its engine is down (`collector_tasks.stop_reason`),
        read after `desktop_running` has asked its status; "" for none, and off Windows."""
        if os.name != "nt":
            return ""
        return collector_tasks.stop_reason(docker_memory.log_dir())

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
        code, listed = self.run(["wsl", "--list", "--running", "--quiet"], WSL_TIMEOUT)
        running = running_distros(listed) if code == 0 else []
        said.append(self.run(wsl_stop(running), WSL_TIMEOUT)[1])
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


def wsl_stuck_cause(state: str) -> str:
    """The failure for an engine silent because `WSL_SERVICE` is stuck in `state`."""
    return (
        f"{NOT_ANSWERING}: WSL's service ({WSL_SERVICE}) is stuck in {state} -- only an "
        "elevated `taskkill /F /IM wslservice.exe`, or a reboot, brings Docker's engine back"
    )


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

    def head(self, checkout: Path, rev: str = "HEAD") -> tuple[str, float] | None:
        """`(short sha, commit time)` of `rev`, HEAD by default; None when git cannot say."""
        code, out = self.run(checkout, "log", "-1", "--format=%h %ct", rev, "--")
        sha, _, when = out.strip().partition(" ")
        try:
            return (sha, float(when)) if code == 0 and sha else None
        except ValueError:
            return None

    def held(self, checkout: Path, rev: str = "HEAD") -> str:
        """Why `rev` must not be deployed by a timer, or "" when it may: it is on origin's
        default branch (at its tip or behind it) and, for HEAD, no tracked file is edited.
        Any other rev is built from an export of the commit (`collector_build`), so the
        files on disk are not what ships."""
        code, _out = self.run(
            checkout, "merge-base", "--is-ancestor", rev, "refs/remotes/origin/HEAD"
        )
        if code == 1:
            return f"{'HEAD' if rev == 'HEAD' else rev[:7]} is not on origin's default branch"
        if code != 0:
            return "origin's default branch is unknown here"
        if rev != "HEAD":
            return ""
        code, out = self.run(checkout, "status", "--porcelain", "--untracked-files=no")
        if code != 0:
            return "git status failed"
        return "the checkout has uncommitted edits" if out.strip() else ""

    def pin(self, checkout: Path, sibling: str) -> str:
        """The ref `checkout`'s own PR gate checks `sibling` out at; "" when it pins none."""
        return worktree_env.pinned_ref(checkout, sibling)

    def resolve(self, checkout: Path, ref: str) -> str:
        """`ref`'s full commit sha in `checkout` -- a branch by its remote-tracking ref,
        which a fetch moves, ahead of a local one -- or "" when it has no such commit."""
        for name in (f"refs/remotes/origin/{ref}", ref):
            code, out = self.run(checkout, "rev-parse", "--verify", "--quiet", f"{name}^{{commit}}")
            if code == 0 and out.strip():
                return out.strip()
        return ""

    def export(self, checkout: Path, rev: str, dest: Path) -> str:
        """Write `rev`'s tree into `dest` (`collector_build.Export`); "" on success."""
        archive = dest.with_name(f"{dest.name}.tar")
        code, out = spawn(
            ["git", "archive", "--format=tar", f"--output={archive}", rev],
            EXPORT_TIMEOUT,
            checkout,
        )
        if code != 0:
            return first_line(out) or f"git archive exited {code}"
        try:
            return collector_build.unpack(archive, dest)
        finally:
            archive.unlink(missing_ok=True)


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
    code -- the newest commit among the checkout and those it `builds_from`, each of
    which must be merged code. A sibling the checkout's PR gate pins is built at that
    pin, not at its own HEAD (`source_heads`, `deploy_sources`). Returns what to record:
    `code_at`, `held` when a redeploy was owed and refused, and `deployed` when this
    pass redeployed (and so must skip the health check)."""
    name, service = target.collector.project, target.collector.service
    known = code_at(box, docker, last)
    if known is None:
        return {}
    sources = source_heads(target, git)
    newest = max(sources, key=lambda source: source.committed, default=None)
    if newest is None or newest.committed <= known:
        return {CODE_AT: known}
    sha = newest.sha if newest.root == target.checkout else f"{newest.root.name}@{newest.sha}"
    # Every checkout the image copies is baked into it, so each must be merged code.
    held = next((why for source in sources if (why := _held(target, source, git))), "")
    if held:
        report.say(
            f"{name}: `{service}` runs code older than {sha} and was not redeployed -- {held}"
        )
        return {CODE_AT: known, HELD: held}
    ok, out = deploy_sources(target, sources, docker, git)
    if not ok:
        report.fail(
            f"{name}: could not redeploy `{service}` onto {sha} -- {first_line(out) or 'no output'}"
        )
        report.lines.extend(f"    {line}" for line in out.splitlines()[-20:])
        return {CODE_AT: known}
    report.say(f"{name}: redeployed `{service}` onto {sha} -- its code was older")
    return {CODE_AT: newest.committed, "deployed": True}


@dataclass(frozen=True)
class Source:
    """A checkout an image is built from, and the commit it is built at: `rev`, HEAD
    unless the target's PR gate pins this sibling, when `pin` is that ref as written and
    `rev` its commit here -- "" when this checkout does not have it."""

    root: Path
    sha: str
    committed: float
    rev: str = "HEAD"
    pin: str = ""


def source_heads(target: Target, git: Git) -> list[Source]:
    """A `Source` for the target's checkout and each it `builds_from`, leaving out any git
    cannot read -- except a pinned sibling, which is kept so the redeploy holds on it.

    8e3c4a67: the image took data-lake at its own HEAD, and twice in two days that HEAD
    was a commit whose models needed a migration ibkr_trader had not merged yet. The
    commit ibkr_trader's gate pins is the one its schema was tested against, so that is
    the commit to build, and the one whose time says whether the image is behind."""
    found: list[Source] = []
    if (head := git.head(target.checkout)) is not None:
        found.append(Source(target.checkout, *head))
    for sibling in target.collector.builds_from:
        root = target.checkout.parent / sibling
        pin = git.pin(target.checkout, sibling)
        if not pin:
            if (head := git.head(root)) is not None:
                found.append(Source(root, *head))
            continue
        rev = git.resolve(root, pin)
        commit = git.head(root, rev) if rev else None
        sha, committed = commit or (pin[:7], 0.0)
        found.append(Source(root, sha, committed, rev if commit else "", pin))
    return found


def _held(target: Target, source: Source, git: Git) -> str:
    """Why `source` must not be built by a timer, named for a sibling, or ""."""
    if source.pin and not source.rev:
        why = f"{target.checkout.name} pins {source.pin[:12]}, which this checkout does not have"
    else:
        why = git.held(source.root, source.rev)
    return _held_in(target, source.root, why) if why else ""


def deploy_sources(
    target: Target, sources: Sequence[Source], docker: Docker, git: Git
) -> tuple[bool, str]:
    """`Docker.deploy` the target from its checkouts as they stand, or -- when a sibling
    is pinned -- from a context staged at each source's commit (`collector_build`)."""
    service = target.collector.service
    if not any(source.pin for source in sources):
        return docker.deploy(target.checkout, service)
    with tempfile.TemporaryDirectory(
        prefix=f"{target.collector.project}-build-", ignore_cleanup_errors=True
    ) as staging:
        files, why = collector_build.stage(
            target.checkout,
            service,
            [(source.root, source.rev) for source in sources],
            Path(staging),
            git.export,
        )
        if why:
            return False, why
        return docker.deploy(target.checkout, service, files)


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


def not_restarting(
    docker: Docker, report: Report, restarts: Path, clock: float, wedged: str
) -> tuple[list[Container] | None, str] | None:
    """`revive_engine`'s answer for a confirmed-silent engine it will not restart this
    pass, or None when a restart is due. WSL's own service stuck (`WSL_STUCK`) restarts
    nothing: the restart and the VM reset both go through it, and only a person can
    unstick it (9e7dfa6f). A restart within `RESTART_EVERY` is not repeated, but resets
    the VM if nothing has since that restart (`VM_RESET_AT`)."""
    stuck = docker.wsl_service()
    if stuck in WSL_STUCK:
        report.say(f"{WSL_SERVICE} is {stuck}: no restart of Docker Desktop can reach its VM")
        return None, wsl_stuck_cause(stuck)
    last = last_restart(restarts)
    if last is None or not 0 <= clock - last < RESTART_EVERY:
        return None
    report.say(
        f"Docker Desktop was restarted {int((clock - last) // 60)} min ago; "
        f"not again within {RESTART_EVERY // 60} minutes of that"
    )
    if not vm_reset_since(restarts, last):
        containers = revive_vm_once(docker, report, restarts, clock)
        if containers is not None:
            return containers, ""
    return None, f"{wedged}, and a restart did not bring its engine back"


def engine_down(
    docker: Docker, report: Report, busy: Sequence[str] = ()
) -> tuple[str, str, Sequence[str]]:
    """`(how the silent engine is down, why no revival is tried, the runs a restart is
    held for)`; the second is "" when `revive_engine` should go on to try one. A Desktop
    not running for no failure is a person's to start, and a hung WSL is a person's to
    restart (`WSL_HUNG`). An engine that failed to start is held for no run in `busy`."""
    wedged = "docker is not answering though Docker Desktop says it is running"
    if not docker.desktop_running():
        stopped = docker.stop_reason()
        if FAILED_TO_START not in stopped.lower():
            return (
                wedged,
                "docker is not answering and Docker Desktop is not running -- start it",
                busy,
            )
        report.say(f"Docker Desktop says its engine stopped: {stopped}")
        wedged = "docker is not answering: Docker Desktop's engine failed to start"
        busy = ()
    return wedged, "" if docker.wsl_answers() else WSL_HUNG, busy


def revive_engine(
    docker: Docker, report: Report, restarts: Path, clock: float, busy: Sequence[str] = ()
) -> tuple[list[Container] | None, str]:
    """`(containers, why not)` for an engine `wait_for_engine` has already given up on.

    Only a *wedged* engine is restarted: Docker Desktop says `running` and its engine
    does not answer for `WEDGE_CONFIRM` more seconds -- one that answers in that time was
    slow, not wedged. A Desktop that is not running was most likely quit by a person, and
    a timer does not start it behind their back; the answer then says so. Unless Desktop
    itself says its engine `FAILED_TO_START` (`Docker.stop_reason`): that is a stop nobody
    asked for, revived like a wedge. One restart per `RESTART_EVERY`; `clock` is POSIX now.

    Nor is it restarted while a scheduled collector's run is in flight (`busy`, from
    `scheduled_in_flight`) and the wedge is new: its services may outlive a silent engine
    API, and the restart would stop them under it (6c504c34). That wait is no failure --
    `(None, "")`. A wedge held for `BUSY_HOLD` is restarted under the run, which it starves
    too: a run's end never comes when the next has started by then (0c428d0e). An engine
    that failed to start runs nothing a restart could stop, so is never held (930844fd).
    Nor where `not_restarting` has another answer.

    Nor while Docker Desktop installs an update of itself (`Docker.updating`): its engine
    is down by design until the installer starts the new app, a restart would cut the
    install short, and the installer may have closed the app it reads as quit. That is
    held, asked first, and fails nothing until it has lasted `HOLD_LIMIT` (3012d246).

    Nor when WSL itself does not answer (`Docker.wsl_answers`): every step of a revival
    goes through it, so each waits out its timeout and the pass outlives its schedule to
    fail anyway (5b71506f). That fails at once on `WSL_HUNG`, a person's step.

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
    wedged, refused, busy = engine_down(docker, report, busy)
    if refused:
        return None, refused
    containers = wait_for_engine(docker, ENGINE_STARTUP - WEDGE_CONFIRM)
    if containers is not None:
        report.say("docker was slow to answer, not wedged -- it answered when asked again")
        return containers, ""
    instead = not_restarting(docker, report, restarts, clock, wedged)
    if instead is not None:
        return instead
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


def needs_engine(chosen: Sequence[Target]) -> bool:
    """Whether this machine needs Docker's engine answering for what it runs: a container
    collector assigned `run`, or a scheduled one whose command `needs` compose services.

    The second is no less a need for keeping nothing up itself: on 2026-10-12 a pass whose
    only container collector was `stop` read a dead engine as "nothing here can be
    running", 0 failures, while every fire of social-scraper failed on it (08ec2a47)."""
    return any(
        t.mode == config.RUN and (t.collector.needs or not t.collector.scheduled) for t in chosen
    )


def reach_engine(
    docker: Docker, report: Report, awake: float | None, needed: bool
) -> list[Container] | None:
    """The engine's containers, once `wait_for_engine` has given it `awake` to start; an
    engine silent past that is `revive_engine`'s where `needed` and `docker.restarts` is
    set. None when it does not answer, failed where `needed` and said where not."""
    containers = wait_for_engine(docker, awake)
    why = "docker is not answering -- is Docker Desktop running?"
    if containers is None and needed and docker.restarts is not None:
        short = docker.memory_evidence(_clock() - docker_memory.OOM_LOOKBACK)
        if short:
            report.say(f"  {short}")
            report.fail(SHORT_OF_MEMORY)
        busy = docker.holds() if docker.holds else ()
        containers, why = revive_engine(docker, report, docker.restarts, _clock(), busy)
    if containers is None:
        if not needed:
            report.say("docker is not answering, so nothing here can be running")
        elif why:
            report.fail(why)
    return containers


def maintain(
    chosen: Sequence[Target],
    docker: Docker,
    report: Report,
    health: dict,
    git: Git | None = None,
    awake: float | None = None,
) -> None:
    """One pass over the assigned *container* collectors, recording health verdicts into
    `health`. The scheduled ones in `chosen` are `maintain_scheduled`'s, and count here
    only for the engine their `needs` run in (`needs_engine`). `awake`
    is how long the machine has been up, which `wait_for_engine` gives Docker to start;
    an engine silent past that is `revive_engine`'s where `docker.restarts` is set, held
    for whatever `docker.holds` names for up to `BUSY_HOLD`. A held restart fails nothing until `HOLD_LIMIT`.

    Memory is checked on both sides (`docker_memory`): a silent engine whose VM Windows
    says ran out fails as `SHORT_OF_MEMORY` before it is restarted, and an answering one
    has the limits of what it keeps up checked (`check_memory`). An answering engine's
    VM also has its file cache dropped (`reclaim_vm_memory`), by the scheduled pass
    alone -- the one that carries `docker.restarts`."""
    mine = [t for t in chosen if not t.collector.scheduled]
    needed = needs_engine(chosen)
    if not mine and not needed:
        return
    containers = reach_engine(docker, report, awake, needed)
    if containers is None:
        return
    if docker.restarts is not None:
        release_hold(docker.restarts)
        reclaim_vm_memory(docker, report)
    check_memory(mine, containers, docker, report)
    for target in mine:
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


def lock_holder(path: Path, clock: float) -> float | None:
    """Until when a live pass holds `PASS_LOCK` at `path`, or None when none does.

    A lock past its `until` is a killed pass's. One that will not parse is a holder
    between creating and writing it for `LOCK_WRITING` seconds, and a dead one after.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raw = None
    until = raw.get(UNTIL) if isinstance(raw, dict) else None
    if isinstance(until, (int, float)) and not isinstance(until, bool):
        return until if clock < until else None
    try:
        written = path.stat().st_mtime
    except OSError:
        return None
    return written + LOCK_WRITING if clock - written < LOCK_WRITING else None


def take_lock(path: Path, until: float, clock: float, extra: dict | None = None) -> bool:
    """Create `path` holding `until`, and `extra` beside it (`move`'s `HOLDER` and
    `PROJECTS`); False while a live pass holds it.

    A dead holder's lock is broken first. Two passes breaking the same dead lock at once
    can both get through, which is no worse than the unserialised passes this replaced
    and needs a pass to have been killed first.
    """
    if lock_holder(path, clock) is None:
        path.unlink(missing_ok=True)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(handle, "w", encoding="utf-8") as lock:
        lock.write(json.dumps({**(extra or {}), STARTED_AT: clock, UNTIL: until}) + "\n")
    return True


def move_holding(path: Path, clock: float) -> dict[str, str]:
    """`{project: holder}` for each collector a live `move` holds `PASS_LOCK` for; {}
    while a pass holds it, or nothing does. The tray's reason to keep a collector `move`
    stopped for its export green (`holding`)."""
    if lock_holder(path, clock) is None:
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    holder = raw.get(HOLDER) if isinstance(raw, dict) else None
    projects = raw.get(PROJECTS) if isinstance(raw, dict) else None
    if not isinstance(holder, str) or not isinstance(projects, list):
        return {}
    return {str(project): holder for project in projects}


@contextlib.contextmanager
def one_pass(
    path: Path,
    bound: float,
    wait: bool,
    sleep: Callable[[float], None] = _time.sleep,
    extra: dict | None = None,
) -> Iterator[bool]:
    """Hold `PASS_LOCK` for a pass of at most `bound` seconds; yields whether it was had.
    `extra` is written into the lock beside its bound (`take_lock`).

    Without `wait` a held lock yields False at once. With it, this looks again every
    `LOCK_POLL` seconds, which ends by the holder's `until` at the latest: a lock past it
    is broken.
    """
    held = take_lock(path, _clock() + bound, _clock(), extra)
    if not held and wait:
        print("collectors: waiting for the collectors pass acting now to finish", file=sys.stderr)
    while not held and wait:
        sleep(LOCK_POLL)
        held = take_lock(path, _clock() + bound, _clock(), extra)
    if not held:
        yield False
        return
    try:
        yield True
    finally:
        path.unlink(missing_ok=True)


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


# --- handing a collector to another machine -----------------------------------------


@dataclass
class Here:
    """This machine, as a handoff acts on it: its devkit checkout (`base`), the directory
    its project checkouts sit in, its name (`handoff.this_host`), and what it spawns
    through."""

    base: Path
    workspace: Path
    name: str
    docker: Docker
    handoffs: handoff.Handoff
    report: Report

    def checkout(self, collector: config.Collector) -> Path:
        return self.workspace / collector.project

    @property
    def assignment(self) -> Path:
        return self.base / config.ASSIGNMENT

    @property
    def outgoing(self) -> Path:
        return self.base / handoff.OUTGOING


def set_mode(here: Here, project: str, mode: str) -> None:
    """Assign `project` `mode` on this machine, rereading the file so nothing else in it
    is undone."""
    path = here.assignment
    config.save_assignment(path, config.assign(config.load_assignment(path), [project], mode))


def refusal(collector: config.Collector, assignment: dict[str, str], here: Here) -> str:
    """Why `move` will not hand `collector` off from here, or ""."""
    name = collector.project
    if not collector.handoff:
        return (
            f"{name}: declares no `handoff`, so its data cannot go with it -- move it by hand: "
            f"`collectors.py stop-here {name}` here, then `collectors.py run-here {name}` there"
        )
    mode = assignment.get(name)
    if mode != config.RUN:
        said = f"`{mode}`" if mode else "hands off"
        return f"{name}: this machine is not set to run it ({said}) -- `move` it from the one that does"
    if not (here.checkout(collector) / ".git").exists():
        return f"{name}: no checkout at {here.checkout(collector)} -- nothing to export from"
    return ""


def move(
    projects: Sequence[str],
    to: str | None,
    collectors: Sequence[config.Collector],
    assignment: dict[str, str],
    here: Here,
) -> None:
    """`move PROJECT.. --to HOST`: hand each to HOST, data and all (`move_one`). Refused
    whole for a missing or self-naming `--to`, a missing or misspelt name; per collector
    for one this machine does not run, or one without a `handoff`."""
    dest = handoff.host_key(to or "")
    picked, unknown = config.pick(list(collectors), list(projects))
    if why := move_refused(dest, projects, unknown, here.name):
        here.report.fail(why)
        return
    for collector in picked:
        if why := refusal(collector, assignment, here):
            here.report.fail(why)
        else:
            move_one(collector, dest, here)


def move_refused(dest: str, projects: Sequence[str], unknown: Sequence[str], this: str) -> str:
    """Why `move` acts on nothing at all, or "": no `--to`, a `--to` naming this machine
    (`this`), no collector named, or one misspelt."""
    if not dest:
        return "`move` needs `--to HOST`: the machine to hand the collector to"
    if dest == this:
        return f"`--to {dest}` names this machine -- name the one to hand it to"
    if not projects:
        return "`move` takes the collectors to move by name"
    if unknown:
        return f"not declared in `{config.SETTING}`: {', '.join(unknown)}"
    return ""


def move_one(collector: config.Collector, dest: str, here: Here) -> None:
    """Stop the collector's service, have the project export its data for `dest`, then
    set this machine to `stop` and stop the rest of the project here (`handed_off`). An
    export that fails starts the service again and leaves the assignment `run`.

    `PASS_LOCK` is held throughout, waited for as a verb waits for it: a scheduled pass
    meanwhile stands down rather than restarting the service under the dump. The lock
    names this move and the collector (`move_holding`), so the tray keeps its row green."""
    name, service = collector.project, collector.service
    checkout = here.checkout(collector)
    holder = {HOLDER: f"`collectors.py move` to {dest}", PROJECTS: [name]}
    with one_pass(here.base / PASS_LOCK, MOVE_BOUND, wait=True, extra=holder) as had:
        if not had:
            here.report.fail(f"{name}: not moved -- {STOOD_DOWN}")
            return
        if why := stop_service(collector, here):
            here.report.fail(f"{name}: not moved -- `{service}` could not be stopped first: {why}")
            return
        print(
            f"collectors: exporting {name}'s data for {dest}, for up to "
            f"{handoff.EXPORT_TIMEOUT // 60} minutes; its account is {checkout / handoff.PROJECT_LOG}",
            flush=True,
        )
        said = here.handoffs.export(collector, checkout, here.name, dest)
        if said.code == 0:
            handed_off(collector, dest, here)
            return
        ok, out = here.docker.up(checkout, service)
        again = (
            f"`{service}` is started again"
            if ok
            else f"`{service}` could NOT be started again ({first_line(out) or 'no output'})"
        )
        here.report.fail(
            f"{name}: not moved -- the handoff export failed ({said.summary()}); see "
            f"{checkout / handoff.PROJECT_LOG}. {again}, and this machine still runs it"
        )


def stop_service(collector: config.Collector, here: Here) -> str:
    """Stop the collector's own container, leaving the rest of its project up; "" once it
    is not running, else why it could not be stopped. `docker stop` by container, as
    `keep_stopped` does: ibkr_trader's `app` sits behind a compose profile, which naming it
    activates for `up` but which `compose stop` is not to be trusted with."""
    containers = here.docker.ps()
    if containers is None:
        return "docker is not answering"
    box = find(containers, here.checkout(collector), collector.service)
    if box is None or not box.running:
        return ""
    ok, out = here.docker.stop(box)
    return "" if ok else f"`docker stop` said {first_line(out) or 'nothing'}"


def handed_off(collector: config.Collector, dest: str, here: Here) -> None:
    """After an export that succeeded: this machine `stop`s it, remembers where it went
    (`handoff.OUTGOING`, which `follow_handoffs` reads), and stops every other container
    the project runs here -- its db, and anything behind a profile -- to give the memory
    back. `docker stop`, never `down`: the volumes hold the data, should it come back."""
    name = collector.project
    set_mode(here, name, config.STOP)
    handoff.record_outgoing(here.outgoing, name, dest, _clock())
    here.report.say(
        f"{name}: handed off to {dest}, and set to `stop` here. {dest} takes it over on its "
        "next collectors pass -- within 15 minutes, if it is on; `collectors.py` here says when"
    )
    containers = here.docker.ps()
    if containers is None:
        here.report.fail(
            f"{name}: handed off, but docker did not answer, so its containers are still up here"
        )
        return
    for box in containers:
        if box.running and same_dir(box.workdir, here.checkout(collector)):
            ok, out = here.docker.stop(box)
            if not ok:
                here.report.fail(
                    f"{name}: handed off, but `{box.service}` could not be stopped here -- "
                    f"{first_line(out) or 'no output'}"
                )


def pick_up(
    collectors: Sequence[config.Collector], assignment: dict[str, str], here: Here
) -> dict[str, str]:
    """Take over each collector handed to this machine (`take_over`), before the pass acts
    on its assignment; returns the assignment to act on, so one taken over is started this
    same pass through `keep_running`, settle and all, as `run-here` would start it.

    Asked for every collector with a `handoff` this machine is not set to `run`: set to
    `stop`, **or not assigned at all**. A marker addressed to this machine by name is as
    explicit as `run-here` typed here, and newer than whatever left it hands off; asking
    only `stop` machines would leave a fresh PC unable to receive one until someone stopped
    there a collector that was never running. That narrows "a machine assigned nothing
    spawns nothing" to docker: a machine with a checkout of a project that declares a
    handoff asks its store once a pass. Without the checkout there is nothing to ask
    with, and nothing is asked.

    A machine set to `run` it is not asked: an import there would restore over the data
    its own collector is writing."""
    for collector in collectors:
        if not collector.handoff or assignment.get(collector.project) == config.RUN:
            continue
        if (here.checkout(collector) / ".git").exists() and take_over(collector, here):
            assignment = {**assignment, collector.project: config.RUN}
    return assignment


def take_over(collector: config.Collector, here: Here) -> bool:
    """Import a handoff addressed to this machine and set it to `run` the collector; True
    when it did. A store that cannot be read, or no answer in time, is silence -- the next
    pass asks again, as `fix_migrate.py` does of a db that is down. A failed import is a
    failure of the pass and assigns nothing, so the next pass tries again. It runs under
    the pass's `PASS_LOCK`, whose bound allows for it (`pass_bound`), so a second import --
    a pass by hand, or the next scheduled one -- cannot start beside it."""
    name, checkout = collector.project, here.checkout(collector)
    pending = here.handoffs.pending(collector, checkout, here.name)
    if pending.state == handoff.BROKEN:
        here.report.say(f"{name}: could not ask whether a handoff waits ({pending.said.summary()})")
    if pending.state != handoff.WAITING:
        return False
    if why := stop_service(collector, here):
        here.report.say(
            f"{name}: {pending.source} handed it here, but `{collector.service}` could not be "
            f"stopped for the import: {why} -- the next pass tries again"
        )
        return False
    said = here.handoffs.restore(collector, checkout, here.name)
    if said.code != 0:
        here.report.fail(
            f"{name}: handed to this machine, and its import failed ({said.summary()}) -- "
            f"see {checkout / handoff.PROJECT_LOG}; nothing assigned, the next pass tries again"
        )
        here.report.lines.extend(f"    {line}" for line in said.tail())
        return False
    set_mode(here, name, config.RUN)
    handoff.forget_outgoing(here.outgoing, name)
    here.report.say(f"{name}: taken over from {pending.source} -- its data is restored here")
    return True


def follow_handoffs(
    collectors: Sequence[config.Collector], assignment: dict[str, str], here: Here
) -> None:
    """Ask, of each handoff this machine sent (`handoff.OUTGOING`), whether its receiver
    has taken it over; forget it once it has, and fail the pass once that is
    `HANDOFF_GRACE` overdue. Until then nothing runs the collector, and no row on either
    machine says so: here it is `stop` by choice, there it is not yet `run`. One this
    machine runs again, or no longer declares with a handoff, is forgotten unasked."""
    declared = {c.project: c for c in collectors if c.handoff}
    for name, record in handoff.load_outgoing(here.outgoing).items():
        collector, dest = declared.get(name), record[handoff.TO]
        if collector is None or assignment.get(name) == config.RUN:
            handoff.forget_outgoing(here.outgoing, name)
            continue
        pending = here.handoffs.pending(collector, here.checkout(collector), dest)
        if pending.state == handoff.NONE:
            handoff.forget_outgoing(here.outgoing, name)
            here.report.say(f"{name}: {dest} has taken it over")
        elif pending.state != handoff.WAITING:
            here.report.say(
                f"{name}: could not ask whether {dest} has taken it over ({pending.said.summary()})"
            )
        elif _clock() - record[handoff.AT] >= HANDOFF_GRACE:
            here.report.fail(
                f"{name}: handed off to {dest}, which has not taken it over within "
                f"{HANDOFF_GRACE // 60} minutes -- nothing runs it until it does. Is {dest} on, "
                "with devkit-collectors running?"
            )
        else:
            here.report.say(
                f"{name}: handed off to {dest}; waiting for its next pass to take it over"
            )


def handoff_status(
    collectors: Sequence[config.Collector], assignment: dict[str, str], here: Here
) -> None:
    """`status`'s lines on handoffs, read-only: one sent from here and not yet taken over,
    and one addressed to this machine and waiting. Asks the project's store once for each,
    and only of a collector with a `handoff` and a checkout here."""
    sent = handoff.load_outgoing(here.outgoing)
    for collector in collectors:
        name, checkout = collector.project, here.checkout(collector)
        if not collector.handoff or not (checkout / ".git").exists():
            continue
        if name in sent:
            dest = sent[name][handoff.TO]
            state = here.handoffs.pending(collector, checkout, dest).state
            said = {handoff.NONE: "taken over", handoff.WAITING: "not taken over yet"}
            here.report.say(
                f"{name}: handed off to {dest} -- {said.get(state, 'the store could not say')}"
            )
        if assignment.get(name) != config.RUN:
            pending = here.handoffs.pending(collector, checkout, here.name)
            if pending.state == handoff.WAITING:
                here.report.say(
                    f"{name}: handed to this machine by {pending.source} -- the next pass takes it over"
                )
            elif pending.state != handoff.NONE:
                here.report.say(
                    f"{name}: could not ask whether a handoff waits here ({pending.said.summary()})"
                )


# --- the tray's half --------------------------------------------------------------


def row(
    target: Target,
    containers: Sequence[Container] | None,
    health: dict,
    busy: bool = False,
    held: str = "",
) -> tuple[str, str]:
    """`(level, detail)` for one assigned collector. `busy` is a `maintain` pass at work
    (`in_pass`), which is starting whatever it finds down: green until it is done, and a
    start that fails fails that pass. `held` names a `move` holding `PASS_LOCK` for this
    collector, which stopped it for its export -- green on the same terms. A `stop`
    one this machine handed off (`held`, then, says to whom) is green and says so."""
    if target.mode == config.STOP:
        box = find(containers or [], target.checkout, target.collector.service)
        if box is not None and box.running:
            return WARN, "running here, but this machine is set to stop it"
        return OK, held or "off on this machine (by choice)"
    if containers is None:
        return FAIL, NOT_ANSWERING
    box = find(containers, target.checkout, target.collector.service)
    if box is None or not box.running:
        return down_row(box, busy, held)
    verdict = health.get(target.collector.project, {})
    if verdict.get("container") == box.id and verdict.get("ok") is False:
        jobs = verdict.get("unhealthy")
        named = f": {', '.join(map(str, jobs))}" if isinstance(jobs, list) and jobs else ""
        return WARN, f"{HEALTH_FAILING}{named} -- {verdict.get('summary', '')}"
    return OK, f"running ({box.status})"


def down_row(box: Container | None, busy: bool, held: str = "") -> tuple[str, str]:
    """`row`'s answer for a `run` collector with no running container: red, unless a
    pass is at work (`busy`), which is starting it, or a `move` is exporting it (`held`)."""
    if busy:
        return OK, f"{STARTING} ({box.status if box else 'no container yet'})"
    if held:
        return OK, f"{held} is acting on it ({box.status if box else 'no container'})"
    if box is None:
        return FAIL, "no container -- see logs/collectors.log"
    return FAIL, f"not running ({box.status})"


def holding(target: Target, moving: dict[str, str], sent: dict[str, dict]) -> str:
    """`row`'s `held` for `target`: for a `run` one, the `move` exporting it (`moving`,
    `move_holding`); for a `stop` one, the machine this one handed it to (`sent`,
    `handoff.OUTGOING`). Local files only -- the tray asks the store nothing."""
    if target.mode == config.RUN:
        return moving.get(target.collector.project, "")
    record = sent.get(target.collector.project)
    if record is None:
        return ""
    return f"handed off to {record[handoff.TO]} -- it takes it over on its next collectors pass"


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
        clock = _clock()
        busy = in_pass(base / IN_PASS, clock)
        sent = handoff.load_outgoing(base / handoff.OUTGOING)
        moving = move_holding(base / PASS_LOCK, clock)
        rows += [
            (
                ROW_PREFIX + t.collector.project,
                *row(t, containers, health, busy, holding(t, moving, sent)),
            )
            for t in chosen
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

# Hands collectors to another machine, data and all (`move`). Not one of `VERBS`: it
# takes a host as well, and assigns this machine only once the export has succeeded.
MOVE = "move"


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
        choices=("status", "maintain", MOVE, *SINGLE, *VERBS, NOTHING),
        help=(
            "status: report only (default). maintain: what the scheduler runs. run-here / "
            "stop-here: assign this machine, then act on it now. release: forget. "
            "move NAME.. --to HOST: hand them, data and all, to HOST, which takes them over "
            "on its next pass. "
            "fire NAME: run one scheduled collector once (what its own task runs). "
            "run-once NAME: the same, by hand, its output in this terminal. "
            "Also takes `<mode>:<project>`, the VS Code picker's spelling."
        ),
    )
    parser.add_argument("projects", nargs="*", help="collectors to assign (default: all)")
    parser.add_argument(
        "--to", help="move: the machine to hand them to, by its hostname (any case)"
    )
    parser.add_argument("--devkit", type=Path, default=REPO_ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(split_pick(sys.argv[1:] if argv is None else argv))
    if args.to is not None and args.mode != MOVE:
        parser.error("--to is for `move` only")
    return args


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
    sent = handoff.load_outgoing(base / handoff.OUTGOING)
    for collector in picked:
        report.say(
            f"{collector.project}: {'released' if mode is None else mode + ' on this machine'}"
        )
        if mode == config.RUN and collector.project in sent:
            dest = sent[collector.project][handoff.TO]
            report.say(
                f"  warning: it was handed off to {dest}. If {dest} has not taken it over yet "
                f"it still will, and then both run it -- `stop-here` it there once it has"
            )
    return updated


def fire(
    name: str,
    base: Path,
    now: _dt.datetime,
    spawner: collector_tasks.Spawner | None = None,
    desktop_logs: Path | None = None,
) -> int:
    """`fire <name>`: what a scheduled collector's task runs. Exits with the command's code.

    Writes `logs/collector-<name>.log` and never `ARTIFACT`, which belongs to the pass, and
    records a run's start before its command runs (`collector_tasks.start_path`). A
    task left behind on a machine no longer assigned `run` -- the pass that deletes it has
    not come round yet -- does nothing and says so, rather than running a second writer.
    `desktop_logs` is where Docker Desktop's own logs are read for why a silent engine
    stopped (`collector_tasks.stop_reason`); the command line passes this machine's.
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
            collector,
            checkout,
            spawner or collector_tasks.spawn,
            fired=now,
            desktop_logs=desktop_logs,
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
    # This machine's Docker Desktop logs go with this machine's docker: a spawner a test
    # hands in answers for an engine those logs know nothing about.
    logs = docker_memory.log_dir() if spawner is None else None
    return fire(args.projects[0], base, now, spawner, logs)


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
    handoffs: handoff.Handoff | None = None,
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
    workspace = sweep.default_workspace(base).parent

    def in_flight() -> list[str]:
        """The scheduled runs a restart would cut short, by the assignment as it stands."""
        now_assigned = config.load_assignment(base / config.ASSIGNMENT)
        return scheduled_in_flight(targets(collectors, now_assigned, workspace), run)

    engine = docker or Docker(restarts=base / RESTARTED, holds=in_flight)
    here = Here(base, workspace, handoff.this_host(), engine, handoffs or handoff.Handoff(), report)
    if args.mode == MOVE:
        move(args.projects, args.to, collectors, assignment, here)
    elif args.mode == "status":
        status(collectors, targets(collectors, assignment, workspace), engine, report, base, run)
        handoff_status(collectors, assignment, here)
    else:
        run_pass(args, collectors, assignment, here, run)
    return finish()


def pass_bound(
    collectors: Sequence[config.Collector], assignment: dict[str, str], picks_up: bool
) -> float:
    """How long a pass acting on `assignment` may hold `PASS_LOCK`: a `TARGET_BOUND` for
    each container collector it may act on, and -- for a `maintain`, which `picks_up` --
    a `PICKUP_BOUND` and a `pending` for each it may take over, counting one it is not yet
    assigned among the targets. A lock that expired mid-restore would let the next pass
    break it and start a second restore into the same database."""
    containers = [c for c in collectors if not c.scheduled]
    takers = [
        c for c in containers if picks_up and c.handoff and assignment.get(c.project) != config.RUN
    ]
    acted_on = {c.project for c in containers if c.project in assignment}
    acted_on |= {c.project for c in takers}
    pickups = len(takers) * (PICKUP_BOUND + handoff.PENDING_TIMEOUT)
    return PS_TIMEOUT + max(1, len(acted_on)) * TARGET_BOUND + pickups


def run_pass(
    args: argparse.Namespace,
    collectors: Sequence[config.Collector],
    assignment: dict[str, str],
    here: Here,
    run: collector_tasks.Runner,
) -> None:
    """`maintain`, `run-here` and `stop-here`, under `PASS_LOCK`: a verb waits for it, a
    scheduled `maintain` that finds it held stands down. A `maintain` takes over what was
    handed to this machine (`pick_up`) and follows what it handed off (`follow_handoffs`)
    inside the lock, so a `move` holding it is never raced, and a restore is never run
    twice at once."""
    if args.mode in VERBS:
        acted_on = apply_verb(args, here.base, list(collectors), run, here.report)
        if acted_on is None:
            return
        assignment = acted_on
    bound = pass_bound(collectors, assignment, picks_up=args.mode == "maintain")
    with one_pass(here.base / PASS_LOCK, bound, wait=args.mode in VERBS) as had:
        if not had:
            here.report.say(STOOD_DOWN)
            return
        if args.mode == "maintain":
            assignment = pick_up(collectors, assignment, here)
            follow_handoffs(collectors, assignment, here)
        chosen = targets(collectors, assignment, here.workspace)
        if not chosen:
            here.report.say("no collector is assigned to this machine -- nothing to do")
        act(chosen, here.base, here.docker, here.report, run)


def act(
    chosen: Sequence[Target],
    base: Path,
    engine: Docker,
    report: Report,
    run: collector_tasks.Runner,
) -> None:
    """The part of a `maintain` or a verb that starts and stops things, under `PASS_LOCK`."""
    maintain_scheduled([t for t in chosen if t.collector.scheduled], base, report, run)
    health = load_health(base / HEALTH)
    in_containers = [t for t in chosen if not t.collector.scheduled]
    since = machine_clock.awake_since()
    awake = None if since is None else _time.time() - since
    with acting(base / IN_PASS, in_containers):
        maintain(chosen, engine, report, health, awake=awake)
    write_file(base / HEALTH, json.dumps(health, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    sys.exit(main())
