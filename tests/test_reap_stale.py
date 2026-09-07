"""`reap-stale.py`: the verdicts, the order, and what the pass leaves behind.

Two properties this suite is here to pin:

- **Nothing a person is in is ever a candidate.** An interactive session, a session with
  a fresh transcript, a stray server with an active session under it, and a dev server
  under a living editor all come out as `kept` -- and `status` never calls the stopper.
- **A tree is stopped once.** A session under a stray that is itself reaped is reported
  as covered, not stopped separately, and the history counts what actually ended.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import time

import pytest
from support import load_script

rc_config = load_script("scripts/rc_config.py")
rc_machine = load_script("scripts/rc_machine.py")
reap_machine = load_script("scripts/reap_machine.py")
reap = load_script("scripts/reap-stale.py")

P = reap_machine.Process
CLAUDE = r'"C:\bin\claude.EXE"'

# The real clock, captured once: the `main` tests run with `time.time()` as "now", and a
# fixture stamped against anything else reads as negative idle -- which is "active".
NOW = time.time()
WHEN = _dt.datetime(2026, 9, 7, 15, 40)


def table():
    return [
        P(1, 0, "explorer", "explorer.exe"),
        P(10, 1, "code", "Code.exe"),
        P(12, 10, "claude", f"{CLAUDE} -w"),
        P(20, 999, "claude", f"{CLAUDE} remote-control --name carameli --spawn worktree"),
        P(21, 20, "claude", f"{CLAUDE} --print --sdk-url u --session-id cse_OLD"),
        P(22, 20, "claude", f"{CLAUDE} --print --sdk-url u --session-id cse_NEW"),
        P(30, 998, "claude", f"{CLAUDE} remote-control --name devkit --spawn same-dir"),
        P(31, 30, "claude", f"{CLAUDE} --print --sdk-url u --session-id cse_STRAYED"),
        P(40, 997, "claude", f"{CLAUDE} remote-control --name roguelike --spawn same-dir"),
        P(50, 996, "node", r'"C:\nvm\npm.exe" run dev'),
        P(51, 50, "node", r'"node" "C:\p\node_modules\vite\bin\vite.js"'),
        P(60, 10, "node", r'"node" "C:\p\node_modules\vite\bin\vite.js" --port 5300'),
    ]


def touch(path, age_seconds):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    os.utime(path, (NOW - age_seconds, NOW - age_seconds))


@pytest.fixture
def store(tmp_path):
    """A transcript store: one session idle ten hours, one active three minutes ago, and
    the stray's session filed under the project directory, idle five hours."""
    root = tmp_path / "projects"
    touch(root / "C--ws-carameli--claude-worktrees-bridge-cse-OLD" / "a.jsonl", 10 * 3600)
    touch(root / "C--ws-carameli--claude-worktrees-bridge-cse-NEW" / "a.jsonl", 180)
    touch(rc_machine.transcript_dir(tmp_path / "devkit", root) / "a.jsonl", 5 * 3600)
    return root


def plan_for(tmp_path, store, known=(20,), projects=("carameli", "devkit", "roguelike"), **kw):
    settings = reap.Settings(**kw) if kw else reap.Settings()
    return reap.Plan(
        table=table(),
        store=store,
        settings=settings,
        root=tmp_path,
        projects=tuple(projects),
        known=frozenset(known),
        now=NOW,
    )


def verdicts(findings):
    return {finding.row.pid: finding.reap for finding in findings}


# --- settings -----------------------------------------------------------------------


def workspace_text(setting):
    return json.dumps({"folders": [], "settings": {reap.SETTING: setting}})


def test_settings_default_when_absent_or_malformed():
    for text in ("", "not json", "{}", json.dumps({"settings": {reap.SETTING: [1]}})):
        assert reap.parse_settings(text) == reap.Settings()


def test_settings_read_the_idle_window_the_switch_and_the_pattern():
    parsed = reap.parse_settings(
        workspace_text({"sessionIdleMinutes": 45, "devServers": False, "devServerPattern": "x"})
    )
    assert parsed == reap.Settings(
        session_idle_minutes=45, dev_servers=False, dev_server_pattern="x"
    )


@pytest.mark.parametrize("bad", [0, -5, True, "60", 1.5])
def test_an_unusable_idle_window_falls_back(bad):
    parsed = reap.parse_settings(workspace_text({"sessionIdleMinutes": bad}))
    assert parsed.session_idle_minutes == reap.DEFAULT_SESSION_IDLE_MINUTES


