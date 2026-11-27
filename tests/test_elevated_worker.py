"""`scripts/elevated_worker.py`: the one thing devkit runs as an administrator.

Each `# ---` block below is one line of the threat model, in the order the module states
them, then the recovery itself. Every test here drives `cycle` over a temporary layout:
nothing touches `C:\\ProgramData` or a service, and the real `main` is never entered on a
machine that could be running the real thing.
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
RECOVERY = [worker.WSL_KILL, worker.WSL_UPDATE, worker.WSL_LIST]
# Real NTFS and Win32: the local tier, which CI deselects and `scripts/e2e.py` runs nightly.
windows_only = pytest.mark.local


def listing(state: str) -> str:
    return f"\r\nSERVICE_NAME: WSLService \r\n        STATE              : 3  {state}  \r\n"


class Rig:
    """A temporary layout, and a `Machine` over a service that is stuck until it is killed."""

    def __init__(self, tmp_path: Path, stuck: bool = True, cured: bool = True):
        self.root = tmp_path / "devkit"
        self.spool = self.root / "requests"
        self.home = self.root / "elevated"
        self.spool.mkdir(parents=True)
        self.home.mkdir()
        (self.spool / worker.SENTINEL).touch()
        self.layout = worker.Layout(str(self.root), str(self.spool), str(self.home))
        self.ran: list[tuple[str, ...]] = []
        self.timeouts: list[int] = []
        self.clock = NOW
        self.stuck = stuck
        self.cured = cured
        self.hung = False

    def run(self, argv, timeout):
        self.ran.append(tuple(argv))
        self.timeouts.append(timeout)
        if tuple(argv) == worker.WSL_KILL and self.cured:
            self.stuck = self.hung = False
        if tuple(argv) == worker.WSL_LIST and (self.stuck or self.hung):
            return worker.TIMED_OUT
        return 0

    def query(self, argv, timeout):
        assert tuple(argv) == worker.WSL_QUERY
        self.timeouts.append(timeout)
        return listing("STOP_PENDING" if self.stuck else "RUNNING")

    @property
    def machine(self):
        return worker.Machine(self.run, self.query, lambda: self.clock, lambda _s: None)

    def ask(self, verb: str = RESTART, content: bytes = b"") -> Path:
        path = self.spool / f"{verb}.request"
        path.write_bytes(content)
        return path

    def cycle(self):
        return worker.cycle(self.layout, self.machine)

    def fire(self) -> int:
        return worker.guarded(self.layout, self.machine)

    @property
    def killed(self) -> int:
        return self.ran.count(worker.WSL_KILL)

    @property
    def left(self) -> list[str]:
        return sorted(name for name in os.listdir(self.spool) if name != worker.SENTINEL)

    @property
    def results(self) -> dict:
        return json.loads((self.home / worker.RESULTS_NAME).read_text(encoding="utf-8"))


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def test_there_is_one_verb_and_it_is_the_request_modules():
    assert list(worker.VERBS) == [RESTART]
    assert worker.VERBS.keys() == set(request.VERBS)


def test_the_layout_is_the_request_modules():
    assert worker.SPOOL == r"C:\ProgramData\devkit\requests" == request.SPOOL
    assert worker.HOME == r"C:\ProgramData\devkit\elevated" == request.HOME
    assert worker.REQUEST_SUFFIX == request.REQUEST_SUFFIX == ".request"
    assert worker.RESULTS_NAME == Path(request.RESULTS.replace("\\", "/")).name
    assert (worker.FAILED, worker.REFUSED) == (request.FAILED, request.REFUSED)


def test_the_copies_of_the_recovery_match_what_they_were_copied_from():
    """It cannot import them, and a worker drifting from `recover-wsl` is one nobody sees
    until the service sticks."""
    tasks = load_script("scripts/collector_tasks.py")
    maint = load_script("scripts/docker-maint.py")
    assert worker.WSL_SERVICE == tasks.WSL_QUERY[-1] == maint.WSL_SERVICE
    assert worker.WSL_QUERY[1:] == tasks.WSL_QUERY[1:]
    assert worker.WSL_SETTLE == tasks.WSL_SETTLE
    assert worker.SERVICE_STATE.pattern == tasks.SERVICE_STATE.pattern
    assert list(worker.WSL_KILL[1:]) == maint.WSL_KILL[1:]
    assert worker.WSL_SETTLED == maint.WSL_SETTLED
    assert worker.WSL_SETTLE_WAIT == maint.WSL_SETTLE_WAIT
    assert worker.WSL_ANSWER_TIMEOUT == maint.WSL_ANSWER_TIMEOUT
    assert worker.WSL_UPDATE_TIMEOUT == maint.WSL_UPDATE_TIMEOUT
    assert worker.TIMED_OUT == maint.TIMED_OUT


# --- 1. a fixed argv, and only an exact name selects a verb --------------------------


def test_a_stuck_service_gets_exactly_its_literal_argvs(rig):
    rig.ask()
    report = rig.cycle()
    assert rig.ran == RECOVERY
    assert not report.failed
    assert rig.left == []


@pytest.mark.parametrize(
    "argv", [worker.WSL_QUERY, worker.WSL_KILL, worker.WSL_LIST, worker.WSL_UPDATE]
)
def test_every_program_is_a_literal_absolute_path_under_system32(argv):
    """A bare `sc` would be found through the user's PATH or cwd, and `%SystemRoot%` is an
    environment value the user sets. Every argument is a literal for the same reason."""
    assert argv[0].startswith("C:\\Windows\\System32\\") and argv[0].endswith(".exe")
    assert all(isinstance(part, str) and "%" not in part for part in argv)


def test_the_argvs_are_data_built_from_constants():
    """No function builds one: each module-level `WSL_*` argv takes module constants."""
    names = {
        node.id
        for assign in TREE.body
        if isinstance(assign, ast.Assign)
        and getattr(assign.targets[0], "id", "")
        in {"WSL_QUERY", "WSL_KILL", "WSL_LIST", "WSL_UPDATE"}
        for node in ast.walk(assign.value)
        if isinstance(node, ast.Name)
    }
    assert names == {"SC", "TASKKILL", "WSL", "WSL_SERVICE"}


def test_a_requests_content_is_never_opened(rig, monkeypatch):
    """Not merely unused: a parser that looks at it is one bug from obeying it."""
    rig.ask(content=b"& calc.exe\n--service evil\n")
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
    assert rig.ran == RECOVERY, "it still ran"
    assert not [path for path in opened if "request" in Path(path).name], opened


def test_content_changes_nothing(tmp_path):
    def ran_with(content: bytes, name: str):
        rig = Rig(tmp_path / name)
        rig.ask(content=content)
        rig.cycle()
        return rig.ran

    assert ran_with(b"", "empty") == ran_with(b"--update --web-download", "hostile")


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
        "reboot-when-idle.request",
        "restart-docker-service.request",
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
        "restart-wsl-service.request\N{ZERO WIDTH SPACE}",
        "RESTAR~1.REQ",
        "NUL",
        "",
    ],
)
def test_only_the_exact_name_selects_a_verb(name):
    assert worker.verb_of(name) is None


def test_the_exact_name_does_select_it():
    assert worker.verb_of(f"{RESTART}.request") == RESTART


def test_anything_else_in_the_spool_is_deleted_logged_and_ignored(rig):
    (rig.spool / "whoami.request").write_bytes(b"x")
    (rig.spool / "restart-wsl-service.req").touch()
    (rig.spool / "notes.txt").touch()
    (rig.spool / "a-folder").mkdir()
    (rig.spool / "restart-wsl-service.request").mkdir()
    report = rig.cycle()
    assert rig.ran == [], "a directory with a verb's name is not a request"
    assert rig.left == []
    assert sum(line.endswith(": deleted") for line in report.lines) == 5
    assert "'whoami.request'" in " ".join(report.lines)
    assert not report.failed


def test_a_failed_delete_is_not_logged_as_deleted(rig, monkeypatch):
    (rig.spool / "junk").touch()
    monkeypatch.setattr(worker, "remove", lambda _entry: False)
    report = rig.cycle()
    assert report.lines == ["ignored 'junk': could not be deleted"]
    assert report.failed


def test_a_folder_with_something_in_it_stays_and_says_so(rig):
    """Never recursive: a walk could be sent through a junction swapped in under it."""
    folder = rig.spool / "full"
    folder.mkdir()
    (folder / "x").touch()
    report = rig.cycle()
    assert report.failed
    assert "could not be deleted" in report.lines[0]
    assert (folder / "x").exists()


def test_a_name_cannot_forge_a_log_line():
    assert "\n" not in worker.shown("x\nrestart-wsl-service: ok")
    assert len(worker.shown("y" * 10_000)) <= 120


@windows_only
def test_a_stream_on_a_valid_request_is_neither_read_nor_left(rig):
    path = rig.ask()
    with open(f"{path}:payload", "w", encoding="utf-8") as handle:
        handle.write("something else")
    rig.cycle()
    assert rig.ran == RECOVERY
    assert rig.left == []


@windows_only
def test_a_stream_named_like_a_request_selects_nothing(rig):
    host = rig.spool / "junk"
    host.touch()
    with open(f"{host}:restart-wsl-service.request", "w", encoding="utf-8") as handle:
        handle.write("x")
    with open(f"{rig.spool}:restart-wsl-service.request", "w", encoding="utf-8") as handle:
        handle.write("x")
    report = rig.cycle()
    assert rig.ran == []
    assert rig.left == []
    assert "'junk'" in " ".join(report.lines)


@windows_only
def test_a_name_win32_would_rewrite_is_that_name(rig):
    """`\\\\?\\` makes this entry reachable, and a Win32 path would strip its dot and hit
    the real request beside it."""
    real = rig.ask()
    dotted = "\\\\?\\" + str(rig.spool / "restart-wsl-service.request.")
    with open(dotted, "w", encoding="utf-8"):
        pass
    entries = {entry.name: entry for entry in worker.scan(str(rig.spool))}
    assert worker.remove(entries["restart-wsl-service.request."])
    assert real.exists()


# --- a read-only request cannot jam the spool --------------------------------------


@windows_only
def test_a_read_only_request_runs_and_goes(rig):
    """Review, 2026-11-06: `0o444` on the create needs nothing but `(WD)`, and a read-only
    file refuses a delete even to an administrator, so the verb never ran again."""
    path = "\\\\?\\" + str(rig.spool / f"{RESTART}.request")
    os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o444))
    assert os.stat(path).st_file_attributes & stat.FILE_ATTRIBUTE_READONLY
    report = rig.cycle()
    assert rig.ran == RECOVERY
    assert rig.left == []
    assert not report.failed


@windows_only
def test_read_only_junk_goes_too(rig):
    path = "\\\\?\\" + str(rig.spool / "junk")
    os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o444))
    assert rig.cycle().lines == ["ignored 'junk': deleted"]
    assert rig.left == []


def test_the_attribute_is_only_cleared_on_a_plain_file(rig, monkeypatch):
    """A link's attribute call could land on what it names."""
    cleared = []
    monkeypatch.setattr(worker, "clear_readonly", cleared.append)
    plain = rig.spool / "a"
    plain.touch()
    assert worker.remove(worker.Entry("a", str(plain), regular=True, directory=False))
    link = rig.spool / "b"
    link.touch()
    assert worker.remove(worker.Entry("b", str(link), regular=False, directory=False))
    assert cleared == [str(plain)]


