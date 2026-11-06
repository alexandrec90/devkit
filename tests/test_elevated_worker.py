"""`scripts/elevated_worker.py`: the one thing devkit runs as an administrator.

Each `# ---` block below is one line of the threat model, in the order the module states
them, then the gate on a reboot. Every test here drives `cycle` over a temporary layout:
nothing touches `C:\\ProgramData`, no service and no `shutdown`, and the real `main` is
never entered on a machine that could be running the real thing.
"""

from __future__ import annotations

import ast
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from support import REPO_ROOT, load_script

worker = load_script("scripts/elevated_worker.py")
request = load_script("scripts/elevated_request.py")

SOURCE = (REPO_ROOT / "scripts" / "elevated_worker.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
NOW = 1_800_000_000.0
RESTART = "restart-wsl-service"
REBOOT = "reboot-when-idle"
# Real NTFS and Win32: the local tier, which CI deselects and `scripts/e2e.py` runs nightly.
windows_only = pytest.mark.local


class Rig:
    """A temporary layout and a `Machine` that records what it was told to run."""

    def __init__(self, tmp_path: Path, refusal: str = "", code: int = 0):
        self.root = tmp_path / "devkit"
        self.spool = self.root / "requests"
        self.home = self.root / "elevated"
        self.spool.mkdir(parents=True)
        self.home.mkdir()
        (self.spool / worker.SENTINEL).touch()
        self.layout = worker.Layout(str(self.root), str(self.spool), str(self.home))
        self.ran: list[tuple[str, ...]] = []
        self.clock = NOW
        self.refusal = refusal
        self.code = code

    def run(self, argv):
        self.ran.append(tuple(argv))
        return self.code

    @property
    def machine(self):
        return worker.Machine(self.run, lambda: self.refusal, lambda: self.clock, lambda _s: None)

    def ask(self, verb: str, content: bytes = b"") -> Path:
        path = self.spool / f"{verb}.request"
        path.write_bytes(content)
        return path

    def cycle(self):
        return worker.cycle(self.layout, self.machine)

    @property
    def left(self) -> list[str]:
        return sorted(name for name in os.listdir(self.spool) if name != worker.SENTINEL)


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def test_the_verbs_are_the_contracts_and_the_request_modules():
    assert set(worker.VERBS) == set(request.VERBS)
    assert set(worker.VERBS) == {"restart-wsl-service", "restart-docker-service", REBOOT}


def test_the_layout_is_the_contracts_and_the_request_modules():
    assert worker.SPOOL == r"C:\ProgramData\devkit\requests" == request.SPOOL
    assert worker.HOME == r"C:\ProgramData\devkit\elevated" == request.HOME
    assert worker.REQUEST_SUFFIX == request.REQUEST_SUFFIX == ".request"


def test_the_services_are_the_ones_the_repo_already_names():
    """`collector_tasks` is what detects a stuck WSL and `docker-maint` what starts Docker's
    service, and both were read off this machine's SCM. A worker restarting a third
    spelling would fail at 03:00 with nobody watching."""
    assert worker.WSL_SERVICE == load_script("scripts/collector_tasks.py").WSL_QUERY[-1]
    assert worker.DOCKER_SERVICE == load_script("scripts/docker-maint.py").DOCKER_SERVICE


# --- 1. a fixed argv per verb, and only an exact name selects one ------------------


@pytest.mark.parametrize("verb", sorted(worker.VERBS))
def test_a_verb_runs_exactly_its_literal_argv(rig, verb):
    rig.ask(verb)
    report = rig.cycle()
    assert rig.ran == [step.argv for step in worker.VERBS[verb].steps]
    assert not report.failed
    assert rig.left == []


@pytest.mark.parametrize(
    "argv", [step.argv for verb in worker.VERBS.values() for step in verb.steps]
)
def test_every_program_is_a_literal_absolute_path_under_system32(argv):
    """A bare `sc` would be found through the user's PATH or cwd, and `%SystemRoot%` is an
    environment value the user sets. Every argument is a literal for the same reason."""
    assert argv[0].startswith("C:\\Windows\\System32\\") and argv[0].endswith(".exe")
    assert all(isinstance(part, str) and "%" not in part for part in argv)


def test_the_argv_is_data_no_function_can_build():
    """The table is a module-level literal; `serve` indexes it and passes `spec.steps`
    through. No f-string in a step takes anything but a module constant."""
    names = {
        node.id
        for assign in TREE.body
        if isinstance(assign, ast.AnnAssign) and getattr(assign.target, "id", "") == "VERBS"
        for node in ast.walk(assign.value)
        if isinstance(node, ast.Name)
    }
    assert names == {
        "ALREADY_RUNNING",
        "DOCKER_SERVICE",
        "NOT_STARTED",
        "REBOOT_DELAY",
        "REBOOT_EVERY",
        "REBOOT_NOTICE",
        "RESTART_EVERY",
        "SC",
        "SHUTDOWN",
        "Step",
        "TASKKILL",
        "Verb",
        "WSL_SERVICE",
        "frozenset",
        "str",
    }


def test_a_requests_content_is_never_opened(rig, monkeypatch):
    """Not merely unused: a parser that looks at it is one bug from obeying it."""
    rig.ask(RESTART, b"& calc.exe\n--service evil\n")
    opened: list[str] = []
    real_open, real_os_open = open, os.open

    def spy(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    def os_spy(path, *args, **kwargs):
        opened.append(str(path))
        return real_os_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", spy)
    monkeypatch.setattr(os, "open", os_spy)
    rig.cycle()
    assert rig.ran == [step.argv for step in worker.VERBS[RESTART].steps], "it still ran"
    assert not [path for path in opened if "request" in Path(path).name], opened


def test_content_changes_nothing(tmp_path):
    def ran_with(content: bytes, name: str):
        rig = Rig(tmp_path / name)
        rig.ask(RESTART, content)
        rig.cycle()
        return rig.ran

    assert ran_with(b"", "empty") == ran_with(b"reboot-when-idle", "hostile")


@pytest.mark.parametrize(
    "name",
    [
        "restart-wsl-service",
        "restart-wsl-service.req",
        "restart-wsl-service.request.txt",
        "restart-wsl-service.request.",
        "restart-wsl-service.request ",
        " restart-wsl-service.request",
        "RESTART-WSL-SERVICE.REQUEST",
        "Reboot-When-Idle.request",
        "..\\restart-wsl-service.request",
        "../restart-wsl-service.request",
        "sub\\restart-wsl-service.request",
        "C:\\Windows\\restart-wsl-service.request",
        "\\\\?\\C:\\ProgramData\\devkit\\requests\\restart-wsl-service.request",
        "restart-wsl-service.request:evil",
        "restart-wsl-service.request::$DATA",
        "x:restart-wsl-service.request",
        "r\N{CYRILLIC SMALL LETTER IE}start-wsl-service.request",
        "restart\N{EN DASH}wsl-service.request",
        "RESTAR~1.REQ",
        "NUL",
        "",
    ],
)
def test_only_the_exact_name_selects_a_verb(name):
    assert worker.verb_of(name) is None


@pytest.mark.parametrize("verb", sorted(worker.VERBS))
def test_the_exact_name_does_select_it(verb):
    assert worker.verb_of(f"{verb}.request") == verb


def test_anything_else_in_the_spool_is_deleted_logged_and_ignored(rig):
    (rig.spool / "whoami.request").write_bytes(b"x")
    (rig.spool / "restart-wsl-service.req").touch()
    (rig.spool / "notes.txt").touch()
    (rig.spool / "a-folder").mkdir()
    (rig.spool / "restart-docker-service.request").mkdir()
    report = rig.cycle()
    assert rig.ran == [], "a directory with a verb's name is not a request"
    assert rig.left == []
    assert sum("deleted" in line for line in report.lines) == 5
    assert "'whoami.request'" in " ".join(report.lines)
    assert not report.failed


def test_a_folder_with_something_in_it_stays_and_says_so(rig):
    """Never recursive: a walk could be sent through a junction swapped in under it."""
    folder = rig.spool / "full"
    folder.mkdir()
    (folder / "x").touch()
    report = rig.cycle()
    assert report.failed
    assert (folder / "x").exists()


def test_a_name_cannot_forge_a_log_line():
    assert "\n" not in worker.shown("x\nrestart-wsl-service: ok")
    assert len(worker.shown("y" * 10_000)) <= 120


@windows_only
def test_a_stream_on_a_valid_request_is_neither_read_nor_left(rig):
    path = rig.ask(RESTART)
    with open(f"{path}:payload", "w", encoding="utf-8") as handle:
        handle.write("restart-docker-service")
    rig.cycle()
    assert rig.ran == [step.argv for step in worker.VERBS[RESTART].steps]
    assert rig.left == []


@windows_only
def test_a_stream_named_like_a_request_selects_nothing(rig):
    host = rig.spool / "junk"
    host.touch()
    with open(f"{host}:reboot-when-idle.request", "w", encoding="utf-8") as handle:
        handle.write("x")
    with open(f"{rig.spool}:reboot-when-idle.request", "w", encoding="utf-8") as handle:
        handle.write("x")
    report = rig.cycle()
    assert rig.ran == []
    assert rig.left == []
    assert "'junk'" in " ".join(report.lines)


@windows_only
def test_a_name_win32_would_rewrite_is_that_name(rig):
    """`\\\\?\\` makes this entry reachable, and a Win32 path would strip its dot and hit
    the real request beside it."""
    real = rig.ask(RESTART)
    dotted = "\\\\?\\" + str(rig.spool / "restart-wsl-service.request.")
    with open(dotted, "w", encoding="utf-8"):
        pass
    entries = {entry.name: entry for entry in worker.scan(str(rig.spool))}
    assert worker.remove(entries["restart-wsl-service.request."])
    assert real.exists()


# --- 2. no command, path, environment or parameter from an unelevated process ------


def test_main_takes_nothing():
    main = next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    assert not main.args.args and not main.args.posonlyargs and not main.args.kwonlyargs


@pytest.mark.parametrize(
    ("owner", "attribute"),
    [
        ("sys", "argv"),
        ("os", "environ"),
        ("os", "getenv"),
        ("os", "environb"),
        ("os", "getcwd"),
        ("os", "expanduser"),
        ("path", "expanduser"),
        ("path", "expandvars"),
        ("Path", "home"),
    ],
)
def test_nothing_reads_the_callers_world(owner, attribute):
    """The task runs with the user's environment, so each of these is unelevated input."""
    for node in ast.walk(TREE):
        if isinstance(node, ast.Attribute) and node.attr == attribute:
            assert getattr(node.value, "id", getattr(node.value, "attr", "")) != owner


def test_no_builtin_takes_input():
    called = {
        node.func.id
        for node in ast.walk(TREE)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not called & {"input", "eval", "exec", "compile", "__import__"}


def test_no_shell_anywhere():
    for node in ast.walk(TREE):
        if isinstance(node, ast.keyword) and node.arg == "shell":
            pytest.fail("a shell would parse an argument")


def test_a_child_gets_the_fixed_environment_not_this_one(monkeypatch):
    monkeypatch.setenv("PATH", r"C:\evil")
    monkeypatch.setenv("SystemRoot", r"C:\evil")
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs, argv=argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)
    assert worker.run_step((worker.SC, "query", "x")) == 0
    assert seen["env"] == worker.CHILD_ENV
    assert seen["cwd"] == worker.SYSTEM32
    assert seen["stdin"] == subprocess.DEVNULL
    assert "shell" not in seen


def test_a_program_that_will_not_start_is_a_failed_step(monkeypatch):
    def refused(*_a, **_k):
        raise OSError("no")

    monkeypatch.setattr(worker.subprocess, "run", refused)
    assert worker.run_step((worker.SC, "query", "x")) == -1
    assert worker.process_listing() is None


def test_the_worker_imports_nothing_of_ours():
    """It runs from a copy under an interpreter with no devkit on its path."""
    imported = set()
    for node in ast.walk(TREE):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported - sys.stdlib_module_names == set()


# --- 3. the reparse points of a spool the user can write ---------------------------


@windows_only
def test_a_spool_that_is_a_junction_is_refused_and_its_target_untouched(tmp_path):
    """The attack that matters: "delete anything else" pointed at a system folder."""
    import _winapi

    rig = Rig(tmp_path / "rig")
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "precious.dll").touch()
    os.unlink(rig.spool / worker.SENTINEL)
    os.rmdir(rig.spool)
    _winapi.CreateJunction(str(victim), str(rig.spool))
    report = rig.cycle()
    assert report.failed
    assert (victim / "precious.dll").exists()
    assert rig.ran == []


@windows_only
def test_a_junction_in_the_spool_goes_and_its_target_stays(rig, tmp_path):
    import _winapi

    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "precious.dll").touch()
    _winapi.CreateJunction(str(victim), str(rig.spool / "restart-wsl-service.request"))
    report = rig.cycle()
    assert rig.ran == [], "a reparse point with a verb's name is not a request"
    assert rig.left == []
    assert (victim / "precious.dll").exists()
    assert not report.failed


