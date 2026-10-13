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

import contextlib
import datetime as dt
import functools
import json
import os
import subprocess
from pathlib import Path
from typing import Any
from types import SimpleNamespace

import pytest
from support import load_script

collectors = load_script("scripts/collectors.py")
config = collectors.config

RUN, STOP = config.RUN, config.STOP


def box(service="app", state="running", workdir="C:/ws/ibkr_trader", cid="c1", status="Up 2h"):
    return collectors.Container(cid, workdir, service, state, status)


class FakeDocker:
    def __init__(
        self, containers=None, up_ok=True, health=(0, "all good"), built=None, started=None
    ):
        self.containers = containers
        self.up_ok = up_ok
        self.health_answer = health
        self.built_at = built
        self.started_at = started
        self.desktop = False
        # Docker Desktop's own reason its engine is down: none, as for a Desktop quit.
        self.stopped = ""
        self.update = False
        self.restarts = None
        self.holds = None
        self.restart_answer = (True, "")
        self.reset_answer = (True, "")
        # Whether WSL itself answers; kept out of `calls`, which other tests compare whole.
        self.wsl = True
        self.wsl_asks = 0
        # The memory half: no limit answered for, no VM size, nothing said of a silent VM
        # -- which checks nothing, so a test about something else fails on nothing here.
        self.limits_answer: dict[str, int] | None = {}
        self.vm = None
        self.evidence = ""
        # Kept out of `calls`, which other tests compare whole.
        self.memory_asks: list[tuple] = []
        self.reclaim_answer = (True, "")
        self.reclaims = 0
        self.calls: list[tuple] = []

    def reclaim_cache(self):
        self.reclaims += 1
        return self.reclaim_answer

    def limits(self, ids):
        self.memory_asks.append(("limits", tuple(ids)))
        return self.limits_answer

    def vm_memory(self):
        return self.vm

    def memory_evidence(self, since):
        self.memory_asks.append(("evidence", since))
        return self.evidence

    def desktop_running(self):
        self.calls.append(("desktop",))
        return self.desktop

    def updating(self):
        return self.update

    def stop_reason(self):
        return self.stopped

    def wsl_answers(self):
        self.wsl_asks += 1
        return self.wsl

    def restart_desktop(self):
        self.calls.append(("restart",))
        return self.restart_answer

    def reset_vm(self):
        self.calls.append(("reset",))
        return self.reset_answer

    def built(self, container):
        self.calls.append(("built", container.id))
        return self.built_at

    def started(self, container):
        self.calls.append(("started", container.id))
        return self.started_at

    def deploy(self, checkout, service, files=()):
        self.calls.append(("deploy", Path(checkout).name, service))
        # What a staged deploy hands compose, read while the staging still exists.
        self.deployed_files = [(Path(f).name, Path(f).read_text(encoding="utf-8")) for f in files]
        return self.up_ok, "" if self.up_ok else "failed to solve: pip install exited 1"

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


class FakeGit:
    def __init__(self, head=("c8c7b02", 2000.0), held=""):
        self.head_answer = head
        self.held_answer = held
        self.calls: list[str] = []

    def head(self, checkout, rev="HEAD"):
        self.calls.append("head")
        return self.head_answer

    def held(self, checkout, rev="HEAD"):
        self.calls.append("held")
        return self.held_answer

    def pin(self, checkout, sibling):
        return ""


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
    assert report.failures == 0 and set(health["ibkr_trader"]) == {collectors.STARTED_AT}


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


# --- redeploying onto merged code ---------------------------------------------------------


def test_a_collector_older_than_its_merged_checkout_is_redeployed_and_not_judged(tmp_path):
    """51cca249: ibkr_trader's health fix merged, and its `app` kept running a day-old
    image with the old code baked in, so the group came back after every fix."""
    docker = FakeDocker([ours(tmp_path)], health=(1, "never-run"), built=1000.0)
    git, report, health = FakeGit(), collectors.Report(), {}
    collectors.maintain([target(tmp_path)], docker, report, health, git)
    assert ("deploy", "ibkr_trader", "app") in docker.calls
    assert not any(call[0] == "health" for call in docker.calls), "the container is seconds old"
    assert health["ibkr_trader"][collectors.CODE_AT] == 2000.0
    assert set(health["ibkr_trader"]) == {collectors.CODE_AT, collectors.STARTED_AT}
    assert report.failures == 0 and any("redeployed `app` onto c8c7b02" in l for l in report.lines)
    containers = [ours(tmp_path, cid="c2")]
    assert collectors.row(target(tmp_path), containers, health)[0] == collectors.OK


def at(monkeypatch, when):
    monkeypatch.setattr(collectors, "_clock", lambda: when)


def test_the_clock_is_now():
    assert abs(collectors._clock() - dt.datetime.now().timestamp()) < 60


def test_a_container_this_job_redeployed_is_not_judged_until_it_has_settled(tmp_path, monkeypatch):
    """221f3b05: the pass after the redeploy that carried ibkr_trader #79 judged
    `reddit`'s 45 failures from before #79 -- its half-hourly job had yet to fire."""
    docker = FakeDocker([ours(tmp_path)], health=(1, "unhealthy: reddit"), built=1000.0)
    health = {"ibkr_trader": {collectors.CODE_AT: 2000.0, collectors.STARTED_AT: 10_000.0}}
    report = collectors.Report()
    at(monkeypatch, 10_000.0 + 15 * 60)  # the pass after the redeploy
    record = collectors.keep_running(
        target(tmp_path), [ours(tmp_path)], docker, report, health["ibkr_trader"], FakeGit()
    )
    assert not any(call[0] == "health" for call in docker.calls)
    assert record == {collectors.CODE_AT: 2000.0, collectors.STARTED_AT: 10_000.0}
    assert any("health check deferred" in line and "15 min ago" in line for line in report.lines)
    assert collectors.row(target(tmp_path), [ours(tmp_path)], {"ibkr_trader": record})[0] == (
        collectors.OK
    )


def test_a_settled_container_is_judged_and_the_start_is_forgotten(tmp_path, monkeypatch):
    docker = FakeDocker([ours(tmp_path)], health=(1, "unhealthy: reddit"), built=1000.0)
    last = {collectors.CODE_AT: 2000.0, collectors.STARTED_AT: 10_000.0}
    at(monkeypatch, 10_000.0 + config.DEFAULT_SETTLE * 60)
    record = collectors.keep_running(
        target(tmp_path), [ours(tmp_path)], docker, collectors.Report(), last, FakeGit()
    )
    assert record is not None and record["ok"] is False
    assert collectors.STARTED_AT not in record


def test_a_settle_of_zero_judges_the_pass_after_the_start(tmp_path, monkeypatch):
    t = collectors.Target(
        config.Collector("ibkr_trader", "app", ("h",), settle=0), RUN, target(tmp_path).checkout
    )
    last = {collectors.CODE_AT: 2000.0, collectors.STARTED_AT: 10_000.0}
    docker = FakeDocker([ours(tmp_path)], built=1000.0)
    at(monkeypatch, 10_001.0)
    record = collectors.keep_running(
        t, [ours(tmp_path)], docker, collectors.Report(), last, FakeGit()
    )
    assert record is not None and record["ok"] is True


def test_a_container_the_engine_restarted_is_not_judged_until_it_has_settled(tmp_path, monkeypatch):
    """3517acbe: Docker Desktop came back from a three-hour wedge and started ibkr_trader's
    `app` itself; the pass three minutes later judged `social` stale by the runs the wedge
    had cost, before its catch-up run could finish. This job had started nothing."""
    docker = FakeDocker(
        [ours(tmp_path)], health=(1, "unhealthy: social"), built=1000.0, started=10_000.0
    )
    report = collectors.Report()
    at(monkeypatch, 10_000.0 + 3 * 60)
    record = collectors.keep_running(
        target(tmp_path), [ours(tmp_path)], docker, report, {collectors.CODE_AT: 2000.0}, FakeGit()
    )
    assert not any(call[0] == "health" for call in docker.calls)
    assert record == {collectors.CODE_AT: 2000.0, collectors.STARTED_AT: 10_000.0}
    assert any("health check deferred" in line and "3 min ago" in line for line in report.lines)


def test_a_container_the_engine_started_long_ago_is_judged(tmp_path, monkeypatch):
    docker = FakeDocker(
        [ours(tmp_path)], health=(1, "unhealthy: social"), built=1000.0, started=10_000.0
    )
    at(monkeypatch, 10_000.0 + config.DEFAULT_SETTLE * 60)
    last = {collectors.CODE_AT: 2000.0}
    record = collectors.keep_running(
        target(tmp_path), [ours(tmp_path)], docker, collectors.Report(), last, FakeGit()
    )
    assert record is not None and record["ok"] is False and record["unhealthy"] == ["social"]
    assert collectors.STARTED_AT not in record


@pytest.mark.parametrize(
    ("recorded", "engine", "expected"),
    [
        (None, None, None),
        (10_000.0, None, 10_000.0),
        (None, 9_000.0, 9_000.0),
        (10_000.0, 12_000.0, 12_000.0),
        (12_000.0, 10_000.0, 12_000.0),
        ("garbage", 9_000.0, 9_000.0),
    ],
)
def test_the_last_start_is_the_later_of_ours_and_the_engines(recorded, engine, expected):
    assert collectors.last_start(recorded, engine) == expected


def test_started_reads_the_engines_start_time(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, "2026-10-09T22:24:01.123456789Z\n", "")

    monkeypatch.setattr(collectors.subprocess, "run", fake_run)
    when = collectors.Docker().started(box(cid="abc"))
    assert seen["argv"] == ["docker", "inspect", "--format", "{{.State.StartedAt}}", "abc"]
    assert when == dt.datetime(2026, 10, 9, 22, 24, 1, tzinfo=dt.UTC).timestamp()


def test_started_is_none_when_the_engine_cannot_say(monkeypatch):
    monkeypatch.setattr(
        collectors.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "Error: No such object"),
    )
    assert collectors.Docker().started(box()) is None


@pytest.mark.parametrize(
    ("elapsed", "expected"),
    [(0, True), (59 * 60, True), (60 * 60, False), (-5, False)],
)
def test_settling_is_the_window_after_the_start(elapsed, expected):
    assert collectors.settling(10_000.0, 10_000.0 + elapsed, 60) is expected


def test_a_rebuild_that_changed_nothing_is_not_redeployed_again(tmp_path):
    """A docs-only merge leaves every layer cached and the image's own date unmoved; the
    commit this job deployed onto is what says the container is current."""
    docker, git = FakeDocker([ours(tmp_path)], built=1000.0), FakeGit()
    health = {"ibkr_trader": {collectors.CODE_AT: 2000.0}}
    collectors.maintain([target(tmp_path)], docker, collectors.Report(), health, git)
    assert not any(call[0] == "deploy" for call in docker.calls)
    assert health["ibkr_trader"][collectors.CODE_AT] == 2000.0, "carried to the next verdict"
    assert health["ibkr_trader"]["ok"] is True


def test_a_checkout_that_is_not_merged_code_is_not_deployed_and_says_why(tmp_path):
    docker = FakeDocker([ours(tmp_path)], built=1000.0, health=(1, "reddit failing"))
    git, report, health = (
        FakeGit(held="HEAD is not on origin's default branch"),
        collectors.Report(),
        {},
    )
    collectors.maintain([target(tmp_path)], docker, report, health, git)
    assert not any(call[0] == "deploy" for call in docker.calls)
    assert health["ibkr_trader"][collectors.HELD] == "HEAD is not on origin's default branch"
    assert health["ibkr_trader"]["ok"] is False, "the verdict is still taken"
    assert report.failures == 0 and any("not redeployed" in line for line in report.lines)


def test_a_redeploy_that_fails_fails_the_job_and_the_old_container_is_still_judged(tmp_path):
    docker, report, health = (
        FakeDocker([ours(tmp_path)], built=1000.0, up_ok=False),
        collectors.Report(),
        {},
    )
    collectors.maintain([target(tmp_path)], docker, report, health, FakeGit())
    assert report.failures == 1 and any("pip install" in line for line in report.lines)
    assert health["ibkr_trader"][collectors.CODE_AT] == 1000.0
    assert any(call[0] == "health" for call in docker.calls)


def test_a_container_newer_than_its_checkout_asks_nothing_more(tmp_path):
    docker, git = FakeDocker([ours(tmp_path)], built=3000.0), FakeGit()
    health = {}
    collectors.maintain([target(tmp_path)], docker, collectors.Report(), health, git)
    assert git.calls == ["head"] and health["ibkr_trader"][collectors.CODE_AT] == 3000.0


def test_an_image_the_engine_cannot_date_is_left_alone_and_asks_git_nothing(tmp_path):
    docker, git = FakeDocker([ours(tmp_path)]), FakeGit()
    collectors.maintain([target(tmp_path)], docker, collectors.Report(), {}, git)
    assert git.calls == [] and not any(call[0] == "deploy" for call in docker.calls)


def test_a_collector_without_a_health_command_still_records_its_code(tmp_path):
    docker = FakeDocker([ours(tmp_path)], built=1000.0)
    record = collectors.keep_running(
        target(tmp_path, health=()), [ours(tmp_path)], docker, collectors.Report(), {}, FakeGit()
    )
    assert record is not None
    assert record[collectors.CODE_AT] == 2000.0, "or the next pass rebuilds it again"


@pytest.mark.parametrize(
    ("built", "last", "expected"),
    [
        (1000.0, {}, 1000.0),
        (1000.0, {collectors.CODE_AT: 2000.0}, 2000.0),
        (3000.0, {collectors.CODE_AT: 2000.0}, 3000.0),
        (None, {collectors.CODE_AT: 2000.0}, 2000.0),
        (None, {collectors.CODE_AT: "junk"}, None),
    ],
)
def test_code_at_is_the_later_of_the_build_and_the_last_redeploy(built, last, expected):
    assert collectors.code_at(box(), FakeDocker(built=built), last) == expected


def test_redeploy_records_the_commit_it_deployed_onto(tmp_path):
    docker, report = FakeDocker(built=1000.0), collectors.Report()
    got = collectors.redeploy(target(tmp_path), ours(tmp_path), docker, FakeGit(), report, {})
    assert got == {collectors.CODE_AT: 2000.0, "deployed": True}


def test_redeploy_with_a_checkout_git_cannot_read_records_only_the_build(tmp_path):
    docker, report = FakeDocker(built=1000.0), collectors.Report()
    got = collectors.redeploy(
        target(tmp_path), ours(tmp_path), docker, FakeGit(head=None), report, {}
    )
    assert got == {collectors.CODE_AT: 1000.0} and report.lines == []


class SiblingGit(FakeGit):
    """`FakeGit` answering per checkout, by its directory's name."""

    def __init__(
        self,
        heads: dict,
        held: dict | None = None,
        pins: dict | None = None,
        commits: dict | None = None,
    ):
        super().__init__()
        self.heads, self.helds = heads, held or {}
        # `pins`: sibling -> the ref the consumer's gate names; `commits`: that ref ->
        # `(short sha, time)` in the sibling, absent when the sibling lacks it.
        self.pins, self.commits = pins or {}, commits or {}
        self.exported: list[tuple[str, str]] = []

    def head(self, checkout, rev="HEAD"):
        self.calls.append(f"head {Path(checkout).name}" + ("" if rev == "HEAD" else f" {rev}"))
        return self.heads.get(Path(checkout).name) if rev == "HEAD" else self.commits.get(rev)

    def held(self, checkout, rev="HEAD"):
        self.calls.append(f"held {Path(checkout).name}" + ("" if rev == "HEAD" else f" {rev}"))
        return self.helds.get(Path(checkout).name, "")

    def pin(self, checkout, sibling):
        return self.pins.get(sibling, "")

    def resolve(self, checkout, ref):
        return ref if ref in self.commits else ""

    def export(self, checkout, rev, dest):
        self.exported.append((Path(checkout).name, rev))
        dest.mkdir(parents=True)
        (dest / "REV").write_text(rev, encoding="utf-8")
        return ""


def built_from(tmp_path, *siblings):
    checkout = target(tmp_path).checkout
    collector = config.Collector("ibkr_trader", "app", ("h",), builds_from=siblings)
    return collectors.Target(collector, RUN, checkout)


def test_a_merge_in_a_checkout_the_image_copies_redeploys_it(tmp_path):
    """110defb0: ibkr_trader's image `COPY`s data-lake, whose fix would otherwise have
    reached the container only with ibkr_trader's next unrelated merge."""
    git = SiblingGit({"ibkr_trader": ("aaa", 1500.0), "data-lake": ("bbb", 2500.0)})
    docker, report = FakeDocker(built=2000.0), collectors.Report()
    got = collectors.redeploy(
        built_from(tmp_path, "data-lake"), ours(tmp_path), docker, git, report, {}
    )
    assert got == {collectors.CODE_AT: 2500.0, "deployed": True}
    assert any("onto data-lake@bbb" in line for line in report.lines)
    unlisted = collectors.redeploy(target(tmp_path), ours(tmp_path), docker, git, report, {})
    assert unlisted == {collectors.CODE_AT: 2000.0}, "not built from it, not redeployed for it"


def test_a_checkout_built_from_that_is_not_merged_code_holds_the_redeploy(tmp_path):
    git = SiblingGit(
        {"ibkr_trader": ("aaa", 1500.0), "data-lake": ("bbb", 2500.0)},
        held={"data-lake": "the checkout has uncommitted edits"},
    )
    docker, report = FakeDocker(built=2000.0), collectors.Report()
    got = collectors.redeploy(
        built_from(tmp_path, "data-lake"), ours(tmp_path), docker, git, report, {}
    )
    assert got == {
        collectors.CODE_AT: 2000.0,
        collectors.HELD: "data-lake: the checkout has uncommitted edits",
    }
    assert not any(call[0] == "deploy" for call in docker.calls)


def test_a_checkout_built_from_that_git_cannot_read_is_left_out(tmp_path):
    git = SiblingGit({"ibkr_trader": ("aaa", 2500.0)})
    docker, report = FakeDocker(built=2000.0), collectors.Report()
    got = collectors.redeploy(built_from(tmp_path, "gone"), ours(tmp_path), docker, git, report, {})
    assert got == {collectors.CODE_AT: 2500.0, "deployed": True}
    assert "held gone" not in git.calls


