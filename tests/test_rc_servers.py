"""`rc-servers.py`: the pass, and the order it does things in.

Two properties this suite is here to pin, both of which are the whole point of the job:

- **A busy project defers the whole cycle**, not just its own server. Stopping the
  others to update a binary that this one is executing fails the update *and* costs the
  stopped sessions for nothing.
- **The update happens between the two loops.** Windows cannot replace a running image,
  so a cycle that updated before stopping -- or after restarting -- would be the no-op
  this job was written to fix. The test asserts the sequence, not just the calls.
"""

from __future__ import annotations

import datetime as _dt
import json

import pytest
from support import load_script

rc_config = load_script("scripts/rc_config.py")
rc_machine = load_script("scripts/rc_machine.py")
rc = load_script("scripts/rc-servers.py")


def plan_for(tmp_path, names=("devkit",), servers=None, config=None):
    return rc.Plan(
        names=list(names),
        root=tmp_path,
        state=rc_machine.State(servers=dict(servers or {})),
        claude="claude",
        config=config or rc_config.Config(projects=tuple(names)),
        logs=tmp_path / "logs",
        store=tmp_path / "projects",
    )


def workspace(tmp_path, setting=None, folders=("devkit", "carameli")):
    payload: dict = {"folders": [{"path": name} for name in folders], "settings": {}}
    if setting is not None:
        payload["settings"][rc_config.RC_SETTING] = setting
    path = tmp_path / "alex-projects.code-workspace"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class Outcome:
    """What `agent_clis.run_pass` hands back, as this module consumes it."""

    def __init__(self, failures=0, summary="1 current"):
        self.lines = ("claude 2.1.258",)
        self.failures = failures
        self.summary = summary


@pytest.fixture
def no_launch(monkeypatch):
    """Record what would have been started instead of starting it."""
    started = []

    def fake(project, name, claude, config, logs):
        started.append(name)
        return 100 + len(started), ""

    monkeypatch.setattr(rc_machine, "start_server", fake)
    return started


# --- Pass --------------------------------------------------------------------


def test_a_pass_counts_only_the_failures():
    report = rc.Pass()
    report.say("fine")
    report.fail("not fine")
    assert report.failures == 1
    assert report.lines == ["fine", "not fine"]


def test_the_plan_resolves_a_project_directory():
    assert (
        rc.Plan([], __import__("pathlib").Path("/ws"), None, "", None, None, None)
        .project("devkit")
        .name
        == "devkit"
    )


# --- ensure_up ---------------------------------------------------------------


def test_a_server_that_is_up_is_left_alone(monkeypatch, no_launch, tmp_path):
    monkeypatch.setattr(rc_machine, "pid_is_server", lambda pid: True)
    plan = plan_for(tmp_path, servers={"devkit": 42})
    report = rc.ensure_up(plan, rc.Pass())
    assert no_launch == []
    assert plan.state.servers == {"devkit": 42}
    assert report.failures == 0 and "up (pid 42)" in report.lines[0]


def test_a_server_that_has_died_is_started_again(monkeypatch, no_launch, tmp_path):
    monkeypatch.setattr(rc_machine, "pid_is_server", lambda pid: False)
    plan = plan_for(tmp_path, servers={"devkit": 42})
    rc.ensure_up(plan, rc.Pass())
    assert no_launch == ["devkit"]
    assert plan.state.servers["devkit"] == 101


def test_a_launch_that_fails_is_a_failure_and_forgets_the_pid(monkeypatch, tmp_path):
    monkeypatch.setattr(rc_machine, "pid_is_server", lambda pid: False)
    monkeypatch.setattr(rc_machine, "start_server", lambda *a, **k: (0, "no such executable"))
    plan = plan_for(tmp_path, servers={"devkit": 42})
    report = rc.ensure_up(plan, rc.Pass())
    assert report.failures == 1
    assert "devkit" not in plan.state.servers


# --- cycle -------------------------------------------------------------------


def test_one_busy_project_defers_the_whole_cycle(monkeypatch, no_launch, tmp_path):
    """Not just its own server -- see the module docstring."""
    monkeypatch.setattr(rc_machine, "is_idle", lambda cwd, store, minutes: cwd.name != "devkit")
    stopped = []
    monkeypatch.setattr(rc_machine, "stop_server", lambda pid: stopped.append(pid) or "")

    def updater(**kwargs):
        raise AssertionError("the update ran with a session in flight")

    plan = plan_for(tmp_path, names=("devkit", "carameli"), servers={"devkit": 42, "carameli": 43})
    report, ran = rc.cycle(plan, rc.Pass(), update=updater)
    assert ran is False
    assert stopped == [] and no_launch == []
    assert "deferred" in report.lines[0] and "devkit" in report.lines[0]