def test_an_entry_that_is_not_a_regular_file_never_runs(rig, monkeypatch):
    """A symlink wearing a verb's name, as `scan` reports it. Portable, since creating a
    real one takes a privilege most tokens lack; the junction above is the real thing."""
    gone = []
    link = worker.Entry("restart-wsl-service.request", "x", regular=False, directory=False)
    monkeypatch.setattr(worker, "scan", lambda _spool: [link])
    monkeypatch.setattr(worker, "remove", lambda entry: gone.append(entry) or True)
    rig.cycle()
    assert rig.ran == []
    assert gone == [link]


def test_a_directory_carrying_the_reparse_flag_is_not_plain(monkeypatch):
    """What `cycle` refuses a spool on, whichever kind of link it is."""

    class Info:
        st_mode = stat.S_IFDIR
        st_file_attributes = stat.FILE_ATTRIBUTE_REPARSE_POINT

    monkeypatch.setattr(worker.os, "lstat", lambda _path: Info())
    assert not worker.plain_directory("x")


def test_a_missing_spool_is_refused(tmp_path):
    rig = Rig(tmp_path)
    os.unlink(rig.spool / worker.SENTINEL)
    os.rmdir(rig.spool)
    assert rig.cycle().failed


def test_the_sentinel_stays_and_runs_nothing(rig):
    assert rig.cycle().lines == []
    assert (rig.spool / worker.SENTINEL).exists()


