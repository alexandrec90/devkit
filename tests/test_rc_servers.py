"""`rc-servers.py`: the opt-in, the idle predicate, and the order the cycle runs in.

The three properties worth stating before the assertions, because each one is a bug this
suite exists to stop rather than a behaviour someone chose for taste:

- **Unknown is busy.** Every "can this be restarted" path has a third answer besides yes
  and no, and it has to fall on the same side as "someone is working". A store that
  cannot be read and a machine that cannot be asked about a pid both mean *do not touch
  it*, which is `agent_clis`' rule for the same question.
- **A busy project stops the whole cycle.** Not just its own server: stopping some
  servers to update a binary another one is executing fails the update *and* costs the
  stopped sessions for nothing.
- **The update happens between the two loops.** Windows cannot replace a running image,
  so a cycle that started the servers before updating would be the no-op this job was
  written to fix.
"""

from __future__ import annotations

import datetime as _dt
import json
import subprocess

import pytest
from support import load_script

rc = load_script("scripts/rc-servers.py")


def completed(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def workspace_text(
    setting: object = None, folders: tuple[str, ...] = ("devkit", "carameli")
) -> str:
    payload: dict = {"folders": [{"path": name} for name in folders], "settings": {}}
    if setting is not None:
        payload["settings"][rc.RC_SETTING] = setting
    return json.dumps(payload)


# --- the opt-in --------------------------------------------------------------


def test_a_bare_list_is_the_shorthand_for_a_config_of_defaults():
    config = rc.parse_config(workspace_text(["devkit"]))
    assert config.projects == ("devkit",)
    assert config.spawn == rc.DEFAULT_SPAWN
    assert config.permission_mode == rc.DEFAULT_PERMISSION_MODE
    assert config.idle_minutes == rc.DEFAULT_IDLE_MINUTES


def test_the_object_form_carries_the_knobs():
    config = rc.parse_config(
        workspace_text(
            {
                "projects": ["devkit"],
                "spawn": "worktree",
                "permissionMode": "acceptEdits",
                "updateAt": "05:30",
                "idleMinutes": 45,
            }
        )
    )
    assert config == rc.Config(("devkit",), "worktree", "acceptEdits", "05:30", 45)


def test_a_workspace_with_no_setting_serves_nothing():
    """The default has to be off. A machine that upgraded into this job and got servers
    it never asked for would be paying 300-400 MB per project to find out."""
    assert rc.parse_config(workspace_text()).projects == ()


@pytest.mark.parametrize(
    "text",
    ["", "{", "[]", '"a string"', json.dumps({"settings": []}), json.dumps({"settings": {}})],
    ids=["empty", "truncated", "list", "scalar", "settings-not-a-dict", "no-setting"],
)
def test_malformed_input_serves_nothing_rather_than_raising(text):
    """A scheduled task's traceback goes nowhere: it runs windowless. Serving nothing is
    visible in the artifact, which a crash is not."""
    assert rc.parse_config(text) == rc.Config()


def test_a_setting_that_is_neither_list_nor_object_is_ignored():
    assert rc.parse_config(workspace_text("devkit")) == rc.Config()


def test_non_string_project_names_are_dropped_not_stringified():
    config = rc.parse_config(workspace_text({"projects": ["devkit", 7, None, ""]}))
    assert config.projects == ("devkit",)


def test_a_boolean_idle_window_falls_back_to_the_default():
    """`isinstance(True, int)` is true, so `"idleMinutes": true` would otherwise set a
    one-minute window -- a restart in the middle of nearly every turn."""
    assert rc.parse_config(
        workspace_text({"projects": ["d"], "idleMinutes": True})
    ).idle_minutes == (rc.DEFAULT_IDLE_MINUTES)


@pytest.mark.parametrize("value", [0, -5, "20", 1.5], ids=["zero", "negative", "string", "float"])
def test_an_unusable_idle_window_falls_back_to_the_default(value):
    config = rc.parse_config(workspace_text({"projects": ["d"], "idleMinutes": value}))
    assert config.idle_minutes == rc.DEFAULT_IDLE_MINUTES


def test_a_name_that_is_not_a_checkout_is_reported_not_skipped_silently():
    """A typo that served nothing would look exactly like a working machine with nothing
    to do, which is the one answer this job must never give wrongly."""
    serve, notes = rc.selected(rc.Config(("devkit", "typo")), ["devkit"], frozenset())
    assert serve == ["devkit"]
    assert len(notes) == 1 and "typo" in notes[0]


def test_a_project_on_hold_gets_no_server():
    serve, notes = rc.selected(rc.Config(("devkit",)), ["devkit"], frozenset({"devkit"}))
    assert serve == []
    assert "on hold" in notes[0]


def test_everything_known_and_running_is_served_with_nothing_to_report():
    serve, notes = rc.selected(
        rc.Config(("devkit", "carameli")), ["devkit", "carameli"], frozenset()
    )
    assert serve == ["devkit", "carameli"]
    assert notes == []


# --- is anyone working in there? ---------------------------------------------


def test_the_slug_matches_the_store_this_machine_actually_keeps():
    assert rc.slug(r"C:\Users\alexa\vs-code\devkit") == "C--Users-alexa-vs-code-devkit"


def test_a_missing_store_is_unknown_rather_than_quiet(tmp_path):
    """`None`, not `0.0`. The two are different answers and `is_idle` treats them so."""
    assert rc.last_activity(tmp_path / "proj", tmp_path / "nope") is None


def test_a_project_the_store_has_never_heard_of_is_infinitely_idle(tmp_path):
    store = tmp_path / "projects"
    store.mkdir()
    assert rc.last_activity(tmp_path / "proj", store) == 0.0


def test_activity_is_the_newest_transcript_mtime(tmp_path):
    store = tmp_path / "projects"
    cwd = tmp_path / "proj"
    directory = rc.transcript_dir(cwd, store)
    directory.mkdir(parents=True)
    for name, mtime in (("old.jsonl", 1000.0), ("new.jsonl", 5000.0)):
        path = directory / name
        path.write_text("{}", encoding="utf-8")
        import os

        os.utime(path, (mtime, mtime))
    assert rc.last_activity(cwd, store) == pytest.approx(5000.0)


def test_a_directory_holding_no_transcripts_is_idle_not_unknown(tmp_path):
    store = tmp_path / "projects"
    cwd = tmp_path / "proj"
    rc.transcript_dir(cwd, store).mkdir(parents=True)
    assert rc.last_activity(cwd, store) == 0.0


def test_an_unreadable_store_is_never_idle(tmp_path):
    assert rc.is_idle(tmp_path / "proj", tmp_path / "nope", 20, now=1_000_000.0) is False


def test_a_project_touched_a_moment_ago_is_not_idle(tmp_path):
    store = tmp_path / "projects"
    cwd = tmp_path / "proj"
    directory = rc.transcript_dir(cwd, store)
    directory.mkdir(parents=True)
    path = directory / "s.jsonl"
    path.write_text("{}", encoding="utf-8")
    import os

    os.utime(path, (1_000_000.0, 1_000_000.0))
    assert rc.is_idle(cwd, store, 20, now=1_000_000.0 + 60) is False
    assert rc.is_idle(cwd, store, 20, now=1_000_000.0 + 20 * 60) is True


# --- processes ---------------------------------------------------------------


def test_a_pid_with_no_match_reads_as_no_process():
    """`tasklist` prints an INFO line and exits 0 for a pid that is gone, so "no rows"
    is not how absence arrives."""
    assert (
        rc.parse_pid_image("INFO: No tasks are running which match the specified criteria.") == ""
    )


def test_a_matching_row_yields_the_normalised_image_name():
    assert rc.parse_pid_image('"claude.exe","4960","Console","1","420,000 K"') == "claude"


def test_a_pid_recycled_by_another_process_is_not_a_server():
    """Pids are reused. Without the name check this job would report a server that is
    not there and `taskkill` a stranger on the next cycle."""
    assert (
        rc.pid_is_server(4960, run=lambda argv: completed('"chrome.exe","4960","Console"')) is False
    )


def test_a_live_claude_is_a_server():
    assert (
        rc.pid_is_server(4960, run=lambda argv: completed('"claude.exe","4960","Console"')) is True
    )


def test_a_machine_that_cannot_be_asked_is_assumed_to_be_serving():
    """Not knowing is the one state where acting is a guess about live work."""
    assert rc.pid_is_server(1, run=lambda argv: completed(returncode=1)) is True

    def explode(argv):
        raise OSError("tasklist is missing")

    assert rc.pid_is_server(1, run=explode) is True


def test_off_windows_the_question_is_not_asked_at_all():
    def explode(argv):  # pragma: no cover - must never be reached
        raise AssertionError("tasklist has no meaning here")

    assert rc.pid_is_server(1, run=explode, windows=False) is True


def test_stopping_asks_before_it_insists():
    assert rc.stop_argv(7) == ["taskkill", "/PID", "7", "/T"]
    assert rc.stop_argv(7, force=True)[-1] == "/F"


def test_a_server_that_exits_politely_is_never_forced():
    calls = []

    def run(argv):
        calls.append(list(argv))
        return completed("INFO: no tasks" if len(calls) > 1 else "")

    assert rc.stop_server(7, run=run, sleep=lambda _s: None) == ""
    assert not any("/F" in argv for argv in calls)


def test_a_server_that_ignores_the_ask_is_forced():
    calls = []

    def run(argv):
        calls.append(list(argv))
        return completed('"claude.exe","7","Console"')

    assert rc.stop_server(7, run=run, sleep=lambda _s: None) == ""
    assert any("/F" in argv for argv in calls)


def test_a_stop_that_fails_reports_why():
    def run(argv):
        if argv[0] == "tasklist":
            return completed('"claude.exe","7","Console"')
        return completed(returncode=1, stderr="Access is denied.")

    assert "denied" in rc.stop_server(7, run=run, sleep=lambda _s: None)


# --- the command ------------------------------------------------------------


def test_every_server_is_named_after_its_project():
    """Without `--name` the phone shows `<hostname>-<adjective>-<noun>` for every
    project, which defeats the one thing the session list is for."""
    argv = rc.launch_argv("claude", "devkit", rc.Config())
    assert argv[:2] == ["claude", "remote-control"]
    assert argv[argv.index("--name") + 1] == "devkit"


def test_no_permission_mode_is_passed_unless_one_was_configured():
    """The default is a standing grant nobody opted into. Absent means the project's own
    settings decide."""
    assert "--permission-mode" not in rc.launch_argv("claude", "d", rc.Config())
    argv = rc.launch_argv("claude", "d", rc.Config(permission_mode="acceptEdits"))
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"


def test_the_default_spawn_mode_does_not_cut_worktrees():
    """`worktree.py reconcile` already manages a worktree tier and reaps what it finds
    stranded. A second unattended worktree manager is how work disappears."""
    assert rc.DEFAULT_SPAWN == "same-dir"
    assert rc.launch_argv("claude", "d", rc.Config())[-1] == "same-dir"


def test_the_cli_is_found_off_path_where_the_native_installer_puts_it(monkeypatch, tmp_path):
    """A scheduled task gets the system PATH, and `~/.local/bin` is not on it."""
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    (home / ".local" / "bin" / "claude.exe").write_text("", encoding="utf-8")
    monkeypatch.setattr(rc.Path, "home", classmethod(lambda cls: home))
    assert rc.claude_executable(which=lambda _n: None).endswith("claude.exe")


def test_path_wins_when_it_has_an_answer():
    assert rc.claude_executable(which=lambda _n: r"C:\bin\claude.exe") == r"C:\bin\claude.exe"


def test_a_machine_with_no_cli_says_so_rather_than_guessing(monkeypatch, tmp_path):
    monkeypatch.setattr(rc.Path, "home", classmethod(lambda cls: tmp_path))
    assert rc.claude_executable(which=lambda _n: None) == ""


# --- state and the daily predicate -------------------------------------------


def test_state_survives_a_round_trip(tmp_path):
    path = tmp_path / "state.json"
    rc.State(servers={"devkit": 42}, last_update="2026-09-02").save(path)
    assert rc.State.load(path) == rc.State(servers={"devkit": 42}, last_update="2026-09-02")


@pytest.mark.parametrize(
    "text", ["not json", "[]", '{"servers": "nope"}'], ids=["garbage", "list", "wrong-type"]
)
def test_corrupt_state_reads_as_empty_rather_than_wedging_the_job(tmp_path, text):
    """The worst an empty state causes is a pass that starts servers it thinks are
    missing. A crash here leaves every server down until someone reads a log."""
    path = tmp_path / "state.json"
    path.write_text(text, encoding="utf-8")
    assert rc.State.load(path) == rc.State()


def test_a_missing_state_file_is_empty(tmp_path):
    assert rc.State.load(tmp_path / "absent.json") == rc.State()


def test_a_boolean_pid_is_not_a_pid(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"servers": {"d": True, "e": 5}}), encoding="utf-8")
    assert rc.State.load(path).servers == {"e": 5}