def test_source_heads_is_the_checkout_then_each_it_builds_from_that_git_can_read(tmp_path):
    git = SiblingGit({"ibkr_trader": ("aaa", 1.0), "data-lake": ("bbb", 2.0)})
    t = built_from(tmp_path, "data-lake", "gone")
    assert collectors.source_heads(t, git) == [
        collectors.Source(t.checkout, "aaa", 1.0),
        collectors.Source(t.checkout.parent / "data-lake", "bbb", 2.0),
    ]
    assert collectors.source_heads(target(tmp_path), git) == [
        collectors.Source(t.checkout, "aaa", 1.0)
    ]


# A full sha, as ibkr_trader's gate writes its pin; zero-padded so it reads as no secret.
PIN = "c0aa963" + "0" * 33


def compose(t):
    (t.checkout / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    return t


def test_a_pinned_sibling_is_read_at_the_pin_not_at_its_head(tmp_path):
    """8e3c4a67: data-lake's HEAD ran ahead of the commit ibkr_trader's gate pins."""
    git = SiblingGit(
        {"ibkr_trader": ("aaa", 1.0), "data-lake": ("new", 9.0)},
        pins={"data-lake": PIN},
        commits={PIN: ("c0aa963", 2.0)},
    )
    t = built_from(tmp_path, "data-lake")
    assert collectors.source_heads(t, git) == [
        collectors.Source(t.checkout, "aaa", 1.0),
        collectors.Source(t.checkout.parent / "data-lake", "c0aa963", 2.0, PIN, PIN),
    ]


def test_a_sibling_moving_past_the_pin_does_not_redeploy(tmp_path):
    """The second 8e3c4a67 incident: data-lake #66 merged, ibkr_trader's migration had
    not, and the 01:00Z pass rebuilt the image onto #66. Its pin had not moved."""
    git = SiblingGit(
        {"ibkr_trader": ("aaa", 1500.0), "data-lake": ("c0aa963", 2500.0)},
        pins={"data-lake": PIN},
        commits={PIN: ("730f759", 1800.0)},
    )
    docker, report = FakeDocker(built=2000.0), collectors.Report()
    got = collectors.redeploy(
        compose(built_from(tmp_path, "data-lake")), ours(tmp_path), docker, git, report, {}
    )
    assert got == {collectors.CODE_AT: 2000.0}
    assert not any(call[0] == "deploy" for call in docker.calls)


def test_a_redeploy_with_a_pinned_sibling_builds_a_context_staged_at_the_pin(tmp_path):
    git = SiblingGit(
        {"ibkr_trader": ("bbb", 2500.0), "data-lake": ("new", 9000.0)},
        pins={"data-lake": PIN},
        commits={PIN: ("c0aa963", 1800.0)},
    )
    docker, report = FakeDocker(built=2000.0), collectors.Report()
    got = collectors.redeploy(
        compose(built_from(tmp_path, "data-lake")), ours(tmp_path), docker, git, report, {}
    )
    assert got == {collectors.CODE_AT: 2500.0, "deployed": True}
    assert git.exported == [("ibkr_trader", "HEAD"), ("data-lake", PIN)]
    assert "held data-lake " + PIN in git.calls, "the pin, not the checkout, must be merged"
    names = [name for name, _text in docker.deployed_files]
    assert names == ["docker-compose.yml", collectors.collector_build.OVERRIDE_NAME]
    context = json.loads(docker.deployed_files[1][1])["services"]["app"]["build"]["context"]
    assert Path(context).name.startswith("ibkr_trader-build-")
    assert not Path(context).exists(), "the staged context is removed once built"


def test_deploy_sources_builds_from_the_checkouts_when_nothing_is_pinned(tmp_path):
    t = built_from(tmp_path, "data-lake")
    sources = [collectors.Source(t.checkout, "aaa", 1.0)]
    docker, git = FakeDocker(), SiblingGit({})
    assert collectors.deploy_sources(t, sources, docker, git) == (True, "")
    assert docker.calls == [("deploy", "ibkr_trader", "app")]
    assert docker.deployed_files == [] and git.exported == [], "no staging without a pin"


def test_a_pin_the_sibling_does_not_have_holds_the_redeploy(tmp_path):
    git = SiblingGit({"ibkr_trader": ("bbb", 2500.0)}, pins={"data-lake": PIN})
    docker, report = FakeDocker(built=2000.0), collectors.Report()
    got = collectors.redeploy(
        compose(built_from(tmp_path, "data-lake")), ours(tmp_path), docker, git, report, {}
    )
    assert got[collectors.HELD] == (
        f"data-lake: ibkr_trader pins {PIN[:12]}, which this checkout does not have"
    )
    assert not any(call[0] == "deploy" for call in docker.calls)


def test_a_staging_that_fails_is_a_failed_redeploy(tmp_path):
    git = SiblingGit(
        {"ibkr_trader": ("bbb", 2500.0)},
        pins={"data-lake": PIN},
        commits={PIN: ("c0aa963", 1800.0)},
    )
    docker, report = FakeDocker(built=2000.0), collectors.Report()
    got = collectors.redeploy(
        built_from(tmp_path, "data-lake"), ours(tmp_path), docker, git, report, {}
    )
    assert got == {collectors.CODE_AT: 2000.0}
    assert report.failures == 1 and "has no compose file to build from" in report.cause
    assert not any(call[0] == "deploy" for call in docker.calls)


def test_held_in_names_only_a_checkout_built_from(tmp_path):
    t = built_from(tmp_path, "data-lake")
    assert collectors._held_in(t, t.checkout, "why") == "why"
    assert collectors._held_in(t, t.checkout.parent / "data-lake", "why") == "data-lake: why"


def test_spawn_names_the_missing_program(monkeypatch):
    def fake_run(argv, **kwargs):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(collectors.subprocess, "run", fake_run)
    assert collectors.spawn(["git", "status"], 5) == (127, "git is not on PATH")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-10-03T00:16:05.123456789Z", dt.datetime(2026, 10, 3, 0, 16, 5, tzinfo=dt.UTC)),
        ("2026-10-03T00:16:05+02:00", dt.datetime(2026, 10, 2, 22, 16, 5, tzinfo=dt.UTC)),
        ("2026-10-03T00:16:05", None),
        ("<no value>", None),
    ],
)
def test_dockers_created_is_read_to_the_second(text, expected):
    assert collectors.parse_created(text) == (expected.timestamp() if expected else None)


def test_built_reads_the_containers_image_then_its_date(monkeypatch):
    asked = []

    def fake_run(argv, **kwargs):
        asked.append(argv)
        out = "sha256:abc\n" if argv[1] == "inspect" else "2026-10-03T00:16:05.5Z\n"
        return subprocess.CompletedProcess(argv, 0, out, "")

    monkeypatch.setattr(collectors.subprocess, "run", fake_run)
    when = collectors.Docker().built(box(cid="c9"))
    assert when == dt.datetime(2026, 10, 3, 0, 16, 5, tzinfo=dt.UTC).timestamp()
    assert asked[0][-1] == "c9" and asked[1][-1] == "sha256:abc"


def test_built_is_none_when_the_engine_cannot_say(monkeypatch):
    monkeypatch.setattr(
        collectors.subprocess,
        "run",
        lambda argv, **k: subprocess.CompletedProcess(argv, 1, "", "No such object"),
    )
    assert collectors.Docker().built(box()) is None


def test_deploy_builds_then_recreates_the_named_service(monkeypatch, tmp_path):
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(argv=argv, cwd=kwargs["cwd"])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(collectors.subprocess, "run", fake_run)
    assert collectors.Docker().deploy(tmp_path, "app") == (True, "")
    assert seen == {"argv": ["docker", "compose", "up", "-d", "--build", "app"], "cwd": tmp_path}


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def merged(tmp_path):
    """A checkout at the tip of origin's default branch, as the static checkout sits."""
    repo = tmp_path / "ibkr_trader"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "one")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    return repo


def test_git_deploys_merged_code_only(merged):
    git = collectors.Git()
    assert git.held(merged) == ""
    sha, when = git.head(merged)
    assert sha and when > 0
    (merged / "a.txt").write_text("edited\n", encoding="utf-8")
    assert git.held(merged) == "the checkout has uncommitted edits"
    _git(merged, "commit", "-q", "-am", "local only")
    assert git.held(merged) == "HEAD is not on origin's default branch"
    _git(merged, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    assert git.held(merged) == "origin's default branch is unknown here"


def test_git_says_nothing_of_a_directory_that_is_not_a_checkout(tmp_path):
    assert collectors.Git().head(tmp_path) is None


def test_deploy_names_the_compose_files_it_is_handed(monkeypatch, tmp_path):
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(argv=argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(collectors.subprocess, "run", fake_run)
    base, extra = tmp_path / "docker-compose.yml", tmp_path / "staged.yaml"
    collectors.Docker().deploy(tmp_path, "app", [base, extra])
    named = ["-f", str(base), "-f", str(extra)]
    assert seen["argv"] == ["docker", "compose", *named, "up", "-d", "--build", "app"]


def test_git_reads_resolves_and_exports_a_pinned_commit(merged, tmp_path):
    git = collectors.Git()

    def sha():
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=merged, capture_output=True, text=True, check=True
        ).stdout.strip()

    pinned = sha()
    (merged / "a.txt").write_text("two\n", encoding="utf-8")
    _git(merged, "commit", "-q", "-am", "two, local only")
    local = sha()
    assert git.resolve(merged, pinned) == pinned
    assert git.resolve(merged, "main") == pinned, "a branch by its remote-tracking ref"
    assert git.resolve(merged, "0" * 40) == ""
    assert git.head(merged, pinned)[0] == pinned[:7]
    (merged / "a.txt").write_text("edited\n", encoding="utf-8")
    assert git.held(merged, pinned) == "", "a pin is built from its commit, not the files"
    assert git.held(merged) == "HEAD is not on origin's default branch"
    assert git.held(merged, local) == f"{local[:7]} is not on origin's default branch"
    dest = tmp_path / "staged" / "ibkr_trader"
    dest.parent.mkdir()
    assert git.export(merged, pinned, dest) == ""
    assert (dest / "a.txt").read_text(encoding="utf-8") == "one\n"
    assert list(dest.parent.iterdir()) == [dest], "the archive is removed once unpacked"
    assert git.export(merged, "0" * 40, tmp_path / "staged" / "gone") != ""


def test_git_pin_is_the_ref_the_checkouts_pr_gate_checks_the_sibling_out_at(tmp_path):
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "pr-gate.yml").write_text(
        "jobs:\n  t:\n    steps:\n"
        "      - uses: actions/checkout@v7\n"
        "        with:\n"
        "          repository: alexandrec90/data-lake\n"
        f"          ref: {PIN}\n",
        encoding="utf-8",
    )
    assert collectors.Git().pin(tmp_path, "data-lake") == PIN
    assert collectors.Git().pin(tmp_path, "other") == ""


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


def test_a_scheduled_collectors_needs_make_a_silent_engine_this_passs_to_fail(tmp_path):
    """08ec2a47: the only container collector here was `stop`, so the pass said "nothing
    here can be running" with 0 failures, while every social-scraper fire failed on the
    engine its `needs` run in."""
    scraper = scheduled(tmp_path)
    report = collectors.Report()
    collectors.maintain([target(tmp_path, mode=STOP), scraper], FakeDocker(None), report, {})
    assert report.failures == 1 and "Docker Desktop" in report.lines[0]

    report = collectors.Report()
    collectors.maintain([scraper], FakeDocker(None), report, {})
    assert report.failures == 1, "no container collector at all is no reason to look away"

    report, docker = collectors.Report(), FakeDocker(None)
    collectors.maintain([scheduled(tmp_path, needs=())], docker, report, {})
    assert report.failures == 0 and docker.calls == [], "nothing here needs the engine"


def scheduled(tmp_path, mode=RUN, needs=("db",)):
    collector = config.Collector("social-scraper", command=("uv",), minutes=30, needs=needs)
    return collectors.Target(collector, mode, target(tmp_path).checkout)


def test_needs_engine_counts_a_run_container_or_a_scheduled_runs_needs(tmp_path):
    assert collectors.needs_engine([target(tmp_path)])
    assert collectors.needs_engine([scheduled(tmp_path)])
    assert not collectors.needs_engine([target(tmp_path, mode=STOP)])
    assert not collectors.needs_engine([scheduled(tmp_path, mode=STOP)])
    assert not collectors.needs_engine([scheduled(tmp_path, needs=())])
    assert not collectors.needs_engine([])


def test_reach_engine_fails_a_silent_engine_only_where_it_is_needed(tmp_path):
    report = collectors.Report()
    assert collectors.reach_engine(FakeDocker([]), report, None, True) == []
    assert collectors.reach_engine(FakeDocker(None), report, None, False) is None
    assert report.failures == 0 and "nothing here can be running" in report.lines[-1]
    assert collectors.reach_engine(FakeDocker(None), report, None, True) is None
    assert report.failures == 1


def test_a_scheduled_collector_is_never_kept_up_as_a_container(tmp_path):
    """`act` hands `maintain` every target, for the engine; only containers are acted on."""
    docker, report = FakeDocker([]), collectors.Report()
    collectors.maintain([scheduled(tmp_path)], docker, report, {})
    assert docker.calls == [("ps",)] and report.failures == 0


def test_act_hands_maintain_the_scheduled_targets_too(tmp_path, monkeypatch):
    root = devkit_home(tmp_path, monkeypatch, {})
    seen: list[list] = []
    monkeypatch.setattr(collectors, "maintain_scheduled", lambda *a, **k: None)
    monkeypatch.setattr(collectors, "maintain", lambda chosen, *a, **k: seen.append(chosen))
    chosen = [target(tmp_path), scheduled(tmp_path)]
    collectors.act(chosen, root, FakeDocker([]), collectors.Report(), run=lambda *a: None)
    assert seen == [chosen]


WSL_HUNG = (
    r"engine linux/wsl failed to start: getting WSL provisioner: listing WSL distros: "
    r"DockerDesktop/Wsl/CommandTimedOut: c:\windows\system32\wsl.exe -l -v --all: exit status 1"
)


def test_an_engine_docker_desktop_failed_to_start_is_revived_not_left_to_a_person(
    tmp_path, instant
):
    """08ec2a47: Docker Desktop said `stopped`, its reason a hung `wsl.exe`. Nobody had
    quit it, and "start it" would have waited for a person who was not coming."""
    docker, report = Wedged(tmp_path / "restarts.json"), collectors.Report()
    docker.desktop, docker.stopped = False, WSL_HUNG
    collectors.maintain([target(tmp_path)], docker, report, {})
    assert ("restart",) in docker.calls and report.failures == 0
    assert report.lines[0] == f"Docker Desktop says its engine stopped: {WSL_HUNG}"


def test_a_failed_start_that_a_restart_cannot_mend_says_so(tmp_path, instant):
    docker = Wedged(tmp_path / "restarts.json", revives=False)
    docker.desktop, docker.stopped, report = False, WSL_HUNG, collectors.Report()
    collectors.maintain([target(tmp_path)], docker, report, {})
    assert report.failures == 1
    assert report.lines[-1] == (
        "docker is not answering: Docker Desktop's engine failed to start, "
        "and a restart did not bring its engine back"
    )


@pytest.mark.parametrize("desktop", [False, True], ids=["failed-to-start", "wedged"])
def test_a_hung_wsl_fails_at_once_without_a_restart_it_cannot_reach(tmp_path, instant, desktop):
    """5b71506f: `WslService` sat in `StopPending`. The 23:45 pass waited out the engine,
    a restart and both `wsl` calls of the VM reset at their full timeouts, ran 35 minutes
    on a 15-minute schedule, skipped the 00:00 fire, and failed on the restart anyway."""
    docker = Wedged(tmp_path / "restarts.json", revives=True, resets=True)
    docker.desktop, docker.stopped, docker.wsl = desktop, "" if desktop else WSL_HUNG, False
    report = collectors.Report()
    collectors.maintain([target(tmp_path)], docker, report, {})
    assert report.failures == 1 and report.lines[-1] == collectors.WSL_HUNG
    assert ("restart",) not in docker.calls and ("reset",) not in docker.calls
    assert sum(instant.slept) == 0, "no wait for an engine whose WSL cannot start it"
    assert collectors.last_restart(tmp_path / "restarts.json") is None
    assert "elevated" in collectors.WSL_HUNG and "reboot" in collectors.WSL_HUNG


def test_engine_down_asks_wsl_only_of_an_engine_a_revival_would_try():
    docker, report = FakeDocker(None), collectors.Report()
    docker.stopped = "user requested"
    _wedged, refused = collectors.engine_down(docker, report)
    assert "start it" in refused and docker.wsl_asks == 0, "a quit Desktop is a person's"
    docker.stopped = WSL_HUNG
    assert collectors.engine_down(docker, report) == (
        "docker is not answering: Docker Desktop's engine failed to start",
        "",
    )
    docker.desktop, docker.wsl = True, False
    assert collectors.engine_down(docker, report) == (
        "docker is not answering though Docker Desktop says it is running",
        collectors.WSL_HUNG,
    )


def test_wsl_is_asked_only_once_the_engine_is_silent(tmp_path, instant):
    docker, report = FakeDocker([]), collectors.Report()
    docker.restarts = tmp_path / "restarts.json"
    collectors.maintain([target(tmp_path)], docker, report, {})
    assert docker.wsl_asks == 0


@pytest.mark.parametrize(
    ("answer", "answers"),
    [
        ((collectors.TIMED_OUT, "timed out after 60s"), False),
        ((1, "Windows Subsystem for Linux has no installed distributions."), True),
        ((collectors.TIMED_OUT, "a wsl that exited 124 itself"), True),
        ((0, "docker-desktop\n"), True),
    ],
    ids=["timed-out", "an-error-is-an-answer", "its-own-124", "listed"],
)
def test_docker_calls_wsl_hung_only_on_a_timeout(monkeypatch, answer, answers):
    monkeypatch.setattr(collectors.os, "name", "nt")
    asked: list = []
    monkeypatch.setattr(
        collectors, "spawn", lambda argv, timeout, cwd=None: asked.append((argv, timeout)) or answer
    )
    assert collectors.Docker().wsl_answers() is answers
    assert asked == [(["wsl", "--list", "--quiet"], collectors.WSL_PROBE_TIMEOUT)]