def test_nothing_is_deleted_by_a_walk():
    """`shutil.rmtree` and `os.walk` are path-based, so a swap under them escapes."""
    assert "shutil" not in SOURCE
    assert "os.walk" not in SOURCE


# --- 4. it writes only under `ROOT` ------------------------------------------------


def test_a_full_cycle_writes_only_in_its_folder(rig):
    for verb in worker.VERBS:
        rig.ask(verb)
    before = set(rig.root.parent.iterdir())
    assert not worker.guarded(rig.layout, rig.machine)
    assert set(rig.root.parent.iterdir()) == before
    assert {path.name for path in rig.home.iterdir()} == {worker.LOG_NAME, worker.LIMITS_NAME}


def test_every_path_literal_is_its_own_folder_or_system32():
    literals = {
        node.value
        for node in ast.walk(TREE)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and re.match(r"^[A-Za-z]:\\", node.value)
    }
    assert literals == {worker.ROOT, r"C:\Windows\System32", r"C:\Windows"}


def test_the_worker_never_starts_docker_desktop_or_the_cli():
    """Desktop would run its backend elevated, and `docker` loads plugins from `~/.docker`."""
    assert not re.search(r"Docker Desktop\.exe|docker\.exe", SOURCE.split('"""', 2)[2])


# --- 5. a rate limit per verb, kept where a loop cannot reset it --------------------