@pytest.mark.parametrize("at", ["04:00", "00:00", "23:59"])
def test_a_well_formed_update_time_is_accepted(at):
    assert rc.valid_time(at)


@pytest.mark.parametrize("at", ["4:00", "0400", "24:00", "04:60", "", "am"])
def test_a_malformed_update_time_is_rejected(at):
    assert not rc.valid_time(at)


def test_the_update_is_owed_once_the_hour_has_passed():
    now = _dt.datetime(2026, 9, 2, 4, 30)
    assert rc.due_for_update("", now, "04:00") is True


def test_the_update_is_not_owed_before_its_hour():
    assert rc.due_for_update("", _dt.datetime(2026, 9, 2, 3, 59), "04:00") is False


def test_the_update_is_not_owed_twice_in_a_day():
    now = _dt.datetime(2026, 9, 2, 9, 0)
    assert rc.due_for_update("2026-09-02", now, "04:00") is False


def test_a_day_that_was_missed_is_caught_up_on_the_next_tick():
    """The scheduler catches up a missed *fire*; every fire happens here, so only this
    predicate can know a whole day went by."""
    now = _dt.datetime(2026, 9, 3, 11, 0)
    assert rc.due_for_update("2026-09-01", now, "04:00") is True


def test_a_malformed_update_time_never_fires_the_cycle():
    assert rc.due_for_update("", _dt.datetime(2026, 9, 2, 23, 0), "4pm") is False


