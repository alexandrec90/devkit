#!/usr/bin/env python3
"""Every project's dashboard on its own port, awake only while someone is using it.

A dashboard is wanted almost all the time and used a few minutes at a stretch, and memory
on these PCs is tight: every container shares one capped Docker VM, and a server left up
all day holds memory nobody is using. So the dashboards do not stay up. One small stdlib
process, this gateway, holds each dashboard's **public** port on 127.0.0.1. The first
request starts the dashboard behind it and is forwarded once it answers. When no bytes
have passed for the dashboard's `idleMinutes` (30 by default), the dashboard is stopped,
and the next request starts it again. The gateway forwards TCP and knows nothing of HTTP
beyond the probe that says a dashboard is up, so the project's framework is the project's
business.

**Which dashboards exist** is `devkit.dashboards` in devkit's `projects.toml`, beside
`devkit.collectors`, keyed by project. An entry is one of two kinds:

- **a host command** (`command`): argv run from the project's checkout, carrying a
  `{port}` token that the gateway fills with a free loopback port for each wake.
  `{python}` is the gateway's console interpreter, for a project script that is stdlib
  until it hands itself to the project's `.venv`. On a merge it is **redeployed**: the
  gateway stops it once the checkout's HEAD moves on the default branch, and the next
  request starts the merged code. A feature branch checked out there, or an uncommitted
  edit, changes nothing (`should_redeploy`), the rule d2-trader's `release.py` chose.
- **a compose service** (`service` and `backendPort`): `docker compose up -d --wait` on
  wake, `docker compose stop` on sleep, forwarded to the host port the service publishes.
  Redeploying a container stays the project's own business.

`port` is the public port. Without one it is the project's `app` port in `ports.toml`
(base plus its slot), so a project with a registered slot needs nothing written. `path` is
what the readiness probe asks for (any HTTP answer counts, a 404 included).
`startSeconds` bounds a wake.

A connection wakes nothing until the client sends a byte, so a port scan, a `status`
check or a browser's speculative preconnect leaves an asleep dashboard asleep. A request
that cannot be served gets a `503` that names why, and the same line goes to
`logs/dashboards.log`. A dashboard's own output from its latest wake is in
`logs/dashboards/<project>.log`, and a wake that dies before serving names the line of it
that says why (`build_cause`).

A host command runs under the Node its checkout's frontend asks for
(`node_toolchain.ensure`): PATH's when it satisfies the pin or `engines.node`, else a
per-user copy, fetched once. When none can be had the 503 says `node <have> < <need>`.

On Windows the gateway puts itself in a job object that kills its children when it ends,
so a dashboard never outlives its gateway holding memory on a port nothing forwards to.

    dashboards.py                 # status: each dashboard's URL, and whether it is awake
    dashboards.py serve           # the gateway: what `devkit-dashboards` runs at logon
    dashboards.py open PROJECT    # its page in the browser, which wakes it

`install-dashboards.py` registers the task. Tested in `tests/test_dashboards.py`.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devkit_ports
import node_toolchain
import project_registry
import sweep

REPO_ROOT = Path(__file__).resolve().parents[1]

SETTING = "devkit.dashboards"

HOST = "127.0.0.1"
DEFAULT_IDLE_MINUTES = 30
DEFAULT_START_SECONDS = 120
DEFAULT_PATH = "/"
# The `ports.toml` service whose port a dashboard takes when its entry names none.
DEFAULT_SERVICE = "app"

# The placeholders a `command` carries (module docstring).
PORT_SLOT = "{port}"
PYTHON_SLOT = "{python}"

ARTIFACT = Path("logs/dashboards.log")
STATE = Path("logs/dashboards.state.json")
OUTPUT_DIR = Path("logs/dashboards")
# The log is appended for the life of the gateway, so it is started over past this.
ARTIFACT_MAX_BYTES = 1_000_000

TICK_SECONDS = 30
PROBE_INTERVAL = 0.5
PROBE_TIMEOUT = 2.0
# How long an accepted connection may send nothing before it is dropped unserved.
FIRST_BYTE_SECONDS = 60
CHUNK = 65536

NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)
WINDOWS = os.name == "nt"


@dataclass(frozen=True)
class Dashboard:
    """One project's dashboard, as `devkit.dashboards` declares it (module docstring)."""

    project: str
    port: int
    command: tuple[str, ...] = ()
    service: str = ""
    backend_port: int = 0
    path: str = DEFAULT_PATH
    idle_minutes: int = DEFAULT_IDLE_MINUTES
    start_seconds: int = DEFAULT_START_SECONDS

    @property
    def container(self) -> bool:
        return bool(self.service)

    @property
    def url(self) -> str:
        return f"http://{HOST}:{self.port}{self.path}"


