"""Tests for the dashboard gateway: the setting, the wake/sleep/redeploy decisions, and
forwarding over real loopback sockets."""

from __future__ import annotations

import http.client
import json
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from support import REPO_ROOT, load_script

dashboards = load_script("scripts/dashboards.py")
Dashboard = dashboards.Dashboard
Head = dashboards.Head


def no_port(_project):
    return None


def setting(entries):
    """`SETTING`'s value as `project_registry.setting` hands it over: the entries."""
    return entries


# --- the setting -----------------------------------------------------------------------


def test_a_command_entry_takes_its_port_from_the_registry():
    found, notes = dashboards.parse_setting(
        setting({"p": {"command": ["serve", "--port", "{port}"]}}), lambda _p: 8003
    )
    assert notes == []
    assert found == [Dashboard(project="p", port=8003, command=("serve", "--port", "{port}"))]


def test_an_explicit_port_wins_over_the_registry():
    found, _notes = dashboards.parse_setting(
        setting({"p": {"command": ["s", "{port}"], "port": 8765}}), lambda _p: 8003
    )
    assert found[0].port == 8765


def test_a_service_entry_needs_its_backend_port():
    found, notes = dashboards.parse_setting(setting({"p": {"service": "app"}}), lambda _p: 8010)
    assert found == []
    assert "backendPort" in notes[0]
    found, notes = dashboards.parse_setting(
        setting({"p": {"service": "app", "backendPort": 18010}}), lambda _p: 8010
    )
    assert notes == []
    assert found[0].container and found[0].backend_port == 18010 and found[0].port == 8010


@pytest.mark.parametrize(
    ("entry", "says"),
    [
        ({}, "exactly one"),
        ({"command": ["s", "{port}"], "service": "app", "backendPort": 1}, "exactly one"),
        ({"command": ["serve"]}, "{port}"),
        ({"command": "serve {port}"}, "list"),
        ({"command": ["s", "{port}"], "idleMinutes": True}, "idleMinutes"),
        ({"command": ["s", "{port}"], "idleMinutes": 0}, "idleMinutes"),
        ({"command": ["s", "{port}"], "startSeconds": -1}, "startSeconds"),
        ({"command": ["s", "{port}"], "port": 70000}, "port"),
        ({"command": ["s", "{port}"], "path": "health"}, "path"),
        ({"service": "app", "backendPort": 8010, "port": 8010}, "differ"),
        ("not an object", "object"),
    ],
)
def test_a_malformed_entry_is_a_note_not_silence(entry, says):
    found, notes = dashboards.parse_setting(setting({"p": entry}), lambda _p: 8000)
    assert found == []
    assert len(notes) == 1 and says in notes[0] and notes[0].startswith("p:")


def test_parse_entry_answers_a_dashboard_or_why_not():
    dashboard, why = dashboards.parse_entry(
        "p", {"service": "app", "backendPort": 19100, "idleMinutes": 5}, lambda _p: 9100
    )
    assert why == ""
    assert dashboard == Dashboard(
        project="p", port=9100, service="app", backend_port=19100, idle_minutes=5
    )
    dashboard, why = dashboards.parse_entry("p", {"command": []}, lambda _p: 9100)
    assert dashboard is None and dashboards.PORT_SLOT in why


def test_no_port_anywhere_is_a_note():
    found, notes = dashboards.parse_setting(setting({"p": {"command": ["s", "{port}"]}}), no_port)
    assert found == [] and "ports.toml" in notes[0]


def test_two_dashboards_on_one_port_keep_the_first():
    found, notes = dashboards.parse_setting(
        setting(
            {
                "a": {"command": ["s", "{port}"], "port": 9100},
                "b": {"service": "app", "backendPort": 9100, "port": 9200},
            }
        ),
        no_port,
    )
    assert [d.project for d in found] == ["a"]
    assert "a's" in notes[0]


def test_the_options_are_read():
    found, _notes = dashboards.parse_setting(
        setting(
            {
                "p": {
                    "command": ["s", "{port}"],
                    "port": 9100,
                    "idleMinutes": 5,
                    "startSeconds": 9,
                    "path": "/health",
                }
            }
        ),
        no_port,
    )
    assert (found[0].idle_minutes, found[0].start_seconds, found[0].path) == (5, 9, "/health")
    assert found[0].url == "http://127.0.0.1:9100/health"