def test_an_idle_machine_stops_updates_then_starts_again(monkeypatch, tmp_path):
    """The order is the assertion. Reverse any two of these and the update does nothing,
    because Windows cannot replace a running image."""
    events = []
    monkeypatch.setattr(rc_machine, "is_idle", lambda cwd, store, minutes: True)
    monkeypatch.setattr(rc_machine, "pid_is_server", lambda pid: False)
    monkeypatch.setattr(rc_machine, "stop_server", lambda pid: events.append(f"stop:{pid}") or "")
    monkeypatch.setattr(
        rc_machine,
        "start_server",
        lambda project, name, claude, config, logs: (events.append(f"start:{name}"), (900, ""))[1],
    )

    def updater(**kwargs):
        events.append("update")
        return Outcome()

    plan = plan_for(tmp_path, servers={"devkit": 42})
    report, ran = rc.cycle(plan, rc.Pass(), update=updater)
    assert ran is True
    assert events == ["stop:42", "update", "start:devkit"]
    assert report.failures == 0
    assert plan.state.servers == {"devkit": 900}


def test_only_claude_is_updated(monkeypatch, no_launch, tmp_path):
    """Codex has nothing to do with a Remote Control server. Including it would make this
    job's verdict depend on a CLI it never stopped -- one Codex session open anywhere
    would redden a pass that did its own work correctly."""
    monkeypatch.setattr(rc_machine, "is_idle", lambda cwd, store, minutes: True)
    monkeypatch.setattr(rc_machine, "pid_is_server", lambda pid: False)
    monkeypatch.setattr(rc_machine, "stop_server", lambda pid: "")
    seen = {}

    def updater(**kwargs):
        seen.update(kwargs)
        return Outcome()

    rc.cycle(plan_for(tmp_path, servers={"devkit": 42}), rc.Pass(), update=updater)
    assert [agent.name for agent in seen["agents"]] == ["claude"]
    assert seen["yes"] is True


def test_the_updater_is_called_the_way_agent_clis_actually_spells_it():
    """`run_pass`, not `update`, and with no `run=`: `agent_clis` has its own `Runner`
    taking a timeout `rc_machine.run_command` does not, so handing it the local one
    type-checks and then fails at the first call. Both halves were wrong once."""
    assert callable(rc.agent_clis.run_pass)
    assert not hasattr(rc.agent_clis, "update")


def test_a_failed_update_reddens_the_pass_but_still_restarts(monkeypatch, no_launch, tmp_path):
    """A CLI that could not be updated is a bad night. Leaving the servers down over it
    would be a worse one, and past four hours the sessions are gone rather than paused."""
    monkeypatch.setattr(rc_machine, "is_idle", lambda cwd, store, minutes: True)
    monkeypatch.setattr(rc_machine, "pid_is_server", lambda pid: False)
    monkeypatch.setattr(rc_machine, "stop_server", lambda pid: "")
    report, ran = rc.cycle(
        plan_for(tmp_path, servers={"devkit": 42}),
        rc.Pass(),
        update=lambda **k: Outcome(failures=1, summary="1 failed"),
    )
    assert ran is True and report.failures == 1
    assert no_launch == ["devkit"]


def test_a_stop_that_fails_is_reported_and_the_cycle_goes_on(monkeypatch, no_launch, tmp_path):
    monkeypatch.setattr(rc_machine, "is_idle", lambda cwd, store, minutes: True)
    monkeypatch.setattr(rc_machine, "pid_is_server", lambda pid: False)
    monkeypatch.setattr(rc_machine, "stop_server", lambda pid: "Access is denied.")
    report, ran = rc.cycle(
        plan_for(tmp_path, servers={"devkit": 42}), rc.Pass(), update=lambda **k: Outcome()
    )
    assert ran is True and report.failures == 1
    assert any("could not stop" in line for line in report.lines)


# --- status and down ---------------------------------------------------------


def test_status_touches_nothing_and_names_both_facts(monkeypatch, tmp_path):
    monkeypatch.setattr(rc_machine, "pid_is_server", lambda pid: True)
    monkeypatch.setattr(rc_machine, "is_idle", lambda cwd, store, minutes: True)
    plan = plan_for(tmp_path, servers={"devkit": 42})
    report = rc.status(plan, rc.Pass())
    assert "up (pid 42), idle" in report.lines[0]
    assert plan.state.servers == {"devkit": 42}