def test_off_windows_clearing_the_attribute_touches_nothing(tmp_path, monkeypatch):
    held = tmp_path / "a"
    held.touch()
    held.chmod(0o444)
    monkeypatch.setattr(worker.sys, "platform", "linux")
    worker.clear_readonly(str(held))
    assert not held.stat().st_mode & stat.S_IWRITE
    held.chmod(0o666)


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


@pytest.mark.parametrize("spawn", ["run_step", "query_step"])
def test_a_child_gets_the_fixed_environment_not_this_one(monkeypatch, spawn):
    monkeypatch.setenv("PATH", r"C:\evil")
    monkeypatch.setenv("SystemRoot", r"C:\evil")
    monkeypatch.setenv("TEMP", r"C:\evil")
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs, argv=argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)
    getattr(worker, spawn)((worker.SC, "query", "x"), 5)
    assert seen["env"] == worker.CHILD_ENV
    assert seen["cwd"] == worker.SYSTEM32
    assert seen["stdin"] == subprocess.DEVNULL
    assert seen["timeout"] == 5
    assert "shell" not in seen


def test_the_update_downloads_only_into_the_admin_only_folder():
    assert worker.CHILD_ENV["TEMP"] == worker.CHILD_ENV["TMP"] == worker.SCRATCH
    assert worker.SCRATCH.startswith(worker.HOME)