def test_the_defaults_are_thirty_minutes_and_the_root():
    found, _notes = dashboards.parse_setting(
        setting({"p": {"command": ["s", "{port}"], "port": 9100}}), no_port
    )
    assert found[0].idle_minutes == 30 and found[0].path == "/"


@pytest.mark.parametrize(
    ("value", "notes"),
    [
        (None, []),
        ([], ["object keyed by project"]),
        ("p", ["object keyed by project"]),
    ],
)
def test_the_setting_itself(value, notes):
    found, said = dashboards.parse_setting(value, no_port)
    assert found == []
    assert len(said) == len(notes)
    assert all(want in got for want, got in zip(notes, said, strict=True))


def test_the_committed_registry_declares_the_three_dashboards_cleanly():
    found, notes = dashboards.declared(REPO_ROOT)
    assert notes == []
    by_project = {d.project: d for d in found}
    assert set(by_project) == {"d2-trader", "ibkr_trader", "sports_betting"}
    assert by_project["d2-trader"].port == 8010 and by_project["d2-trader"].container
    assert by_project["ibkr_trader"].port == 8001
    assert by_project["sports_betting"].port == 8765


def test_the_registry_port_is_the_app_port_plus_the_slot():
    port = dashboards.registry_port(REPO_ROOT)
    assert port("ibkr_trader") == 8001
    assert port("no-such-project") is None


def test_an_unreadable_registry_gives_no_port(tmp_path):
    assert dashboards.registry_port(tmp_path)("ibkr_trader") is None


# --- decisions -------------------------------------------------------------------------


def test_expand_fills_both_tokens():
    assert dashboards.expand(["{python}", "x.py", "--port={port}"], 4321, "py.exe") == [
        "py.exe",
        "x.py",
        "--port=4321",
    ]


def test_idle_is_thirty_minutes_of_no_bytes():
    assert not dashboards.is_idle(0, 29 * 60 + 59, 30)
    assert dashboards.is_idle(0, 30 * 60, 30)


@pytest.mark.parametrize(
    ("started", "now", "expected"),
    [
        ("a", Head("main", "b"), True),
        ("a", Head("main", "a"), False),
        ("a", Head("feature", "b"), False),
        ("a", None, False),
        ("", Head("main", "b"), False),
    ],
)
def test_only_a_move_on_the_default_branch_redeploys(started, now, expected):
    assert dashboards.should_redeploy(started, now, "main") is expected


def test_the_unavailable_answer_is_a_complete_503():
    raw = dashboards.unavailable("no checkout")
    head, _, body = raw.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 503")
    assert f"Content-Length: {len(body)}".encode() in head
    assert b"no checkout" in body


def completed(argv, code=0, out=""):
    return subprocess.CompletedProcess(argv, code, out, "")


def test_read_head_and_the_default_branch():
    answers = {
        ("git", "rev-parse", "--abbrev-ref", "HEAD"): "main\n",
        ("git", "rev-parse", "HEAD"): "abc\n",
        ("git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"): "origin/trunk\n",
    }

    def run(argv, **_kwargs):
        return completed(argv, 0, answers[tuple(argv)])

    assert dashboards.read_head(Path("."), run) == Head("main", "abc")
    assert dashboards.default_branch(Path("."), run) == "trunk"

    def fail(argv, **_kwargs):
        return completed(argv, 128)

    assert dashboards.read_head(Path("."), fail) is None
    assert dashboards.default_branch(Path("."), fail) == "main"


def test_status_lines_read_the_state_file():
    asleep = Dashboard(project="a", port=9100, command=("s", "{port}"))
    awake = Dashboard(project="b", port=9101, command=("s", "{port}"))
    broken = Dashboard(project="c", port=9102, command=("s", "{port}"))
    unseen = Dashboard(project="d", port=9103, command=("s", "{port}"))
    state = {
        "dashboards": {
            "a": {"listening": True, "awake": False, "error": ""},
            "b": {"listening": True, "awake": True, "idleSeconds": 125, "error": ""},
            "c": {"listening": False, "awake": False, "error": "port 9102 is taken"},
        }
    }
    lines = dashboards.status_lines([asleep, awake, broken, unseen], state)
    assert lines[0].endswith("asleep")
    assert lines[1].endswith("awake, idle 2 min")
    assert lines[2].endswith("error: port 9102 is taken")
    assert lines[3].endswith("no gateway has served it")
    assert "none declared" in dashboards.status_lines([], {})[0]