# --- the setting -----------------------------------------------------------------------


def _positive(raw: object) -> int | None:
    """A positive int, refusing a bool, which JSON's `true` would otherwise pass as 1."""
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        return None
    return raw


def _port(raw: object) -> int | None:
    value = _positive(raw)
    return value if value is not None and value < 65536 else None


# Each helper below answers `(fields, "")`, or `({}, why)` naming the first thing wrong.
Fields = tuple[dict[str, Any], str]


def _command_fields(project: str, command: object) -> Fields:
    if not isinstance(command, list) or not all(isinstance(p, str) and p for p in command):
        return {}, f"{project}: `command` must be a list of non-empty strings"
    if not any(PORT_SLOT in part for part in command):
        return {}, f"{project}: `command` must carry {PORT_SLOT}, the port it serves on"
    return {"command": tuple(command)}, ""


def _service_fields(project: str, service: object, backend_raw: object) -> Fields:
    backend = _port(backend_raw)
    if not isinstance(service, str) or not service or backend is None:
        return {}, f"{project}: a `service` needs `backendPort`, the port it publishes"
    return {"service": service, "backend_port": backend}, ""


def _kind_fields(project: str, raw: Mapping[str, Any]) -> Fields:
    """The host-command or compose-service half of an entry: exactly one of them."""
    command = raw.get("command")
    service = raw.get("service")
    if (command is None) == (service is None):
        return {}, f"{project}: give exactly one of `command` and `service`"
    if command is not None:
        return _command_fields(project, command)
    return _service_fields(project, service, raw.get("backendPort"))


def _public_port(
    project: str, raw: Mapping[str, Any], default_port: Callable[[str], int | None]
) -> tuple[int | None, str]:
    if "port" not in raw:
        port = default_port(project)
        return (
            port,
            "" if port is not None else f"{project}: no `port`, and no slot for it in ports.toml",
        )
    port = _port(raw["port"])
    return port, "" if port is not None else f"{project}: `port` must be a port number"


def _option_fields(project: str, raw: Mapping[str, Any]) -> Fields:
    fields: dict[str, Any] = {}
    for key, name in (("idleMinutes", "idle_minutes"), ("startSeconds", "start_seconds")):
        if key not in raw:
            continue
        value = _positive(raw[key])
        if value is None:
            return {}, f"{project}: `{key}` must be a positive whole number"
        fields[name] = value
    if "path" in raw:
        path = raw["path"]
        if not isinstance(path, str) or not path.startswith("/"):
            return {}, f"{project}: `path` must start with /"
        fields["path"] = path
    return fields, ""


def parse_entry(
    project: str, raw: object, default_port: Callable[[str], int | None]
) -> tuple[Dashboard | None, str]:
    """`(dashboard, "")`, or `(None, why)` for an entry that declares nothing usable."""
    if not isinstance(raw, dict):
        return None, f"{project}: an entry must be an object"
    fields, why = _kind_fields(project, raw)
    if why:
        return None, why
    port, why = _public_port(project, raw, default_port)
    if port is None:
        return None, why
    if fields.get("backend_port") == port:
        return None, f"{project}: `backendPort` and `port` must differ"
    options, why = _option_fields(project, raw)
    if why:
        return None, why
    return Dashboard(project=project, port=port, **fields, **options), ""