def test_each_verb_names_its_limit():
    assert worker.VERBS[RESTART].every == worker.RESTART_EVERY
    assert worker.VERBS["restart-docker-service"].every == worker.RESTART_EVERY
    assert worker.VERBS[REBOOT].every == worker.REBOOT_EVERY == 24 * 60 * 60


def test_a_loop_reboots_once(rig):
    """An unelevated bug writing the request every cycle for a day."""
    for _cycle in range(288):
        rig.ask(REBOOT)
        rig.cycle()
        rig.clock += 300
    assert rig.ran == [step.argv for step in worker.VERBS[REBOOT].steps]


def test_it_runs_again_once_the_window_has_passed(rig):
    rig.ask(REBOOT)
    rig.cycle()
    rig.clock += worker.REBOOT_EVERY
    rig.ask(REBOOT)
    rig.cycle()
    assert len(rig.ran) == 2


def test_a_limited_request_is_dropped_not_held(rig):
    """Held, a restart would fire hours later into whatever is running by then."""
    rig.ask(RESTART)
    rig.cycle()
    rig.ran.clear()
    rig.clock += 60
    rig.ask(RESTART)
    report = rig.cycle()
    assert rig.ran == []
    assert rig.left == []
    assert "rate limit" in " ".join(report.lines)