# --- one dashboard's lifecycle, with the machine faked ---------------------------------


class FakeProcess:
    pid = 4242

    def __init__(self):
        self.returncode = None

    def poll(self):
        return self.returncode


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


MAIN_AT_C1 = Head("main", "c1")


class Machine:
    """Everything `Backend` asks of the machine, recorded."""

    def __init__(self, *, ready_after=0, head=MAIN_AT_C1, up_code=0):
        self.clock = Clock()
        self.started: list[list[str]] = []
        self.stopped: list[FakeProcess] = []
        self.commands: list[list[str]] = []
        self.process = FakeProcess()
        self.ready_after = ready_after
        self.probes = 0
        self.head = head
        self.up_code = up_code
        self.lines: list[str] = []

    def run(self, argv, **_kwargs):
        self.commands.append(list(argv))
        if argv[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return completed(argv, 0, self.head.branch)
        if argv[:2] == ["git", "rev-parse"]:
            return completed(argv, 0, self.head.sha)
        if argv[:2] == ["git", "symbolic-ref"]:
            return completed(argv, 0, "origin/main")
        if argv[:3] == ["docker", "compose", "up"]:
            return completed(argv, self.up_code)
        return completed(argv)

    def start(self, argv, _cwd, _output):
        self.started.append(list(argv))
        self.process = FakeProcess()
        return self.process

    def stop(self, process):
        self.stopped.append(process)
        process.returncode = 1

    def probe(self, _port, _path):
        self.probes += 1
        return self.probes > self.ready_after

    def pause(self, seconds):
        self.clock.advance(seconds)

    def backend(self, dashboard, checkout):
        return dashboards.Backend(
            dashboard,
            checkout,
            python="py",
            log=self.lines.append,
            output=checkout / "out.log",
            machinery=dashboards.Machinery(
                run=self.run,
                start=self.start,
                stop=self.stop,
                probe=self.probe,
                clock=self.clock,
                pause=self.pause,
            ),
        )


HOST_DASHBOARD = Dashboard(project="p", port=9100, command=("serve", "{port}"), start_seconds=10)
CONTAINER = Dashboard(project="p", port=9100, service="app", backend_port=19100, start_seconds=10)


def test_the_first_wake_starts_it_and_the_next_reuses_it(tmp_path):
    machine = Machine(ready_after=2)
    backend = machine.backend(HOST_DASHBOARD, tmp_path)
    port = backend.wake()
    assert machine.started == [["serve", str(port)]]
    assert backend.wake() == port
    assert len(machine.started) == 1
    assert backend.awake and backend.started_at == "c1"


def test_a_dashboard_that_exits_before_serving_is_a_wake_error(tmp_path):
    machine = Machine(ready_after=10**6)

    def start(argv, cwd, output):
        process = machine.start(argv, cwd, output)
        process.returncode = 3
        return process

    backend = machine.backend(HOST_DASHBOARD, tmp_path)
    backend.start = start
    with pytest.raises(dashboards.WakeError, match=r"exited \(3\)"):
        backend.wake()
    assert not backend.awake
    assert "exited (3)" in backend.state()["error"]


def test_a_dashboard_that_never_answers_is_stopped_at_its_deadline(tmp_path):
    machine = Machine(ready_after=10**6)
    backend = machine.backend(HOST_DASHBOARD, tmp_path)
    with pytest.raises(dashboards.WakeError, match="not serving after 10s"):
        backend.wake()
    assert machine.stopped and not backend.awake


def test_a_missing_checkout_is_a_wake_error(tmp_path):
    machine = Machine()
    backend = machine.backend(HOST_DASHBOARD, tmp_path / "gone")
    with pytest.raises(dashboards.WakeError, match="no checkout"):
        backend.wake()
    assert machine.started == []


def test_it_sleeps_after_its_idle_minutes_without_a_byte(tmp_path):
    machine = Machine()
    backend = machine.backend(HOST_DASHBOARD, tmp_path)
    backend.wake()
    machine.clock.advance(29 * 60)
    backend.touch()
    machine.clock.advance(29 * 60)
    backend.tick()
    assert backend.awake and not machine.stopped
    machine.clock.advance(60)
    backend.tick()
    assert not backend.awake and machine.stopped
    assert any("asleep" in line for line in machine.lines)
    backend.wake()
    assert len(machine.started) == 2


def test_a_merge_on_the_default_branch_stops_it_for_the_next_request(tmp_path):
    machine = Machine()
    backend = machine.backend(HOST_DASHBOARD, tmp_path)
    backend.wake()
    machine.head = Head("main", "c2")
    backend.tick()
    assert not backend.awake and machine.stopped


def test_a_feature_branch_in_the_checkout_redeploys_nothing(tmp_path):
    machine = Machine()
    backend = machine.backend(HOST_DASHBOARD, tmp_path)
    backend.wake()
    machine.head = Head("topic", "c2")
    backend.tick()
    assert backend.awake and not machine.stopped


def test_a_dashboard_that_died_is_counted_asleep(tmp_path):
    machine = Machine()
    backend = machine.backend(HOST_DASHBOARD, tmp_path)
    backend.wake()
    machine.process.returncode = 1
    backend.tick()
    assert not backend.awake
    assert any("exited (1)" in line for line in machine.lines)


def test_a_container_wakes_with_compose_up_and_sleeps_with_compose_stop(tmp_path):
    machine = Machine()
    backend = machine.backend(CONTAINER, tmp_path)
    assert backend.wake() == 19100
    assert ["docker", "compose", "up", "-d", "--wait", "app"] in machine.commands
    machine.clock.advance(30 * 60)
    backend.tick()
    assert ["docker", "compose", "stop", "app"] in machine.commands
    assert not backend.awake


def test_a_container_is_never_redeployed_by_the_gateway(tmp_path):
    machine = Machine()
    backend = machine.backend(CONTAINER, tmp_path)
    backend.wake()
    machine.head = Head("main", "c2")
    backend.tick()
    assert backend.awake


def test_a_failed_compose_up_is_a_wake_error(tmp_path):
    machine = Machine(up_code=1)
    backend = machine.backend(CONTAINER, tmp_path)
    with pytest.raises(dashboards.WakeError, match="docker compose up failed"):
        backend.wake()


def test_a_container_left_up_is_adopted_and_sleeps_on_the_clock(tmp_path):
    machine = Machine()
    backend = machine.backend(CONTAINER, tmp_path)
    backend.adopt()
    assert backend.awake
    machine.clock.advance(30 * 60)
    backend.tick()
    assert not backend.awake


def test_a_container_started_behind_the_gateways_back_is_adopted_on_a_tick(tmp_path):
    machine = Machine()
    backend = machine.backend(CONTAINER, tmp_path)
    backend.tick()
    assert backend.awake
    assert not any(argv[:3] == ["docker", "compose", "up"] for argv in machine.commands)


def test_a_host_dashboard_is_never_adopted(tmp_path):
    machine = Machine()
    backend = machine.backend(HOST_DASHBOARD, tmp_path)
    backend.adopt()
    assert not backend.awake


def test_lost_counts_it_asleep(tmp_path):
    machine = Machine()
    backend = machine.backend(HOST_DASHBOARD, tmp_path)
    backend.wake()
    backend.lost()
    assert not backend.awake and machine.stopped


# --- forwarding, over real loopback sockets --------------------------------------------


class Hello(BaseHTTPRequestHandler):
    def do_GET(self):
        body = f"hello {self.path}".encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@pytest.fixture
def upstream():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Hello)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