# --- the pass ---------------------------------------------------------------


@pytest.fixture
def no_launch(monkeypatch):
    """Record what would have been started instead of starting it."""
    started = []

    def fake(project, name, claude, config, logs):
        started.append(name)
        return 100 + len(started), ""

    monkeypatch.setattr(rc, "start_server", fake)
    return started


def test_a_server_that_is_up_is_left_alone(monkeypatch, no_launch, tmp_path):
    monkeypatch.setattr(rc, "pid_is_server", lambda pid, run=None: True)
    state = rc.State(servers={"devkit": 42})
    report = rc.ensure_up(["devkit"], tmp_path, state, "claude", rc.Config(), tmp_path)
    assert no_launch == []
    assert state.servers == {"devkit": 42}
    assert report.failures == 0 and "up (pid 42)" in report.lines[0]


def test_a_server_that_has_died_is_started_again(monkeypatch, no_launch, tmp_path):
    monkeypatch.setattr(rc, "pid_is_server", lambda pid, run=None: False)
    state = rc.State(servers={"devkit": 42})
    rc.ensure_up(["devkit"], tmp_path, state, "claude", rc.Config(), tmp_path)
    assert no_launch == ["devkit"]
    assert state.servers["devkit"] == 101


def test_a_launch_that_fails_is_a_failure_and_forgets_the_pid(monkeypatch, tmp_path):
    monkeypatch.setattr(rc, "pid_is_server", lambda pid, run=None: False)
    monkeypatch.setattr(rc, "start_server", lambda *a, **k: (0, "no such executable"))
    state = rc.State(servers={"devkit": 42})
    report = rc.ensure_up(["devkit"], tmp_path, state, "claude", rc.Config(), tmp_path)
    assert report.failures == 1
    assert "devkit" not in state.servers


