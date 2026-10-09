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
        self.update = False
        self.restarts = None
        self.holds = None
        self.restart_answer = (True, "")
        self.reset_answer = (True, "")
        self.calls: list[tuple] = []

    def desktop_running(self):
        self.calls.append(("desktop",))
        return self.desktop

    def updating(self):
        return self.update

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

    def deploy(self, checkout, service):
        self.calls.append(("deploy", Path(checkout).name, service))
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

    def head(self, checkout):
        self.calls.append("head")
        return self.head_answer

    def held(self, checkout):
        self.calls.append("held")
        return self.held_answer


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

    def __init__(self, heads: dict, held: dict | None = None):
        super().__init__()
        self.heads, self.helds = heads, held or {}

    def head(self, checkout):
        self.calls.append(f"head {Path(checkout).name}")
        return self.heads.get(Path(checkout).name)

    def held(self, checkout):
        self.calls.append(f"held {Path(checkout).name}")
        return self.helds.get(Path(checkout).name, "")


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
        (t.checkout, ("aaa", 1.0)),
        (t.checkout.parent / "data-lake", ("bbb", 2.0)),
    ]
    assert collectors.source_heads(target(tmp_path), git) == [(t.checkout, ("aaa", 1.0))]


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