def parse_setting(
    raw: object, default_port: Callable[[str], int | None]
) -> tuple[list[Dashboard], list[str]]:
    """`(dashboards, notes)` out of `SETTING`'s value, as stored. Never raises.

    A malformed entry is a note, not silence: a dashboard dropped quietly reads exactly
    like a dashboard nobody declared. Two entries on one port keep the first.
    """
    if raw is None:
        return [], []
    if not isinstance(raw, dict):
        return [], [f"`{SETTING}` must be an object keyed by project name"]
    found: list[Dashboard] = []
    notes: list[str] = []
    taken: dict[int, str] = {}
    for project, entry in raw.items():
        dashboard, note = parse_entry(project, entry, default_port)
        if dashboard is None:
            notes.append(note)
            continue
        ports = [dashboard.port] + ([dashboard.backend_port] if dashboard.container else [])
        clash = next((taken[p] for p in ports if p in taken), None)
        if clash is not None:
            notes.append(f"{project}: its port is already {clash}'s")
            continue
        taken.update(dict.fromkeys(ports, str(project)))
        found.append(dashboard)
    return found, notes


def registry_port(root: Path) -> Callable[[str], int | None]:
    """A project's `app` port from `ports.toml`, or None where it has no slot."""
    try:
        registry = devkit_ports.load(root)
    except (OSError, ValueError):
        return lambda _project: None

    def port(project: str) -> int | None:
        try:
            return registry.ports_for(project, [DEFAULT_SERVICE])[DEFAULT_SERVICE]
        except (devkit_ports.RegistryError, KeyError):
            return None

    return port


def home(root: Path = REPO_ROOT) -> Path:
    """The checkout whose `logs/` this writes: `$DEVKIT_DIR`, else the static checkout."""
    named = (os.environ.get("DEVKIT_DIR") or "").strip()
    if named and Path(named).is_dir():
        return Path(named)
    return sweep.source_checkout(root)


def declared(root: Path) -> tuple[list[Dashboard], list[str]]:
    """The dashboards the devkit checkout at `root` declares in its `projects.toml`, with
    any notes -- one naming the file when it is missing or does not parse."""
    try:
        raw = project_registry.setting(SETTING, repo_root=root)
    except (project_registry.RegistryError, OSError) as exc:
        return [], [str(exc)]
    return parse_setting(raw, registry_port(root))


def checkout_of(project: str, root: Path) -> Path:
    """The project's static checkout, at the projects root beside `root`."""
    return sweep.projects_root.projects_root(root) / project


# --- decisions -------------------------------------------------------------------------


def expand(command: Sequence[str], port: int, python: str) -> list[str]:
    """`command` with its tokens filled in."""
    return [part.replace(PORT_SLOT, str(port)).replace(PYTHON_SLOT, python) for part in command]


def is_idle(last_activity: float, now: float, idle_minutes: int) -> bool:
    """No bytes either way for `idle_minutes`. An open but silent connection is nobody
    looking: a tab left open on a page that polls keeps its dashboard awake, as it should."""
    return now - last_activity >= idle_minutes * 60


@dataclass(frozen=True)
class Head:
    branch: str
    sha: str


def should_redeploy(started_at: str, now: Head | None, default_branch: str) -> bool:
    """Whether a host dashboard started at commit `started_at` runs stale code.

    Only a move on the default branch counts. A checkout switched to a feature branch is
    someone working in it, and a dashboard restarted onto their half-finished branch is a
    deploy nobody asked for. An unreadable HEAD changes nothing.
    """
    return bool(
        started_at and now is not None and now.sha != started_at and now.branch == default_branch
    )


def unavailable(message: str) -> bytes:
    """The HTTP answer to a request this gateway could not hand to its dashboard."""
    body = f"dashboard unavailable: {message}\n".encode()
    head = (
        "HTTP/1.1 503 Service Unavailable\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode()
    return head + body


# --- the machine: git, processes, sockets ----------------------------------------------

Run = Callable[..., subprocess.CompletedProcess]


def run_quiet(argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """`subprocess.run` with no window, captured output, and a failure to spawn as a
    returncode rather than a traceback."""
    kwargs.setdefault("timeout", 60)
    try:
        return subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            check=False,
            creationflags=NO_WINDOW,
            **kwargs,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(list(argv), 1, "", str(exc))


def read_head(checkout: Path, run: Run = run_quiet) -> Head | None:
    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=checkout)
    sha = run(["git", "rev-parse", "HEAD"], cwd=checkout)
    if branch.returncode or sha.returncode:
        return None
    return Head(branch.stdout.strip(), sha.stdout.strip())


def default_branch(checkout: Path, run: Run = run_quiet) -> str:
    """origin's default branch, by name; `main` where the clone never recorded one."""
    found = run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=checkout)
    name = found.stdout.strip() if found.returncode == 0 else ""
    return name.partition("/")[2] or "main"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((HOST, 0))
        return probe.getsockname()[1]