def test_a_step_captures_nothing_so_a_grandchild_cannot_outlast_its_timeout(monkeypatch):
    """A killed `wsl.exe` whose child still holds a pipe would hold the wait open."""
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(worker.subprocess, "run", fake_run)
    worker.run_step(worker.WSL_LIST, 5)
    assert seen["stdout"] == seen["stderr"] == subprocess.DEVNULL


def test_a_timeout_and_a_spawn_failure_are_told_apart(monkeypatch):
    def expired(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 5)

    def refused(*_a, **_k):
        raise OSError("no")

    monkeypatch.setattr(worker.subprocess, "run", expired)
    assert worker.run_step(worker.WSL_LIST, 5) == worker.TIMED_OUT
    assert worker.query_step(worker.WSL_QUERY, 5) == ""
    monkeypatch.setattr(worker.subprocess, "run", refused)
    assert worker.run_step(worker.WSL_LIST, 5) == worker.SPAWN_FAILED
    assert worker.query_step(worker.WSL_QUERY, 5) == ""


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


def test_a_missing_spool_is_refused_and_says_so_to_the_pass(tmp_path):
    rig = Rig(tmp_path)
    os.unlink(rig.spool / worker.SENTINEL)
    os.rmdir(rig.spool)
    assert rig.fire() == 1
    assert rig.results[worker.WORKER]["result"] == worker.REFUSED