def busy_store(tmp_path, name: str):
    """A store in which `name` was active a moment ago."""
    store = tmp_path / "projects"
    directory = rc.transcript_dir(tmp_path / name, store)
    directory.mkdir(parents=True)
    (directory / "s.jsonl").write_text("{}", encoding="utf-8")
    return store


def test_one_busy_project_defers_the_whole_cycle(monkeypatch, no_launch, tmp_path):
    """Not just its own server. Stopping the others to update a binary this one is
    executing fails the update and costs those sessions for nothing."""
    store = busy_store(tmp_path, "devkit")
    stopped = []
    monkeypatch.setattr(rc, "stop_server", lambda pid, run=None: stopped.append(pid) or "")
    state = rc.State(servers={"devkit": 42, "carameli": 43})

    def updater(**kwargs):  # pragma: no cover - must never be reached
        raise AssertionError("the update ran with a session in flight")

    report, ran = rc.cycle(
        ["devkit", "carameli"],
        tmp_path,
        state,
        "claude",
        rc.Config(),
        tmp_path,
        store,
        update=updater,
    )
    assert ran is False
    assert stopped == [] and no_launch == []
    assert "deferred" in report.lines[0]


def test_an_idle_machine_stops_updates_then_starts_again(monkeypatch, no_launch, tmp_path):
    """The order is the whole point: Windows cannot replace a running image, so an
    update that ran before the stop -- or after the restart -- would do nothing."""
    store = tmp_path / "projects"
    store.mkdir()
    events = []
    monkeypatch.setattr(rc, "pid_is_server", lambda pid, run=None: False)
    monkeypatch.setattr(rc, "stop_server", lambda pid, run=None: events.append(f"stop:{pid}") or "")

    def start(project, name, claude, config, logs):
        events.append(f"start:{name}")
        return 900, ""

    monkeypatch.setattr(rc, "start_server", start)

    class Outcome:
        lines = ("claude 2.1.258 -> 2.1.259",)
        failures = 0
        summary = "1 updated"

    def updater(**kwargs):
        events.append("update")
        return Outcome()

    state = rc.State(servers={"devkit": 42})
    report, ran = rc.cycle(
        ["devkit"], tmp_path, state, "claude", rc.Config(), tmp_path, store, update=updater
    )
    assert ran is True
    assert events == ["stop:42", "update", "start:devkit"]
    assert report.failures == 0
    assert state.servers == {"devkit": 900}