def test_one_verbs_limit_is_not_anothers(rig):
    rig.ask(RESTART)
    rig.cycle()
    rig.ask("restart-docker-service")
    rig.cycle()
    assert rig.ran[-1][1:] == ("start", worker.DOCKER_SERVICE)


def test_the_limit_is_kept_in_the_admin_only_folder_across_runs(rig):
    rig.ask(RESTART)
    rig.cycle()
    saved = json.loads((rig.home / worker.LIMITS_NAME).read_text(encoding="utf-8"))
    assert saved == {RESTART: NOW}
    fresh = worker.Machine(rig.run, lambda: "", lambda: NOW + 60, lambda _s: None)
    rig.ran.clear()
    rig.ask(RESTART)
    worker.cycle(rig.layout, fresh)
    assert rig.ran == [], "a new process read the same window"


def test_the_limit_is_not_in_the_spool(rig):
    rig.ask(RESTART)
    rig.cycle()
    assert worker.LIMITS_NAME not in os.listdir(rig.spool)


@pytest.mark.parametrize("broken", ["not json", "[1]", '{"reboot-when-idle": "soon"}'])
def test_a_broken_record_fails_closed_for_one_window(rig, broken):
    (rig.home / worker.LIMITS_NAME).write_text(broken, encoding="utf-8")
    rig.ask(REBOOT)
    rig.cycle()
    assert rig.ran == [], "unreadable reads as just ran"
    rig.clock += worker.REBOOT_EVERY
    rig.ask(REBOOT)
    rig.cycle()
    assert len(rig.ran) == 1, "and it heals, rather than locking the verb for good"


def test_a_clock_set_back_does_not_open_the_window():
    assert worker.limited(NOW + 1000, 60, NOW)
    assert not worker.limited(None, 60, NOW)
    assert not worker.limited(NOW - 60, 60, NOW)


def test_a_window_that_cannot_be_saved_means_it_does_not_run(rig, monkeypatch):
    def refused(*_a, **_k):
        raise PermissionError("no")

    monkeypatch.setattr(worker, "write_limits", refused)
    rig.ask(RESTART)
    report = rig.cycle()
    assert rig.ran == []
    assert report.failed


def test_the_window_is_saved_before_anything_runs(rig):
    """So a run that dies half way leaves the verb spent, not ready to go again."""
    seen = []

    def run(argv):
        seen.append(json.loads((rig.home / worker.LIMITS_NAME).read_text(encoding="utf-8")))
        return 0

    machine = worker.Machine(run, lambda: "", lambda: NOW, lambda _s: None)
    rig.ask(REBOOT)
    worker.cycle(rig.layout, machine)
    assert seen == [{REBOOT: NOW}]


def test_a_request_that_will_not_delete_does_not_run(rig, monkeypatch):
    """Or it would run again every window, for as long as it would not go."""
    monkeypatch.setattr(worker, "remove", lambda _entry: False)
    rig.ask(RESTART)
    report = rig.cycle()
    assert rig.ran == []
    assert report.failed


# --- the steps of a verb -----------------------------------------------------------