def test_the_sentinel_stays_and_runs_nothing(rig):
    assert rig.cycle().lines == []
    assert (rig.spool / worker.SENTINEL).exists()


def test_nothing_is_deleted_by_a_walk():
    """`shutil.rmtree` and `os.walk` are path-based, so a swap under them escapes."""
    assert "shutil" not in SOURCE
    assert "os.walk" not in SOURCE


# --- 4. it writes only under `ROOT` ------------------------------------------------


def test_a_full_cycle_writes_only_in_its_folder(rig):
    rig.ask()
    before = set(rig.root.parent.iterdir())
    assert rig.fire() == 0
    assert set(rig.root.parent.iterdir()) == before
    assert {path.name for path in rig.home.iterdir()} == {
        worker.LOG_NAME,
        worker.LIMITS_NAME,
        worker.RESULTS_NAME,
    }


def test_every_path_literal_is_its_own_folder_or_the_system():
    literals = {
        node.value
        for node in ast.walk(TREE)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and re.match(r"^[A-Za-z]:\\", node.value)
    }
    assert literals == {
        worker.ROOT,
        r"C:\Windows\System32",
        r"C:\Windows",
        r"C:\ProgramData",
        r"C:\Program Files",
    }


def test_the_worker_never_starts_docker_desktop_or_the_cli():
    """Desktop would run its backend elevated, and `docker` loads plugins from `~/.docker`."""
    assert not re.search(r"Docker Desktop\.exe|docker\.exe", SOURCE.split('"""', 2)[2])


# --- 5. a healthy service is left alone ---------------------------------------------


def test_a_healthy_service_is_left_alone_and_that_is_logged(tmp_path):
    rig = Rig(tmp_path, stuck=False)
    rig.ask()
    assert rig.fire() == 0
    assert worker.WSL_KILL not in rig.ran
    assert worker.WSL_UPDATE not in rig.ran
    assert rig.left == [], "the request is answered, not held"
    assert "healthy; left alone" in (rig.home / worker.LOG_NAME).read_text(encoding="utf-8")
    assert rig.results[RESTART]["result"] == worker.HEALTHY


def test_a_healthy_service_spends_no_window(tmp_path):
    rig = Rig(tmp_path, stuck=False)
    rig.ask()
    rig.cycle()
    assert not (rig.home / worker.LIMITS_NAME).exists()
    rig.stuck = True
    rig.ask()
    rig.cycle()
    assert rig.killed == 1


@pytest.mark.parametrize(
    ("states", "code", "unhealthy"),
    [
        (["RUNNING"], 0, ""),
        (["STOPPED"], 0, ""),
        ([""], 0, ""),
        (["RUNNING"], 1, ""),
        (["RUNNING"], worker.SPAWN_FAILED, ""),
        (["START_PENDING", "RUNNING"], 0, ""),
        (["STOP_PENDING", "STOPPED"], 0, ""),
        (["STOP_PENDING", "STOP_PENDING"], 0, "WSLService is stuck in STOP_PENDING"),
        (["START_PENDING", "START_PENDING"], 0, "WSLService is stuck in START_PENDING"),
        (
            ["RUNNING"],
            worker.TIMED_OUT,
            "WSLService is RUNNING and wsl --list does not answer",
        ),
        (
            [""],
            worker.TIMED_OUT,
            "WSLService is in an unknown state and wsl --list does not answer",
        ),
    ],
)
def test_what_reads_as_unhealthy(states, code, unhealthy):
    """`docker-maint.wsl_stuck`'s verdicts: a start in progress is not stuck, and only
    silence is hung, since any exit code is WSL answering."""
    answers = iter(states)
    pauses = []
    machine = worker.Machine(
        lambda argv, timeout: code,
        lambda argv, timeout: listing(next(answers)),
        lambda: NOW,
        pauses.append,
    )
    assert worker.wsl_unhealthy(machine) == unhealthy
    if len(states) == 2:
        assert pauses == [worker.WSL_SETTLE]