def test_docker_asks_no_wsl_off_windows(monkeypatch):
    monkeypatch.setattr(collectors.os, "name", "posix")
    monkeypatch.setattr(collectors, "spawn", lambda *a, **k: pytest.fail("spawned"))
    assert collectors.Docker().wsl_answers() is True


def test_spawn_reports_a_timeout_as_timed_out(monkeypatch):
    def slow(*args, **kwargs):
        raise collectors.subprocess.TimeoutExpired("wsl", 60)

    monkeypatch.setattr(collectors.subprocess, "run", slow)
    assert collectors.spawn(["wsl"], 60) == (collectors.TIMED_OUT, "timed out after 60s")


def test_a_desktop_stopped_for_no_failure_is_still_left_to_a_person(tmp_path, instant):
    docker, report = Wedged(tmp_path / "restarts.json"), collectors.Report()
    docker.desktop, docker.stopped = False, "user requested"
    collectors.maintain([target(tmp_path)], docker, report, {})
    assert ("restart",) not in docker.calls and "start it" in report.lines[-1]


def test_docker_reads_desktops_stop_reason_only_on_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(collectors.os, "name", "posix")
    assert collectors.Docker().stop_reason() == ""
    monkeypatch.setattr(collectors.os, "name", "nt")
    monkeypatch.setattr(collectors.docker_memory, "log_dir", lambda: tmp_path)
    read: list[Path] = []
    monkeypatch.setattr(
        collectors.collector_tasks, "stop_reason", lambda logs: read.append(logs) or WSL_HUNG
    )
    assert collectors.Docker().stop_reason() == WSL_HUNG and read == [tmp_path]


@pytest.fixture
def instant(monkeypatch):
    """`collectors._time` on a `Clock`: `revive_engine`'s re-asking costs a test nothing."""
    clock = Clock()
    fake = SimpleNamespace(monotonic=clock, sleep=clock.sleep, time=collectors._time.time)
    monkeypatch.setattr(collectors, "_time", fake)
    return clock


class Wedged(FakeDocker):
    """Docker Desktop says `running`, and its engine answers only once restarted -- or,
    where `revives` is False, only once its VM is reset too, where `resets` is True."""

    def __init__(self, restarts, revives=True, resets=False):
        super().__init__(None)
        self.desktop = True
        self.restarts = restarts
        self.revives = revives
        self.resets = resets

    def restart_desktop(self):
        if self.revives:
            self.containers = []
        return super().restart_desktop()

    def reset_vm(self):
        if self.resets:
            self.containers = []
        return super().reset_vm()


def test_a_wedged_engine_is_restarted_once_and_the_pass_then_acts(tmp_path, instant):
    """4436a22a: the engine answered every request with a 500 for six hours behind a
    Docker Desktop that said `running`, and the pass failed every 15 minutes asking
    whether Desktop was running; `docker desktop restart` had it back in minutes."""
    restarts = tmp_path / "restarts.json"
    docker, report = Wedged(restarts), collectors.Report()
    collectors.maintain([target(tmp_path)], docker, report, {})
    assert report.failures == 0
    restart = docker.calls.index(("restart",))
    assert docker.calls[:2] == [("ps",), ("desktop",)]
    assert set(docker.calls[2:restart]) == {("ps",)}, "asked again before it is called wedged"
    assert sum(instant.slept) == collectors.WEDGE_CONFIRM
    assert docker.calls[restart + 1] == ("ps",)
    assert ("up", "ibkr_trader", "app") in docker.calls
    assert "restarted it" in report.lines[0]
    assert collectors.last_restart(restarts) is not None


class Slow(Wedged):
    """An engine that misses the pass's first question and answers its `answers_at`-th."""

    def __init__(self, restarts, answers_at):
        super().__init__(restarts)
        self.answers_at = answers_at
        self.asked = 0

    def ps(self):
        super().ps()
        self.asked += 1
        return [] if self.asked >= self.answers_at else None


def test_an_engine_slow_to_answer_is_not_restarted(tmp_path, instant):
    """6c504c34: at 22:15 UTC one `docker ps` that did not answer on a loaded machine
    restarted Docker Desktop, while the engine was serving the scrape's compose calls;
    the restart shut down social-scraper's db under its scrape mid-run."""
    restarts = tmp_path / "restarts.json"
    docker, report = Slow(restarts, answers_at=4), collectors.Report()
    collectors.maintain([target(tmp_path)], docker, report, {})
    assert ("restart",) not in docker.calls
    assert report.failures == 0 and "slow to answer, not wedged" in report.lines[0]
    assert ("up", "ibkr_trader", "app") in docker.calls
    assert not restarts.exists()


def test_a_wedged_engine_is_not_restarted_under_a_scheduled_run(tmp_path, instant):
    """The engine's API can be silent while the containers serve: a scrape writing to its
    db is cut short by the restart, not by the wedge. The pass after the run restarts."""
    restarts = tmp_path / "restarts.json"
    docker, report = Wedged(restarts), collectors.Report()

    def in_flight():
        return ["social-scraper"]

    docker.holds = in_flight
    collectors.maintain([target(tmp_path)], docker, report, {})
    assert ("restart",) not in docker.calls
    assert "social-scraper's scheduled run" in report.lines[0]
    assert collectors.last_restart(restarts) is None, "a held restart is not a restart"
    docker.holds = list
    collectors.maintain([target(tmp_path)], docker, collectors.Report(), {})
    assert ("restart",) in docker.calls
    assert collectors.HOLD_SINCE not in collectors.restart_record(restarts)


def test_a_held_restart_is_no_failure(tmp_path, instant):
    """84ad65d7: 2026-10-09 19:15 UTC Docker's VM froze under a scrape; the pass held the
    restart and failed, and by the next pass the engine was back on its own."""
    restarts = tmp_path / "restarts.json"
    docker, report = Wedged(restarts, revives=False), collectors.Report()
    docker.holds = lambda: ["social-scraper"]
    collectors.maintain([target(tmp_path)], docker, report, {})
    assert report.failures == 0 and "(held 0 min)" in report.lines[0]
    since = collectors.restart_record(restarts)[collectors.HOLD_SINCE]

    report = collectors.Report()
    collectors.maintain([target(tmp_path)], docker, report, {})
    assert report.failures == 0, "a second pass inside the hold still waits"
    assert collectors.restart_record(restarts)[collectors.HOLD_SINCE] == since
    assert ("restart",) not in docker.calls


@pytest.mark.parametrize("revives", [True, False], ids=["comes-back", "stays-wedged"])
def test_a_wedge_held_for_a_pass_is_restarted_under_the_run(tmp_path, instant, revives):
    """0c428d0e: 2026-10-09 the engine wedged under the 20:30 UTC scrape and the pass held
    its restart for the run, which blocked on its db until its `FIRE_TIMEOUT`; the next
    fire was running by then, so no pass ever found nothing in flight to restart under."""
    assert collectors.BUSY_HOLD < 15 * 60, "the second pass to find the wedge restarts it"
    assert collectors.BUSY_HOLD < collectors.HOLD_LIMIT
    restarts = tmp_path / "restarts.json"
    old = collectors._clock() - collectors.BUSY_HOLD - 1
    collectors.write_file(restarts, json.dumps({collectors.HOLD_SINCE: old}))
    docker, report = Wedged(restarts, revives=revives), collectors.Report()
    docker.holds = lambda: ["social-scraper"]
    collectors.maintain([target(tmp_path)], docker, report, {})
    assert ("restart",) in docker.calls
    assert report.lines[0] == (
        "restarting Docker Desktop under social-scraper's scheduled run: its engine has "
        f"not answered for {collectors.BUSY_HOLD // 60} min, so the run is not being served either"
    )
    record = collectors.restart_record(restarts)
    assert collectors.HOLD_SINCE not in record and collectors.last_restart(restarts) is not None
    if revives:
        assert report.failures == 0 and ("up", "ibkr_trader", "app") in docker.calls
    else:
        assert report.lines[-1] == (
            "docker is not answering though Docker Desktop says it is running, "
            "and a restart did not bring its engine back"
        )


def test_an_engine_that_answers_again_ends_the_hold(tmp_path, instant):
    restarts = tmp_path / "restarts.json"
    collectors.write_file(restarts, json.dumps({"started_at": 5.0, collectors.HOLD_SINCE: 9.0}))
    docker = FakeDocker([])
    docker.restarts = restarts
    collectors.maintain([target(tmp_path)], docker, collectors.Report(), {})
    assert collectors.restart_record(restarts) == {"started_at": 5.0}


def test_release_hold_leaves_a_record_with_no_hold_alone(tmp_path):
    restarts = tmp_path / "restarts.json"
    collectors.release_hold(restarts)
    assert not restarts.exists()
    collectors.write_file(restarts, '{"started_at": 5.0}')
    before = restarts.stat().st_mtime_ns
    collectors.release_hold(restarts)
    assert restarts.stat().st_mtime_ns == before


def test_a_hold_stamped_in_the_future_starts_again_now(tmp_path):
    restarts = tmp_path / "restarts.json"
    collectors.write_file(restarts, json.dumps({collectors.HOLD_SINCE: 2000.0}))
    assert collectors.hold_restart(restarts, 1000.0) == 1000.0
    assert collectors.hold_restart(restarts, 1500.0) == 1000.0


def test_the_scheduled_pass_holds_a_restart_for_a_running_scrape(tmp_path, monkeypatch, instant):
    scraper_home(tmp_path, monkeypatch, {"social-scraper": RUN, "ibkr_trader": RUN})
    built = []

    def make(**kw):
        docker = Wedged(kw["restarts"])
        docker.holds = kw["holds"]
        built.append(docker)
        return docker

    monkeypatch.setattr(collectors, "Docker", make)
    assert collectors.main(["maintain"], run=Running()) == 0, "a young hold fails nothing"
    assert built[0].holds() == ["social-scraper"]
    assert ("restart",) not in built[0].calls


def test_scheduled_in_flight_names_only_a_running_task_this_machine_runs(tmp_path):
    scraper = config.Collector("social-scraper", command=("uv", "run", "x"), minutes=30)
    run_here = collectors.Target(scraper, RUN, tmp_path / "social-scraper")
    assert collectors.scheduled_in_flight([run_here, target(tmp_path)], Running()) == [
        "social-scraper"
    ]
    assert collectors.scheduled_in_flight([run_here], FakeSchtasks()) == []
    stopped = collectors.Target(scraper, STOP, tmp_path / "social-scraper")
    assert collectors.scheduled_in_flight([stopped], Running()) == []


@pytest.mark.parametrize("desktop", [True, False], ids=["app-up", "app-closed"])
def test_an_engine_down_under_a_docker_desktop_update_is_held_not_failed(
    tmp_path, instant, desktop
):
    """3012d246: 2026-10-09 Docker Desktop installed 4.94.0 from 19:55 to 20:28 UTC, 10
    minutes after this pass's restart, and the 20:02 and 20:15 passes failed "a restart
    did not bring its engine back". The engine was down for the installer, not wedged."""
    restarts = tmp_path / "restarts.json"
    collectors.write_file(restarts, json.dumps({"started_at": collectors._clock() - 17 * 60}))
    docker, report = Wedged(restarts, revives=False), collectors.Report()
    docker.update, docker.desktop = True, desktop
    collectors.maintain([target(tmp_path)], docker, report, {})
    assert report.failures == 0
    assert "installing an update" in report.lines[0] and "(held 0 min)" in report.lines[0]
    assert ("restart",) not in docker.calls and ("desktop",) not in docker.calls
    assert collectors.HOLD_SINCE in collectors.restart_record(restarts)


def test_an_update_that_outlasts_the_hold_limit_fails_with_a_stable_cause(tmp_path, instant):
    restarts = tmp_path / "restarts.json"
    old = collectors._clock() - collectors.HOLD_LIMIT - 1
    collectors.write_file(restarts, json.dumps({collectors.HOLD_SINCE: old}))
    docker, report = Wedged(restarts, revives=False), collectors.Report()
    docker.update = True
    collectors.maintain([target(tmp_path)], docker, report, {})
    assert report.failures == 1 and ("restart",) not in docker.calls
    assert report.lines[-1] == ("docker is not answering while Docker Desktop installs an update")


def test_the_engine_answering_after_an_update_ends_its_hold(tmp_path, instant):
    restarts = tmp_path / "restarts.json"
    docker = Wedged(restarts, revives=False)
    docker.update = True
    collectors.maintain([target(tmp_path)], docker, collectors.Report(), {})
    docker.update, docker.containers = False, []
    collectors.maintain([target(tmp_path)], docker, collectors.Report(), {})
    assert collectors.HOLD_SINCE not in collectors.restart_record(restarts)


def test_a_restart_is_not_repeated_within_the_hour(tmp_path, instant):
    restarts = tmp_path / "restarts.json"
    collectors.write_file(restarts, json.dumps({"started_at": collectors._clock() - 60}))
    docker, report = Wedged(restarts), collectors.Report()
    collectors.maintain([target(tmp_path)], docker, report, {})
    assert ("restart",) not in docker.calls
    assert report.failures == 1 and "restart did not bring" in report.lines[-1]
    assert docker.calls.count(("reset",)) == 1, "the VM is reset once since that restart"
    docker.calls.clear()
    collectors.maintain([target(tmp_path)], docker, collectors.Report(), {})
    assert ("restart",) not in docker.calls and ("reset",) not in docker.calls


def test_a_pass_inside_the_hour_resets_the_vm_a_restart_left_wedged(tmp_path, instant):
    """a70fa348: 2026-10-09 22:45 UTC a pass from a checkout without `reset_vm` restarted
    the wedged engine to no effect; the 23:15 pass had the reset, and the hour hold failed
    it without running it. The 23:45 pass would have failed the same way."""
    restarts = tmp_path / "restarts.json"
    collectors.write_file(restarts, json.dumps({"started_at": collectors._clock() - 30 * 60}))
    docker, report = Wedged(restarts, revives=False, resets=True), collectors.Report()
    collectors.maintain([target(tmp_path)], docker, report, {})
    assert report.failures == 0
    assert ("restart",) not in docker.calls and ("reset",) in docker.calls
    assert ("up", "ibkr_trader", "app") in docker.calls
    assert any("WSL VM was reset" in line for line in report.lines)


def test_a_vm_reset_since_the_restart_is_not_repeated(tmp_path, instant):
    restarts = tmp_path / "restarts.json"
    now = collectors._clock()
    record = {collectors.STARTED_AT: now - 30 * 60, collectors.VM_RESET_AT: now - 29 * 60}
    collectors.write_file(restarts, json.dumps(record))
    docker, report = Wedged(restarts, revives=False, resets=True), collectors.Report()
    collectors.maintain([target(tmp_path)], docker, report, {})
    assert report.failures == 1 and ("reset",) not in docker.calls


def test_revive_vm_once_records_the_reset_beside_the_restart_before_it_runs(tmp_path, instant):
    restarts = tmp_path / "restarts.json"
    collectors.write_file(restarts, json.dumps({collectors.STARTED_AT: 1000.0}))
    docker = Wedged(restarts, revives=False)
    docker.reset_answer = (False, "error: timed out\n")
    assert collectors.revive_vm_once(docker, collectors.Report(), restarts, 1500.0) is None
    record = collectors.restart_record(restarts)
    assert record == {collectors.STARTED_AT: 1000.0, collectors.VM_RESET_AT: 1500.0}
    assert collectors.vm_reset_since(restarts, 1000.0), "a reset that failed still counts"


def test_a_vm_reset_before_the_last_restart_does_not_count(tmp_path):
    path = tmp_path / "r.json"
    collectors.write_file(path, json.dumps({"started_at": 2000.0, "vm_reset_at": 1000.0}))
    assert not collectors.vm_reset_since(path, 2000.0)
    assert collectors.vm_reset_since(path, 500.0)
    assert not collectors.vm_reset_since(tmp_path / "missing.json", 0.0)


def test_a_restart_older_than_the_hour_is_tried_again(tmp_path, instant):
    restarts = tmp_path / "restarts.json"
    old = collectors._clock() - collectors.RESTART_EVERY - 1
    collectors.write_file(restarts, json.dumps({"started_at": old}))
    docker = Wedged(restarts)
    collectors.maintain([target(tmp_path)], docker, collectors.Report(), {})
    assert ("restart",) in docker.calls
    assert collectors.last_restart(restarts) > old


def test_a_restart_that_does_not_help_fails_with_a_stable_cause(tmp_path, instant):
    restarts = tmp_path / "restarts.json"
    docker, report = Wedged(restarts, revives=False), collectors.Report()
    docker.restart_answer = (False, "error: timed out waiting for Docker Desktop\n")
    collectors.maintain([target(tmp_path)], docker, report, {})
    assert report.failures == 1
    assert report.lines[-1] == (
        "docker is not answering though Docker Desktop says it is running, "
        "and a restart did not bring its engine back"
    )
    assert "timed out waiting" in report.lines[0]
    assert collectors.last_restart(restarts) is not None, "a failed restart still counts"


def test_a_restart_that_leaves_the_engine_silent_resets_its_vm(tmp_path, instant):
    """d0651b43, 4034a9e2: 2026-10-09 21:45 UTC `docker desktop restart` could not shut
    down a frozen VM ("init failed to shutdown the VM"), started the engine on it again,
    and every pass for the next hour failed "a restart did not bring its engine back"."""
    restarts = tmp_path / "restarts.json"
    docker, report = Wedged(restarts, revives=False, resets=True), collectors.Report()
    collectors.maintain([target(tmp_path)], docker, report, {})
    assert report.failures == 0
    assert docker.calls.index(("restart",)) < docker.calls.index(("reset",))
    assert ("up", "ibkr_trader", "app") in docker.calls
    assert "WSL VM was reset" in report.lines[1]
    assert collectors.last_restart(restarts) is not None