def test_a_failed_step_is_a_failure_and_does_not_stop_the_next(tmp_path):
    rig = Rig(tmp_path, code=5)
    rig.ask("restart-docker-service")
    assert rig.cycle().failed
    assert len(rig.ran) == 2, "a stop refused must still reach its start"


def test_a_service_already_in_the_state_is_not_a_failure(tmp_path):
    steps = worker.VERBS["restart-docker-service"].steps
    assert worker.NOT_STARTED in steps[0].accepted
    assert worker.ALREADY_RUNNING in steps[1].accepted


def test_wsl_is_ended_by_process_because_a_stop_is_refused_while_one_is_pending():
    """The case the verb exists for: STOP_PENDING, which `sc stop` cannot reach."""
    first = worker.VERBS[RESTART].steps[0].argv
    assert Path(first[0].replace("\\", "/")).name == "taskkill.exe"
    assert first[-1] == f"SERVICES eq {worker.WSL_SERVICE}"


def test_steps_settle_between_each_other(rig):
    pauses = []
    machine = worker.Machine(rig.run, lambda: "", lambda: NOW, pauses.append)
    rig.ask(RESTART)
    worker.cycle(rig.layout, machine)
    assert pauses == [worker.SETTLE_SECONDS]


def test_nothing_else_in_scripts_may_reboot():
    """The reboot needs no elevation, so the limit only means anything if this is the
    one path to it. Whole string literals, so `wsl --shutdown` and prose stay out of it."""

    def names_it(path: Path) -> bool:
        return any(
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.lower().split("\\")[-1] in ("shutdown", "shutdown.exe")
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        )

    found = {path.name for path in (REPO_ROOT / "scripts").rglob("*.py") if names_it(path)}
    assert found == {"elevated_worker.py"}


# --- the gate on a reboot ----------------------------------------------------------


def test_a_reboot_refused_by_the_gate_stays_for_the_next_cycle(tmp_path):
    rig = Rig(tmp_path, refusal="input 12s ago")
    rig.ask(REBOOT)
    report = rig.cycle()
    assert rig.ran == []
    assert rig.left == [f"{REBOOT}.request"]
    assert "deferred, input 12s ago" in report.lines[0]
    assert not report.failed


def test_a_deferred_reboot_does_not_spend_its_window(tmp_path):
    rig = Rig(tmp_path, refusal="busy")
    rig.ask(REBOOT)
    rig.cycle()
    rig.refusal = ""
    rig.cycle()
    assert len(rig.ran) == 1
    assert rig.left == []


def test_only_the_reboot_is_gated(tmp_path):
    rig = Rig(tmp_path, refusal="busy")
    rig.ask(RESTART)
    rig.cycle()
    assert rig.ran


IDLE = worker.IDLE_SECONDS


def test_the_gate_is_thirty_minutes():
    assert worker.IDLE_SECONDS == 30 * 60


@pytest.mark.parametrize(
    ("idle", "listing", "refused"),
    [
        (IDLE, '"explorer.exe","1"\n', False),
        (IDLE - 1, '"explorer.exe","1"\n', True),
        (IDLE, '"claude.exe","1"\n', True),
        (IDLE, '"codex.exe","1"\n', True),
        (IDLE, '"CLAUDE.EXE","1"\n', True),
        (IDLE, '"docker-buildx.exe","1"\n', True),
        (IDLE, '"docker-compose.exe","1"\n', True),
        (None, '"explorer.exe","1"\n', True),
        (IDLE, None, True),
    ],
)
def test_the_gate(idle, listing, refused):
    assert bool(worker.refusal_of(idle, listing)) is refused


def test_what_is_resident_does_not_read_as_busy():
    """Both were running on the machine this was written on, all day, doing no work: a
    substring match would mean this PC never reads idle."""
    listing = '"com.docker.build.exe","1"\n"codex-windows-sandbox-service.exe","2"\n'
    assert worker.busy_images(listing) == []


def test_the_gate_names_what_is_running():
    assert worker.refusal_of(IDLE, '"claude.exe","1"\n"codex.exe","2"\n') == (
        "running: claude.exe, codex.exe"
    )