def test_each_query_has_a_bound(rig):
    rig.stuck = False
    rig.hung = True
    assert worker.wsl_unhealthy(rig.machine)
    assert sorted(rig.timeouts) == [worker.QUERY_TIMEOUT, worker.WSL_ANSWER_TIMEOUT]


def test_nothing_in_the_health_check_changes_anything(tmp_path):
    rig = Rig(tmp_path, stuck=False)
    worker.wsl_unhealthy(rig.machine)
    assert rig.ran == [worker.WSL_LIST]
    assert "--list" in worker.WSL_LIST and "--quiet" in worker.WSL_LIST


# --- 6. a rate limit, kept where a loop cannot reset it -----------------------------


def test_the_limit_is_six_hours():
    assert worker.RESTART_EVERY == 6 * 60 * 60
    assert worker.VERBS[RESTART].every == worker.RESTART_EVERY


def test_a_loop_restarts_once_while_the_service_stays_stuck(tmp_path):
    """An unelevated bug writing the request every cycle for most of the limit, on a
    service the restart did not cure."""
    rig = Rig(tmp_path, cured=False)
    for _cycle in range(60):
        rig.ask()
        rig.cycle()
        rig.clock += 300
    assert rig.killed == 1


def test_it_runs_again_once_the_window_has_passed(tmp_path):
    rig = Rig(tmp_path, cured=False)
    rig.ask()
    rig.cycle()
    rig.clock += worker.RESTART_EVERY
    rig.ask()
    rig.cycle()
    assert rig.killed == 2


def test_a_limited_request_is_dropped_and_reported_refused(tmp_path):
    """Held, a restart would fire hours later into whatever is running by then."""
    rig = Rig(tmp_path, cured=False)
    rig.ask()
    rig.cycle()
    rig.ran.clear()
    rig.clock += 60
    rig.ask()
    assert rig.fire() == 0, "a refusal is not the job failing"
    assert worker.WSL_KILL not in rig.ran
    assert rig.left == []
    held = rig.results[RESTART]
    assert held["result"] == worker.REFUSED
    assert held["detail"] == "WSLService is stuck in STOP_PENDING, inside its 6-hour rate limit"


def test_the_limit_is_kept_in_the_admin_only_folder_across_runs(tmp_path):
    rig = Rig(tmp_path, cured=False)
    rig.ask()
    rig.cycle()
    saved = json.loads((rig.home / worker.LIMITS_NAME).read_text(encoding="utf-8"))
    assert saved == {RESTART: NOW}
    assert worker.LIMITS_NAME not in os.listdir(rig.spool)
    rig.ran.clear()
    rig.clock += 60
    rig.ask()
    worker.cycle(rig.layout, rig.machine)
    assert worker.WSL_KILL not in rig.ran, "a new process read the same window"


@pytest.mark.parametrize("broken", ["not json", "[1]", '{"restart-wsl-service": "soon"}'])
def test_a_broken_record_fails_closed_for_one_window(tmp_path, broken):
    rig = Rig(tmp_path, cured=False)
    (rig.home / worker.LIMITS_NAME).write_text(broken, encoding="utf-8")
    rig.ask()
    rig.cycle()
    assert rig.killed == 0, "unreadable reads as just ran"
    rig.clock += worker.RESTART_EVERY
    rig.ask()
    rig.cycle()
    assert rig.killed == 1, "and it heals, rather than locking the verb for good"


def test_a_clock_set_back_does_not_open_the_window():
    assert worker.limited(NOW + 1000, 60, NOW)
    assert not worker.limited(None, 60, NOW)
    assert not worker.limited(NOW - 60, 60, NOW)


def test_a_window_that_cannot_be_saved_means_it_does_not_run(rig, monkeypatch):
    def refused(*_a, **_k):
        raise PermissionError("no")

    monkeypatch.setattr(worker, "write_limits", refused)
    rig.ask()
    assert rig.fire() == 1
    assert rig.killed == 0
    assert rig.results[RESTART]["result"] == worker.REFUSED


def test_a_broken_record_that_will_not_rewrite_runs_nothing(rig, monkeypatch):
    def refused(*_a, **_k):
        raise PermissionError("no")

    (rig.home / worker.LIMITS_NAME).write_text("not json", encoding="utf-8")
    monkeypatch.setattr(worker, "write_limits", refused)
    rig.ask()
    with pytest.raises(PermissionError):
        rig.cycle()
    assert rig.killed == 0