class FakeBackend:
    def __init__(self, port=0, error=None):
        self.port = port
        self.error = error
        self.wakes = 0
        self.touches = 0
        self.losses = 0

    def wake(self):
        self.wakes += 1
        if self.error:
            raise dashboards.WakeError(self.error)
        return self.port

    def touch(self):
        self.touches += 1

    def lost(self):
        self.losses += 1


def gateway_for(backend):
    """A listener on a free port handing every connection to `backend`."""
    server = dashboards.listen(0)

    def accept():
        while True:
            try:
                client, _address = server.accept()
            except OSError:
                return
            threading.Thread(
                target=dashboards.serve_connection, args=(client, backend), daemon=True
            ).start()

    threading.Thread(target=accept, daemon=True).start()
    return server


def get(port, path="/x"):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, response.read().decode()
    finally:
        connection.close()


def test_a_request_is_forwarded_to_the_woken_dashboard(upstream):
    backend = FakeBackend(port=upstream)
    server = gateway_for(backend)
    try:
        assert get(server.getsockname()[1]) == (200, "hello /x")
    finally:
        server.close()
    assert backend.wakes == 1 and backend.touches >= 2


def test_a_connection_that_sends_nothing_wakes_nothing(upstream):
    backend = FakeBackend(port=upstream)
    server = gateway_for(backend)
    try:
        with socket.create_connection(server.getsockname(), timeout=5):
            pass
        time.sleep(0.2)
    finally:
        server.close()
    assert backend.wakes == 0