def probe_http(port: int, path: str, timeout: float = PROBE_TIMEOUT) -> bool:
    """Whether anything answers HTTP on `port`. Any status counts.

    A connect alone is not enough: Docker Desktop's port proxy accepts a connection to a
    published port before the container behind it listens, then resets it.
    """
    connection = http.client.HTTPConnection(HOST, port, timeout=timeout)
    try:
        connection.request("GET", path)
        connection.getresponse().read(0)
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()
    return True


def spawn(
    argv: Sequence[str], cwd: Path, output: Path, env: Mapping[str, str] | None = None
) -> subprocess.Popen:
    """A host dashboard, windowless, its output in `output` (this wake's only).
    `env` None inherits the gateway's; `Backend` passes one only to put a Node first."""
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as sink:
        return subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=None if env is None else dict(env),
            stdin=subprocess.DEVNULL,
            stdout=sink,
            stderr=subprocess.STDOUT,
            creationflags=NO_WINDOW,
            start_new_session=not WINDOWS,
        )


def kill_tree(process: subprocess.Popen, run: Run = run_quiet) -> None:
    """Stop a host dashboard and everything it started: `uv run` is a parent, and
    `ui-task.py` serves from a child, so killing the top process alone leaks the server."""
    if process.poll() is not None:
        return
    if WINDOWS:
        run(["taskkill", "/PID", str(process.pid), "/T", "/F"])
    elif sys.platform != "win32":  # also what tells mypy on Windows `killpg` is POSIX's
        with contextlib.suppress(OSError):
            os.killpg(process.pid, 15)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


def die_with_me() -> str:
    """Put this process in a job that kills every child when it ends. "" or why not.

    The gateway ends at sign-out, or by `schtasks /End`, with no chance to stop what it
    started; without this, each awake dashboard would run on, unreachable, until a reboot.
    Children join the job as they are created, so this runs once, before any spawn.
    """
    if not WINDOWS:
        return ""
    if sys.platform != "win32":  # also what tells mypy `WinDLL` exists below
        return ""
    import ctypes
    from ctypes import wintypes

    class BasicLimit(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_uint64)
            for name in ("Read", "Write", "Other", "ReadBytes", "WriteBytes", "OtherBytes")
        ]

    class ExtendedLimit(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimit),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    job_object_extended_limit_information = 9
    job_object_limit_kill_on_job_close = 0x2000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return f"CreateJobObject failed ({ctypes.get_last_error()})"
    info = ExtendedLimit()
    info.BasicLimitInformation.LimitFlags = job_object_limit_kill_on_job_close
    if not kernel32.SetInformationJobObject(
        job, job_object_extended_limit_information, ctypes.byref(info), ctypes.sizeof(info)
    ):
        return f"SetInformationJobObject failed ({ctypes.get_last_error()})"
    if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
        return f"AssignProcessToJobObject failed ({ctypes.get_last_error()})"
    # The handle is deliberately never closed: closing it is what kills the children,
    # and the process ending closes it.
    _JOB.append(job)
    return ""


_JOB: list[object] = []


class Log:
    """`logs/dashboards.log`: started over by each gateway, appended, thread-safe."""

    def __init__(self, path: Path, clock: Callable[[], _dt.datetime] | None = None):
        self.path = path
        self.clock = clock or (lambda: _dt.datetime.now().astimezone())
        self.lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")

    def __call__(self, line: str) -> None:
        stamp = self.clock().isoformat(timespec="seconds")
        with self.lock:
            with contextlib.suppress(OSError):
                if self.path.stat().st_size > ARTIFACT_MAX_BYTES:
                    self.path.write_text("", encoding="utf-8")
            with contextlib.suppress(OSError), self.path.open("a", encoding="utf-8") as sink:
                sink.write(f"{stamp} {line}\n")


# --- one dashboard's lifecycle ---------------------------------------------------------


class WakeError(RuntimeError):
    """A dashboard that could not be started; the message says why."""