@pytest.mark.parametrize("answer", [(True, ""), (False, "timed out after 630s")])
def test_a_vm_reset_that_does_not_help_fails_with_the_stable_cause(tmp_path, instant, answer):
    restarts = tmp_path / "restarts.json"
    docker, report = Wedged(restarts, revives=False), collectors.Report()
    docker.reset_answer = answer
    collectors.maintain([target(tmp_path)], docker, report, {})
    assert report.failures == 1 and ("reset",) in docker.calls
    assert "reset Docker's WSL VM, and its engine still did not answer" in report.lines[1]
    assert report.lines[-1] == (
        "docker is not answering though Docker Desktop says it is running, "
        "and a restart did not bring its engine back"
    )
    docker.calls.clear()
    collectors.maintain([target(tmp_path)], docker, collectors.Report(), {})
    assert ("reset",) not in docker.calls, "nor is the reset repeated within the hour"


def test_revive_vm_asks_the_engine_only_after_a_start_that_succeeded(tmp_path, instant):
    docker, report = Wedged(tmp_path / "r.json", revives=False, resets=True), collectors.Report()
    assert collectors.revive_vm(docker, report) == []
    docker.containers, docker.calls = None, []
    docker.reset_answer = (False, "error: timed out\n")
    assert collectors.revive_vm(docker, report) is None
    assert docker.calls == [("reset",)], "a failed start is not waited on"
    assert report.lines[-1].endswith("error: timed out")


@pytest.mark.parametrize(
    ("listed", "names"),
    [
        (
            "d\x00o\x00c\x00k\x00e\x00r\x00-\x00d\x00e\x00s\x00k\x00t\x00o\x00p\x00\r\x00\n\x00",
            ["docker-desktop"],
        ),
        ("docker-desktop\r\nUbuntu\r\n\r\n", ["docker-desktop", "Ubuntu"]),
        ("", []),
    ],
)
def test_running_distros_reads_wsls_utf16_listing(listed, names):
    assert collectors.running_distros(listed) == names


@pytest.mark.parametrize(
    ("running", "argv"),
    [
        (["docker-desktop"], ["wsl", "--shutdown"]),
        (["docker-desktop", "docker-desktop-data"], ["wsl", "--shutdown"]),
        ([], ["wsl", "--shutdown"]),
        (["docker-desktop", "Ubuntu"], ["wsl", "--terminate", "docker-desktop"]),
    ],
)
def test_the_vm_is_shut_down_only_when_no_other_distro_is_running(running, argv):
    assert collectors.wsl_stop(running) == argv


def test_reset_vm_stops_desktop_then_its_vm_then_starts_it(monkeypatch):
    monkeypatch.setattr(collectors.os, "name", "nt")
    docker = collectors.Docker()
    asked: list[list[str]] = []

    def run(argv, timeout, cwd=None):
        asked.append(list(argv))
        if argv[:2] == ["wsl", "--list"]:
            return 0, "docker-desktop\r\nUbuntu\r\n"
        return (0, "Docker Desktop is running") if "start" in argv else (0, "")

    docker.run = run
    assert docker.reset_vm() == (True, "Docker Desktop is running")
    assert [argv[:3] for argv in asked] == [
        ["docker", "desktop", "stop"],
        ["wsl", "--list", "--running"],
        ["wsl", "--terminate", "docker-desktop"],
        ["docker", "desktop", "start"],
    ]


def test_off_windows_there_is_no_vm_to_reset(monkeypatch):
    monkeypatch.setattr(collectors.os, "name", "posix")
    docker = collectors.Docker()
    docker.run = lambda argv, timeout, cwd=None: pytest.fail(f"asked {argv}")
    assert docker.reset_vm()[0] is False


def test_a_docker_desktop_someone_quit_is_not_started_behind_their_back(tmp_path):
    docker, report = Wedged(tmp_path / "r.json"), collectors.Report()
    docker.desktop = False
    collectors.maintain([target(tmp_path)], docker, report, {})
    assert ("restart",) not in docker.calls
    assert report.failures == 1 and "Docker Desktop is not running" in report.lines[0]
    assert not (tmp_path / "r.json").exists()


def test_a_stop_machine_never_restarts_docker(tmp_path):
    docker, report = Wedged(tmp_path / "r.json"), collectors.Report()
    collectors.maintain([target(tmp_path, mode=STOP)], docker, report, {})
    assert docker.calls == [("ps",)] and report.failures == 0


def test_revive_engine_answers_the_containers_once_the_restart_brings_them(tmp_path, instant):
    docker, report = Wedged(tmp_path / "r.json"), collectors.Report()
    assert collectors.revive_engine(docker, report, docker.restarts, 1000.0) == ([], "")
    assert collectors.last_restart(docker.restarts) == 1000.0


def test_no_restart_record_means_no_restart(tmp_path):
    """The tray's `Docker` and a test's carry none: only the scheduled pass restarts."""
    docker, report = Wedged(None), collectors.Report()
    collectors.maintain([target(tmp_path)], docker, report, {})
    assert docker.calls == [("ps",)] and report.failures == 1
    assert collectors.Docker().restarts is None


# --- memory: the VM's budget, and a silent VM that ran out ---------------------------


def test_a_silent_engine_whose_vm_ran_out_of_memory_fails_as_that_before_the_restart(
    tmp_path, instant
):
    """2026-10-09: the VM ran out of memory, froze its engine, and booted eight times; the
    pass restarted it each time and named nothing else. The restart still happens -- it
    is what brings the collectors back -- but the cause the ledger files is memory."""
    docker, report = Wedged(tmp_path / "r.json"), collectors.Report()
    docker.evidence = "the VM holds 3.8 GiB of its 4.0 GiB cap"
    collectors.maintain([target(tmp_path)], docker, report, {})
    assert report.cause == collectors.SHORT_OF_MEMORY
    said = report.lines.index("  the VM holds 3.8 GiB of its 4.0 GiB cap")
    assert said < report.lines.index(collectors.SHORT_OF_MEMORY)
    assert ("restart",) in docker.calls


def test_a_silent_engine_with_nothing_said_of_memory_fails_nothing_for_it(tmp_path, instant):
    docker, report = Wedged(tmp_path / "r.json"), collectors.Report()
    collectors.maintain([target(tmp_path)], docker, report, {})
    assert collectors.SHORT_OF_MEMORY not in report.lines
    assert [ask[0] for ask in docker.memory_asks] == ["evidence"]


def test_memory_evidence_is_read_only_by_the_pass_that_may_restart(tmp_path):
    """The tray's `Docker` carries no restart record and has nothing to file."""
    docker = Wedged(None)
    collectors.maintain([target(tmp_path)], docker, collectors.Report(), {})
    assert docker.memory_asks == []


def test_a_kept_up_container_without_a_memory_limit_fails_the_pass(tmp_path):
    """The unbounded container is what ran the VM out: one with a limit is OOM-killed
    alone and its restart policy brings it back."""
    docker, report = (
        FakeDocker([ours(tmp_path, cid="c1"), ours(tmp_path, service="db", cid="c2")]),
        collectors.Report(),
    )
    docker.limits_answer = {"c1" + "0" * 62: 0, "c2" + "0" * 62: 768 * 2**20}
    collectors.maintain([target(tmp_path)], docker, report, {})
    assert report.failures == 1
    assert report.cause.startswith("ibkr_trader: `app` has no memory limit")
    assert docker.memory_asks == [("limits", ("c1", "c2"))]


def test_limits_that_do_not_fit_the_vm_fail_the_pass_with_the_numbers_beside(tmp_path):
    docker, report = (
        FakeDocker([ours(tmp_path, cid="c1"), ours(tmp_path, service="db", cid="c2")]),
        collectors.Report(),
    )
    docker.limits_answer = {"c1": 3 * 2**30, "c2": 2**30}
    docker.vm = 4 * 2**30
    collectors.maintain([target(tmp_path)], docker, report, {})
    assert report.failures == 1 and "do not fit Docker's VM" in report.cause
    assert any("limits total 4.0 GiB" in line for line in report.lines)


def test_limits_that_fit_fail_nothing(tmp_path):
    docker, report = FakeDocker([ours(tmp_path, cid="c1")]), collectors.Report()
    docker.limits_answer, docker.vm = {"c1": 1536 * 2**20}, 4 * 2**30
    collectors.maintain([target(tmp_path)], docker, report, {})
    assert report.failures == 0


def test_only_the_containers_of_a_collector_this_machine_runs_are_judged(tmp_path):
    """A box's stack, carameli parked in the next directory, a stopped container: none
    of them is kept up by this pass, so none is its business."""
    foreign = box(workdir=str(tmp_path / "carameli"), cid="f1")
    stopped = ours(tmp_path, service="db", state="exited", cid="s1")
    docker = FakeDocker([ours(tmp_path, cid="c1"), foreign, stopped])
    collectors.maintain([target(tmp_path)], docker, collectors.Report(), {})
    assert docker.memory_asks == [("limits", ("c1",))]


def test_kept_up_pairs_each_running_container_with_its_collectors_project(tmp_path):
    app, db = ours(tmp_path, cid="c1"), ours(tmp_path, service="db", cid="c2")
    other = box(workdir=str(tmp_path / "carameli"), cid="f1")
    assert collectors.kept_up([target(tmp_path)], [app, db, other]) == [
        ("ibkr_trader", app),
        ("ibkr_trader", db),
    ]


def test_check_memory_with_nothing_kept_up_asks_nothing(tmp_path):
    docker, report = FakeDocker([]), collectors.Report()
    collectors.check_memory([target(tmp_path)], [], docker, report)
    assert docker.memory_asks == [] and report.lines == []


def test_a_stopped_collector_is_not_budgeted(tmp_path):
    docker = FakeDocker([ours(tmp_path, cid="c1")])
    collectors.maintain([target(tmp_path, mode=STOP)], docker, collectors.Report(), {})
    assert docker.memory_asks == []


def test_limits_the_engine_would_not_say_are_said_not_failed(tmp_path):
    docker, report = FakeDocker([ours(tmp_path, cid="c1")]), collectors.Report()
    docker.limits_answer = None
    collectors.maintain([target(tmp_path)], docker, report, {})
    assert report.failures == 0
    assert any("budget is unchecked" in line for line in report.lines)


def test_the_real_docker_asks_the_engine_for_limits_and_the_vm_size(monkeypatch):
    asked = []

    def fake_run(argv, **kwargs):
        asked.append(argv)
        out = "c1full\t536870912\n" if argv[1] == "inspect" else "4109926400\n"
        return subprocess.CompletedProcess(argv, 0, out, "")

    monkeypatch.setattr(collectors.subprocess, "run", fake_run)
    docker = collectors.Docker()
    assert docker.limits(["c1"]) == {"c1full": 512 * 2**20}
    assert docker.vm_memory() == 4109926400
    assert asked[0][:3] == ["docker", "inspect", "--format"] and asked[0][-1] == "c1"


def test_the_scheduled_pass_drops_the_vms_file_cache_once_the_engine_answers(tmp_path):
    """2026-10-10: the VM held 3.81 GB of Windows' memory for containers using 950 MiB,
    and `autoMemoryReclaim` never fired because the VM never idles."""
    docker, report = FakeDocker([ours(tmp_path, cid="c1")]), collectors.Report()
    docker.restarts = tmp_path / "r.json"
    collectors.maintain([target(tmp_path)], docker, report, {})
    assert docker.reclaims == 1 and report.failures == 0


def test_a_drop_that_did_not_run_is_said_not_failed(tmp_path):
    docker, report = FakeDocker([]), collectors.Report()
    docker.reclaim_answer = (False, "Access is denied.\n")
    collectors.reclaim_vm_memory(docker, report)
    assert report.failures == 0
    assert report.lines == ["the VM's file cache was not dropped: Access is denied."]


def test_only_the_scheduled_pass_drops_the_cache(tmp_path):
    """The tray's `Docker` carries no restart record, and a refresh must change nothing."""
    docker = FakeDocker([ours(tmp_path, cid="c1")])
    collectors.maintain([target(tmp_path)], docker, collectors.Report(), {})
    assert docker.reclaims == 0


def test_a_silent_engine_has_no_cache_dropped(tmp_path):
    docker = FakeDocker(None)
    docker.restarts = tmp_path / "r.json"
    collectors.maintain([target(tmp_path, mode=STOP)], docker, collectors.Report(), {})
    assert docker.reclaims == 0


def test_the_real_docker_drops_the_cache_as_root_inside_dockers_distro(monkeypatch):
    monkeypatch.setattr(collectors.os, "name", "nt")
    docker = collectors.Docker()
    asked = []

    def run(argv, timeout, cwd=None):
        asked.append(list(argv))
        return 1, "n\x00o\x00 \x00s\x00u\x00c\x00h\x00"  # wsl.exe writes UTF-16

    docker.run = run
    assert docker.reclaim_cache() == (False, "no such")
    assert asked == [collectors.docker_memory.reclaim_argv(collectors.DOCKER_DISTRO)]


def test_off_windows_there_is_no_cache_to_drop(monkeypatch):
    monkeypatch.setattr(collectors.os, "name", "posix")
    docker = collectors.Docker()
    docker.run = lambda argv, timeout, cwd=None: pytest.fail(f"asked {argv}")
    assert docker.reclaim_cache() == (True, "")


def test_a_silent_vm_is_read_from_windows_alone(tmp_path):
    """Nothing here may ask the engine: this runs exactly when it does not answer."""
    asked = []

    def run(argv):
        asked.append(argv)
        return 0, '"vmmem","11480","Services","0","3,937,576 K"\n'

    (tmp_path / ".wslconfig").write_text("[wsl2]\nmemory=4GB\n", encoding="utf-8")
    said = collectors.docker_memory.read_silent_vm(run, tmp_path, tmp_path / ".wslconfig", 0.0)
    assert said == "the VM holds 3.8 GiB of its 4.0 GiB cap"
    assert [argv[0] for argv in asked] == ["tasklist"]


def test_no_wslconfig_and_no_vm_process_say_nothing(tmp_path):
    said = collectors.docker_memory.read_silent_vm(
        lambda argv: (1, ""), tmp_path, tmp_path / ".wslconfig", 0.0
    )
    assert said == ""


def test_off_windows_a_silent_vm_has_nothing_to_say(monkeypatch):
    monkeypatch.setattr(collectors.os, "name", "posix")
    assert collectors.Docker().memory_evidence(0.0) == ""


def test_the_scheduled_pass_records_its_restarts_beside_its_artifact(tmp_path, monkeypatch):
    root = devkit_home(tmp_path, monkeypatch, {"ibkr_trader": RUN})
    built = []
    monkeypatch.setattr(collectors, "Docker", lambda **kw: built.append(kw) or FakeDocker([]))
    collectors.main(["maintain"])
    assert [kw["restarts"] for kw in built] == [root / collectors.RESTARTED]
    assert built[0]["holds"]() == [], "no scheduled collector here, so nothing holds it"


STATUS = ["docker", "desktop", "status", "--format", "json"]
DESKTOP_UP = '"Docker Desktop.exe","17592","RDP-Tcp#5","1","79,860 K"'
NO_DESKTOP = "INFO: No tasks are running which match the specified criteria."


@pytest.mark.parametrize(
    ("code", "out", "expected"),
    [
        (0, '{"SessionID": "x", "Status": "running"}', True),
        (0, '{"Status": "stopped"}', False),
        (0, 'request returned 500 Internal Server Error\n{"Status": "running"}', True),
    ],
)
def test_docker_desktop_answers_by_its_own_status_when_it_gives_one(code, out, expected):
    docker = collectors.Docker()
    asked: list[list[str]] = []

    def run(argv, timeout, cwd=None):
        asked.append(list(argv))
        return code, out

    docker.run = run
    assert docker.desktop_running() is expected
    assert asked == [STATUS], "a readable status is the answer: no second question"


@pytest.mark.parametrize(
    ("code", "out"),
    [(0, "not json"), (0, "[]"), (1, '{"Status": "running"}'), (124, "timed out after 30s")],
)
@pytest.mark.parametrize(("tasks", "expected"), [(DESKTOP_UP, True), (NO_DESKTOP, False)])
def test_a_status_that_failed_is_answered_by_desktops_process(
    monkeypatch, code, out, tasks, expected
):
    """2026-10-08 21:31 and 21:46: the status failed while the Desktop the 20:15 revive
    started was up, the pass said "not running -- start it", and stood the revive down."""
    monkeypatch.setattr(collectors.os, "name", "nt")
    docker = collectors.Docker()

    def run(argv, timeout, cwd=None):
        return (code, out) if argv == STATUS else (0, tasks)

    docker.run = run
    assert docker.desktop_running() is expected


@pytest.mark.parametrize(
    ("text", "status"),
    [
        ('{"Status": "running"}', "running"),
        ('warning: context "x" not found\n{\n "Status": "stopped"\n}\n', "stopped"),
        ('{"Status": 3}', None),
        ("[]", None),
        ("", None),
        ("{not json}", None),
    ],
)
def test_desktop_status_reads_the_object_wherever_it_sits(text, status):
    assert collectors.desktop_status(text) == status


INSTALLER = '"Docker Desktop Installer.exe","5120","RDP-Tcp#9","1","61,000 K"'


@pytest.mark.parametrize(
    ("answer", "expected"),
    [((0, f"{DESKTOP_UP}\n{INSTALLER}"), True), ((0, DESKTOP_UP), False), ((1, INSTALLER), False)],
)
def test_docker_reads_an_update_off_the_process_listing(monkeypatch, answer, expected):
    monkeypatch.setattr(collectors.os, "name", "nt")
    docker = collectors.Docker()
    asked: list[tuple] = []

    def run(argv, timeout, cwd=None):
        asked.append(tuple(argv))
        return answer

    docker.run = run
    assert docker.updating() is expected
    assert asked == [collectors.collector_tasks.UPDATE_LISTING]


def test_off_windows_docker_is_never_updating(monkeypatch):
    monkeypatch.setattr(collectors.os, "name", "posix")
    docker = collectors.Docker()
    docker.run = lambda argv, timeout, cwd=None: pytest.fail(f"asked {argv}")
    assert docker.updating() is False