def test_status_says_when_the_update_has_never_run(monkeypatch, tmp_path):
    monkeypatch.setattr(rc_machine, "pid_is_server", lambda pid: False)
    monkeypatch.setattr(rc_machine, "is_idle", lambda cwd, store, minutes: False)
    report = rc.status(plan_for(tmp_path), rc.Pass())
    assert report.lines[-1] == "last update: never"


def test_down_stops_every_server_it_started(monkeypatch, tmp_path):
    stopped = []
    monkeypatch.setattr(rc_machine, "stop_server", lambda pid: stopped.append(pid) or "")
    plan = plan_for(tmp_path, names=("devkit", "carameli"), servers={"devkit": 1, "carameli": 2})
    rc.down(plan, rc.Pass())
    assert stopped == [1, 2]
    assert plan.state.servers == {}


def test_down_reports_a_server_it_could_not_stop(monkeypatch, tmp_path):
    monkeypatch.setattr(rc_machine, "stop_server", lambda pid: "Access is denied.")
    report = rc.down(plan_for(tmp_path, servers={"devkit": 1}), rc.Pass())
    assert report.failures == 1


# --- the artifact ------------------------------------------------------------


def test_the_artifact_names_the_failure_count_and_the_time():
    text = rc.render(["devkit: up (pid 42)"], 0, _dt.datetime(2026, 9, 2, 4, 0))
    assert "0 failure(s)" in text and "2026-09-02T04:00:00" in text
    assert text.endswith("\n")


def test_write_artifact_creates_the_logs_directory(tmp_path):
    rc.write_artifact("# hello\n", tmp_path)
    assert (tmp_path / rc.ARTIFACT).read_text(encoding="utf-8") == "# hello\n"


# --- build_plan and the CLI --------------------------------------------------


def test_build_plan_reports_a_workspace_that_opts_nothing_in(tmp_path):
    report = rc.Pass()
    assert rc.build_plan(workspace(tmp_path), tmp_path, report) is None
    assert report.failures == 0 and "nothing to serve" in report.lines[0]


def test_build_plan_resolves_the_projects_that_opted_in(tmp_path):
    plan = rc.build_plan(workspace(tmp_path, ["devkit"]), tmp_path, rc.Pass())
    assert plan is not None and plan.names == ["devkit"]
    assert plan.root == tmp_path


def test_build_plan_fails_when_every_named_project_is_unknown(tmp_path):
    report = rc.Pass()
    assert rc.build_plan(workspace(tmp_path, ["typo"]), tmp_path, report) is None
    assert report.failures == 1


def test_parse_args_defaults_to_the_read_only_mode():
    """`status` is the safe default, which is exactly why the installer must name
    `maintain` explicitly -- see `tests/test_scheduled_jobs.py`."""
    assert rc.parse_args([]).mode == "status"


def test_parse_args_rejects_a_mode_that_is_not_one():
    with pytest.raises(SystemExit):
        rc.parse_args(["sideways"])


def test_run_mode_updates_only_when_the_day_is_owed(monkeypatch, tmp_path):
    monkeypatch.setattr(rc_machine, "pid_is_server", lambda pid: True)
    calls = []
    monkeypatch.setattr(
        rc, "cycle", lambda plan, report, **k: (calls.append("cycle"), (report, True))[1]
    )
    plan = plan_for(tmp_path, servers={"devkit": 42})
    plan.state.last_update = "2026-09-02"
    rc.run_mode("maintain", plan, rc.Pass(), _dt.datetime(2026, 9, 2, 23, 0))
    assert calls == []
    plan.state.last_update = "2026-09-01"
    rc.run_mode("maintain", plan, rc.Pass(), _dt.datetime(2026, 9, 2, 23, 0))
    assert calls == ["cycle"]


def test_a_maintain_run_that_cycles_records_the_day(monkeypatch, tmp_path):
    monkeypatch.setattr(rc, "cycle", lambda plan, report, **k: (report, True))
    plan = plan_for(tmp_path)
    rc.run_mode("maintain", plan, rc.Pass(), _dt.datetime(2026, 9, 2, 23, 0))
    assert plan.state.last_update == "2026-09-02"


def test_a_deferred_cycle_does_not_record_the_day(monkeypatch, tmp_path):
    """Otherwise a busy 04:45 would count as done and the update would wait a full day."""
    monkeypatch.setattr(rc, "cycle", lambda plan, report, **k: (report, False))
    plan = plan_for(tmp_path)
    rc.run_mode("maintain", plan, rc.Pass(), _dt.datetime(2026, 9, 2, 23, 0))
    assert plan.state.last_update == ""