def test_only_a_bare_false_switches_dev_servers_off():
    assert reap.parse_settings(workspace_text({"devServers": "false"})).dev_servers
    assert not reap.parse_settings(workspace_text({"devServers": False})).dev_servers


def test_the_default_window_is_longer_than_the_restart_window():
    """A stop that does not resume must wait longer than a restart that does."""
    assert reap.DEFAULT_SESSION_IDLE_MINUTES > rc_config.DEFAULT_IDLE_MINUTES


# --- sessions -----------------------------------------------------------------------


def test_an_idle_session_is_reaped_and_a_fresh_one_kept(tmp_path, store):
    findings = reap.assess_sessions(plan_for(tmp_path, store))
    assert verdicts(findings) == {21: True, 22: False, 31: True}
    by_pid = {finding.row.pid: finding for finding in findings}
    assert by_pid[21].reason == "idle 600 min"
    assert by_pid[22].reason == "idle 3 min"
    assert "cse_OLD" in by_pid[21].label and "carameli" in by_pid[21].label


def test_the_idle_window_is_the_setting(tmp_path, store):
    findings = reap.assess_sessions(plan_for(tmp_path, store, session_idle_minutes=2))
    assert verdicts(findings)[22] is True


def test_a_session_whose_activity_is_unknowable_is_kept(tmp_path):
    findings = reap.assess_sessions(plan_for(tmp_path, tmp_path / "no-store"))
    assert all(not finding.reap for finding in findings)
    assert {finding.reason for finding in findings} == {"activity unknown"}


def test_an_interactive_session_is_never_a_finding(tmp_path, store):
    assert 12 not in verdicts(reap.assess(plan_for(tmp_path, store)))


# --- strays -------------------------------------------------------------------------


def test_a_stray_is_reaped_once_nothing_live_is_under_it(tmp_path, store):
    plan = plan_for(tmp_path, store)
    findings = reap.assess_strays(plan, reap.assess_sessions(plan))
    assert verdicts(findings) == {30: True, 40: True}
    reasons = {finding.row.pid: finding.reason for finding in findings}
    assert reasons[30] == "1 idle session(s) under it"
    assert reasons[40] == "no sessions under it"


def test_a_stray_with_an_active_session_is_kept_whole(tmp_path, store):
    plan = plan_for(tmp_path, store, known=())  # the served server is now a stray too
    findings = reap.assess_strays(plan, reap.assess_sessions(plan))
    assert verdicts(findings)[20] is False
    assert {f.reason for f in findings if f.row.pid == 20} == {"1 active session(s) under it"}


def test_a_server_the_state_file_owns_is_not_this_jobs(tmp_path, store):
    plan = plan_for(tmp_path, store, known=(20, 30, 40))
    assert reap.assess_strays(plan, []) == []


def test_a_server_for_an_unserved_project_was_started_by_hand(tmp_path, store):
    plan = plan_for(tmp_path, store, projects=("carameli",))
    assert 30 not in verdicts(reap.assess_strays(plan, reap.assess_sessions(plan)))


# --- dev servers --------------------------------------------------------------------


def test_orphaned_dev_servers_are_found_and_owned_ones_are_not(tmp_path, store):
    findings = reap.assess_dev_servers(plan_for(tmp_path, store))
    assert verdicts(findings) == {50: True}
    assert findings[0].label == "dev server pid 50 `npm.exe run dev`"


def test_the_dev_server_label_shortens_paths_of_either_slant(tmp_path, store):
    plan = plan_for(tmp_path, store)
    plan.table = [
        P(9, 999, "node", "C:/nvm/node.exe C:/proj/node_modules/vite/bin/vite.js --host 127.0.0.1")
    ]
    assert (
        reap.assess_dev_servers(plan)[0].label
        == "dev server pid 9 `node.exe vite.js --host 127.0.0.1`"
    )


def test_dev_servers_can_be_switched_off(tmp_path, store):
    assert reap.assess_dev_servers(plan_for(tmp_path, store, dev_servers=False)) == []


def test_assess_puts_sessions_before_strays_before_dev_servers(tmp_path, store):
    kinds = [finding.label.split()[0] for finding in reap.assess(plan_for(tmp_path, store))]
    assert kinds == ["session", "session", "session", "stray", "stray", "dev"]