def test_off_windows_a_failed_status_is_not_running(monkeypatch):
    monkeypatch.setattr(collectors.os, "name", "posix")
    docker = collectors.Docker()
    docker.run = lambda argv, timeout, cwd=None: (
        pytest.fail(f"asked {argv}") if argv != STATUS else (1, "")
    )
    assert docker.desktop_running() is False


def test_the_restart_is_docker_desktops_own_bounded_by_its_timeout():
    docker = collectors.Docker()
    asked: list[tuple] = []

    def run(argv, timeout, cwd=None):
        asked.append((list(argv), timeout))
        return 0, "Restarting Docker Desktop"

    docker.run = run
    assert docker.restart_desktop() == (True, "Restarting Docker Desktop")
    argv, timeout = asked[0]
    assert argv[:3] == ["docker", "desktop", "restart"]
    assert timeout > collectors.RESTART_TIMEOUT


def test_an_unreadable_restart_record_is_no_restart(tmp_path):
    path = tmp_path / "r.json"
    assert collectors.last_restart(path) is None
    path.write_text("[1]", encoding="utf-8")
    assert collectors.last_restart(path) is None


class Starting(FakeDocker):
    """An engine that answers from its `answers_at`-th question on: Docker Desktop
    coming up after a boot."""

    def __init__(self, answers_at):
        super().__init__([])
        self.answers_at = answers_at

    def ps(self):
        super().ps()
        return self.containers if len(self.calls) >= self.answers_at else None


class Clock:
    """A monotonic clock that `sleep` advances, so a wait costs the test nothing."""

    def __init__(self):
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def test_the_logon_fire_waits_for_docker_desktop_to_start_and_then_acts(tmp_path, monkeypatch):
    """aea4ccfa: the logon fire ran 108 seconds after boot, found the engine silent and
    failed, and the containers were up on their own a minute later."""
    clock = Clock()
    monkeypatch.setattr(collectors, "_time", SimpleNamespace(monotonic=clock, sleep=clock.sleep))
    docker, report = Starting(answers_at=4), collectors.Report()
    collectors.maintain([target(tmp_path)], docker, report, {}, awake=108.0)
    assert report.failures == 0
    assert ("up", "ibkr_trader", "app") in docker.calls
    assert clock.slept == [collectors.ENGINE_POLL] * 3


def test_an_engine_still_silent_when_the_startup_window_closes_fails(tmp_path):
    clock = Clock()
    awake = collectors.ENGINE_STARTUP - 40.0
    docker = Starting(answers_at=10_000)
    assert collectors.wait_for_engine(docker, awake, clock, clock.sleep) is None
    assert sum(clock.slept) == 40.0, "waits out the window, and not a second past it"
    assert clock.slept[-1] == 40.0 - 2 * collectors.ENGINE_POLL


@pytest.mark.parametrize("awake", [None, collectors.ENGINE_STARTUP, 86_400.0])
def test_a_machine_long_up_or_unable_to_say_asks_once(awake):
    clock = Clock()
    docker = Starting(answers_at=2)
    assert collectors.wait_for_engine(docker, awake, clock, clock.sleep) is None
    assert docker.calls == [("ps",)] and clock.slept == []


def test_an_engine_that_answers_at_once_is_never_waited_on():
    clock = Clock()
    docker = FakeDocker([])
    assert collectors.wait_for_engine(docker, 5.0, clock, clock.sleep) == []
    assert clock.slept == []


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


def test_a_silent_engine_row_is_spelled_exactly_as_the_fix_pass_skips_it(tmp_path):
    """`fix_loop.collector_findings` leaves this row to the collectors job by equality."""
    assert collectors.row(target(tmp_path), None, {}) == (collectors.FAIL, collectors.NOT_ANSWERING)


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


def test_a_container_a_pass_is_starting_is_green_and_red_once_the_pass_is_over(
    tmp_path, monkeypatch
):
    """3cc7de43: the fix pass read ibkr_trader's row seven seconds into a redeploy's start,
    compose's new container still `Created`, and filed "not running (Created)" against a
    collector that was up by the time the finding landed."""
    root = devkit_home(tmp_path, monkeypatch, {"ibkr_trader": RUN})
    created = box(workdir=str(tmp_path / "ibkr_trader"), state="created", status="Created")
    docker = FakeDocker([created])
    at(monkeypatch, 10_000.0)
    collectors.write_file(
        root / collectors.IN_PASS,
        json.dumps({collectors.STARTED_AT: 9_900.0, collectors.UNTIL: 12_000.0}),
    )
    assert collectors.tray_rows(docker=docker) == [
        ("collector: ibkr_trader", collectors.OK, f"{collectors.STARTING} (Created)")
    ]
    (root / collectors.IN_PASS).unlink()
    assert collectors.tray_rows(docker=docker) == [
        ("collector: ibkr_trader", collectors.FAIL, "not running (Created)")
    ]


def test_a_first_start_still_building_has_no_container_yet_and_is_green_meanwhile(tmp_path):
    assert collectors.row(target(tmp_path), [], {}, busy=True) == (
        collectors.OK,
        f"{collectors.STARTING} (no container yet)",
    )


@pytest.mark.parametrize(
    ("container", "busy", "expected"),
    [
        (None, False, (collectors.FAIL, "no container -- see logs/collectors.log")),
        ("exited", False, (collectors.FAIL, "not running (Exited (1))")),
        ("exited", True, (collectors.OK, f"{collectors.STARTING} (Exited (1))")),
        (None, True, (collectors.OK, f"{collectors.STARTING} (no container yet)")),
    ],
)
def test_down_row_is_red_unless_a_pass_is_starting_it(container, busy, expected):
    down = box(state=container, status="Exited (1)") if container else None
    assert collectors.down_row(down, busy) == expected


def test_a_pass_says_nothing_of_a_running_collectors_health_or_a_silent_engine(tmp_path):
    """The marker covers a container being started, never a verdict on one that is up."""
    health = {"ibkr_trader": {"ok": False, "summary": "exit 1", "container": "c1"}}
    level, _detail = collectors.row(target(tmp_path), [ours(tmp_path)], health, busy=True)
    assert level == collectors.WARN
    assert collectors.row(target(tmp_path), None, {}, busy=True)[0] == collectors.FAIL


@pytest.mark.parametrize(
    ("text", "clock", "expected"),
    [
        ('{"started_at": 100, "until": 200}', 150, True),
        ('{"started_at": 100, "until": 200}', 100, True),
        ('{"started_at": 100, "until": 200}', 200, False),  # a killed pass's marker
        ('{"started_at": 100, "until": 200}', 50, False),  # a clock that moved
        ('{"started_at": "x", "until": 200}', 150, False),
        ("[1, 2]", 150, False),
        ("not json", 150, False),
        (None, 150, False),
    ],
)
def test_in_pass_reads_only_a_live_marker(tmp_path, text, clock, expected):
    path = tmp_path / "pass.json"
    if text is not None:
        path.write_text(text, encoding="utf-8")
    assert collectors.in_pass(path, clock) is expected


def test_the_marker_is_up_while_the_pass_starts_containers_and_gone_after(tmp_path, monkeypatch):
    root = devkit_home(tmp_path, monkeypatch, {"ibkr_trader": RUN, "sports_betting": RUN})
    seen = []

    class Watching(FakeDocker):
        def up(self, checkout, service):
            seen.append(json.loads((root / collectors.IN_PASS).read_text(encoding="utf-8")))
            return super().up(checkout, service)

    at(monkeypatch, 10_000.0)
    assert collectors.main(["maintain"], docker=Watching([])) == 0
    assert len(seen) == 2 and seen[0][collectors.STARTED_AT] == 10_000.0
    assert seen[0][collectors.UNTIL] == 10_000.0 + 2 * collectors.TARGET_BOUND + (
        collectors.PS_TIMEOUT
    ), "each run target gets its whole bound"
    assert not (root / collectors.IN_PASS).exists()


def test_the_marker_goes_even_when_the_pass_raises(tmp_path):
    path = tmp_path / "logs" / "pass.json"
    with pytest.raises(RuntimeError), collectors.acting(path, [target(tmp_path)]):
        assert path.is_file()
        raise RuntimeError("boom")
    assert not path.exists()


@pytest.mark.parametrize(
    ("text", "clock", "age", "expected"),
    [
        ('{"started_at": 100, "until": 200}', 150, 0, 200),
        ('{"started_at": 100, "until": 200}', 250, 0, None),  # a killed pass's lock
        ('{"until": true}', 150, 2, 158),  # unreadable and young: a holder mid-write
        ("", 150, 0, 160),
        ("not json", 150, collectors.LOCK_WRITING + 1, None),  # unreadable and old: dead
        (None, 150, 0, None),
    ],
)
def test_lock_holder_reads_only_a_live_lock(tmp_path, text, clock, age, expected):
    path = tmp_path / "collectors.lock"
    if text is not None:
        path.write_text(text, encoding="utf-8")
        os.utime(path, (clock - age, clock - age))
    assert collectors.lock_holder(path, clock) == expected


def test_a_held_lock_refuses_a_second_taker_and_a_dead_one_is_broken(tmp_path):
    path = tmp_path / "logs" / "collectors.lock"
    assert collectors.take_lock(path, until=200.0, clock=100.0)
    assert json.loads(path.read_text(encoding="utf-8")) == {
        collectors.STARTED_AT: 100.0,
        collectors.UNTIL: 200.0,
    }
    assert not collectors.take_lock(path, until=300.0, clock=150.0), "a live holder"
    assert collectors.take_lock(path, until=400.0, clock=250.0), "a killed pass's lock"
    assert json.loads(path.read_text(encoding="utf-8"))[collectors.UNTIL] == 400.0


def test_a_waiting_pass_takes_the_lock_once_its_holder_lets_go(tmp_path, monkeypatch, capsys):
    path = tmp_path / "collectors.lock"
    at(monkeypatch, 100.0)
    assert collectors.take_lock(path, until=10_000.0, clock=100.0)
    looks = []

    def sleep(seconds):
        looks.append(seconds)
        if len(looks) == 2:
            path.unlink()

    with collectors.one_pass(path, 50.0, wait=True, sleep=sleep) as had:
        assert had and json.loads(path.read_text(encoding="utf-8"))[collectors.UNTIL] == 150.0
    assert looks == [collectors.LOCK_POLL] * 2
    assert not path.exists(), "released after the pass"
    assert "waiting for the collectors pass" in capsys.readouterr().err


def test_a_pass_that_does_not_wait_leaves_a_held_lock_alone(tmp_path, monkeypatch):
    path = tmp_path / "collectors.lock"
    at(monkeypatch, 100.0)
    assert collectors.take_lock(path, until=10_000.0, clock=100.0)
    with collectors.one_pass(path, 50.0, wait=False, sleep=pytest.fail) as had:
        assert not had
    assert json.loads(path.read_text(encoding="utf-8"))[collectors.UNTIL] == 10_000.0


def test_the_lock_goes_even_when_the_pass_raises(tmp_path, monkeypatch):
    path = tmp_path / "collectors.lock"
    at(monkeypatch, 100.0)
    with pytest.raises(RuntimeError), collectors.one_pass(path, 50.0, wait=False) as had:
        assert had
        raise RuntimeError("boom")
    assert not path.exists()


def test_a_scheduled_pass_stands_down_while_a_run_here_is_acting(tmp_path, monkeypatch):
    """3c3e36f2: `run-here ibkr_trader sports_betting` and the scheduled `maintain` 23s
    later both ran `compose up -d` for the same collectors, and each exited 2 on the
    container name the other had just taken."""
    root = devkit_home(tmp_path, monkeypatch, {"sports_betting": RUN})
    at(monkeypatch, 10_000.0)
    assert collectors.take_lock(root / collectors.PASS_LOCK, until=12_000.0, clock=10_000.0)
    docker = FakeDocker([])
    assert collectors.main(["maintain"], docker=docker) == 0
    assert docker.calls == [], "nothing started under another pass"
    assert collectors.STOOD_DOWN in (root / collectors.ARTIFACT).read_text(encoding="utf-8")
    assert (root / collectors.PASS_LOCK).is_file(), "the holder's lock is the holder's"


def test_act_starts_what_it_is_given_and_records_its_health(tmp_path, monkeypatch):
    root = devkit_home(tmp_path, monkeypatch, {"ibkr_trader": RUN})
    docker = FakeDocker([])
    report = collectors.Report()
    collectors.act([target(tmp_path)], root, docker, report, run=lambda *a, **k: None)
    assert ("up", "ibkr_trader", "app") in docker.calls
    assert (root / collectors.HEALTH).is_file()
    assert not (root / collectors.IN_PASS).exists()


def test_a_pass_releases_the_lock_it_acted_under(tmp_path, monkeypatch):
    root = devkit_home(tmp_path, monkeypatch, {"sports_betting": RUN})
    seen = []

    class Watching(FakeDocker):
        def up(self, checkout, service):
            seen.append((root / collectors.PASS_LOCK).is_file())
            return super().up(checkout, service)

    assert collectors.main(["run-here", "sports_betting"], docker=Watching([])) == 0
    assert seen == [True]
    assert not (root / collectors.PASS_LOCK).exists()


def test_a_pass_that_only_stops_containers_writes_no_marker(tmp_path):
    path = tmp_path / "pass.json"
    with collectors.acting(path, [target(tmp_path, mode=STOP)]):
        assert not path.exists()
    with collectors.acting(path, []):
        assert not path.exists()


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


def test_a_failing_verdict_names_the_jobs_the_project_says_are_unhealthy(tmp_path):
    """6b140f4e: every failing ibkr_trader job was one row, "health check failing", so its
    `social` job missing boto3 read as the fixed `reddit` failure recurring. The project's
    own `unhealthy:` line names which, and the row carries it into the group's key."""
    out = (
        "health artifact: logs/scheduler-health.json (written 2026-10-04T22:16:52+00:00)\n"
        "job     status   last success\nsocial  failing  never\n\nunhealthy: social, reddit\n"
    )
    record = collectors.check_health(
        target(tmp_path), ours(tmp_path), FakeDocker(health=(1, out)), collectors.Report()
    )
    assert record["unhealthy"] == ["reddit", "social"]
    level, detail = collectors.row(target(tmp_path), [ours(tmp_path)], {"ibkr_trader": record})
    assert level == collectors.WARN
    assert detail.startswith("health check failing: reddit, social -- exit 1: health artifact")
    assert collectors.unhealthy_jobs("  Unhealthy:  prices \n") == ["prices"]
    assert collectors.unhealthy_jobs("api-sports  idle  runs 7 fail 1\n") == []


def test_keep_running_records_nothing_for_a_collector_without_a_health_command(tmp_path):
    no_health = target(tmp_path, health=())
    docker = FakeDocker()
    assert collectors.keep_running(no_health, [ours(tmp_path)], docker, collectors.Report()) is None
    assert docker.calls == [("built", "c1")], "only the redeploy's question, never a health check"


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


def test_a_failed_pass_ends_on_its_cause_not_on_a_healthy_line():
    """f783d741: `log-wrap.py` filed `sports_betting: healthy`, the run's last line, as
    the cause of a pass some earlier line had failed. The first failure is repeated last
    as an `error:` line, which is what `log-wrap.cause_said` reads."""
    report = collectors.Report()
    report.fail("ibkr_trader: could not start `app` -- boom")
    report.fail("social-scraper: second")
    report.say("sports_betting: healthy")
    text = collectors.render(report.lines, report.failures, dt.datetime(2026, 10, 8), report.cause)
    assert text.splitlines()[-1] == "error: ibkr_trader: could not start `app` -- boom"
    wrap = load_script("scripts/log-wrap.py")
    assert wrap.cause_said(text) == "error: ibkr_trader: could not start `app` -- boom"
    clean = collectors.Report()
    clean.say("sports_betting: healthy")
    assert "error:" not in collectors.render(clean.lines, 0, dt.datetime(2026, 10, 8), clean.cause)


def test_parse_args_defaults_to_read_only_status():
    args = collectors.parse_args([])
    assert (args.mode, args.projects) == ("status", [])
    assert collectors.parse_args(["run-here", "a", "b"]).projects == ["a", "b"]


def test_split_pick_takes_the_pickers_one_argument_apart():
    assert collectors.split_pick(["run-here:ibkr_trader"]) == ["run-here", "ibkr_trader"]
    assert collectors.split_pick(["stop-here:"]) == ["stop-here"]
    assert collectors.split_pick(["run-here", "a"]) == ["run-here", "a"]
    assert collectors.split_pick([]) == []
    assert collectors.parse_args(["release:sports_betting"]).projects == ["sports_betting"]


def test_the_picker_row_that_runs_nothing_runs_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DEVKIT_DIR", str(tmp_path))
    docker = FakeDocker([])
    assert collectors.main([collectors.NOTHING], docker=docker) == 0
    assert docker.calls == [] and not (tmp_path / collectors.ARTIFACT).exists()
    assert "nothing picked" in capsys.readouterr().out


def test_reassign_refuses_a_typo_without_writing(tmp_path):
    report = collectors.Report()
    args = collectors.parse_args(["stop-here", "nope"])
    assert collectors.reassign(args, tmp_path, [config.Collector("a", "s")], report) is None
    assert report.failures == 1 and not (tmp_path / config.ASSIGNMENT).exists()


def test_status_names_an_engine_it_could_not_ask(tmp_path):
    chosen = [target(tmp_path)]
    report = collectors.Report()
    collectors.status([chosen[0].collector], chosen, FakeDocker(None), report)
    assert report.lines == ["ibkr_trader: assigned `run` -- `app` docker not answering"]


def test_status_with_nothing_declared_says_where_to_declare_it():
    report = collectors.Report()
    collectors.status([], [], FakeDocker(), report)
    assert config.SETTING in report.lines[0]


def test_status_reads_the_mode_off_the_targets_and_skips_the_unassigned(tmp_path):
    stopped = target(tmp_path, mode=STOP)
    other = config.Collector("sports_betting", "collector")
    report = collectors.Report()
    collectors.status([stopped.collector, other], [stopped], FakeDocker([]), report)
    assert report.lines == [
        "ibkr_trader: assigned `stop` -- `app` no container",
        "sports_betting: not assigned on this machine (hands off)",
    ]