def test_no_listing_is_spawned_for_a_machine_in_use(monkeypatch):
    monkeypatch.setattr(worker, "idle_seconds", lambda: 5.0)
    monkeypatch.setattr(worker, "process_listing", lambda: pytest.fail("spawned"))
    assert worker.reboot_refusal() == "input 5s ago"


def test_the_idle_clock_survives_the_tick_counter_wrapping():
    assert worker.elapsed_seconds(1_000, 0xFFFFFFFF - 999) == 2.0
    assert worker.elapsed_seconds(61_000, 1_000) == 60.0
    assert worker.elapsed_seconds(-1, 0xFFFFFFFF - 2000) == 2.0, "ctypes hands back a signed int"


@windows_only
def test_this_session_answers_the_idle_question():
    assert isinstance(worker.idle_seconds(), float)


@windows_only
def test_the_fixed_environment_is_enough_for_the_programs_on_this_machine():
    """Read-only: the listing the gate reads, under exactly what a child is given."""
    listing = worker.process_listing()
    assert listing and ".exe" in listing
    for program in (worker.SC, worker.TASKKILL, worker.TASKLIST, worker.SHUTDOWN):
        assert os.path.isfile(program)


# --- the run -----------------------------------------------------------------------


def test_a_failure_is_the_exit_code_because_it_cannot_reach_the_ledger(rig):
    (rig.spool / "full").mkdir()
    (rig.spool / "full" / "x").touch()
    assert worker.guarded(rig.layout, rig.machine) == 1


def test_a_crash_is_logged_and_left_to_be_the_exit_code(rig, monkeypatch):
    def boom(*_a):
        raise RuntimeError("the unexpected")

    monkeypatch.setattr(worker, "cycle", boom)
    with pytest.raises(RuntimeError):
        worker.guarded(rig.layout, rig.machine)
    assert "the unexpected" in (rig.home / worker.LOG_NAME).read_text(encoding="utf-8")


def test_a_broken_record_that_will_not_rewrite_runs_nothing(rig, monkeypatch):
    def refused(*_a, **_k):
        raise PermissionError("no")

    (rig.home / worker.LIMITS_NAME).write_text("not json", encoding="utf-8")
    monkeypatch.setattr(worker, "write_limits", refused)
    rig.ask(REBOOT)
    with pytest.raises(PermissionError):
        rig.cycle()
    assert rig.ran == []


def test_a_log_that_cannot_be_written_is_a_failure(rig, monkeypatch):
    def refused(*_a, **_k):
        raise OSError("no")

    monkeypatch.setattr(worker, "append_log", refused)
    assert worker.guarded(rig.layout, rig.machine) == 1


def test_a_quiet_cycle_leaves_no_log(rig):
    assert worker.guarded(rig.layout, rig.machine) == 0
    assert not (rig.home / worker.LOG_NAME).exists()


def test_the_log_is_bounded_and_keeps_the_newest_line(tmp_path):
    log = str(tmp_path / "worker.log")
    for index in range(50):
        worker.append_log(log, [f"line {index} " + "x" * 40], NOW, keep=200)
    text = Path(log).read_text(encoding="utf-8")
    assert len(text) <= 200
    assert "line 49 " in text
    assert text.startswith("20"), "cut at a line, not inside one"


def test_a_plain_directory_is_not_a_file_or_nothing(tmp_path):
    (tmp_path / "f").touch()
    assert worker.plain_directory(str(tmp_path))
    assert not worker.plain_directory(str(tmp_path / "f"))
    assert not worker.plain_directory(str(tmp_path / "absent"))


def test_a_reparse_flag_is_read_off_the_attributes():
    class Info:
        st_file_attributes = stat.FILE_ATTRIBUTE_REPARSE_POINT

    assert worker.is_reparse(Info())


def test_it_says_windows_only_and_exits_clean_elsewhere(monkeypatch, capsys):
    monkeypatch.setattr(worker.os, "name", "posix")
    assert worker.main() == 0
    assert "Windows-only" in capsys.readouterr().out