# How much of a failed wake's output `build_cause` reads, from the end.
CAUSE_TAIL_BYTES = 64 * 1024
# Lines that say why a build died, strongest first: an engine refusal names the Node
# that was too old, and an error line beats whatever npm printed after it.
_CAUSE_PATTERNS = (
    re.compile(r"EBADENGINE|Unsupported engine|engine \"node\"|requires? node", re.IGNORECASE),
    re.compile(r"\b(?:\w*Error|ERR!|error)\b[:\s]"),
)
# npm's closing lines point at its own log and name no cause.
_NOISE = re.compile(r"A complete log of this run|npm (?:ERR!|error) *$|^\s*at ", re.IGNORECASE)


def build_cause(output: Path) -> str:
    """The line in a failed wake's output that says why, "" when it holds none.

    A dashboard that dies before serving was, at ibkr_trader, a `npm run build` under
    Node 18 (`node:util` has no `styleText`), and the 503 said only `exited (1)`. The
    output was on disk all along, so the cause is read from it rather than left to
    whoever opens the file.
    """
    try:
        with output.open("rb") as source:
            source.seek(0, os.SEEK_END)
            source.seek(max(0, source.tell() - CAUSE_TAIL_BYTES))
            text = source.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    lines = [line for line in lines if not _NOISE.search(line)]
    for pattern in _CAUSE_PATTERNS:
        hits = [line for line in lines if pattern.search(line)]
        if hits:
            return hits[0] if pattern is _CAUSE_PATTERNS[0] else hits[-1]
    return lines[-1] if lines else ""


@dataclass(frozen=True)
class Machinery:
    """Everything `Backend` asks of the machine, so a test can stand in for any of it.
    `stop` unset is `kill_tree` through `run`. `node` is the Node a host command runs
    under (`node_toolchain.ensure`): None for a checkout that asks for none."""

    run: Run = run_quiet
    start: Callable[..., subprocess.Popen] = spawn
    stop: Callable[[subprocess.Popen], None] | None = None
    probe: Callable[[int, str], bool] = probe_http
    clock: Callable[[], float] = time.monotonic
    pause: Callable[[float], None] = time.sleep
    node: Callable[[Path], node_toolchain.Provision | None] = node_toolchain.ensure