def test_apply_verb_hands_back_only_what_it_assigned(tmp_path):
    declared = [config.Collector("a", "s"), config.Collector("b", "s")]
    report = collectors.Report()
    args = collectors.parse_args(["stop-here", "a"])
    assert collectors.apply_verb(args, tmp_path, declared, lambda argv: 0 / 0, report) == {
        "a": STOP
    }
    released = collectors.parse_args(["release", "a"])
    assert collectors.apply_verb(released, tmp_path, declared, lambda argv: 0 / 0, report) is None
    assert config.load_assignment(tmp_path / config.ASSIGNMENT) == {}


def test_apply_verb_on_a_typo_is_nothing_to_act_on_and_a_failure(tmp_path):
    report = collectors.Report()
    args = collectors.parse_args(["run-here", "nope"])
    assert collectors.apply_verb(args, tmp_path, [config.Collector("a", "s")], None, report) is None
    assert report.failures == 1


# --- a scheduled collector -------------------------------------------------------------

SCHEDULED = {
    "ibkr_trader": {"service": "app", "health": ["h"]},
    "social-scraper": {"command": ["uv", "run", "social-scraper", "scrape"], "minutes": 30},
}


class FakeSchtasks:
    """Nothing registered until `/Create`; records every call."""

    def __init__(self, registered=False):
        self.registered = registered
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        if argv[0] == "whoami":
            return subprocess.CompletedProcess(argv, 1, "", "")
        if argv[1] == "/Query":
            code = 0 if self.registered else 1
            body = "<Exec><Command>x</Command></Exec>" if code == 0 else ""
            return subprocess.CompletedProcess(argv, code, body, "")
        if argv[1] == "/Create":
            self.registered = True
        if argv[1] == "/Delete":
            self.registered = False
        return subprocess.CompletedProcess(argv, 0, "", "")

    def verbs(self):
        return [argv[1] for argv in self.calls if argv[0] == "schtasks"]


def scraper_home(tmp_path, monkeypatch, assignment=None):
    root = devkit_home(tmp_path, monkeypatch, assignment, SCHEDULED)
    (tmp_path / "social-scraper" / ".git").mkdir(parents=True)
    monkeypatch.setattr(collectors.collector_tasks, "interpreter", lambda: r"C:\py\pythonw.exe")
    return root


def test_run_here_registers_a_scheduled_collectors_task_and_asks_docker_nothing(
    tmp_path, monkeypatch
):
    scraper_home(tmp_path, monkeypatch)
    docker, schtasks = FakeDocker([]), FakeSchtasks()
    assert collectors.main(["run-here", "social-scraper"], docker=docker, run=schtasks) == 0
    assert "/Create" in schtasks.verbs()
    assert docker.calls == []


def test_stop_here_removes_a_scheduled_collectors_task(tmp_path, monkeypatch):
    scraper_home(tmp_path, monkeypatch, {"social-scraper": RUN})
    schtasks = FakeSchtasks(registered=True)
    assert collectors.main(["stop-here", "social-scraper"], run=schtasks) == 0
    assert "/Delete" in schtasks.verbs()


def test_release_removes_a_scheduled_collectors_task_too(tmp_path, monkeypatch):
    """Unlike a container, the task exists only because this job registered it; left
    behind, it would fire with nobody assigned."""
    root = scraper_home(tmp_path, monkeypatch, {"social-scraper": RUN})
    schtasks = FakeSchtasks(registered=True)
    assert collectors.main(["release", "social-scraper"], run=schtasks) == 0
    assert "/Delete" in schtasks.verbs()
    assert config.load_assignment(root / config.ASSIGNMENT) == {}


def test_the_pass_keeps_both_kinds(tmp_path, monkeypatch):
    scraper_home(tmp_path, monkeypatch, {"social-scraper": RUN, "ibkr_trader": RUN})
    docker, schtasks = FakeDocker([]), FakeSchtasks()
    assert collectors.main(["maintain"], docker=docker, run=schtasks) == 0
    assert "/Create" in schtasks.verbs()
    assert ("up", "ibkr_trader", "app") in docker.calls


def test_status_reports_a_scheduled_collector_without_registering_it(tmp_path, monkeypatch, capsys):
    scraper_home(tmp_path, monkeypatch, {"social-scraper": RUN})
    docker, schtasks = FakeDocker([]), FakeSchtasks()
    assert collectors.main([], docker=docker, run=schtasks) == 0
    assert "/Create" not in schtasks.verbs() and docker.calls == []
    assert "social-scraper: assigned `run` --" in capsys.readouterr().out


def test_fire_runs_the_command_and_exits_with_its_code(tmp_path, monkeypatch):
    root = scraper_home(tmp_path, monkeypatch, {"social-scraper": RUN})
    seen = []

    def spawner(argv, cwd, timeout, env=None):
        seen.append((list(argv), Path(cwd).name))
        return 4, "x: challenge page"

    assert collectors.main(["fire", "social-scraper"], spawner=spawner) == 4
    assert seen[0][1] == "social-scraper"
    log = (root / "logs" / "collector-social-scraper.log").read_text(encoding="utf-8")
    assert "exit 4" in log and "x: challenge page" in log
    assert not (root / collectors.ARTIFACT).exists(), "the pass's log is not the fire's"


def test_fire_on_a_machine_not_assigned_runs_nothing(tmp_path, monkeypatch):
    """A task the pass has not deleted yet must not become a second writer."""
    scraper_home(tmp_path, monkeypatch, {"social-scraper": STOP})

    def spawner(argv, cwd, timeout, env=None):
        raise AssertionError("ran on a machine set to stop it")

    assert collectors.main(["fire", "social-scraper"], spawner=spawner) == 0


def test_fire_names_an_undeclared_collector(tmp_path, monkeypatch):
    root = scraper_home(tmp_path, monkeypatch, {"social-scraper": RUN})
    assert collectors.main(["fire", "ibkr_trader"]) == 2
    log = (root / "logs" / "collector-ibkr_trader.log").read_text(encoding="utf-8")
    assert "declares no scheduled collector" in log


def test_fire_takes_exactly_one_name(tmp_path, monkeypatch):
    scraper_home(tmp_path, monkeypatch)
    assert collectors.main(["fire"]) == 2


class Running(FakeSchtasks):
    """A scheduler whose `/FO CSV` status says the task is mid-run."""

    def __call__(self, argv):
        argv = list(argv)
        if argv[:2] == ["schtasks", "/Query"] and "CSV" in argv:
            self.calls.append(argv)
            row = '"\\social-scraper","10/2/2026 3:00:00 PM","Running"\n'
            return subprocess.CompletedProcess(argv, 0, row, "")
        return super().__call__(argv)


def test_run_once_runs_by_hand_whatever_this_machine_is_set_to(tmp_path, monkeypatch, capsys):
    """Its first run comes before `run-here`, which is the case the task exists for."""
    scraper_home(tmp_path, monkeypatch)
    seen = []

    def streamer(argv, cwd):
        seen.append((list(argv)[1:], Path(cwd).name))
        return 0

    assert collectors.main(["run-once:social-scraper"], run=FakeSchtasks(), streamer=streamer) == 0
    assert seen == [(["run", "social-scraper", "scrape"], "social-scraper")]
    assert "social-scraper exited 0" in capsys.readouterr().out


def test_run_once_is_refused_while_the_scheduled_run_is_going(tmp_path, monkeypatch):
    scraper_home(tmp_path, monkeypatch, {"social-scraper": RUN})

    def streamer(argv, cwd):
        raise AssertionError("a second run on one browser profile")

    assert collectors.main(["run-once", "social-scraper"], run=Running(), streamer=streamer) == 2


def test_run_once_names_a_collector_that_is_not_scheduled(tmp_path, monkeypatch, capsys):
    scraper_home(tmp_path, monkeypatch)
    assert collectors.main(["run-once", "ibkr_trader"], run=FakeSchtasks()) == 2
    assert "declares no scheduled collector" in capsys.readouterr().out


def test_the_scheduled_tasks_the_tray_asks_about_are_the_ones_run_here(tmp_path, monkeypatch):
    scraper_home(tmp_path, monkeypatch, {"social-scraper": RUN, "ibkr_trader": RUN})
    assert collectors.scheduled_tasks() == {"social-scraper": "logs/collector-social-scraper.log"}


def test_a_scheduled_collector_this_machine_runs_is_not_a_container_row(tmp_path, monkeypatch):
    """Its row is the scheduler's (`tray_state.collector_task_states`); asking docker
    about it would report a container that was never meant to exist."""
    scraper_home(tmp_path, monkeypatch, {"social-scraper": RUN})
    docker = FakeDocker([])
    assert collectors.tray_rows(docker=docker) == []
    assert docker.calls == []


def test_a_scheduled_collector_set_to_stop_is_a_green_row(tmp_path, monkeypatch):
    scraper_home(tmp_path, monkeypatch, {"social-scraper": STOP})
    assert collectors.tray_rows(docker=FakeDocker([])) == [
        ("collector: social-scraper", collectors.OK, "off on this machine (by choice)")
    ]


# --- the scheduled half, called directly ------------------------------------------------

SCRAPER = config.Collector("social-scraper", command=("uv", "run"), minutes=30)
PYTHONW = r"C:\py\pythonw.exe"


def test_assigned_is_what_is_declared_and_what_this_machine_was_told(tmp_path, monkeypatch):
    root = devkit_home(tmp_path, monkeypatch, {"ibkr_trader": STOP})
    declared, chosen = collectors.assigned(root)
    assert sorted(c.project for c in declared) == ["ibkr_trader", "sports_betting"]
    assert [(t.collector.project, t.mode) for t in chosen] == [("ibkr_trader", STOP)]
    assert chosen[0].checkout == tmp_path / "ibkr_trader"


def test_assigned_reads_no_workspace_on_a_machine_assigned_nothing(tmp_path, monkeypatch):
    root = devkit_home(tmp_path, monkeypatch)
    monkeypatch.setattr(collectors.config, "declared", lambda base: 0 / 0)
    assert collectors.assigned(root) == ([], [])


def test_maintain_scheduled_registers_on_run_and_removes_on_stop(tmp_path):
    schtasks, report = FakeSchtasks(), collectors.Report()
    here = collectors.Target(SCRAPER, RUN, tmp_path / "social-scraper")
    collectors.maintain_scheduled([here], tmp_path, report, schtasks, python=PYTHONW)
    assert schtasks.verbs()[-1] == "/Create"
    elsewhere = collectors.Target(SCRAPER, STOP, tmp_path / "social-scraper")
    collectors.maintain_scheduled([elsewhere], tmp_path, report, schtasks, python=PYTHONW)
    assert schtasks.verbs()[-1] == "/Delete"
    assert report.failures == 0


def test_maintain_scheduled_with_nothing_to_keep_asks_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(collectors.collector_tasks, "interpreter", lambda: 0 / 0)
    collectors.maintain_scheduled([], tmp_path, collectors.Report(), lambda argv: 0 / 0)


def test_fire_logs_under_its_own_name_and_returns_the_commands_code(tmp_path, monkeypatch):
    root = scraper_home(tmp_path, monkeypatch, {"social-scraper": RUN})
    when = dt.datetime(2026, 10, 2, 15, 0)
    envs = []
    code = collectors.fire(
        "social-scraper",
        root,
        when,
        lambda argv, cwd, timeout, env=None: envs.append(env) or (3, "blocked"),
    )
    assert code == 3
    assert envs[-1] == collectors.collector_tasks.fired_env(when), "the pass's own moment"
    log = (root / "logs" / "collector-social-scraper.log").read_text(encoding="utf-8")
    assert log.startswith("# collector social-scraper 2026-10-02T15:00:00 -- exit 3")


def test_fire_records_when_its_run_began_before_the_command_runs(tmp_path, monkeypatch):
    """4933b284: a run still going when the next fire was skipped had written nothing,
    since the log is written at the end, so the pass judged it by the skipped fire's
    time and called a run begun before its fix merged a fix that did not hold."""
    root = scraper_home(tmp_path, monkeypatch, {"social-scraper": RUN})
    when = dt.datetime(2026, 10, 9, 16, 30)
    marker = root / collectors.collector_tasks.start_path("social-scraper")
    seen = []

    def spawner(argv, cwd, timeout, env=None):
        seen.append(collectors.collector_tasks.started_at(marker))
        return 0, "ok"

    assert collectors.fire("social-scraper", root, when, spawner) == 0
    assert seen == [when], "recorded before the command ran, as the scheduler's local time"


def test_a_fire_that_runs_nothing_records_no_start(tmp_path, monkeypatch):
    root = scraper_home(tmp_path, monkeypatch, {"social-scraper": STOP})
    collectors.fire("social-scraper", root, dt.datetime(2026, 10, 9, 16, 30), lambda *a, **k: 0 / 0)
    assert not (root / collectors.collector_tasks.start_path("social-scraper")).exists()


def test_run_once_streams_the_command_and_returns_its_code(tmp_path, monkeypatch, capsys):
    root = scraper_home(tmp_path, monkeypatch)
    assert collectors.run_once("social-scraper", root, FakeSchtasks(), lambda argv, cwd: 5) == 5
    assert "social-scraper exited 5" in capsys.readouterr().out


def test_single_takes_exactly_one_name_and_dispatches_on_the_verb(tmp_path, monkeypatch, capsys):
    root = scraper_home(tmp_path, monkeypatch)
    when = dt.datetime(2026, 10, 2, 15, 0)
    two = collectors.parse_args(["fire", "a", "b"])
    assert collectors.single(two, root, when, FakeSchtasks(), None, None) == 2
    assert "exactly one collector name" in capsys.readouterr().err
    once = collectors.parse_args(["run-once", "social-scraper"])
    assert collectors.single(once, root, when, FakeSchtasks(), None, lambda argv, cwd: 7) == 7


# --- the pass lock, as a move holds it -----------------------------------------------------


def test_a_lock_a_move_takes_names_the_move_and_its_collectors(tmp_path):
    path = tmp_path / "collectors.lock"
    extra = {collectors.HOLDER: "`collectors.py move` to b", collectors.PROJECTS: ["ibkr_trader"]}
    assert collectors.take_lock(path, until=200.0, clock=100.0, extra=extra)
    assert collectors.move_holding(path, 150.0) == {"ibkr_trader": "`collectors.py move` to b"}
    assert collectors.move_holding(path, 200.0) == {}, "a killed move's lock speaks for nothing"


@pytest.mark.parametrize(
    "body",
    [
        None,
        '{"started_at": 100, "until": 200}',  # a pass's: `IN_PASS` speaks for it
        '{"started_at": 100, "until": 200, "holder": "m", "projects": "ibkr_trader"}',
        '{"started_at": 100, "until": 200, "holder": 3, "projects": ["ibkr_trader"]}',
    ],
)
def test_only_a_live_moves_lock_holds_a_collector(tmp_path, body):
    path = tmp_path / "collectors.lock"
    if body is not None:
        path.write_text(body, encoding="utf-8")
    assert collectors.move_holding(path, 150.0) == {}


def test_one_pass_writes_what_it_is_given_into_the_lock(tmp_path, monkeypatch):
    at(monkeypatch, 100.0)
    path = tmp_path / "collectors.lock"
    with collectors.one_pass(path, 50.0, wait=False, extra={collectors.HOLDER: "x"}) as had:
        record = json.loads(path.read_text(encoding="utf-8"))
    assert had and record == {"holder": "x", "started_at": 100.0, "until": 150.0}


def test_a_maintain_pass_holds_the_lock_long_enough_to_restore_a_handoff(tmp_path, monkeypatch):
    """A lock past its `until` is broken, and a second pass would start a second restore."""
    ibkr = config.Collector("ibkr_trader", "app", handoff=("uv",))
    sports = config.Collector("sports_betting", "collector")
    scraper = config.Collector("social-scraper", command=("uv",), minutes=30)
    every = [ibkr, sports, scraper]
    base = collectors.PS_TIMEOUT
    assert collectors.pass_bound(every, {}, picks_up=False) == base + collectors.TARGET_BOUND
    assert collectors.pass_bound(every, {"sports_betting": RUN}, picks_up=True) == (
        base + 2 * collectors.TARGET_BOUND + collectors.PICKUP_BOUND + 120
    ), "a hands-off collector it may take over is a target as well"
    assert collectors.pass_bound(every, {"ibkr_trader": STOP}, picks_up=True) == (
        base + collectors.TARGET_BOUND + collectors.PICKUP_BOUND + 120
    )
    assert collectors.pass_bound(every, {"ibkr_trader": RUN}, picks_up=True) == (
        base + collectors.TARGET_BOUND
    ), "one it runs is never taken over"


def test_the_pass_lock_a_maintain_takes_covers_the_restore_it_may_run(tmp_path, monkeypatch):
    root = devkit_home(
        tmp_path,
        monkeypatch,
        {"ibkr_trader": STOP},
        {"ibkr_trader": {"service": "app", "handoff": ["uv"]}},
    )
    at(monkeypatch, 1_000.0)
    seen = []

    class Watching(collectors.handoff.Handoff):
        def run(self, command, cwd, timeout, echo=False):
            seen.append(json.loads((root / collectors.PASS_LOCK).read_text(encoding="utf-8")))
            return collectors.handoff.Said(0)

    collectors.main(["maintain"], docker=FakeDocker([]), handoffs=Watching())
    assert seen and seen[0][collectors.UNTIL] - 1_000.0 >= collectors.PICKUP_BOUND


# --- move: handing a collector to another machine ------------------------------------------

HANDOFF = ["uv", "run", "--extra", "archive", "python", "scripts/handoff.py"]
THIS, OTHER = "desktop-b9fc8vp", "desktop-ml5f3pj"
Said = collectors.handoff.Said
MARKER = json.dumps({"state": "pending", "from": OTHER, "to": THIS})


# A module loaded by path is opaque to mypy, so its class is a base only through `Any`.
HandoffBase: Any = collectors.handoff.Handoff


