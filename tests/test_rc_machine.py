"""`rc_machine.py`: the transcript store, the process probes, and the state file.

The property this suite exists for is **unknown is busy**. Every question here has a
third answer besides yes and no, and it has to fall on the side of "someone is working":
a store that cannot be read, a `tasklist` that cannot be run, a pid whose name cannot be
confirmed. Getting one of them backwards costs an interrupted turn on someone's phone,
which is the failure the whole idle predicate exists to avoid.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from support import load_script

rc_config = load_script("scripts/rc_config.py")
rc = load_script("scripts/rc_machine.py")


def completed(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def transcript(tmp_path: Path, name: str, mtime: float) -> Path:
    """A store in which `name` was last active at `mtime`."""
    store = tmp_path / "projects"
    directory = rc.transcript_dir(tmp_path / name, store)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "s.jsonl"
    path.write_text("{}", encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return store


# --- the transcript store ----------------------------------------------------


def test_the_slug_matches_the_store_this_machine_actually_keeps():
    assert rc.slug(r"C:\Users\alexa\vs-code\devkit") == "C--Users-alexa-vs-code-devkit"


def test_transcript_dir_puts_the_slug_under_the_store(tmp_path):
    assert rc.transcript_dir(Path("/a/b"), tmp_path).parent == tmp_path
    assert rc.transcript_dir(Path("/a/b"), tmp_path).name == rc.slug(Path("/a/b"))


def test_sessions_store_honours_the_config_directory(tmp_path):
    """A machine that moved the store and did not get this would read an empty directory
    as "nobody is working"."""
    assert rc.sessions_store(str(tmp_path)) == tmp_path / "projects"


def test_sessions_store_defaults_beside_the_home_directory(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(rc.Path, "home", classmethod(lambda cls: tmp_path))
    assert rc.sessions_store() == tmp_path / ".claude" / "projects"


def test_a_missing_store_is_unknown_rather_than_quiet(tmp_path):
    """`None`, not `0.0`. The two are different answers and `is_idle` treats them so."""
    assert rc.last_activity(tmp_path / "proj", tmp_path / "nope") is None


def test_a_project_the_store_has_never_heard_of_is_infinitely_idle(tmp_path):
    store = tmp_path / "projects"
    store.mkdir()
    assert rc.last_activity(tmp_path / "proj", store) == 0.0


def test_a_directory_holding_no_transcripts_is_idle_not_unknown(tmp_path):
    store = tmp_path / "projects"
    rc.transcript_dir(tmp_path / "proj", store).mkdir(parents=True)
    assert rc.last_activity(tmp_path / "proj", store) == 0.0


def test_activity_is_the_newest_transcript_mtime(tmp_path):
    store = transcript(tmp_path, "proj", 1000.0)
    older = rc.transcript_dir(tmp_path / "proj", store) / "old.jsonl"
    older.write_text("{}", encoding="utf-8")
    os.utime(older, (500.0, 500.0))
    assert rc.last_activity(tmp_path / "proj", store) == pytest.approx(1000.0)


def test_an_unreadable_store_is_never_idle(tmp_path):
    assert rc.is_idle(tmp_path / "proj", tmp_path / "nope", 20, now=1_000_000.0) is False


def test_a_project_touched_a_moment_ago_is_not_idle(tmp_path):
    store = transcript(tmp_path, "proj", 1_000_000.0)
    assert rc.is_idle(tmp_path / "proj", store, 20, now=1_000_000.0 + 60) is False


def test_a_project_quiet_for_the_window_is_idle(tmp_path):
    store = transcript(tmp_path, "proj", 1_000_000.0)
    assert rc.is_idle(tmp_path / "proj", store, 20, now=1_000_000.0 + 20 * 60) is True


# --- the process probes ------------------------------------------------------


def test_pid_query_argv_filters_by_pid_and_asks_for_parseable_output():
    assert rc.pid_query_argv(7) == ["tasklist", "/FI", "PID eq 7", "/FO", "CSV", "/NH"]


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
    def explode(argv):
        raise AssertionError("tasklist has no meaning here")

    assert rc.pid_is_server(1, run=explode, windows=False) is True


def test_run_command_captures_and_never_raises_on_a_non_zero_exit():
    """`check=False` is load-bearing: `taskkill` on a pid that has already gone exits
    non-zero, and that is an ordinary outcome of a polite stop."""
    result = rc.run_command([rc.sys.executable, "-c", "import sys; sys.exit(3)"])
    assert result.returncode == 3


# --- stopping ----------------------------------------------------------------


def test_stopping_asks_before_it_insists():
    assert rc.stop_argv(7) == ["taskkill", "/PID", "7", "/T"]
    assert rc.stop_argv(7, force=True)[-1] == "/F"


def test_the_whole_tree_is_stopped_so_mcp_servers_do_not_leak():
    """A server spawns MCP servers; leaving those behind leaks the memory the restart
    was partly meant to reclaim."""
    assert "/T" in rc.stop_argv(7)


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


def test_a_stop_that_cannot_run_at_all_reports_why():
    def explode(argv):
        raise OSError("taskkill is missing")

    assert "taskkill is missing" in rc.stop_server(7, run=explode, sleep=lambda _s: None)


# --- launching ---------------------------------------------------------------


def test_every_server_is_named_after_its_project():
    """Without `--name` the phone shows `<hostname>-<adjective>-<noun>` for every
    project, which defeats the one thing the session list is for."""
    argv = rc.launch_argv("claude", "devkit", rc_config.Config())
    assert argv[:2] == ["claude", "remote-control"]
    assert argv[argv.index("--name") + 1] == "devkit"


def test_no_permission_mode_is_passed_unless_one_was_configured():
    assert "--permission-mode" not in rc.launch_argv("claude", "d", rc_config.Config())
    argv = rc.launch_argv("claude", "d", rc_config.Config(permission_mode="acceptEdits"))
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"


def test_the_spawn_mode_is_always_passed_explicitly():
    argv = rc.launch_argv("claude", "d", rc_config.Config())
    assert argv[argv.index("--spawn") + 1] == "same-dir"


def test_the_cli_is_found_off_path_where_the_native_installer_puts_it(monkeypatch, tmp_path):
    """A scheduled task gets the system PATH, and `~/.local/bin` is not on it."""
    (tmp_path / ".local" / "bin").mkdir(parents=True)
    (tmp_path / ".local" / "bin" / "claude.exe").write_text("", encoding="utf-8")
    monkeypatch.setattr(rc.Path, "home", classmethod(lambda cls: tmp_path))
    assert rc.claude_executable(which=lambda _n: None).endswith("claude.exe")


def test_path_wins_when_it_has_an_answer():
    assert rc.claude_executable(which=lambda _n: r"C:\bin\claude.exe") == r"C:\bin\claude.exe"


def test_a_machine_with_no_cli_says_so_rather_than_guessing(monkeypatch, tmp_path):
    monkeypatch.setattr(rc.Path, "home", classmethod(lambda cls: tmp_path))
    assert rc.claude_executable(which=lambda _n: "") == ""


def test_start_server_reports_the_reason_when_the_binary_is_missing(tmp_path):
    """`(0, reason)`, not an exception: the caller turns this into one artifact line and
    goes on to the next project."""
    pid, error = rc.start_server(
        tmp_path, "devkit", str(tmp_path / "nope.exe"), rc_config.Config(), tmp_path / "logs"
    )
    assert pid == 0 and error


def test_start_server_leaves_the_child_a_file_to_speak_into(tmp_path):
    """`DEVNULL` would be the difference between reporting "started, then gone" every
    fifteen minutes forever and reporting the reason once."""
    logs = tmp_path / "logs"
    pid, error = rc.start_server(tmp_path, "devkit", rc.sys.executable, rc_config.Config(), logs)
    assert error == "" and pid
    assert (logs / "rc-devkit.out").is_file()


# --- state -------------------------------------------------------------------


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


def test_saving_creates_the_directory_it_needs(tmp_path):
    path = tmp_path / "logs" / "state.json"
    rc.State().save(path)
    assert path.is_file()


# --- the memory knobs -------------------------------------------------------


def test_the_session_capacity_is_always_passed():
    """Left off, a server inherits `remote-control`'s default of 32 and grows all week."""
    argv = rc.launch_argv("claude", "devkit", rc_config.Config())
    assert argv[argv.index("--capacity") + 1] == str(rc_config.DEFAULT_CAPACITY)