class Backend:
    """One dashboard: asleep, or awake behind `backend_port`.

    Every transition holds `lock`, so the burst of connections a page load opens wakes it
    once: the first starts it and the rest wait on the lock for its port.
    """

    def __init__(
        self,
        dashboard: Dashboard,
        checkout: Path,
        *,
        python: str,
        log: Callable[[str], None],
        output: Path,
        machinery: Machinery | None = None,
    ):
        machinery = machinery or Machinery()
        run = machinery.run
        self.dashboard = dashboard
        self.checkout = checkout
        self.python = python
        self.log = log
        self.output = output
        self.run = run
        self.start = machinery.start
        self.stop = machinery.stop or (lambda process: kill_tree(process, run))
        self.probe = machinery.probe
        self.node = machinery.node
        self.clock = machinery.clock
        self.pause = machinery.pause
        self.lock = threading.Lock()
        self.process: subprocess.Popen | None = None
        self.awake = False
        self.backend_port = 0
        self.started_at = ""
        self.last_activity = self.clock()
        self.error = ""

    # Called from every pump on every chunk, so it takes no lock: a float store is atomic.
    def touch(self) -> None:
        self.last_activity = self.clock()

    def wake(self) -> int:
        """The port the dashboard serves on, starting it first if it sleeps."""
        with self.lock:
            self.last_activity = self.clock()
            if self.awake and self._alive():
                return self.backend_port
            self.awake = False
            try:
                self._start()
            except WakeError as exc:
                self.error = str(exc)
                self.log(f"{self.dashboard.project}: could not wake: {exc}")
                raise
            self.awake = True
            self.error = ""
            self.log(f"{self.dashboard.project}: awake on {self.backend_port}")
            return self.backend_port

    def lost(self) -> None:
        """The dashboard refused a connection: count it asleep, so the next one wakes it."""
        with self.lock:
            if self.awake:
                self.log(f"{self.dashboard.project}: stopped answering")
                self._halt()

    def adopt(self) -> None:
        """A container found serving -- left up by an earlier gateway, or started by
        someone else -- is awake, and sleeps on the usual clock rather than running on
        unmanaged."""
        dashboard = self.dashboard
        if dashboard.container and self.probe(dashboard.backend_port, dashboard.path):
            with self.lock:
                self.awake = True
                self.backend_port = dashboard.backend_port
                self.last_activity = self.clock()
            self.log(f"{dashboard.project}: found awake, adopted")

    def tick(self) -> None:
        """Put it to sleep when idle; stop a host dashboard running stale code.

        An asleep container is probed too: a `docker compose up -d` of the whole stack,
        by hand or by another job, starts it behind the gateway's back, and unadopted it
        would never sleep again.
        """
        if not self.awake and self.dashboard.container:
            self.adopt()
            return
        with self.lock:
            if not self.awake:
                return
            dashboard = self.dashboard
            if not self._alive():
                code = self.process.returncode if self.process else None
                self.log(f"{dashboard.project}: exited ({code}); see {self.output}")
                self._halt()
            elif is_idle(self.last_activity, self.clock(), dashboard.idle_minutes):
                self.log(f"{dashboard.project}: idle {dashboard.idle_minutes} min, asleep")
                self._halt()
            elif not dashboard.container and should_redeploy(
                self.started_at,
                read_head(self.checkout, self.run),
                default_branch(self.checkout, self.run),
            ):
                self.log(f"{dashboard.project}: merged code; next request starts it")
                self._halt()

    def shutdown(self) -> None:
        with self.lock:
            if self.awake:
                self._halt()

    def state(self) -> dict[str, Any]:
        idle_for = max(0, int(self.clock() - self.last_activity))
        return {
            "url": self.dashboard.url,
            "awake": self.awake,
            "idleSeconds": idle_for if self.awake else None,
            "error": self.error,
        }

    # --- under the lock ---

    def _alive(self) -> bool:
        if self.dashboard.container:
            return True
        return self.process is not None and self.process.poll() is None

    def _start(self) -> None:
        dashboard = self.dashboard
        if not self.checkout.is_dir():
            raise WakeError(f"no checkout at {self.checkout}")
        if dashboard.container:
            deadline = self.clock() + dashboard.start_seconds
            port = self._start_container()
        else:
            # Before the deadline starts: a first fetch of a Node is not the dashboard
            # being slow to serve.
            node = self.node(self.checkout)
            if node is not None and not node.ok:
                raise WakeError(node.problem)
            deadline = self.clock() + dashboard.start_seconds
            port = self._start_host(node_toolchain.environ(node))
        self._await(port, deadline)

    def _start_container(self) -> int:
        dashboard = self.dashboard
        up = self.run(
            ["docker", "compose", "up", "-d", "--wait", dashboard.service],
            cwd=self.checkout,
            timeout=dashboard.start_seconds,
        )
        if up.returncode:
            tail = (up.stderr or up.stdout or "").strip().splitlines()[-1:]
            raise WakeError(f"docker compose up failed: {' '.join(tail) or up.returncode}")
        return dashboard.backend_port

    def _start_host(self, env: Mapping[str, str] | None) -> int:
        """Run the command on a free port, under `env` when a Node had to be provided."""
        port = free_port()
        head = read_head(self.checkout, self.run)
        self.started_at = head.sha if head else ""
        argv = expand(self.dashboard.command, port, self.python)
        extra = {} if env is None else {"env": env}
        try:
            self.process = self.start(argv, self.checkout, self.output, **extra)
        except OSError as exc:
            raise WakeError(f"could not run {argv[0]}: {exc}") from exc
        return port

    def _await(self, port: int, deadline: float) -> None:
        """Probe `port` until it answers, the process dies, or `deadline` passes."""
        dashboard = self.dashboard
        while True:
            if not self._alive():
                code = self.process.returncode if self.process else None
                self.process = None
                cause = build_cause(self.output)
                said = f": {cause}" if cause else ""
                raise WakeError(f"exited ({code}) before serving{said}; see {self.output}")
            if self.probe(port, dashboard.path):
                self.backend_port = port
                return
            if self.clock() >= deadline:
                self._halt()
                raise WakeError(f"not serving after {dashboard.start_seconds}s; see {self.output}")
            self.pause(PROBE_INTERVAL)

    def _halt(self) -> None:
        dashboard = self.dashboard
        if dashboard.container:
            self.run(["docker", "compose", "stop", dashboard.service], cwd=self.checkout)
        elif self.process is not None:
            self.stop(self.process)
        self.process = None
        self.awake = False