class FakeHandoff(HandoffBase):
    """The project's handoff command; logs into `log`, which a test shares with docker's
    calls when the order between the two is the property."""

    def __init__(self, log=None, export=None, pending=None, restore=None):
        self.log = [] if log is None else log
        self.answers = {
            verb: Said(0) if answer is None else answer
            for verb, answer in (("export", export), ("pending", pending), ("import", restore))
        }

    def run(self, command, cwd, timeout, echo=False):
        assert list(command[: len(HANDOFF)]) == HANDOFF
        verb, flags = command[len(HANDOFF)], tuple(command[len(HANDOFF) + 1 :])
        self.log.append(("handoff", verb, flags, Path(cwd).name, timeout, echo))
        answer = self.answers[verb]
        return answer(flags) if callable(answer) else answer


class Moving(FakeDocker):
    """A docker whose `stop` stops the container, so a later `ps` says so."""

    def stop(self, container):
        self.containers = [
            collectors.Container(c.id, c.workdir, c.service, "exited", "Exited (0)")
            if c.id == container.id
            else c
            for c in self.containers
        ]
        return super().stop(container)


def handoff_home(tmp_path, monkeypatch, assignment=None):
    monkeypatch.setenv("DEVKIT_HOST", THIS.upper())
    return devkit_home(
        tmp_path,
        monkeypatch,
        assignment,
        {
            "ibkr_trader": {"service": "app", "health": ["h"], "handoff": HANDOFF},
            "sports_betting": {"service": "collector"},
        },
    )


def project(tmp_path, cid, service, state="running", name="ibkr_trader"):
    return box(service, state, str(tmp_path / name), cid)


def stack(tmp_path):
    return [
        project(tmp_path, "c1", "app"),
        project(tmp_path, "c2", "db"),
        project(tmp_path, "c3", "collector", name="sports_betting"),
    ]


def artifact(root):
    return (root / collectors.ARTIFACT).read_text(encoding="utf-8")


def test_move_stops_exports_then_assigns_stop_and_stops_the_rest_in_that_order(
    tmp_path, monkeypatch
):
    root = handoff_home(tmp_path, monkeypatch, {"ibkr_trader": RUN, "sports_betting": RUN})
    at(monkeypatch, 5_000.0)
    assignments = []

    class Watching(Moving):
        def stop(self, container):
            assignments.append(config.load_assignment(root / config.ASSIGNMENT)["ibkr_trader"])
            return super().stop(container)

    docker = Watching(stack(tmp_path))
    handoffs = FakeHandoff(docker.calls)
    assert (
        collectors.main(["move", "ibkr_trader", "--to", OTHER.upper()], docker, handoffs=handoffs)
        == 0
    )
    assert docker.calls == [
        ("ps",),
        ("stop", "c1"),
        (
            "handoff",
            "export",
            ("--from", THIS, "--to", OTHER),
            "ibkr_trader",
            collectors.handoff.EXPORT_TIMEOUT,
            True,
        ),
        ("ps",),
        ("stop", "c2"),
    ], "the db goes only after the export, and sports_betting is never touched"
    assert assignments == [RUN, STOP]
    assert config.load_assignment(root / config.ASSIGNMENT) == {
        "ibkr_trader": STOP,
        "sports_betting": RUN,
    }
    sent = collectors.handoff.load_outgoing(root / collectors.handoff.OUTGOING)
    assert sent == {"ibkr_trader": {"to": OTHER, "at": 5_000.0}}
    assert f"handed off to {OTHER}" in artifact(root)
    assert not (root / collectors.PASS_LOCK).exists()


def test_move_holds_the_pass_lock_for_the_whole_export(tmp_path, monkeypatch):
    root = handoff_home(tmp_path, monkeypatch, {"ibkr_trader": RUN})
    at(monkeypatch, 5_000.0)
    during = []

    def export(flags):
        lock = root / collectors.PASS_LOCK
        during.append(collectors.move_holding(lock, 5_000.0))
        during.append(json.loads(lock.read_text(encoding="utf-8"))[collectors.UNTIL])
        pass_docker = FakeDocker([project(tmp_path, "c1", "app", "exited")])
        collectors.main(["maintain"], docker=pass_docker)
        during.append(pass_docker.calls)
        return Said(0)

    docker = Moving(stack(tmp_path))
    collectors.main(
        ["move", "ibkr_trader", "--to", OTHER], docker, handoffs=FakeHandoff(export=export)
    )
    assert during[0] == {"ibkr_trader": f"`collectors.py move` to {OTHER}"}
    assert during[1] == 5_000.0 + collectors.MOVE_BOUND
    assert during[2] == [], "the scheduled pass stands down rather than restart it mid-dump"
    assert collectors.STOOD_DOWN not in artifact(root), "the move's own artifact is last"


def test_move_waits_for_a_pass_acting_now(tmp_path, monkeypatch):
    root = handoff_home(tmp_path, monkeypatch, {"ibkr_trader": RUN})
    at(monkeypatch, 1_000.0)
    lock = root / collectors.PASS_LOCK
    assert collectors.take_lock(lock, until=1_000.0 + 60, clock=1_000.0)
    waited = []

    def sleep(seconds):
        waited.append(seconds)
        lock.unlink()

    monkeypatch.setattr(collectors, "one_pass", functools.partial(collectors.one_pass, sleep=sleep))
    docker = Moving(stack(tmp_path))
    assert (
        collectors.main(["move", "ibkr_trader", "--to", OTHER], docker, handoffs=FakeHandoff()) == 0
    )
    assert waited == [collectors.LOCK_POLL] and ("stop", "c1") in docker.calls


def test_a_failed_export_starts_the_service_again_and_leaves_the_assignment(tmp_path, monkeypatch):
    root = handoff_home(tmp_path, monkeypatch, {"ibkr_trader": RUN})
    docker = Moving(stack(tmp_path))
    handoffs = FakeHandoff(docker.calls, export=Said(1, "", "upload failed: 403"))
    assert collectors.main(["move", "ibkr_trader", "--to", OTHER], docker, handoffs=handoffs) == 2
    assert [c[0] for c in docker.calls] == ["ps", "stop", "handoff", "up"]
    assert docker.calls[-1] == ("up", "ibkr_trader", "app")
    assert config.load_assignment(root / config.ASSIGNMENT) == {"ibkr_trader": RUN}
    assert collectors.handoff.load_outgoing(root / collectors.handoff.OUTGOING) == {}
    text = artifact(root)
    assert "export failed (exit 1: upload failed: 403)" in text
    assert str(Path("logs/handoff.json")) in text and "`app` is started again" in text


def test_a_failed_export_whose_restart_fails_says_both(tmp_path, monkeypatch):
    root = handoff_home(tmp_path, monkeypatch, {"ibkr_trader": RUN})
    docker = Moving(stack(tmp_path), up_ok=False)
    collectors.main(
        ["move", "ibkr_trader", "--to", OTHER], docker, handoffs=FakeHandoff(export=Said(124))
    )
    assert "could NOT be started again (Error response from daemon: no space)" in artifact(root)


def test_move_exports_nothing_when_the_service_cannot_be_stopped(tmp_path, monkeypatch):
    root = handoff_home(tmp_path, monkeypatch, {"ibkr_trader": RUN})
    handoffs = FakeHandoff()
    assert (
        collectors.main(["move", "ibkr_trader", "--to", OTHER], FakeDocker(None), handoffs=handoffs)
        == 2
    )
    assert handoffs.log == []
    assert "docker is not answering" in artifact(root)


def test_move_of_a_collector_already_stopped_exports_without_stopping_it(tmp_path, monkeypatch):
    handoff_home(tmp_path, monkeypatch, {"ibkr_trader": RUN})
    docker = Moving([project(tmp_path, "c1", "app", "exited"), project(tmp_path, "c2", "db")])
    handoffs = FakeHandoff(docker.calls)
    assert collectors.main(["move", "ibkr_trader", "--to", OTHER], docker, handoffs=handoffs) == 0
    assert [c[:2] for c in docker.calls] == [
        ("ps",),
        ("handoff", "export"),
        ("ps",),
        ("stop", "c2"),
    ]


def test_a_move_whose_rest_cannot_be_stopped_is_handed_off_and_still_fails(tmp_path, monkeypatch):
    root = handoff_home(tmp_path, monkeypatch, {"ibkr_trader": RUN})

    class Silenced(Moving):
        def ps(self):
            asked = sum(call == ("ps",) for call in self.calls)
            answer = super().ps()
            return answer if asked == 0 else None

    assert (
        collectors.main(
            ["move", "ibkr_trader", "--to", OTHER],
            Silenced(stack(tmp_path)),
            handoffs=FakeHandoff(),
        )
        == 2
    )
    assert config.load_assignment(root / config.ASSIGNMENT) == {"ibkr_trader": STOP}
    assert "handed off, but docker did not answer" in artifact(root)


@pytest.mark.parametrize(
    ("assignment", "argv", "words"),
    [
        ({"ibkr_trader": STOP}, ["ibkr_trader", "--to", OTHER], "not set to run it (`stop`)"),
        ({}, ["ibkr_trader", "--to", OTHER], "not set to run it (hands off)"),
        (
            {"sports_betting": RUN},
            ["sports_betting", "--to", OTHER],
            "`collectors.py stop-here sports_betting` here",
        ),
        ({"ibkr_trader": RUN}, ["ibkr_trader", "--to", THIS.upper()], "names this machine"),
        (
            {"ibkr_trader": RUN},
            ["ibkr_trader", "--to", f"{THIS}.tail1.ts.net"],
            "names this machine",
        ),
        ({"ibkr_trader": RUN}, ["ibkr_trader"], "needs `--to HOST`"),
        ({"ibkr_trader": RUN}, ["--to", OTHER], "by name"),
        ({"ibkr_trader": RUN}, ["ibkr", "--to", OTHER], "not declared"),
    ],
)
def test_a_move_that_cannot_happen_is_refused_and_touches_nothing(
    tmp_path, monkeypatch, assignment, argv, words
):
    root = handoff_home(tmp_path, monkeypatch, assignment)
    docker, handoffs = FakeDocker(stack(tmp_path)), FakeHandoff()
    assert collectors.main(["move", *argv], docker, handoffs=handoffs) == 2
    assert docker.calls == [] and handoffs.log == []
    assert config.load_assignment(root / config.ASSIGNMENT) == assignment
    assert words in artifact(root)


def test_a_collector_without_a_handoff_is_told_how_to_move_by_hand(tmp_path, monkeypatch):
    root = handoff_home(tmp_path, monkeypatch, {"sports_betting": RUN})
    collectors.main(
        ["move", "sports_betting", "--to", OTHER], FakeDocker([]), handoffs=FakeHandoff()
    )
    assert "then `collectors.py run-here sports_betting` there" in artifact(root)


def test_a_move_without_a_checkout_is_refused(tmp_path, monkeypatch):
    root = handoff_home(tmp_path, monkeypatch, {"ibkr_trader": RUN})
    (tmp_path / "ibkr_trader" / ".git").rmdir()
    assert (
        collectors.main(
            ["move", "ibkr_trader", "--to", OTHER], FakeDocker([]), handoffs=FakeHandoff()
        )
        == 2
    )
    assert "no checkout at" in artifact(root)


def test_a_move_that_never_gets_the_lock_moves_nothing(tmp_path, monkeypatch):
    here = here_at(tmp_path, monkeypatch, {"ibkr_trader": RUN}, FakeDocker(stack(tmp_path)))

    @contextlib.contextmanager
    def never(*_args, **_kwargs):
        yield False

    monkeypatch.setattr(collectors, "one_pass", never)
    [ibkr] = [c for c in declared_here(here) if c.project == "ibkr_trader"]
    collectors.move_one(ibkr, OTHER, here)
    assert here.docker.calls == [] and here.handoffs.log == []
    assert here.report.cause == f"ibkr_trader: not moved -- {collectors.STOOD_DOWN}"


def test_to_belongs_to_move_alone():
    with pytest.raises(SystemExit):
        collectors.parse_args(["run-here", "ibkr_trader", "--to", OTHER])
    assert collectors.parse_args(["move", "ibkr_trader", "--to", OTHER]).to == OTHER


# --- the pass: taking over a handoff, and following one sent ----------------------------


@pytest.mark.parametrize("assignment", [{"ibkr_trader": STOP}, {}])
def test_a_pass_takes_over_a_handoff_addressed_here_and_starts_it(
    tmp_path, monkeypatch, assignment
):
    """Hands off as well as `stop`: a marker addressed to this machine by name is as
    explicit as `run-here` typed here."""
    root = handoff_home(tmp_path, monkeypatch, assignment)
    at(monkeypatch, 1_000.0)
    docker = FakeDocker([project(tmp_path, "c2", "db")])
    seen = []

    def restore(flags):
        lock = json.loads((root / collectors.PASS_LOCK).read_text(encoding="utf-8"))
        seen.append(lock[collectors.UNTIL] - 1_000.0 >= collectors.PICKUP_BOUND)
        return Said(0)

    handoffs = FakeHandoff(docker.calls, pending=Said(0, MARKER + "\n"), restore=restore)
    assert collectors.main(["maintain"], docker=docker, handoffs=handoffs) == 0
    steps = [c[:2] for c in docker.calls]
    assert steps[:3] == [("handoff", "pending"), ("ps",), ("handoff", "import")]
    assert docker.calls[0][2] == ("--host", THIS) and docker.calls[2][2] == ("--host", THIS)
    assert docker.calls[2][4] == collectors.handoff.IMPORT_TIMEOUT
    assert ("up", "ibkr_trader", "app") in docker.calls, "started this pass, as run-here would"
    assert seen == [True], "under a lock no takeover can break mid-restore"
    assert config.load_assignment(root / config.ASSIGNMENT) == {"ibkr_trader": RUN}
    health = collectors.load_health(root / collectors.HEALTH)
    assert collectors.STARTED_AT in health["ibkr_trader"], "settles like any start"
    assert f"taken over from {OTHER}" in artifact(root)


def test_the_import_runs_only_once_the_service_is_stopped_here(tmp_path, monkeypatch):
    handoff_home(tmp_path, monkeypatch, {"ibkr_trader": STOP})
    docker = Moving([project(tmp_path, "c1", "app")])
    handoffs = FakeHandoff(docker.calls, pending=Said(0, MARKER))
    collectors.main(["maintain"], docker=docker, handoffs=handoffs)
    assert [c[:2] for c in docker.calls][:4] == [
        ("handoff", "pending"),
        ("ps",),
        ("stop", "c1"),
        ("handoff", "import"),
    ]


def test_a_handoff_waiting_while_docker_is_silent_is_left_for_the_next_pass(tmp_path, monkeypatch):
    root = handoff_home(tmp_path, monkeypatch, {"ibkr_trader": STOP})
    handoffs = FakeHandoff(pending=Said(0, MARKER))
    collectors.main(["maintain"], docker=FakeDocker(None), handoffs=handoffs)
    assert [c[1] for c in handoffs.log] == ["pending"]
    assert config.load_assignment(root / config.ASSIGNMENT) == {"ibkr_trader": STOP}
    assert "the next pass tries again" in artifact(root)


def test_nothing_addressed_here_imports_nothing_and_assigns_nothing(tmp_path, monkeypatch):
    root = handoff_home(tmp_path, monkeypatch, {"ibkr_trader": STOP})
    docker = FakeDocker([])
    handoffs = FakeHandoff(pending=Said(0, "", "Installed 40 packages"))
    assert collectors.main(["maintain"], docker=docker, handoffs=handoffs) == 0
    assert [c[1] for c in handoffs.log] == ["pending"]
    assert config.load_assignment(root / config.ASSIGNMENT) == {"ibkr_trader": STOP}
    assert ("up", "ibkr_trader", "app") not in docker.calls


@pytest.mark.parametrize("said", [Said(2, "", "R2 unreachable"), Said(124, "", "timed out")])
def test_a_store_that_cannot_be_read_is_silence_not_a_failure(tmp_path, monkeypatch, said):
    root = handoff_home(tmp_path, monkeypatch, {"ibkr_trader": STOP})
    assert (
        collectors.main(["maintain"], docker=FakeDocker([]), handoffs=FakeHandoff(pending=said))
        == 0
    )
    assert "handoff" not in artifact(root)


def test_a_broken_pending_is_said_and_imports_nothing(tmp_path, monkeypatch):
    root = handoff_home(tmp_path, monkeypatch, {"ibkr_trader": STOP})
    handoffs = FakeHandoff(pending=Said(1, "", "ModuleNotFoundError: boto3"))
    assert collectors.main(["maintain"], docker=FakeDocker([]), handoffs=handoffs) == 0
    assert [c[1] for c in handoffs.log] == ["pending"]
    assert "could not ask whether a handoff waits (exit 1: ModuleNotFoundError: boto3)" in artifact(
        root
    )


def test_a_failed_import_fails_the_pass_and_assigns_nothing(tmp_path, monkeypatch):
    root = handoff_home(tmp_path, monkeypatch, {"ibkr_trader": STOP})
    docker = FakeDocker([])
    handoffs = FakeHandoff(pending=Said(0, MARKER), restore=Said(1, "", "sha256 mismatch\nmore"))
    assert collectors.main(["maintain"], docker=docker, handoffs=handoffs) == 2
    assert config.load_assignment(root / config.ASSIGNMENT) == {"ibkr_trader": STOP}
    assert ("up", "ibkr_trader", "app") not in docker.calls
    text = artifact(root)
    assert "its import failed (exit 1: sha256 mismatch)" in text and "    more" in text
    assert text.rstrip().splitlines()[-1].startswith("error: ibkr_trader: handed to this machine")


def test_a_machine_that_runs_it_is_never_asked_about_a_handoff(tmp_path, monkeypatch):
    handoff_home(tmp_path, monkeypatch, {"ibkr_trader": RUN})
    handoffs = FakeHandoff(pending=Said(0, MARKER))
    collectors.main(
        ["maintain"], docker=FakeDocker([project(tmp_path, "c1", "app")]), handoffs=handoffs
    )
    assert handoffs.log == []