def test_the_window_is_saved_before_anything_runs(rig):
    """So a run that dies half way leaves the verb spent, not ready to go again."""
    seen = []

    def run(argv, timeout):
        if tuple(argv) == worker.WSL_KILL:
            seen.append(json.loads((rig.home / worker.LIMITS_NAME).read_text(encoding="utf-8")))
        return 0

    machine = worker.Machine(run, rig.query, lambda: NOW, lambda _s: None)
    rig.ask()
    worker.cycle(rig.layout, machine)
    assert seen == [{RESTART: NOW}]


def test_a_request_that_will_not_delete_does_not_run(rig, monkeypatch):
    """Or it would run again every window, for as long as it would not go."""
    monkeypatch.setattr(worker, "remove", lambda _entry: False)
    rig.ask()
    assert rig.fire() == 1
    assert rig.killed == 0
    assert rig.results[RESTART]["detail"] == "its request could not be deleted"


# --- 7. the recovery, and what the pass hears ----------------------------------------


def test_the_recovery_runs_in_recover_wsls_order(rig):
    """End it, wait for it, then update, so Windows Update has nothing left to retry."""
    rig.ask()
    rig.cycle()
    assert rig.ran == [worker.WSL_KILL, worker.WSL_UPDATE, worker.WSL_LIST]


def test_the_update_gets_recover_wsls_timeout(rig):
    rig.ask()
    rig.cycle()
    assert worker.WSL_UPDATE_TIMEOUT in rig.timeouts


def test_it_polls_until_the_service_settles(rig):
    """Up to `WSL_SETTLE_WAIT`, and no further: it goes on to the update regardless."""
    states = iter(["STOP_PENDING", "STOP_PENDING", "STOPPED"])
    pauses = []
    machine = worker.Machine(
        rig.run, lambda argv, timeout: listing(next(states)), lambda: NOW, pauses.append
    )
    worker.wsl_recover(machine)
    assert pauses == [worker.WSL_POLL, worker.WSL_POLL]


def test_a_service_that_never_settles_is_bounded(tmp_path):
    rig = Rig(tmp_path, cured=False)
    pauses = []
    machine = worker.Machine(rig.run, rig.query, lambda: NOW, pauses.append)
    recovered, lines = worker.wsl_recover(machine)
    assert sum(pauses) == worker.WSL_SETTLE_WAIT
    assert not recovered
    assert "WSLService after the kill: STOP_PENDING" in lines


def test_a_recovered_run_is_reported(rig):
    rig.ask()
    assert rig.fire() == 0
    assert rig.results[RESTART]["result"] == worker.RECOVERED


def test_a_failed_run_is_the_exit_code_and_lasts_past_it(tmp_path):
    """The next fire overwrites the exit code; the pass reads `RESULTS`."""
    rig = Rig(tmp_path, cured=False)
    rig.ask()
    assert rig.fire() == 1
    held = rig.results[RESTART]
    assert held["result"] == worker.FAILED
    assert held["detail"] == (
        "WSLService is stuck in STOP_PENDING, and a restart did not bring it back"
    )
    assert rig.fire() == 0
    assert rig.results[RESTART]["result"] == worker.FAILED, "a quiet fire does not clear it"


def test_a_failed_update_alone_is_not_a_failed_recovery(tmp_path):
    """`recover-wsl` judges by whether WSL answers, and offline the update just fails."""
    rig = Rig(tmp_path)
    real = rig.run
    machine = worker.Machine(
        lambda argv, timeout: 5 if tuple(argv) == worker.WSL_UPDATE else real(argv, timeout),
        rig.query,
        lambda: NOW,
        lambda _s: None,
    )
    assert worker.wsl_recover(machine)[0] is True


def test_a_wsl_that_cannot_be_started_afterwards_is_not_recovered(rig):
    real = rig.run
    machine = worker.Machine(
        lambda argv, timeout: (
            worker.SPAWN_FAILED if tuple(argv) == worker.WSL_LIST else real(argv, timeout)
        ),
        rig.query,
        lambda: NOW,
        lambda _s: None,
    )
    assert worker.wsl_recover(machine)[0] is False


def test_a_result_is_written_only_when_it_changed(rig):
    rig.fire()
    first = (rig.home / worker.RESULTS_NAME).stat().st_mtime_ns
    held = rig.results
    rig.clock += 300
    rig.fire()
    assert rig.results == held
    assert (rig.home / worker.RESULTS_NAME).stat().st_mtime_ns == first