# --- forwarding ------------------------------------------------------------------------


def pump(source: socket.socket, sink: socket.socket, touch: Callable[[], None]) -> None:
    """Copy one direction until it closes, then pass the close on."""
    with contextlib.suppress(OSError):
        while True:
            data = source.recv(CHUNK)
            if not data:
                break
            touch()
            sink.sendall(data)
    with contextlib.suppress(OSError):
        sink.shutdown(socket.SHUT_WR)


def connect(port: int) -> socket.socket:
    return socket.create_connection((HOST, port), timeout=10)


def serve_connection(
    client: socket.socket,
    backend: Backend,
    dial: Callable[[int], socket.socket] = connect,
) -> None:
    """Hand one accepted connection to its dashboard, waking it on the first byte."""
    with contextlib.closing(client):
        client.settimeout(FIRST_BYTE_SECONDS)
        try:
            first = client.recv(CHUNK)
        except OSError:
            return
        if not first:
            return
        upstream = None
        for _attempt in range(2):
            try:
                upstream = dial(backend.wake())
                break
            except WakeError as exc:
                with contextlib.suppress(OSError):
                    client.sendall(unavailable(str(exc)))
                return
            except OSError:
                backend.lost()
        if upstream is None:
            with contextlib.suppress(OSError):
                client.sendall(unavailable("it accepted no connection"))
            return
        with contextlib.closing(upstream):
            client.settimeout(None)
            upstream.settimeout(None)
            try:
                upstream.sendall(first)
            except OSError:
                return
            backend.touch()
            inbound = threading.Thread(
                target=pump, args=(client, upstream, backend.touch), daemon=True
            )
            inbound.start()
            pump(upstream, client, backend.touch)
            # The dashboard is done with this connection, or was put to sleep: a browser
            # holding its keep-alive open would otherwise park the other pump forever.
            with contextlib.suppress(OSError):
                client.shutdown(socket.SHUT_RDWR)
            inbound.join()


def listen(port: int) -> socket.socket:
    """The public port, held exclusively: a second gateway fails to bind rather than
    sharing it, as Windows otherwise would with a socket that set `SO_REUSEADDR`."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
    try:
        if exclusive is not None:
            server.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        server.bind((HOST, port))
        server.listen(64)
    except OSError:
        server.close()
        raise
    return server


class Gateway:
    """Every backend's listener, and the clock that puts them to sleep."""

    def __init__(
        self,
        backends: Iterable[Backend],
        *,
        log: Callable[[str], None],
        state_path: Path | None = None,
        bind: Callable[[int], socket.socket] = listen,
        dial: Callable[[int], socket.socket] = connect,
    ):
        self.backends = list(backends)
        self.log = log
        self.state_path = state_path
        self.bind = bind
        self.dial = dial
        self.listeners: dict[str, socket.socket] = {}
        self.bind_errors: dict[str, str] = {}
        self.closing = threading.Event()

    def bind_missing(self) -> None:
        """Bind every port not yet held. A port something else holds is retried every
        tick and logged once, so a stack still publishing it is a line, not a loop."""
        for backend in self.backends:
            project = backend.dashboard.project
            if project in self.listeners:
                continue
            try:
                server = self.bind(backend.dashboard.port)
            except OSError as exc:
                message = f"port {backend.dashboard.port} is taken ({exc}); retrying"
                if self.bind_errors.get(project) != message:
                    self.log(f"{project}: {message}")
                self.bind_errors[project] = message
                continue
            self.bind_errors.pop(project, None)
            self.listeners[project] = server
            self.log(f"{project}: listening on {backend.dashboard.url}")
            threading.Thread(target=self._accept, args=(server, backend), daemon=True).start()

    def _accept(self, server: socket.socket, backend: Backend) -> None:
        while not self.closing.is_set():
            try:
                client, _address = server.accept()
            except OSError:
                return
            threading.Thread(
                target=serve_connection, args=(client, backend, self.dial), daemon=True
            ).start()

    def tick(self) -> None:
        self.bind_missing()
        for backend in self.backends:
            try:
                backend.tick()
            # One dashboard's fault must not stop the others' clock: what a tick's git,
            # docker and process calls raise is logged, and anything else is a bug.
            except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
                self.log(f"{backend.dashboard.project}: tick failed: {exc!r}")
        self.write_state()

    def write_state(self) -> None:
        if self.state_path is None:
            return
        state = {}
        for backend in self.backends:
            entry = backend.state()
            entry["error"] = entry["error"] or self.bind_errors.get(backend.dashboard.project, "")
            entry["listening"] = backend.dashboard.project in self.listeners
            state[backend.dashboard.project] = entry
        payload = {"pid": os.getpid(), "dashboards": state}
        with contextlib.suppress(OSError):
            self.state_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def run(self, tick_seconds: float = TICK_SECONDS) -> None:
        for backend in self.backends:
            backend.adopt()
        self.tick()
        while not self.closing.wait(tick_seconds):
            self.tick()

    def close(self) -> None:
        self.closing.set()
        for server in self.listeners.values():
            with contextlib.suppress(OSError):
                server.close()
        for backend in self.backends:
            backend.shutdown()
        self.write_state()