def test_a_dashboard_that_cannot_wake_answers_503_with_the_reason():
    backend = FakeBackend(error="no checkout at C:/x")
    server = gateway_for(backend)
    try:
        status, body = get(server.getsockname()[1])
    finally:
        server.close()
    assert status == 503 and "no checkout at C:/x" in body


def test_a_refused_dial_counts_the_dashboard_lost_and_retries_once():
    closed = dashboards.free_port()
    backend = FakeBackend(port=closed)
    server = gateway_for(backend)
    try:
        status, body = get(server.getsockname()[1])
    finally:
        server.close()
    assert status == 503 and "accepted no connection" in body
    assert backend.wakes == 2 and backend.losses == 2


def test_probe_http_counts_any_answer_and_nothing_else(upstream):
    assert dashboards.probe_http(upstream, "/missing")
    assert not dashboards.probe_http(dashboards.free_port(), "/", timeout=0.5)


def test_a_taken_port_is_logged_once_and_retried(tmp_path):
    lines: list[str] = []
    backend = Machine().backend(HOST_DASHBOARD, tmp_path)
    calls = {"n": 0}

    def bind(_port):
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError("in use")
        return dashboards.listen(0)

    gateway = dashboards.Gateway([backend], log=lines.append, bind=bind)
    try:
        gateway.bind_missing()
        gateway.bind_missing()
        assert sum("taken" in line for line in lines) == 1
        assert "p" not in gateway.listeners
        gateway.bind_missing()
        assert "p" in gateway.listeners
    finally:
        gateway.close()


def test_the_gateway_writes_its_state(tmp_path):
    backend = Machine().backend(HOST_DASHBOARD, tmp_path)
    state = tmp_path / "state.json"
    gateway = dashboards.Gateway(
        [backend], log=lambda _line: None, state_path=state, bind=lambda _p: dashboards.listen(0)
    )
    try:
        gateway.tick()
    finally:
        gateway.close()
    payload = json.loads(state.read_text(encoding="utf-8"))
    assert payload["dashboards"]["p"]["url"] == "http://127.0.0.1:9100/"
    assert payload["dashboards"]["p"]["awake"] is False


def test_the_log_starts_over_and_appends(tmp_path):
    path = tmp_path / "logs" / "dashboards.log"
    path.parent.mkdir()
    path.write_text("old\n", encoding="utf-8")
    log = dashboards.Log(path)
    log("one")
    log("two")
    lines = path.read_text(encoding="utf-8").splitlines()
    assert [line.split(" ", 1)[1] for line in lines] == ["one", "two"]


def test_status_is_the_default_verb():
    assert dashboards.build_parser().parse_args([]).verb is None
    assert dashboards.build_parser().parse_args(["open", "p"]).project == "p"


def test_pump_copies_until_close_and_passes_the_close_on():
    left, right = socket.socketpair()
    out_left, out_right = socket.socketpair()
    touched: list[int] = []
    try:
        left.sendall(b"abc")
        left.shutdown(socket.SHUT_WR)
        dashboards.pump(right, out_left, lambda: touched.append(1))
        assert out_right.recv(10) == b"abc"
        assert out_right.recv(10) == b""
        assert touched
    finally:
        for sock in (left, right, out_left, out_right):
            sock.close()


def test_connect_dials_loopback(upstream):
    with dashboards.connect(upstream) as sock:
        assert sock.getpeername()[1] == upstream


def test_run_quiet_turns_a_missing_program_into_a_returncode():
    result = dashboards.run_quiet(["no-such-program-devkit-dashboards"])
    assert result.returncode == 1 and result.stderr