def test_a_result_the_pass_can_read_is_what_is_written(rig):
    rig.ask()
    rig.fire()
    read = request.results(str(rig.home / worker.RESULTS_NAME))
    assert read[RESTART] == (worker.RECOVERED, "answers again")
    assert read[worker.WORKER][0] == worker.HEALTHY


# --- the run -----------------------------------------------------------------------


def test_a_crash_is_logged_and_left_to_be_the_exit_code(rig, monkeypatch):
    def boom(*_a):
        raise RuntimeError("the unexpected")

    monkeypatch.setattr(worker, "cycle", boom)
    with pytest.raises(RuntimeError):
        worker.guarded(rig.layout, rig.machine)
    assert "the unexpected" in (rig.home / worker.LOG_NAME).read_text(encoding="utf-8")
    assert rig.results[worker.WORKER]["detail"] == "it crashed: RuntimeError"


def test_a_log_that_cannot_be_written_is_a_failure(rig, monkeypatch):
    def refused(*_a, **_k):
        raise OSError("no")

    monkeypatch.setattr(worker, "append_log", refused)
    assert rig.fire() == 1


def test_a_quiet_cycle_leaves_no_log(rig):
    assert rig.fire() == 0
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


@windows_only
def test_the_fixed_environment_is_enough_for_the_health_check_here():
    """Read-only, under exactly what a child is given. `wsl --update` is not run here: it
    changes the machine, and that it works under `CHILD_ENV` is unproven until a real run."""
    assert worker.service_state(worker.query_step(worker.WSL_QUERY, worker.QUERY_TIMEOUT))
    assert worker.run_step(worker.WSL_LIST, worker.WSL_ANSWER_TIMEOUT) == 0
    for program in (worker.SC, worker.TASKKILL, worker.WSL):
        assert os.path.isfile(program)


# --- the pieces a cycle is built from ------------------------------------------------


def test_a_report_collects_lines_failure_and_each_subjects_latest_result():
    report = worker.Report()
    report.say("first")
    assert not report.failed
    report.result(RESTART, worker.HEALTHY, "left alone")
    other = worker.Report()
    other.say("second", failed=True)
    other.result(RESTART, worker.FAILED, "why")
    other.result(worker.WORKER, worker.HEALTHY, "")
    report.absorb(other)
    assert report.lines == ["first", "second"]
    assert report.failed, "a failure absorbed is not forgotten"
    assert report.results == {RESTART: (worker.FAILED, "why"), worker.WORKER: (worker.HEALTHY, "")}
    report.absorb(worker.Report())
    assert report.failed


def test_the_service_state_is_read_with_its_own_query_and_bound():
    seen = []

    def query(argv, timeout):
        seen.append((tuple(argv), timeout))
        return listing("STOP_PENDING")

    machine = worker.Machine(lambda _a, _t: pytest.fail("ran"), query)
    assert worker.wsl_state(machine) == "STOP_PENDING"
    assert seen == [(worker.WSL_QUERY, worker.QUERY_TIMEOUT)]


def test_a_query_that_said_nothing_is_no_state():
    machine = worker.Machine(lambda _a, _t: 0, lambda _a, _t: "")
    assert worker.wsl_state(machine) == ""


def test_verbatim_turns_win32_name_rewriting_off_only_on_windows(monkeypatch):
    path = r"C:\ProgramData\devkit\requests"
    monkeypatch.setattr(worker.os, "name", "nt")
    rewritten = worker.verbatim(path)
    already = worker.verbatim("\\\\?\\" + path)
    monkeypatch.setattr(worker.os, "name", "posix")
    elsewhere = worker.verbatim(path)
    monkeypatch.undo()
    assert rewritten == "\\\\?\\" + path
    assert already == "\\\\?\\" + path, "never prefixed twice"
    assert elsewhere == path


def test_json_is_replaced_whole_and_leaves_no_temporary(tmp_path):
    path = str(tmp_path / "x.json")
    worker.write_json(path, {"a": 1})
    worker.write_json(path, {"b": 2})
    assert json.loads(Path(path).read_text(encoding="utf-8")) == {"b": 2}
    assert os.listdir(tmp_path) == ["x.json"]


def test_limits_written_are_the_limits_read(tmp_path):
    path = str(tmp_path / worker.LIMITS_NAME)
    worker.write_limits(path, {RESTART: NOW})
    assert worker.read_limits(path, NOW + 5) == {RESTART: NOW}