# --- acting -------------------------------------------------------------------------


def test_act_stops_each_tree_once_and_writes_the_history(tmp_path, store):
    plan = plan_for(tmp_path, store)
    stopped = []
    report = reap.Pass()
    history = tmp_path / "logs" / "history.log"
    count = reap.act(
        plan, reap.assess(plan), report, lambda pid: stopped.append(pid) or "", history, WHEN
    )
    # 21 idle; 22 kept; 31 covered by 30; 30 and 40 strays; 50 dev server.
    assert stopped == [21, 30, 40, 50]
    assert count == 4
    assert report.failures == 0
    lines = report.lines
    assert any(line.endswith("-- with pid 30") and "cse_STRAYED" in line for line in lines)
    assert any(line.endswith("-- kept") and "cse_NEW" in line for line in lines)
    written = history.read_text(encoding="utf-8").splitlines()
    assert len(written) == 4
    assert written[0].startswith("2026-09-07T15:40:00 session cse_OLD")


def test_a_stop_that_fails_is_a_failure_of_the_pass(tmp_path, store):
    plan = plan_for(tmp_path, store)
    report = reap.Pass()
    count = reap.act(plan, reap.assess(plan), report, lambda pid: "access denied", None, WHEN)
    assert count == 0
    assert report.failures == 4
    assert any("could not stop: access denied" in line for line in report.lines)


def test_a_finding_not_marked_for_reaping_never_reaches_the_stopper(tmp_path, store):
    plan = plan_for(tmp_path, store)
    kept = reap.Finding(plan.table_row(22), "session x", "idle 3 min", False)
    report = reap.Pass()

    def stop(pid):
        raise AssertionError(f"stopped {pid}")

    assert reap.act(plan, [kept], report, stop, None, WHEN) == 0
    assert report.lines == ["session x: idle 3 min -- kept"]


def test_history_is_append_only_and_an_unwritable_one_is_not_fatal(tmp_path):
    path = tmp_path / "h.log"
    reap.append_history(path, "one")
    reap.append_history(path, "two")
    assert path.read_text(encoding="utf-8") == "one\ntwo\n"
    reap.append_history(tmp_path / "h.log" / "under-a-file", "three")  # a file is not a directory


def test_describe_reports_verdicts_without_a_stopper(tmp_path, store):
    report = reap.Pass()
    reap.describe(reap.assess(plan_for(tmp_path, store)), report)
    assert sum(line.endswith("-- would reap") for line in report.lines) == 5
    assert sum(line.endswith("-- kept") for line in report.lines) == 1


def test_a_pass_counts_only_the_failures():
    report = reap.Pass()
    report.say("fine")
    report.fail("not fine")
    assert (report.failures, report.lines) == (1, ["fine", "not fine"])


def test_the_plan_looks_a_row_up_by_pid(tmp_path, store):
    plan = plan_for(tmp_path, store)
    assert plan.table_row(21).ppid == 20
    with pytest.raises(KeyError):
        plan.table_row(9999)


# --- the artifact and the plan --------------------------------------------------------


def test_render_leads_with_the_stamp_the_mode_and_the_failure_count():
    text = reap.render(["a", "b"], 1, WHEN, "maintain")
    assert text == "# reap-stale 2026-09-07T15:40:00 [maintain] -- 1 failure(s)\na\nb\n"


def test_the_artifact_lands_under_logs_in_the_named_root(tmp_path):
    reap.write_artifact("x\n", tmp_path)
    assert (tmp_path / reap.ARTIFACT).read_text(encoding="utf-8") == "x\n"


def write_workspace(tmp_path, settings):
    path = tmp_path / "alex-projects.code-workspace"
    path.write_text(
        json.dumps({"folders": [{"path": "carameli"}], "settings": settings}), encoding="utf-8"
    )
    return path


def test_the_plan_reads_the_workspace_and_the_state_file(tmp_path, store):
    workspace = write_workspace(
        tmp_path,
        {rc_config.RC_SETTING: ["carameli"], reap.SETTING: {"sessionIdleMinutes": 30}},
    )
    (tmp_path / reap.RC_STATE).parent.mkdir()
    rc_machine.State(servers={"carameli": 20}).save(tmp_path / reap.RC_STATE)
    report = reap.Pass()
    plan = reap.build_plan(table(), workspace, tmp_path, report, store)
    assert plan.projects == ("carameli",)
    assert plan.known == frozenset({20})
    assert plan.settings.session_idle_minutes == 30
    assert plan.root == tmp_path
    assert report.lines == []


