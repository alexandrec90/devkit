"""`collectors.py`: keep a collector up where it is assigned, down where it is not.

Every docker call goes through a fake here. The properties pinned are the ones that
decide whether a machine ingests at all:

- a machine assigned nothing spawns nothing, from the job or the tray;
- `run` starts what is down and never touches what is up;
- `stop` stops only what is running, and only the collector's own container;
- a project's own health verdict is amber, never a failure of this job;
- the assignment written by `run-here` is what the scheduled pass then acts on.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path

import pytest
from support import load_script

collectors = load_script("scripts/collectors.py")
config = collectors.config

RUN, STOP = config.RUN, config.STOP


def box(service="app", state="running", workdir="C:/ws/ibkr_trader", cid="c1", status="Up 2h"):
    return collectors.Container(cid, workdir, service, state, status)


class FakeDocker:
    def __init__(self, containers=None, up_ok=True, health=(0, "all good")):
        self.containers = containers
        self.up_ok = up_ok
        self.health_answer = health
        self.calls: list[tuple] = []

    def ps(self):
        self.calls.append(("ps",))
        return self.containers

    def up(self, checkout, service):
        self.calls.append(("up", Path(checkout).name, service))
        return self.up_ok, "" if self.up_ok else "Error response from daemon: no space"

    def stop(self, container):
        self.calls.append(("stop", container.id))
        return True, ""

    def health(self, container, argv):
        self.calls.append(("health", container.id, tuple(argv)))
        return self.health_answer


def target(tmp_path, mode=RUN, project="ibkr_trader", service="app", health=("h",)):
    checkout = tmp_path / project
    (checkout / ".git").mkdir(parents=True, exist_ok=True)
    return collectors.Target(config.Collector(project, service, health), mode, checkout)


def ours(tmp_path, **kw):
    return box(workdir=str(tmp_path / "ibkr_trader"), **kw)


# --- reading docker ----------------------------------------------------------------


def test_ps_rows_are_parsed_and_foreign_containers_skipped():
    text = "c1\tC:\\ws\\ibkr_trader\tapp\trunning\tUp 3 hours\nc2\t\t\trunning\tUp\nshort\n"
    assert collectors.parse_ps(text) == [
        collectors.Container("c1", "C:\\ws\\ibkr_trader", "app", "running", "Up 3 hours")
    ]


def test_a_container_is_matched_on_its_directory_caselessly_and_across_slashes():
    found = collectors.find(
        [box(workdir="c:\\WS\\ibkr_trader\\")], Path("C:/ws/ibkr_trader"), "app"
    )
    assert found is not None


def test_a_box_of_the_same_project_is_not_the_collector():
    """A box runs its own stack under the same service names; only the checkout's counts."""
    other = box(workdir="C:/ws/.worktrees/ibkr_trader--topic")
    assert collectors.find([other], Path("C:/ws/ibkr_trader"), "app") is None


def test_a_running_container_is_preferred_over_a_stopped_one():
    stale = box(cid="old", state="exited")
    live = box(cid="new")
    assert collectors.find([stale, live], Path("C:/ws/ibkr_trader"), "app").id == "new"