# --- the CLI ---------------------------------------------------------------------------


def _verdict(row: object) -> str:
    """What one dashboard's row in the state file says about it."""
    if not isinstance(row, Mapping):
        return "no gateway has served it"
    if row.get("error"):
        return f"error: {row['error']}"
    if not row.get("listening"):
        return "not listening"
    if row.get("awake"):
        return f"awake, idle {int(row.get('idleSeconds') or 0) // 60} min"
    return "asleep"


def status_lines(dashboards: Sequence[Dashboard], state: Mapping[str, Any]) -> list[str]:
    """One line per dashboard from the gateway's state file. Pure."""
    rows = state.get("dashboards", {}) if isinstance(state, Mapping) else {}
    if not dashboards:
        return [f"dashboards: none declared in `{SETTING}`"]
    if not isinstance(rows, Mapping):
        rows = {}
    return [f"{d.project:<16} {d.url:<28} {_verdict(rows.get(d.project))}" for d in dashboards]


def read_state(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    verbs = parser.add_subparsers(dest="verb")
    verbs.add_parser("status", help="each dashboard's URL and whether it is awake")
    verbs.add_parser("serve", help="run the gateway until stopped")
    opener = verbs.add_parser("open", help="open a dashboard in the browser, waking it")
    opener.add_argument("project")
    return parser


def serve(root: Path, dashboards: Sequence[Dashboard], notes: Sequence[str]) -> int:
    log = Log(root / ARTIFACT)
    log(f"gateway {os.getpid()} starting")
    for note in notes:
        log(f"note: {note}")
    why = die_with_me()
    if why:
        log(f"note: children may outlive this gateway: {why}")
    python = sweep.console_python()
    backends = []
    for dashboard in dashboards:
        checkout = checkout_of(dashboard.project, root)
        if not checkout.is_dir():
            log(f"{dashboard.project}: no checkout on this PC; skipped")
            continue
        backends.append(
            Backend(
                dashboard,
                checkout,
                python=python,
                log=log,
                output=root / OUTPUT_DIR / f"{dashboard.project}.log",
            )
        )
    if not backends:
        log("nothing to serve on this PC")
        return 0
    gateway = Gateway(backends, log=log, state_path=root / STATE)
    try:
        gateway.run()
    except KeyboardInterrupt:
        pass
    finally:
        gateway.close()
        log("gateway stopped")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    root = home()
    dashboards, notes = declared(root)
    if args.verb == "serve":
        return serve(root, dashboards, notes)
    if args.verb == "open":
        match = next((d for d in dashboards if d.project == args.project), None)
        if match is None:
            known = ", ".join(d.project for d in dashboards) or "none"
            print(f"dashboards: {args.project} declares no dashboard (declared: {known})")
            return 2
        webbrowser.open(match.url)
        print(f"opening {match.url}")
        return 0
    for line in status_lines(dashboards, read_state(root / STATE)):
        print(line)
    for note in notes:
        print(f"note: {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