def test_spawn_writes_its_output_and_kill_tree_stops_it(tmp_path):
    output = tmp_path / "logs" / "p.log"
    process = dashboards.spawn(
        [sys.executable, "-c", "print('up', flush=True); import time; time.sleep(60)"],
        tmp_path,
        output,
    )
    deadline = time.monotonic() + 20
    while b"up" not in output.read_bytes() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert b"up" in output.read_bytes()
    dashboards.kill_tree(process)
    assert process.poll() is not None


def test_die_with_me_is_a_no_op_off_windows(monkeypatch):
    monkeypatch.setattr(dashboards, "WINDOWS", False)
    assert dashboards.die_with_me() == ""


def test_read_state_tolerates_a_missing_or_broken_file(tmp_path):
    assert dashboards.read_state(tmp_path / "none.json") == {}
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    assert dashboards.read_state(broken) == {}


def test_the_gateway_run_ticks_until_closed(tmp_path):
    backend = Machine().backend(HOST_DASHBOARD, tmp_path)
    state = tmp_path / "state.json"
    gateway = dashboards.Gateway(
        [backend], log=lambda _line: None, state_path=state, bind=lambda _p: dashboards.listen(0)
    )
    gateway.closing.set()
    gateway.run(tick_seconds=0)
    gateway.close()
    assert state.is_file()


def workspace_with(tmp_path, entries) -> Path:
    """A devkit checkout under `tmp_path` whose `projects.toml` declares `entries`, each
    a command dashboard on an explicit port -- never the live registry: these run in CI."""
    devkit = tmp_path / "devkit"
    (devkit / "logs").mkdir(parents=True)
    text = ""
    for project, entry in entries.items():
        command = ", ".join(json.dumps(part) for part in entry["command"])
        text += f'[settings."{dashboards.SETTING}".{project}]\ncommand = [{command}]\n'
        text += f"port = {entry['port']}\n"
    (devkit / "projects.toml").write_text(text, encoding="utf-8")
    return devkit


def test_declared_reads_the_registry_of_the_checkout_and_never_a_workspace_file(tmp_path):
    missing = tmp_path / "nowhere"
    found, notes = dashboards.declared(missing)
    assert found == [] and len(notes) == 1 and "no projects.toml" in notes[0]
    (tmp_path / dashboards.sweep.WORKSPACE_FILE_NAME).write_text(
        json.dumps({"settings": {dashboards.SETTING: {"q": {"command": ["s", "{port}"]}}}}),
        encoding="utf-8",
    )
    devkit = workspace_with(tmp_path, {"p": {"command": ["s", "{port}"], "port": 9100}})
    found, notes = dashboards.declared(devkit)
    assert [d.project for d in found] == ["p"] and notes == []
    assert dashboards.checkout_of("p", devkit) == tmp_path / "p"


def test_home_prefers_devkit_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVKIT_DIR", str(tmp_path))
    assert dashboards.home() == tmp_path


def test_serve_with_no_checkout_on_this_pc_serves_nothing(tmp_path, monkeypatch):
    devkit = workspace_with(tmp_path, {"p": {"command": ["s", "{port}"], "port": 9100}})
    monkeypatch.setattr(dashboards, "die_with_me", lambda: "")
    dashboard = Dashboard(project="p", port=9100, command=("s", "{port}"))
    monkeypatch.setattr(dashboards, "checkout_of", lambda project, root: tmp_path / "absent")
    assert dashboards.serve(devkit, [dashboard], ["a note"]) == 0
    log = (devkit / dashboards.ARTIFACT).read_text(encoding="utf-8")
    assert "note: a note" in log and "no checkout on this PC" in log
    assert "nothing to serve" in log


def test_main_status_and_open(tmp_path, monkeypatch, capsys):
    devkit = workspace_with(tmp_path, {"p": {"command": ["s", "{port}"], "port": 9100}})
    monkeypatch.setenv("DEVKIT_DIR", str(devkit))
    opened: list[str] = []
    monkeypatch.setattr(dashboards.webbrowser, "open", opened.append)
    assert dashboards.main([]) == 0
    assert "no gateway has served it" in capsys.readouterr().out
    assert dashboards.main(["open", "p"]) == 0
    assert opened == ["http://127.0.0.1:9100/"]
    assert dashboards.main(["open", "q"]) == 2
    assert "declares no dashboard" in capsys.readouterr().out