def test_main_writes_the_artifact_when_there_is_no_workspace_file(tmp_path, capsys):
    code = rc.main(["status", "--workspace", str(tmp_path / "nope"), "--devkit", str(tmp_path)])
    assert code == 2
    assert "no workspace file" in (tmp_path / rc.ARTIFACT).read_text(encoding="utf-8")


def test_main_is_a_no_op_on_a_workspace_that_opted_nothing_in(tmp_path):
    code = rc.main(["status", "--workspace", str(workspace(tmp_path)), "--devkit", str(tmp_path)])
    assert code == 0
    assert "nothing to serve" in (tmp_path / rc.ARTIFACT).read_text(encoding="utf-8")


def test_main_refuses_to_start_anything_without_a_cli(monkeypatch, tmp_path):
    monkeypatch.setattr(rc_machine, "claude_executable", lambda: "")
    code = rc.main(
        ["up", "--workspace", str(workspace(tmp_path, ["devkit"])), "--devkit", str(tmp_path)]
    )
    assert code == 2
    assert "claude is not on PATH" in (tmp_path / rc.ARTIFACT).read_text(encoding="utf-8")


def test_status_needs_no_cli_at_all(monkeypatch, tmp_path):
    """A read-only report on a machine where the binary moved is still worth having."""
    monkeypatch.setattr(rc_machine, "claude_executable", lambda: "")
    monkeypatch.setattr(rc_machine, "pid_is_server", lambda pid: False)
    code = rc.main(
        ["status", "--workspace", str(workspace(tmp_path, ["devkit"])), "--devkit", str(tmp_path)]
    )
    assert code == 0


def test_a_status_run_never_rewrites_the_state_file(monkeypatch, tmp_path):
    """`status` is the default mode, so it is the one most likely to run by accident."""
    monkeypatch.setattr(rc_machine, "pid_is_server", lambda pid: False)
    rc.main(
        ["status", "--workspace", str(workspace(tmp_path, ["devkit"])), "--devkit", str(tmp_path)]
    )
    assert not (tmp_path / rc.STATE).exists()


# --- power saving ------------------------------------------------------------


def test_power_saving_stops_the_servers_while_someone_is_at_the_desk(monkeypatch, tmp_path):
    monkeypatch.setattr(rc_machine, "at_the_desk", lambda minutes: True)
    stopped = []
    monkeypatch.setattr(rc_machine, "stop_server", lambda pid: stopped.append(pid) or "")
    plan = plan_for(
        tmp_path, servers={"devkit": 42}, config=rc_config.Config(("devkit",), power_saving=True)
    )
    plan.state.last_update = _dt.date.today().isoformat()
    report = rc.Pass()
    rc.run_mode("maintain", plan, report, _dt.datetime.now())
    assert stopped == [42]
    assert "power saving" in report.lines[0]


def test_power_saving_starts_them_again_once_the_desk_is_empty(monkeypatch, no_launch, tmp_path):
    monkeypatch.setattr(rc_machine, "at_the_desk", lambda minutes: False)
    monkeypatch.setattr(rc_machine, "pid_is_server", lambda pid: False)
    plan = plan_for(tmp_path, config=rc_config.Config(("devkit",), power_saving=True))
    plan.state.last_update = _dt.date.today().isoformat()
    rc.run_mode("maintain", plan, rc.Pass(), _dt.datetime.now())
    assert no_launch == ["devkit"]


def test_power_saving_never_overrides_an_explicit_up(monkeypatch, no_launch, tmp_path):
    """Someone who types `up` is asking for servers. A mode that answered "no, you are at
    your desk" would be refusing the one instruction it was given."""
    monkeypatch.setattr(rc_machine, "at_the_desk", lambda minutes: True)
    monkeypatch.setattr(rc_machine, "pid_is_server", lambda pid: False)
    plan = plan_for(tmp_path, config=rc_config.Config(("devkit",), power_saving=True))
    rc.run_mode("up", plan, rc.Pass(), _dt.datetime.now())
    assert no_launch == ["devkit"]


def test_the_desk_is_never_consulted_when_power_saving_is_off(monkeypatch, no_launch, tmp_path):
    def explode(minutes):
        raise AssertionError("power saving is off; the desk is none of its business")

    monkeypatch.setattr(rc_machine, "at_the_desk", explode)
    monkeypatch.setattr(rc_machine, "pid_is_server", lambda pid: False)
    plan = plan_for(tmp_path)
    plan.state.last_update = _dt.date.today().isoformat()
    rc.run_mode("maintain", plan, rc.Pass(), _dt.datetime.now())
    assert no_launch == ["devkit"]