def test_no_limits_record_is_no_window_spent(tmp_path):
    path = tmp_path / worker.LIMITS_NAME
    assert worker.read_limits(str(path), NOW) == {}
    assert not path.exists(), "nothing to rewrite"


def test_a_limits_record_that_will_not_read_is_every_verb_just_run(tmp_path):
    path = tmp_path / worker.LIMITS_NAME
    path.write_text("not json", encoding="utf-8")
    assert worker.read_limits(str(path), NOW) == {RESTART: NOW}
    assert json.loads(path.read_text(encoding="utf-8")) == {RESTART: NOW}, "and rewritten"


def test_results_are_written_only_for_a_subject_that_changed(tmp_path):
    path = tmp_path / worker.RESULTS_NAME
    worker.write_results(str(path), {RESTART: (worker.FAILED, "why")}, NOW)
    worker.write_results(str(path), {RESTART: (worker.FAILED, "why")}, NOW + 300)
    held = json.loads(path.read_text(encoding="utf-8"))
    assert held == {RESTART: {"result": worker.FAILED, "detail": "why", "at": NOW}}
    worker.write_results(str(path), {worker.WORKER: (worker.HEALTHY, "")}, NOW + 600)
    held = json.loads(path.read_text(encoding="utf-8"))
    assert held[RESTART]["at"] == NOW, "another subject is kept as it was"
    assert held[worker.WORKER] == {"result": worker.HEALTHY, "detail": "", "at": NOW + 600}


def test_nothing_new_writes_no_results_file(tmp_path):
    path = tmp_path / worker.RESULTS_NAME
    worker.write_results(str(path), {}, NOW)
    assert not path.exists()


@pytest.mark.parametrize("text", ["not json", "[1]"])
def test_a_results_file_that_will_not_read_is_started_again(tmp_path, text):
    path = tmp_path / worker.RESULTS_NAME
    path.write_text(text, encoding="utf-8")
    worker.write_results(str(path), {RESTART: (worker.HEALTHY, "left alone")}, NOW)
    assert json.loads(path.read_text(encoding="utf-8")) == {
        RESTART: {"result": worker.HEALTHY, "detail": "left alone", "at": NOW}
    }


def entry_for(path: Path):
    return worker.Entry(path.name, str(path), regular=True, directory=False)


def test_serving_a_stuck_service_spends_the_window_then_recovers(rig):
    limits: dict[str, float] = {}
    request_file = rig.ask()
    report = worker.serve(entry_for(request_file), RESTART, limits, rig.layout, rig.machine)
    assert report.results == {RESTART: (worker.RECOVERED, "answers again")}
    assert not report.failed
    assert limits == {RESTART: NOW}
    assert json.loads((rig.home / worker.LIMITS_NAME).read_text(encoding="utf-8")) == limits
    assert not request_file.exists()
    assert report.lines[0] == f"{RESTART}: WSLService is stuck in STOP_PENDING"


def test_serving_a_healthy_service_spends_nothing(tmp_path):
    rig = Rig(tmp_path, stuck=False)
    limits: dict[str, float] = {}
    request_file = rig.ask()
    report = worker.serve(entry_for(request_file), RESTART, limits, rig.layout, rig.machine)
    assert report.results == {RESTART: (worker.HEALTHY, "left alone")}
    assert limits == {}
    assert not request_file.exists()


def test_serving_inside_the_window_refuses_and_runs_nothing(rig):
    limits = {RESTART: NOW - 60}
    report = worker.serve(entry_for(rig.ask()), RESTART, limits, rig.layout, rig.machine)
    assert report.results[RESTART][0] == worker.REFUSED
    assert rig.killed == 0
    assert limits == {RESTART: NOW - 60}


def test_serving_a_verb_runs_its_own_recovery_and_reports_its_failure(rig, monkeypatch):
    """Whatever the verb: the order is the same, and a recovery that did not take fails."""
    tried = []

    def recover(machine):
        tried.append(machine)
        return False, ["tried once"]

    verb = worker.Verb(lambda _machine: "it is sick", recover, 60)
    monkeypatch.setitem(worker.VERBS, "probe", verb)
    limits: dict[str, float] = {}
    request_file = rig.spool / "probe.request"
    request_file.touch()
    report = worker.serve(entry_for(request_file), "probe", limits, rig.layout, rig.machine)
    assert len(tried) == 1
    assert report.lines == ["probe: it is sick", "probe: tried once"]
    assert report.failed
    assert report.results == {
        "probe": (worker.FAILED, "it is sick, and a restart did not bring it back")
    }
    assert limits == {"probe": NOW}
    assert verb.every == 60