def test_a_machine_without_the_checkout_asks_nothing(tmp_path, monkeypatch):
    handoff_home(tmp_path, monkeypatch)
    (tmp_path / "ibkr_trader" / ".git").rmdir()
    handoffs = FakeHandoff(pending=Said(0, MARKER))
    collectors.main(["maintain"], docker=FakeDocker([]), handoffs=handoffs)
    assert handoffs.log == []


def test_an_import_already_running_is_not_started_again(tmp_path, monkeypatch):
    """The scheduler runs one pass at a time, but a pass killed mid-restore, or one run by
    hand, would otherwise start a second restore into the same database."""
    root = handoff_home(tmp_path, monkeypatch, {"ibkr_trader": STOP})
    at(monkeypatch, 1_000.0)
    handoffs = FakeHandoff(pending=Said(0, MARKER))
    # A pass mid-restore, holding the lock it took for one.
    bound = collectors.pass_bound(config.declared(root)[0], {"ibkr_trader": STOP}, True)
    assert collectors.take_lock(root / collectors.PASS_LOCK, 1_000.0 + bound, 1_000.0)
    at(monkeypatch, 1_000.0 + collectors.PICKUP_BOUND)
    assert collectors.main(["maintain"], docker=FakeDocker([]), handoffs=handoffs) == 0
    assert handoffs.log == []
    assert collectors.STOOD_DOWN in artifact(root)


def test_a_taken_over_collector_forgets_a_handoff_it_once_sent(tmp_path, monkeypatch):
    root = handoff_home(tmp_path, monkeypatch, {"ibkr_trader": STOP})
    collectors.handoff.record_outgoing(
        root / collectors.handoff.OUTGOING, "ibkr_trader", OTHER, 1.0
    )
    handoffs = FakeHandoff(pending=lambda flags: Said(0, MARKER) if THIS in flags else Said(0))
    collectors.main(["maintain"], docker=FakeDocker([]), handoffs=handoffs)
    assert collectors.handoff.load_outgoing(root / collectors.handoff.OUTGOING) == {}


def sent_home(tmp_path, monkeypatch, sent_at, assignment=None):
    root = handoff_home(tmp_path, monkeypatch, assignment or {"ibkr_trader": STOP})
    collectors.handoff.record_outgoing(
        root / collectors.handoff.OUTGOING, "ibkr_trader", OTHER, sent_at
    )
    return root


def test_a_handoff_taken_over_is_forgotten(tmp_path, monkeypatch):
    root = sent_home(tmp_path, monkeypatch, 1_000.0)
    handoffs = FakeHandoff(pending=lambda flags: Said(0))
    assert collectors.main(["maintain"], docker=FakeDocker([]), handoffs=handoffs) == 0
    assert ("handoff", "pending", ("--host", OTHER), "ibkr_trader", 120, False) in handoffs.log
    assert collectors.handoff.load_outgoing(root / collectors.handoff.OUTGOING) == {}
    assert f"{OTHER} has taken it over" in artifact(root)


def waiting_there(flags):
    """A marker addressed to the other machine, and none to this one."""
    return Said(0, MARKER) if OTHER in flags else Said(0)


def test_a_handoff_waiting_inside_its_grace_is_said(tmp_path, monkeypatch):
    root = sent_home(tmp_path, monkeypatch, 1_000.0)
    at(monkeypatch, 1_000.0 + collectors.HANDOFF_GRACE - 1)
    assert (
        collectors.main(
            ["maintain"], docker=FakeDocker([]), handoffs=FakeHandoff(pending=waiting_there)
        )
        == 0
    )
    assert "waiting for its next pass" in artifact(root)
    assert "ibkr_trader" in collectors.handoff.load_outgoing(root / collectors.handoff.OUTGOING)


def test_a_handoff_nobody_took_over_fails_the_pass_once_overdue(tmp_path, monkeypatch):
    """Until it is taken over nothing runs the collector, and no row on either machine
    says so: here it is `stop` by choice, there not yet `run`."""
    root = sent_home(tmp_path, monkeypatch, 1_000.0)
    at(monkeypatch, 1_000.0 + collectors.HANDOFF_GRACE)
    assert (
        collectors.main(
            ["maintain"], docker=FakeDocker([]), handoffs=FakeHandoff(pending=waiting_there)
        )
        == 2
    )
    last = artifact(root).rstrip().splitlines()[-1]
    assert last == (
        f"error: ibkr_trader: handed off to {OTHER}, which has not taken it over within "
        f"{collectors.HANDOFF_GRACE // 60} minutes -- nothing runs it until it does. Is "
        f"{OTHER} on, with devkit-collectors running?"
    ), "no count in the cause, so recurrences are one ledger group"


def test_a_handoff_whose_store_cannot_be_read_is_said_and_kept(tmp_path, monkeypatch):
    root = sent_home(tmp_path, monkeypatch, 0.0)
    assert (
        collectors.main(["maintain"], docker=FakeDocker([]), handoffs=FakeHandoff(pending=Said(2)))
        == 0
    )
    assert f"could not ask whether {OTHER} has taken it over (exit 2)" in artifact(root)
    assert "ibkr_trader" in collectors.handoff.load_outgoing(root / collectors.handoff.OUTGOING)


def test_a_handoff_this_machine_runs_again_is_forgotten_unasked(tmp_path, monkeypatch):
    root = sent_home(tmp_path, monkeypatch, 0.0, {"ibkr_trader": RUN})
    handoffs = FakeHandoff()
    collectors.main(
        ["maintain"], docker=FakeDocker([project(tmp_path, "c1", "app")]), handoffs=handoffs
    )
    assert handoffs.log == []
    assert collectors.handoff.load_outgoing(root / collectors.handoff.OUTGOING) == {}


def test_run_here_over_a_handoff_sent_warns_of_two_writers(tmp_path, monkeypatch):
    root = sent_home(tmp_path, monkeypatch, 0.0)
    collectors.main(["run-here", "ibkr_trader"], docker=FakeDocker([]), handoffs=FakeHandoff())
    assert f"warning: it was handed off to {OTHER}" in artifact(root)


# --- status and the tray ---------------------------------------------------------------


def test_status_says_a_handoff_waiting_here_and_one_sent_from_here(tmp_path, monkeypatch, capsys):
    root = sent_home(tmp_path, monkeypatch, 0.0)
    handoffs = FakeHandoff(pending=lambda flags: Said(0, MARKER))
    assert collectors.main([], docker=FakeDocker([]), handoffs=handoffs) == 0
    out = capsys.readouterr().out
    assert f"ibkr_trader: handed off to {OTHER} -- not taken over yet" in out
    assert f"ibkr_trader: handed to this machine by {OTHER} -- the next pass takes it over" in out
    assert config.load_assignment(root / config.ASSIGNMENT) == {"ibkr_trader": STOP}
    assert [c[1] for c in handoffs.log] == ["pending", "pending"], "and nothing imported"


@pytest.mark.parametrize(
    ("said", "words"),
    [
        (Said(0), f"handed off to {OTHER} -- taken over"),
        (Said(2), f"handed off to {OTHER} -- the store could not say"),
    ],
)
def test_status_says_whether_a_handoff_sent_was_taken_over(
    tmp_path, monkeypatch, capsys, said, words
):
    sent_home(tmp_path, monkeypatch, 0.0, {"ibkr_trader": RUN})
    collectors.main([], docker=FakeDocker([]), handoffs=FakeHandoff(pending=said))
    assert words in capsys.readouterr().out


def test_status_says_when_it_could_not_ask_about_a_handoff_here(tmp_path, monkeypatch, capsys):
    handoff_home(tmp_path, monkeypatch, {"ibkr_trader": STOP})
    collectors.main([], docker=FakeDocker([]), handoffs=FakeHandoff(pending=Said(2, "", "R2 down")))
    assert "could not ask whether a handoff waits here (exit 2: R2 down)" in capsys.readouterr().out


def test_status_asks_nothing_of_a_collector_without_a_handoff(tmp_path, monkeypatch):
    devkit_home(tmp_path, monkeypatch, {"ibkr_trader": STOP})
    handoffs = FakeHandoff()
    collectors.main([], docker=FakeDocker([]), handoffs=handoffs)
    assert handoffs.log == []


def test_the_tray_shows_a_collector_handed_off_from_here(tmp_path, monkeypatch):
    sent_home(tmp_path, monkeypatch, 0.0)
    assert collectors.tray_rows(docker=FakeDocker([])) == [
        (
            "collector: ibkr_trader",
            collectors.OK,
            f"handed off to {OTHER} -- it takes it over on its next collectors pass",
        )
    ]


def test_the_tray_keeps_a_collector_being_moved_green(tmp_path, monkeypatch):
    """The fix pass files every row that is not OK, and a move stops the service for an
    hour on purpose."""
    root = handoff_home(tmp_path, monkeypatch, {"ibkr_trader": RUN, "sports_betting": RUN})
    at(monkeypatch, 1_000.0)
    docker = FakeDocker(
        [
            project(tmp_path, "c1", "app", "exited"),
            project(tmp_path, "c3", "collector", "exited", name="sports_betting"),
        ]
    )
    holder = {
        collectors.HOLDER: f"`collectors.py move` to {OTHER}",
        collectors.PROJECTS: ["ibkr_trader"],
    }
    with collectors.one_pass(root / collectors.PASS_LOCK, 600, wait=False, extra=holder):
        rows = dict(
            (name, (level, detail)) for name, level, detail in collectors.tray_rows(docker=docker)
        )
    level, detail = rows["collector: ibkr_trader"]
    assert level == collectors.OK and detail.startswith(
        f"`collectors.py move` to {OTHER} is acting on it"
    )
    assert rows["collector: sports_betting"][0] == collectors.FAIL, "only what the move holds"
    assert collectors.tray_rows(docker=docker)[0][1] == collectors.FAIL


def test_holding_reads_only_local_files(tmp_path):
    run, stop = target(tmp_path), target(tmp_path, mode=STOP)
    assert collectors.holding(run, {}, {}) == ""
    assert collectors.holding(run, {"ibkr_trader": "m"}, {}) == "m"
    assert collectors.holding(stop, {"ibkr_trader": "m"}, {}) == ""
    sent = {"ibkr_trader": {"to": OTHER, "at": 0.0}}
    assert collectors.holding(stop, {}, sent).startswith(f"handed off to {OTHER}")


def test_down_row_names_whoever_holds_the_collector():
    assert collectors.down_row(None, False, "x") == (
        collectors.OK,
        "x is acting on it (no container)",
    )


# --- the handoff functions, one by one -------------------------------------------------


def here_at(tmp_path, monkeypatch, assignment=None, docker=None, handoffs=None):
    root = handoff_home(tmp_path, monkeypatch, assignment)
    return collectors.Here(
        root,
        tmp_path,
        THIS,
        docker or FakeDocker([]),
        handoffs or FakeHandoff(),
        collectors.Report(),
    )


def declared_here(here):
    return config.declared(here.base)[0]


@pytest.mark.parametrize(
    ("dest", "projects", "unknown", "words"),
    [
        ("", ["p"], [], "needs `--to HOST`"),
        (THIS, ["p"], [], "names this machine"),
        (OTHER, [], [], "by name"),
        (OTHER, ["p", "x"], ["x"], "not declared"),
        (OTHER, ["p"], [], ""),
    ],
)
def test_move_refused_says_why_nothing_moves(dest, projects, unknown, words):
    why = collectors.move_refused(dest, projects, unknown, THIS)
    assert (words in why) if words else why == ""


def test_move_hands_off_what_it_can_and_refuses_the_rest(tmp_path, monkeypatch):
    here = here_at(tmp_path, monkeypatch, {"ibkr_trader": RUN, "sports_betting": RUN})
    here.docker = Moving(stack(tmp_path))
    collectors.move(
        ["sports_betting", "ibkr_trader"],
        OTHER,
        declared_here(here),
        {"ibkr_trader": RUN, "sports_betting": RUN},
        here,
    )
    assert here.report.failures == 1 and "declares no `handoff`" in here.report.cause
    assert config.load_assignment(here.assignment)["ibkr_trader"] == STOP


def test_move_one_restarts_the_service_after_a_failed_export(tmp_path, monkeypatch):
    here = here_at(tmp_path, monkeypatch, {"ibkr_trader": RUN}, Moving(stack(tmp_path)))
    here.handoffs = FakeHandoff(here.docker.calls, export=Said(1))
    [ibkr] = [c for c in declared_here(here) if c.project == "ibkr_trader"]
    collectors.move_one(ibkr, OTHER, here)
    assert here.docker.calls[-1] == ("up", "ibkr_trader", "app") and here.report.failures == 1


def test_stop_service_stops_only_a_running_collector_and_says_why_it_could_not(
    tmp_path, monkeypatch
):
    here = here_at(tmp_path, monkeypatch)
    [ibkr] = [c for c in declared_here(here) if c.project == "ibkr_trader"]
    here.docker = FakeDocker([project(tmp_path, "c1", "app", "exited")])
    assert collectors.stop_service(ibkr, here) == "" and here.docker.calls == [("ps",)]

    class Refusing(FakeDocker):
        def stop(self, container):
            return False, "Error: permission denied"

    here.docker = Refusing([project(tmp_path, "c1", "app")])
    assert collectors.stop_service(ibkr, here) == "`docker stop` said Error: permission denied"
    here.docker = FakeDocker(None)
    assert collectors.stop_service(ibkr, here) == "docker is not answering"


def test_handed_off_assigns_stop_records_the_host_and_stops_the_project(tmp_path, monkeypatch):
    here = here_at(tmp_path, monkeypatch, {"ibkr_trader": RUN}, Moving(stack(tmp_path)))
    at(monkeypatch, 7.0)
    [ibkr] = [c for c in declared_here(here) if c.project == "ibkr_trader"]
    collectors.handed_off(ibkr, OTHER, here)
    assert config.load_assignment(here.assignment) == {"ibkr_trader": STOP}
    assert collectors.handoff.load_outgoing(here.outgoing) == {
        "ibkr_trader": {"to": OTHER, "at": 7.0}
    }
    assert here.docker.calls == [("ps",), ("stop", "c1"), ("stop", "c2")]


def test_pick_up_returns_the_assignment_to_act_on(tmp_path, monkeypatch):
    here = here_at(tmp_path, monkeypatch, handoffs=FakeHandoff(pending=Said(0, MARKER)))
    assert collectors.pick_up(declared_here(here), {}, here) == {"ibkr_trader": RUN}
    here.handoffs = FakeHandoff(pending=Said(0))
    assert collectors.pick_up(declared_here(here), {"ibkr_trader": STOP}, here) == {
        "ibkr_trader": STOP
    }


def test_take_over_with_nothing_waiting_does_nothing(tmp_path, monkeypatch):
    here = here_at(tmp_path, monkeypatch)
    [ibkr] = [c for c in declared_here(here) if c.project == "ibkr_trader"]
    assert collectors.take_over(ibkr, here) is False
    assert here.docker.calls == [] and here.report.lines == []


def test_follow_handoffs_forgets_one_no_longer_declared_with_a_handoff(tmp_path, monkeypatch):
    here = here_at(tmp_path, monkeypatch)
    collectors.handoff.record_outgoing(here.outgoing, "sports_betting", OTHER, 0.0)
    collectors.handoff.record_outgoing(here.outgoing, "retired", OTHER, 0.0)
    collectors.follow_handoffs(declared_here(here), {}, here)
    assert collectors.handoff.load_outgoing(here.outgoing) == {} and here.handoffs.log == []


def test_handoff_status_says_nothing_when_nothing_waits(tmp_path, monkeypatch):
    here = here_at(tmp_path, monkeypatch)
    collectors.handoff_status(declared_here(here), {}, here)
    assert here.report.lines == [] and [c[1] for c in here.handoffs.log] == ["pending"]


def test_run_pass_takes_over_inside_the_lock_or_stands_down_whole(tmp_path, monkeypatch):
    here = here_at(tmp_path, monkeypatch, {"ibkr_trader": STOP})
    here.handoffs = FakeHandoff(pending=Said(0, MARKER))
    at(monkeypatch, 1_000.0)
    maintain = collectors.parse_args(["maintain"])
    assert collectors.take_lock(here.base / collectors.PASS_LOCK, 2_000.0, 1_000.0)
    collectors.run_pass(maintain, declared_here(here), {"ibkr_trader": STOP}, here, FakeSchtasks())
    assert here.handoffs.log == [] and here.report.lines == [collectors.STOOD_DOWN]
    (here.base / collectors.PASS_LOCK).unlink()
    collectors.run_pass(maintain, declared_here(here), {"ibkr_trader": STOP}, here, FakeSchtasks())
    assert [c[1] for c in here.handoffs.log] == ["pending", "import"]
    assert ("up", "ibkr_trader", "app") in here.docker.calls
    assert not (here.base / collectors.PASS_LOCK).exists()


def test_run_pass_on_a_typo_takes_no_lock(tmp_path, monkeypatch):
    here = here_at(tmp_path, monkeypatch)
    verb = collectors.parse_args(["run-here", "ibkr"])
    collectors.run_pass(verb, declared_here(here), {}, here, FakeSchtasks())
    assert here.report.failures == 1 and here.docker.calls == []
    assert not (here.base / collectors.PASS_LOCK).exists()


def test_set_mode_keeps_the_rest_of_the_assignment(tmp_path):
    here = collectors.Here(
        tmp_path, tmp_path, THIS, FakeDocker([]), FakeHandoff(), collectors.Report()
    )
    config.save_assignment(here.assignment, {"a": RUN, "b": RUN})
    collectors.set_mode(here, "a", STOP)
    assert config.load_assignment(here.assignment) == {"a": STOP, "b": RUN}


def test_refusal_is_empty_for_a_collector_that_can_move(tmp_path):
    here = collectors.Here(
        tmp_path, tmp_path, THIS, FakeDocker([]), FakeHandoff(), collectors.Report()
    )
    (tmp_path / "ibkr_trader" / ".git").mkdir(parents=True)
    ibkr = config.Collector("ibkr_trader", "app", handoff=tuple(HANDOFF))
    assert collectors.refusal(ibkr, {"ibkr_trader": RUN}, here) == ""