def test_every_spawn_is_captured_and_reports_a_missing_docker_as_a_code(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        raise FileNotFoundError("docker")

    monkeypatch.setattr(collectors.subprocess, "run", fake_run)
    code, out = collectors.Docker().run(["docker", "ps"], 5)
    assert (code, out) == (127, "docker is not on PATH")
    assert seen["capture_output"] is True and seen["creationflags"] == collectors.NO_WINDOW


def test_a_hung_engine_is_a_timeout_not_a_hang(monkeypatch):
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(collectors.subprocess, "run", fake_run)
    assert collectors.Docker(ps_timeout=3).ps() is None


def test_up_names_the_service_from_the_checkout(monkeypatch, tmp_path):
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(argv=argv, cwd=kwargs["cwd"])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(collectors.subprocess, "run", fake_run)
    assert collectors.Docker().up(tmp_path, "app") == (True, "")
    assert seen == {"argv": ["docker", "compose", "up", "-d", "app"], "cwd": tmp_path}


def test_stop_is_docker_stop_never_down(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(collectors.subprocess, "run", fake_run)
    collectors.Docker().stop(box(cid="abc"))
    assert seen["argv"] == ["docker", "stop", "abc"]


# --- the pass ----------------------------------------------------------------------


def test_nothing_assigned_asks_docker_nothing():
    docker, report = FakeDocker([]), collectors.Report()
    collectors.maintain([], docker, report, {})
    assert docker.calls == [] and report.failures == 0


def test_run_starts_a_collector_that_is_down_and_skips_its_health_this_pass(tmp_path):
    docker, report, health = FakeDocker([ours(tmp_path, state="exited")]), collectors.Report(), {}
    collectors.maintain([target(tmp_path)], docker, report, health)
    assert ("up", "ibkr_trader", "app") in docker.calls
    assert not any(call[0] == "health" for call in docker.calls)
    assert report.failures == 0 and health == {}


def test_run_leaves_a_running_collector_alone_and_records_its_health(tmp_path):
    docker, report, health = FakeDocker([ours(tmp_path)]), collectors.Report(), {}
    collectors.maintain([target(tmp_path)], docker, report, health)
    assert not any(call[0] in {"up", "stop"} for call in docker.calls)
    assert health["ibkr_trader"] == {"ok": True, "summary": "healthy", "container": "c1"}


def test_a_failing_health_check_is_recorded_but_is_not_a_job_failure(tmp_path):
    docker = FakeDocker([ours(tmp_path)], health=(1, "reddit: stale since 04:00\nmore"))
    report, health = collectors.Report(), {}
    collectors.maintain([target(tmp_path)], docker, report, health)
    assert report.failures == 0
    assert health["ibkr_trader"]["ok"] is False
    assert "reddit: stale" in health["ibkr_trader"]["summary"]


def test_a_start_that_fails_is_a_failure_with_dockers_words(tmp_path):
    docker, report = FakeDocker([], up_ok=False), collectors.Report()
    collectors.maintain([target(tmp_path)], docker, report, {})
    assert report.failures == 1
    assert any("no space" in line for line in report.lines)


def test_a_missing_checkout_is_a_failure_and_nothing_is_started(tmp_path):
    missing = collectors.Target(config.Collector("gone", "app"), RUN, tmp_path / "gone")
    docker, report = FakeDocker([]), collectors.Report()
    collectors.maintain([missing], docker, report, {})
    assert report.failures == 1 and not any(call[0] == "up" for call in docker.calls)


def test_stop_stops_only_a_running_collector(tmp_path):
    docker, report = FakeDocker([ours(tmp_path)]), collectors.Report()
    collectors.maintain([target(tmp_path, mode=STOP)], docker, report, {})
    assert ("stop", "c1") in docker.calls

    idle = FakeDocker([ours(tmp_path, state="exited")])
    collectors.maintain([target(tmp_path, mode=STOP)], idle, collectors.Report(), {})
    assert not any(call[0] == "stop" for call in idle.calls)


def test_a_silent_engine_fails_a_run_machine_but_not_a_stop_machine(tmp_path):
    report = collectors.Report()
    collectors.maintain([target(tmp_path)], FakeDocker(None), report, {})
    assert report.failures == 1 and "Docker Desktop" in report.lines[0]

    report = collectors.Report()
    collectors.maintain([target(tmp_path, mode=STOP)], FakeDocker(None), report, {})
    assert report.failures == 0


def test_targets_are_only_the_assigned_collectors(tmp_path):
    declared = [config.Collector("a", "s"), config.Collector("b", "s")]
    found = collectors.targets(declared, {"b": STOP}, tmp_path)
    assert [(t.collector.project, t.mode, t.checkout) for t in found] == [
        ("b", STOP, tmp_path / "b")
    ]


# --- the tray's rows -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("containers", "health", "level", "words"),
    [
        (None, {}, collectors.FAIL, "not answering"),
        ([], {}, collectors.FAIL, "no container"),
        ("exited", {}, collectors.FAIL, "not running"),
        ("running", {}, collectors.OK, "running"),
        (
            "running",
            {"ok": False, "summary": "exit 1", "container": "c1"},
            collectors.WARN,
            "health",
        ),
        # A verdict about a container since replaced says nothing about this one.
        (
            "running",
            {"ok": False, "summary": "exit 1", "container": "old"},
            collectors.OK,
            "running",
        ),
    ],
)
def test_a_run_row_says_how_the_collector_is(tmp_path, containers, health, level, words):
    if isinstance(containers, str):
        containers = [ours(tmp_path, state=containers)]
    got_level, detail = collectors.row(target(tmp_path), containers, {"ibkr_trader": health})
    assert got_level == level and words in detail


def test_a_stop_row_is_green_unless_the_collector_is_running_here(tmp_path):
    stop = target(tmp_path, mode=STOP)
    assert collectors.row(stop, None, {})[0] == collectors.OK
    assert collectors.row(stop, [ours(tmp_path, state="exited")], {})[0] == collectors.OK
    assert collectors.row(stop, [ours(tmp_path)], {})[0] == collectors.WARN


def devkit_home(tmp_path, monkeypatch, assignment=None, setting=None):
    """A static devkit checkout beside a workspace file, as `home` resolves it."""
    root = tmp_path / "devkit"
    root.mkdir()
    monkeypatch.setenv("DEVKIT_DIR", str(root))
    settings = {
        config.SETTING: setting
        or {
            "ibkr_trader": {"service": "app", "health": ["h"]},
            "sports_betting": {"service": "collector"},
        }
    }
    (tmp_path / "alex-projects.code-workspace").write_text(
        json.dumps({"folders": [], "settings": settings}), encoding="utf-8"
    )
    for name in ("ibkr_trader", "sports_betting"):
        (tmp_path / name / ".git").mkdir(parents=True)
    if assignment is not None:
        config.save_assignment(root / config.ASSIGNMENT, assignment)
    return root


def test_the_tray_on_an_unassigned_machine_shows_nothing_and_spawns_nothing(tmp_path, monkeypatch):
    devkit_home(tmp_path, monkeypatch)
    docker = FakeDocker([])
    assert collectors.tray_rows(docker=docker) == []
    assert docker.calls == []


def test_the_tray_shows_one_row_per_assigned_collector(tmp_path, monkeypatch):
    devkit_home(tmp_path, monkeypatch, {"ibkr_trader": RUN})
    docker = FakeDocker([box(workdir=str(tmp_path / "ibkr_trader"))])
    assert collectors.tray_rows(docker=docker) == [
        ("collector: ibkr_trader", collectors.OK, "running (Up 2h)")
    ]


def test_an_assignment_the_workspace_no_longer_declares_is_amber(tmp_path, monkeypatch):
    devkit_home(tmp_path, monkeypatch, {"retired": RUN})
    rows = collectors.tray_rows(docker=FakeDocker([]))
    assert (
        rows
        == [
            ("collector: retired", collectors.WARN, rows[0][2]),
        ]
        and "no longer declares" in rows[0][2]
    )


# --- the command line ----------------------------------------------------------------


def test_run_here_assigns_this_machine_and_acts_at_once(tmp_path, monkeypatch, capsys):
    root = devkit_home(tmp_path, monkeypatch)
    docker = FakeDocker([])
    assert collectors.main(["run-here", "ibkr_trader"], docker=docker) == 0
    assert config.load_assignment(root / config.ASSIGNMENT) == {"ibkr_trader": RUN}
    assert ("up", "ibkr_trader", "app") in docker.calls
    assert not any(call[1:] == ("sports_betting", "collector") for call in docker.calls)
    assert (root / collectors.ARTIFACT).is_file()


def test_run_here_with_no_names_takes_every_declared_collector(tmp_path, monkeypatch):
    root = devkit_home(tmp_path, monkeypatch)
    collectors.main(["run-here"], docker=FakeDocker([]))
    assert config.load_assignment(root / config.ASSIGNMENT) == {
        "ibkr_trader": RUN,
        "sports_betting": RUN,
    }


def test_the_scheduled_pass_acts_on_what_run_here_recorded(tmp_path, monkeypatch):
    devkit_home(tmp_path, monkeypatch, {"sports_betting": RUN, "ibkr_trader": STOP})
    running_ibkr = box(workdir=str(tmp_path / "ibkr_trader"))
    docker = FakeDocker([running_ibkr])
    assert collectors.main(["maintain"], docker=docker) == 0
    assert ("up", "sports_betting", "collector") in docker.calls
    assert ("stop", "c1") in docker.calls


def test_a_typo_assigns_nothing_and_fails(tmp_path, monkeypatch):
    root = devkit_home(tmp_path, monkeypatch)
    docker = FakeDocker([])
    assert collectors.main(["run-here", "ibkr"], docker=docker) == 2
    assert config.load_assignment(root / config.ASSIGNMENT) == {}
    assert docker.calls == []


def test_release_forgets_without_touching_docker(tmp_path, monkeypatch):
    root = devkit_home(tmp_path, monkeypatch, {"ibkr_trader": RUN})
    docker = FakeDocker([])
    assert collectors.main(["release", "ibkr_trader"], docker=docker) == 0
    assert config.load_assignment(root / config.ASSIGNMENT) == {}
    assert docker.calls == []


def test_status_is_read_only(tmp_path, monkeypatch, capsys):
    root = devkit_home(tmp_path, monkeypatch, {"ibkr_trader": RUN})
    docker = FakeDocker([])
    assert collectors.main([], docker=docker) == 0
    assert docker.calls == [("ps",)]
    out = capsys.readouterr().out
    assert "ibkr_trader: assigned `run`" in out and "sports_betting: not assigned" in out
    assert not (root / collectors.HEALTH).exists()


# --- the helpers, one by one ---------------------------------------------------------


def test_same_dir_ignores_case_separators_and_a_trailing_slash():
    assert collectors.same_dir("C:\\WS\\ibkr_trader\\", Path("c:/ws/ibkr_trader"))
    assert not collectors.same_dir("C:/ws/ibkr_trader", "C:/ws/ibkr_trader2")


def test_first_line_skips_blanks_and_truncates():
    assert collectors.first_line("\n\n  reddit: stale  \nnext") == "reddit: stale"
    assert collectors.first_line("x" * 200, limit=10) == "x" * 9 + "…"
    assert collectors.first_line("") == ""


def test_check_health_keeps_the_projects_words_in_the_log(tmp_path):
    report = collectors.Report()
    record = collectors.check_health(
        target(tmp_path), ours(tmp_path), FakeDocker(health=(1, "a\nb")), report
    )
    assert record == {"ok": False, "summary": "exit 1: a", "container": "c1"}
    assert "    a" in report.lines and "    b" in report.lines


def test_keep_running_records_nothing_for_a_collector_without_a_health_command(tmp_path):
    no_health = target(tmp_path, health=())
    docker = FakeDocker()
    assert collectors.keep_running(no_health, [ours(tmp_path)], docker, collectors.Report()) is None
    assert docker.calls == []


def test_keep_stopped_reports_a_stop_docker_refused(tmp_path):
    class Refusing(FakeDocker):
        def stop(self, container):
            return False, "permission denied"

    report = collectors.Report()
    collectors.keep_stopped(target(tmp_path, mode=STOP), [ours(tmp_path)], Refusing(), report)
    assert report.failures == 1 and "permission denied" in report.lines[0]


@pytest.mark.parametrize("body", [None, "{ torn", "[]", '{"p": "not a record"}'])
def test_load_health_reads_only_records(tmp_path, body):
    path = tmp_path / "health.json"
    if body is not None:
        path.write_text(body, encoding="utf-8")
    assert collectors.load_health(path) == {}


def test_load_health_round_trips_what_the_pass_writes(tmp_path):
    path = tmp_path / "logs" / "health.json"
    collectors.write_file(path, json.dumps({"p": {"ok": True}}))
    assert collectors.load_health(path) == {"p": {"ok": True}}


def test_render_heads_the_artifact_with_the_time_and_failure_count():
    when = dt.datetime(2026, 9, 29, 4, 15)
    text = collectors.render(["a", "b"], 1, when)
    assert text == "# collectors 2026-09-29T04:15:00 -- 1 failure(s)\na\nb\n"


def test_parse_args_defaults_to_read_only_status():
    args = collectors.parse_args([])
    assert (args.mode, args.projects) == ("status", [])
    assert collectors.parse_args(["run-here", "a", "b"]).projects == ["a", "b"]


def test_reassign_refuses_a_typo_without_writing(tmp_path):
    report = collectors.Report()
    args = collectors.parse_args(["stop-here", "nope"])
    assert collectors.reassign(args, tmp_path, [config.Collector("a", "s")], report) is None
    assert report.failures == 1 and not (tmp_path / config.ASSIGNMENT).exists()


def test_status_names_an_engine_it_could_not_ask(tmp_path):
    chosen = [target(tmp_path)]
    report = collectors.Report()
    collectors.status([chosen[0].collector], {"ibkr_trader": RUN}, chosen, FakeDocker(None), report)
    assert report.lines == ["ibkr_trader: assigned `run` -- `app` docker not answering"]


def test_status_with_nothing_declared_says_where_to_declare_it():
    report = collectors.Report()
    collectors.status([], {}, [], FakeDocker(), report)
    assert config.SETTING in report.lines[0]