def test_only_claude_is_updated(monkeypatch, no_launch, tmp_path):
    """Codex has nothing to do with a Remote Control server. Including it would make
    this job's verdict depend on a CLI it never stopped -- one Codex session open
    anywhere would redden a pass that did its own work correctly."""
    store = tmp_path / "projects"
    store.mkdir()
    monkeypatch.setattr(rc, "pid_is_server", lambda pid, run=None: False)
    monkeypatch.setattr(rc, "stop_server", lambda pid, run=None: "")
    seen = {}

    class Outcome:
        lines = ()
        failures = 0
        summary = "current"

    def updater(**kwargs):
        seen.update(kwargs)
        return Outcome()

    rc.cycle(
        ["devkit"],
        tmp_path,
        rc.State(servers={"devkit": 42}),
        "claude",
        rc.Config(),
        tmp_path,
        store,
        update=updater,
    )
    assert [agent.name for agent in seen["agents"]] == ["claude"]
    assert seen["yes"] is True


def test_the_updater_is_called_the_way_agent_clis_actually_spells_it():
    """`run_pass`, not `update`, and with no `run=`: `agent_clis` has its own `Runner`
    taking a timeout this module's does not, so handing it the local one type-checks and
    then fails at the first call. Both halves were wrong in the first draft."""
    assert callable(rc.agent_clis.run_pass)
    assert not hasattr(rc.agent_clis, "update")


def test_a_failed_update_reddens_the_pass_but_still_restarts(monkeypatch, no_launch, tmp_path):
    """A CLI that could not be updated is a bad night. Leaving the servers down over it
    would be a worse one, and past four hours the sessions are gone rather than paused."""
    store = tmp_path / "projects"
    store.mkdir()
    monkeypatch.setattr(rc, "pid_is_server", lambda pid, run=None: False)
    monkeypatch.setattr(rc, "stop_server", lambda pid, run=None: "")

    class Outcome:
        lines = ()
        failures = 1
        summary = "1 failed"

    report, ran = rc.cycle(
        ["devkit"],
        tmp_path,
        rc.State(servers={"devkit": 42}),
        "claude",
        rc.Config(),
        tmp_path,
        store,
        update=lambda **k: Outcome(),
    )
    assert ran is True and report.failures == 1
    assert no_launch == ["devkit"]


def test_the_artifact_names_the_failure_count_and_the_time():
    text = rc.render(["devkit: up (pid 42)"], 0, _dt.datetime(2026, 9, 2, 4, 0))
    assert "0 failure(s)" in text and "2026-09-02T04:00:00" in text
    assert text.endswith("\n")
