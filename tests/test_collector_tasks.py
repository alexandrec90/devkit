"""`collector_tasks.py`: a scheduled collector's own task, and one fire of it.

Every `schtasks` and every spawn goes through a fake. The properties pinned are the ones
that decide whether the project's job runs and whether its failure is seen:

- the task runs `collectors.py fire <name>` from the devkit checkout, window-less, never
  under a `devkit-` name;
- a current registration is left alone, a drifted one re-registered, a `stop` one removed;
- a fire starts what the command `needs` first, and does not run it when that fails;
- the exit code is the command's own, and its output is kept, tail first.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import ClassVar

from support import load_script

tasks = load_script("scripts/collector_tasks.py")
config = tasks.config
devkit_schtasks = tasks.devkit_schtasks

SCRAPER = config.Collector(
    "social-scraper", command=("uv", "run", "social-scraper", "scrape"), minutes=30, needs=("db",)
)
PYTHONW = r"C:\Python312\pythonw.exe"
ROOT = Path(r"C:\ws\devkit")


class Report:
    def __init__(self):
        self.lines: list[str] = []
        self.failures = 0

    def say(self, line):
        self.lines.append(line)

    def fail(self, line):
        self.lines.append(line)
        self.failures += 1


class FakeSchtasks:
    """Answers `/Query /XML` with whatever is registered, and records every call."""

    def __init__(self, registered: str | None = None, delete_code: int = 0):
        self.registered = registered
        self.delete_code = delete_code
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        if argv[0] == "whoami":
            return subprocess.CompletedProcess(argv, 1, "", "")
        if argv[1] == "/Query":
            if self.registered is None:
                return subprocess.CompletedProcess(argv, 1, "", "ERROR: not found")
            return subprocess.CompletedProcess(argv, 0, self.registered, "")
        if argv[1] == "/Create":
            return subprocess.CompletedProcess(argv, 0, "SUCCESS", "")
        if argv[1] == "/Delete":
            return subprocess.CompletedProcess(argv, self.delete_code, "", "Access is denied.")
        raise AssertionError(argv)

    def verbs(self):
        return [argv[1] for argv in self.calls if argv[0] == "schtasks"]


# --- the task -----------------------------------------------------------------------


def test_the_task_is_named_after_the_collector_not_devkit():
    assert tasks.task_name(SCRAPER) == "social-scraper"
    assert not tasks.task_name(SCRAPER).startswith("devkit-")


def test_the_task_fires_the_wrapper_from_the_devkit_checkout_every_interval():
    document = tasks.task_document(SCRAPER, ROOT, PYTHONW)
    registration = devkit_schtasks.parse_task(document)
    assert registration.command == PYTHONW
    fire = rf'"{ROOT}\scripts\collectors.py" fire social-scraper'
    label = "Scheduled collector: social-scraper"
    assert registration.arguments == devkit_schtasks.logged(label, PYTHONW, fire, ROOT)
    assert f"<WorkingDirectory>{ROOT}</WorkingDirectory>" in document
    assert "<Interval>PT30M</Interval>" in document
    assert "<ExecutionTimeLimit>PT1H</ExecutionTimeLimit>" in document


def test_each_failed_fire_reaches_the_ledger_beside_its_own_log():
    """Every devkit job files each failed run through `log-wrap.py --always`; a collector
    that fails every 30 minutes was seen only when a fix pass caught the scheduler's Last
    Result between runs. The wrapper's files must not overwrite the fire's own record."""
    arguments = devkit_schtasks.parse_task(tasks.task_document(SCRAPER, ROOT, PYTHONW)).arguments
    assert "log-wrap.py" in arguments and " --always " in arguments
    label = tasks.wrapper_label("social-scraper")
    slugged = "".join(c if c.isalnum() else "-" for c in label.lower()).strip("-")
    assert f"logs/{slugged}.log" != tasks.log_path("social-scraper").as_posix()


def test_task_arguments_name_the_fire_verb_since_the_default_runs_nothing():
    assert tasks.task_arguments(ROOT, "x") == rf'"{ROOT}\scripts\collectors.py" fire x'


def test_the_interpreter_is_the_windowless_twin_of_the_console_one(monkeypatch):
    monkeypatch.setattr(tasks.sweep, "console_python", lambda: r"C:\py\python.exe")
    monkeypatch.setattr(tasks.devkit_schtasks, "windowless", lambda exe: f"windowless:{exe}")
    assert tasks.interpreter() == r"windowless:C:\py\python.exe"


def test_is_registered_is_the_xml_querys_exit_code():
    assert not tasks.is_registered("social-scraper", FakeSchtasks())
    assert tasks.is_registered("social-scraper", FakeSchtasks(registered="<Task/>"))


def test_interval_tag_is_the_cadence_as_the_document_spells_it():
    assert tasks.interval_tag(SCRAPER) == "<Interval>PT30M</Interval>"
    assert tasks.interval_tag(SCRAPER) in tasks.task_document(SCRAPER, ROOT, PYTHONW)


def test_check_is_current_only_for_this_document_at_this_cadence():
    current = FakeSchtasks(registered=tasks.task_document(SCRAPER, ROOT, PYTHONW))
    assert tasks.check(SCRAPER, ROOT, PYTHONW, current)[0] == devkit_schtasks.CHECK_CURRENT
    hourly = config.Collector("social-scraper", command=SCRAPER.command, minutes=60)
    drifted = FakeSchtasks(registered=tasks.task_document(hourly, ROOT, PYTHONW))
    code, message = tasks.check(SCRAPER, ROOT, PYTHONW, drifted)
    assert code == devkit_schtasks.CHECK_STALE and "every 30 minutes" in message


def test_the_reporter_is_the_say_and_fail_a_report_has():
    assert {"say", "fail"} <= set(vars(tasks.Reporter))
    assert callable(Report().say) and callable(Report().fail)


def test_the_wrapper_ends_a_wedged_command_before_the_scheduler_would():
    """The scheduler's kill records nothing; the wrapper's timeout writes the log."""
    assert tasks.FIRE_TIMEOUT < 60 * 60


def test_the_command_is_not_in_the_task_so_editing_it_needs_no_reregistration():
    edited = config.Collector("social-scraper", command=("other",), minutes=30)
    assert tasks.task_document(edited, ROOT, PYTHONW) == tasks.task_document(SCRAPER, ROOT, PYTHONW)


def test_a_current_task_is_left_alone():
    fake = FakeSchtasks(registered=tasks.task_document(SCRAPER, ROOT, PYTHONW))
    report = Report()
    tasks.keep_registered(SCRAPER, ROOT, PYTHONW, fake, report)
    assert "/Create" not in fake.verbs()
    assert report.lines == ["social-scraper: scheduled every 30 minutes"]


def test_a_missing_task_is_registered():
    fake = FakeSchtasks()
    report = Report()
    tasks.keep_registered(SCRAPER, ROOT, PYTHONW, fake, report)
    assert "/Create" in fake.verbs() and report.failures == 0
    assert "registered" in report.lines[0]


def test_a_changed_cadence_is_reregistered():
    """`devkit_schtasks.drift` compares no trigger, so `check` has to: `minutes` is a
    hand-edited value, and an edit nothing could see would never reach the task."""
    old = config.Collector("social-scraper", command=SCRAPER.command, minutes=60)
    fake = FakeSchtasks(registered=tasks.task_document(old, ROOT, PYTHONW))
    report = Report()
    tasks.keep_registered(SCRAPER, ROOT, PYTHONW, fake, report)
    assert "/Create" in fake.verbs()
    assert "another interval" in report.lines[0]


def test_a_task_registered_from_another_checkout_is_reregistered():
    moved = FakeSchtasks(registered=tasks.task_document(SCRAPER, Path(r"C:\box"), PYTHONW))
    tasks.keep_registered(SCRAPER, ROOT, PYTHONW, moved, Report())
    assert "/Create" in moved.verbs()


def test_a_failed_registration_is_a_failure():
    class Refusing(FakeSchtasks):
        def __call__(self, argv):
            if list(argv)[:2] == ["schtasks", "/Create"]:
                return subprocess.CompletedProcess(list(argv), 1, "", "Access is denied.")
            return super().__call__(argv)

    report = Report()
    tasks.keep_registered(SCRAPER, ROOT, PYTHONW, Refusing(), report)
    assert report.failures == 1 and "Access is denied." in report.lines[0]


def test_removing_a_task_that_is_not_there_deletes_nothing():
    fake, report = FakeSchtasks(), Report()
    tasks.keep_removed("social-scraper", fake, report)
    assert "/Delete" not in fake.verbs() and report.failures == 0


def test_removing_a_registered_task_deletes_it():
    fake, report = FakeSchtasks(registered="<Exec><Command>x</Command></Exec>"), Report()
    tasks.keep_removed("social-scraper", fake, report)
    assert fake.calls[-1] == devkit_schtasks.delete_argv("social-scraper")
    assert report.failures == 0


def test_a_refused_delete_is_a_failure():
    fake = FakeSchtasks(registered="<Exec><Command>x</Command></Exec>", delete_code=1)
    report = Report()
    tasks.keep_removed("social-scraper", fake, report)
    assert report.failures == 1 and "Access is denied." in report.lines[0]


def test_describe_reads_without_registering():
    fake = FakeSchtasks()
    words = tasks.describe(SCRAPER, ROOT, PYTHONW, fake)
    assert "nothing is scheduled" in words
    assert "/Create" not in fake.verbs()


def test_the_schtasks_runner_is_captured_and_a_spawn_failure_is_a_code(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        raise OSError("no schtasks here")

    monkeypatch.setattr(tasks.subprocess, "run", fake_run)
    result = tasks.run_argv(["schtasks", "/Query"])
    assert result.returncode == 1 and "no schtasks" in result.stderr
    assert seen["creationflags"] == tasks.NO_WINDOW and seen["capture_output"]


# --- one fire -----------------------------------------------------------------------


class FakeSpawn:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[tuple] = []

    def __call__(self, argv, cwd, timeout):
        self.calls.append((list(argv), Path(cwd).name, timeout))
        return self.answers.pop(0)


def checkout(tmp_path):
    path = tmp_path / "social-scraper"
    (path / ".git").mkdir(parents=True)
    return path


def test_a_fire_starts_what_it_needs_then_runs_the_command_in_the_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks.shutil, "which", lambda name: rf"C:\bin\{name}.exe")
    spawn = FakeSpawn((0, ""), (0, "reddit: 40 posts"))
    code, lines = tasks.fire(SCRAPER, checkout(tmp_path), spawn)
    assert code == 0
    assert spawn.calls[0] == (
        ["docker", "compose", "up", "-d", "--wait", "db"],
        "social-scraper",
        tasks.NEEDS_TIMEOUT,
    )
    assert spawn.calls[1][0] == [r"C:\bin\uv.exe", "run", "social-scraper", "scrape"]
    assert spawn.calls[1][2] == tasks.FIRE_TIMEOUT
    assert "reddit: 40 posts" in lines


def test_the_exit_code_is_the_commands_own(tmp_path):
    code, lines = tasks.fire(SCRAPER, checkout(tmp_path), FakeSpawn((0, ""), (3, "x blocked")))
    assert code == 3 and "x blocked" in lines


def test_a_need_that_will_not_start_skips_the_command_and_fails(tmp_path):
    spawn = FakeSpawn((1, "error during connect: docker not running"))
    code, lines = tasks.fire(SCRAPER, checkout(tmp_path), spawn)
    assert code == 1 and len(spawn.calls) == 1
    assert any("could not start db" in line for line in lines)
    assert "error during connect: docker not running" in lines


def test_a_command_with_no_needs_spawns_only_itself(tmp_path):
    bare = config.Collector("social-scraper", command=("uv",), minutes=5)
    spawn = FakeSpawn((0, ""))
    tasks.fire(bare, checkout(tmp_path), spawn)
    assert len(spawn.calls) == 1


def test_a_missing_checkout_runs_nothing(tmp_path):
    spawn = FakeSpawn()
    code, lines = tasks.fire(SCRAPER, tmp_path / "absent", spawn)
    assert code == 2 and spawn.calls == [] and "no checkout" in lines[-1]


def test_a_long_output_keeps_its_tail(tmp_path):
    out = "\n".join(f"line {n}" for n in range(tasks.LOG_LINES + 50))
    _code, lines = tasks.fire(SCRAPER, checkout(tmp_path), FakeSpawn((0, ""), (1, out)))
    assert f"line {tasks.LOG_LINES + 49}" in lines and "line 0" not in lines
    assert any("50 earlier lines dropped" in line for line in lines)


def test_a_program_not_on_path_is_left_for_the_spawn_to_report():
    assert tasks.resolve(["nope", "a"], which=lambda name: None) == ["nope", "a"]


def test_the_spawn_reports_a_missing_program_as_127(tmp_path):
    code, out = tasks.spawn(["definitely-not-a-program-here"], tmp_path, 10)
    assert code == 127 and "not on PATH" in out


class FakePopen:
    instances: ClassVar[list[FakePopen]] = []
    hang = False

    def __init__(self, argv, **kwargs):
        self.argv, self.kwargs = argv, kwargs
        self.pid, self.returncode, self.killed = 4242, 3, False
        self.waits = 0
        FakePopen.instances.append(self)

    def communicate(self, timeout=None):
        self.waits += 1
        if self.hang and self.waits == 1:
            raise subprocess.TimeoutExpired(self.argv, timeout)
        return "partial output\n", None

    def kill(self):
        self.killed = True


def test_the_spawn_is_window_less_and_captured_with_the_streams_merged(monkeypatch, tmp_path):
    FakePopen.hang = False
    monkeypatch.setattr(tasks.subprocess, "Popen", FakePopen)
    assert tasks.spawn(["uv"], tmp_path, 10) == (3, "partial output")
    kwargs = FakePopen.instances[-1].kwargs
    assert kwargs["creationflags"] == tasks.NO_WINDOW
    assert kwargs["stderr"] == subprocess.STDOUT and kwargs["stdin"] == subprocess.DEVNULL


def test_a_collector_runs_uv_frozen_so_it_never_rewrites_the_checkouts_lock(monkeypatch, tmp_path):
    """The command runs in the static checkout, where a bare `uv run` relocks against a
    moved sibling and leaves `uv.lock` uncommitted on the default branch -- ibkr_trader's
    `main` from 2026-10-03 on."""
    assert tasks.command_env({"PATH": "p", "UV_FROZEN": "0"}) == {"PATH": "p", "UV_FROZEN": "1"}
    FakePopen.hang = False
    monkeypatch.setattr(tasks.subprocess, "Popen", FakePopen)
    tasks.spawn(["uv", "run", "x"], tmp_path, 10)
    assert FakePopen.instances[-1].kwargs["env"]["UV_FROZEN"] == "1"
    ran: list = []
    monkeypatch.setattr(
        tasks.subprocess,
        "run",
        lambda argv, **kw: ran.append(kw) or subprocess.CompletedProcess(argv, 0),
    )
    assert tasks.stream(["uv", "run", "x"], tmp_path) == 0
    assert ran[0]["env"]["UV_FROZEN"] == "1"


def test_a_timeout_ends_the_whole_tree_and_keeps_what_was_said(monkeypatch, tmp_path):
    """`uv`'s grandchild is the Chrome holding the profile lock; killing `uv` alone
    leaves it, and every later fire fails on the lock."""
    FakePopen.hang = True
    killed = []
    monkeypatch.setattr(tasks.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(tasks, "kill_tree", killed.append)
    code, out = tasks.spawn(["uv"], tmp_path, 10)
    assert code == 124 and killed == [4242] and FakePopen.instances[-1].killed
    assert "partial output" in out and "timed out after 10s" in out


def test_kill_tree_asks_taskkill_for_the_subtree_window_less(monkeypatch):
    seen = []
    monkeypatch.setattr(tasks.os, "name", "nt")
    monkeypatch.setattr(tasks.subprocess, "run", lambda argv, **kw: seen.append((argv, kw)))
    tasks.kill_tree(7)
    ((argv, kwargs),) = seen
    assert argv == ["taskkill", "/F", "/T", "/PID", "7"]
    assert kwargs["creationflags"] == tasks.NO_WINDOW


# --- a run by hand ------------------------------------------------------------------


def test_a_run_by_hand_starts_its_needs_then_streams_the_command(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks.shutil, "which", lambda name: rf"C:\bin\{name}.exe")
    seen = []

    def streamer(argv, cwd):
        seen.append((list(argv), Path(cwd).name))
        return 0

    assert tasks.run_once(SCRAPER, checkout(tmp_path), streamer) == 0
    assert seen == [
        (["docker", "compose", "up", "-d", "--wait", "db"], "social-scraper"),
        ([r"C:\bin\uv.exe", "run", "social-scraper", "scrape"], "social-scraper"),
    ]


def test_a_run_by_hand_stops_when_its_needs_will_not_start(tmp_path):
    seen = []
    code = tasks.run_once(SCRAPER, checkout(tmp_path), lambda argv, cwd: seen.append(argv) or 1)
    assert code == 1 and len(seen) == 1


def test_a_run_by_hand_without_a_checkout_runs_nothing(tmp_path):
    assert tasks.run_once(SCRAPER, tmp_path / "absent", lambda argv, cwd: 0 / 0) == 2


def csv_answer(stdout, code=0):
    return lambda argv: subprocess.CompletedProcess(list(argv), code, stdout, "")


def test_running_reads_the_status_column_of_any_trigger_row():
    ready = '"\\social-scraper","10/2/2026 3:00:00 PM","Ready"\n'
    busy = ready + '"\\social-scraper","N/A","Running"\n'
    assert not tasks.running("social-scraper", csv_answer(ready))
    assert tasks.running("social-scraper", csv_answer(busy))
    assert not tasks.running("social-scraper", csv_answer("", code=1))


def test_the_streamed_spawn_is_window_less_and_hands_down_real_streams(monkeypatch, tmp_path):
    handle = (tmp_path / "out.txt").open("w", encoding="utf-8")
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 5)

    monkeypatch.setattr(tasks.sys, "stdout", handle)
    monkeypatch.setattr(tasks.subprocess, "run", fake_run)
    try:
        assert tasks.stream(["uv"], tmp_path) == 5
    finally:
        handle.close()
    assert seen["creationflags"] == tasks.NO_WINDOW and seen["stdout"] is handle


def test_the_streamed_spawn_reports_a_missing_program_as_127(tmp_path):
    assert tasks.stream(["definitely-not-a-program-here"], tmp_path) == 127


def test_render_heads_the_log_with_the_name_time_and_exit():
    import datetime as dt

    text = tasks.render("social-scraper", 3, ["a"], dt.datetime(2026, 10, 2, 14, 30))
    assert text.splitlines()[0] == "# collector social-scraper 2026-10-02T14:30:00 -- exit 3"


def test_the_log_is_under_logs():
    assert tasks.log_path("social-scraper").as_posix() == "logs/collector-social-scraper.log"