def test_a_raised_capacity_reaches_the_command():
    argv = rc.launch_argv("claude", "d", rc_config.Config(capacity=20))
    assert argv[argv.index("--capacity") + 1] == "20"


def test_the_idle_probe_answers_in_seconds_or_not_at_all():
    """Real call, no mock: the point of `GetLastInputInfo` here is that it works on this
    machine, and a mocked ctypes call would assert only that the mock was written."""
    idle = rc.user_idle_seconds()
    assert idle is None or (isinstance(idle, float) and idle >= 0.0)


def test_a_machine_that_cannot_answer_is_not_treated_as_occupied(monkeypatch):
    """The opposite of this module's usual rule, deliberately. The action gated on this
    is *stopping* servers, and a wrong "yes" destroys sessions; elsewhere the action is a
    restart and unknown means busy. The invariant is "unknown never destroys"."""
    monkeypatch.setattr(rc, "user_idle_seconds", lambda: None)
    assert rc.at_the_desk(15) is False


def test_recent_input_means_someone_is_here(monkeypatch):
    monkeypatch.setattr(rc, "user_idle_seconds", lambda: 60.0)
    assert rc.at_the_desk(15) is True


def test_a_long_silence_means_the_desk_is_empty(monkeypatch):
    monkeypatch.setattr(rc, "user_idle_seconds", lambda: 16 * 60.0)
    assert rc.at_the_desk(15) is False