def test_a_missing_workspace_is_reported_and_leaves_the_pass_stray_blind(tmp_path, store):
    report = reap.Pass()
    plan = reap.build_plan(table(), tmp_path / "nope.code-workspace", tmp_path, report, store)
    assert plan.projects == () and plan.root is None
    assert report.lines and "no workspace file" in report.lines[0]
    assert reap.assess_strays(plan, []) == []
    # Sessions filed under a bridge directory are still assessable by id alone.
    assert verdicts(reap.assess_sessions(plan))[21] is True


def test_the_plan_defaults_the_store_to_claudes(tmp_path):
    plan = reap.build_plan([], None, tmp_path, reap.Pass())
    assert plan.store == rc_machine.sessions_store()
    assert plan.now == pytest.approx(time.time(), abs=5)


# --- main ---------------------------------------------------------------------------


def test_parse_args_defaults_to_the_read_only_mode():
    assert reap.parse_args([]).mode == "status"
    assert reap.parse_args(["maintain"]).mode == "maintain"
    with pytest.raises(SystemExit):
        reap.parse_args(["cycle"])


def test_status_reports_and_never_stops(tmp_path, store, monkeypatch, capsys):
    workspace = write_workspace(tmp_path, {rc_config.RC_SETTING: ["carameli"]})
    monkeypatch.setattr(reap_machine, "process_table", lambda: table())
    monkeypatch.setattr(rc_machine, "sessions_store", lambda: store)
    stopped = []
    monkeypatch.setattr(reap_machine, "stop_tree", lambda pid: stopped.append(pid) or "")
    code = reap.main(["status", "--workspace", str(workspace), "--devkit", str(tmp_path)])
    assert code == 0
    assert stopped == []
    out = capsys.readouterr().out
    assert "would reap" in out and "[status]" in out
    assert (tmp_path / reap.ARTIFACT).read_text(encoding="utf-8") == out
    assert not (tmp_path / reap.HISTORY).exists()


def test_maintain_stops_what_status_would_and_records_it(tmp_path, store, monkeypatch, capsys):
    workspace = write_workspace(tmp_path, {rc_config.RC_SETTING: ["carameli"]})
    monkeypatch.setattr(reap_machine, "process_table", lambda: table())
    monkeypatch.setattr(rc_machine, "sessions_store", lambda: store)
    stopped = []
    monkeypatch.setattr(reap_machine, "stop_tree", lambda pid: stopped.append(pid) or "")
    code = reap.main(["maintain", "--workspace", str(workspace), "--devkit", str(tmp_path)])
    assert code == 0
    # Only carameli is served here, so devkit's and roguelike's servers are by-hand and
    # kept -- but an idle session is idle whoever's server spawned it.
    assert stopped == [21, 31, 50]
    assert "stopped 3 process tree(s)" in capsys.readouterr().out
    assert len((tmp_path / reap.HISTORY).read_text(encoding="utf-8").splitlines()) == 3


def test_an_unreadable_table_assesses_nothing_and_exits_red(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(reap_machine, "process_table", lambda: None)
    code = reap.main(["maintain", "--devkit", str(tmp_path)])
    assert code == 2
    assert "could not be read" in capsys.readouterr().out
    assert (tmp_path / reap.ARTIFACT).is_file()


def test_a_quiet_machine_says_so(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(reap_machine, "process_table", lambda: [P(1, 0, "explorer", "")])
    assert reap.main(["maintain", "--devkit", str(tmp_path)]) == 0
    assert "nothing left behind" in capsys.readouterr().out


def test_a_failed_stop_reddens_the_pass(tmp_path, store, monkeypatch):
    workspace = write_workspace(tmp_path, {rc_config.RC_SETTING: ["carameli"]})
    monkeypatch.setattr(reap_machine, "process_table", lambda: table())
    monkeypatch.setattr(rc_machine, "sessions_store", lambda: store)
    monkeypatch.setattr(reap_machine, "stop_tree", lambda pid: "denied")
    assert reap.main(["reap", "--workspace", str(workspace), "--devkit", str(tmp_path)]) == 2
