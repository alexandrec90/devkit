"""`reap_machine.py`: the table, the ancestry, and the three classifications.

The fixture table is the machine this was written on, reduced: an interactive session
with its MCP server, a served Remote Control server with a spawned session, a stray
server with one, and the two shapes of orphaned dev server -- one under a shell whose
owner has gone, one whose parent is gone outright -- beside a dev server a person
started. Every classification is asserted against that one table, so a change that
makes an owned process look like a leftover fails here before it fails on a desk.
"""

from __future__ import annotations

import os
import subprocess
import time

import pytest
from support import load_script

rc_machine = load_script("scripts/rc_machine.py")
reap_machine = load_script("scripts/reap_machine.py")

P = reap_machine.Process

CLAUDE = r'"C:\Users\me\.local\bin\claude.EXE"'
NODE = r'"C:\nvm\v26\node.exe"'

TABLE = [
    P(0, 0, "system idle process", ""),
    P(1, 0, "explorer", "explorer.exe"),
    P(10, 1, "code", r'"C:\Code.exe"'),
    P(11, 10, "pwsh", "pwsh"),
    # An interactive session and the MCP server every session starts.
    P(12, 11, "claude", f"{CLAUDE} -w"),
    P(13, 12, "cmd", "cmd /c chrome-devtools-mcp"),
    P(14, 13, "node", f"{NODE} chrome-devtools-mcp.js"),
    # The served server (in the state file) and the session it spawned.
    P(20, 999, "claude", f"{CLAUDE} remote-control --name carameli --spawn worktree --capacity 8"),
    P(21, 20, "claude", f"{CLAUDE} --print --sdk-url https://x --session-id cse_AAA"),
    # A stray server from an earlier configuration, still spawning.
    P(30, 998, "claude", f"{CLAUDE} remote-control --name devkit --spawn same-dir --capacity 8"),
    P(31, 30, "claude", f"{CLAUDE} --print --sdk-url https://x --session-id cse_BBB"),
    # A dev server an agent's shell started; the agent has gone, the shell has not.
    P(40, 997, "bash", "bash"),
    P(41, 40, "npm", r'"C:\nvm\npm.exe" run dev'),
    P(42, 41, "node", f'{NODE} "C:\\nvm\\npm-cli.js" run dev'),
    P(43, 42, "cmd", "cmd /c vite --host"),
    P(44, 43, "node", f'{NODE} "C:\\proj\\node_modules\\vite\\bin\\vite.js" --host'),
    # A dev server a person started in an editor terminal.
    P(50, 11, "node", f'{NODE} "C:\\proj\\node_modules\\vite\\bin\\vite.js" --port 5300'),
    # A dev server whose parent is gone outright.
    P(60, 996, "node", "node C:/proj/node_modules/vite/bin/vite.js --port 4108"),
    # A session-shaped process nobody's server started.
    P(70, 11, "claude", f"{CLAUDE} --print --sdk-url https://x --session-id cse_CCC"),
]


def completed(stdout: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


# --- the table --------------------------------------------------------------------


def test_the_windows_lister_asks_for_the_columns_the_parser_reads():
    argv = reap_machine.table_argv(windows=True)
    assert argv[0] == "powershell"
    assert "-NonInteractive" in argv
    assert "ParentProcessId" in argv[-1] and "CommandLine" in argv[-1]


def test_the_posix_lister_asks_ps_for_the_same_four_columns():
    assert reap_machine.table_argv(windows=False) == ["ps", "-eo", "pid=,ppid=,comm=,args="]


def test_windows_rows_are_read_with_names_normalised_and_null_command_lines_kept():
    text = (
        '[{"ProcessId":4,"ParentProcessId":0,"Name":"System","CommandLine":null},'
        '{"ProcessId":18584,"ParentProcessId":13432,"Name":"claude.EXE",'
        '"CommandLine":"C:\\\\bin\\\\claude.EXE remote-control --name devkit"}]'
    )
    table = reap_machine.parse_windows_table(text)
    assert table == [
        P(4, 0, "system", ""),
        P(18584, 13432, "claude", r"C:\bin\claude.EXE remote-control --name devkit"),
    ]


def test_a_single_row_comes_back_as_an_object_and_is_still_a_table():
    text = '{"ProcessId":7,"ParentProcessId":1,"Name":"node.exe","CommandLine":"node x"}'
    assert reap_machine.parse_windows_table(text) == [P(7, 1, "node", "node x")]


@pytest.mark.parametrize("text", ["", "not json", "[1, 2]", '[{"ProcessId": true}]'])
def test_unusable_windows_output_is_an_empty_table(text):
    assert reap_machine.parse_windows_table(text) == []


def test_posix_rows_split_into_four_columns_with_free_text_last():
    text = "  1     0 systemd /sbin/init splash\n 42     1 node    node vite.js --host\nbad line\n"
    assert reap_machine.parse_posix_table(text) == [
        P(1, 0, "systemd", "/sbin/init splash"),
        P(42, 1, "node", "node vite.js --host"),
    ]


def test_the_table_is_none_when_the_lister_fails_or_answers_nothing():
    assert reap_machine.process_table(lambda argv: completed(returncode=1), windows=True) is None
    assert reap_machine.process_table(lambda argv: completed("[]"), windows=True) is None

    def boom(argv):
        raise OSError("no powershell")

    assert reap_machine.process_table(boom, windows=True) is None


def test_the_table_is_read_through_the_platform_parser():
    text = '[{"ProcessId":7,"ParentProcessId":1,"Name":"node.exe","CommandLine":"x"}]'
    assert reap_machine.process_table(lambda argv: completed(text), windows=True) == [
        P(7, 1, "node", "x")
    ]
    assert reap_machine.process_table(lambda argv: completed("7 1 node x"), windows=False) == [
        P(7, 1, "node", "x")
    ]


def test_run_command_captures_rather_than_streaming():
    result = reap_machine.run_command([os.fsdecode(__import__("sys").executable), "-c", "print(1)"])
    assert result.stdout.strip() == "1"


def test_argv_keeps_windows_paths_whole_and_survives_an_unbalanced_quote():
    assert reap_machine.argv_of(r'"C:\a b\claude.EXE" remote-control --name x') == [
        r'"C:\a b\claude.EXE"',
        "remote-control",
        "--name",
        "x",
    ]
    assert reap_machine.argv_of('"unbalanced') == []


# --- ancestry ----------------------------------------------------------------------


def test_ancestry_walks_to_the_root_nearest_first():
    assert [row.pid for row in reap_machine.ancestors(TABLE, 14)] == [13, 12, 11, 10, 1, 0]


def test_ancestry_stops_at_a_parent_the_table_has_never_heard_of():
    assert [row.pid for row in reap_machine.ancestors(TABLE, 44)] == [43, 42, 41, 40]
    assert reap_machine.ancestors(TABLE, 60) == []


def test_ancestry_is_bounded_when_pids_form_a_loop():
    loop = [P(1, 2, "a", ""), P(2, 1, "b", "")]
    assert [row.pid for row in reap_machine.ancestors(loop, 1)] == [2]
    assert reap_machine.ancestors(TABLE, 0) == []


def test_a_chain_that_reaches_an_editor_or_a_session_is_hosted():
    assert reap_machine.is_hosted(TABLE, 14)  # under an interactive claude
    assert reap_machine.is_hosted(TABLE, 50)  # under Code


def test_a_chain_that_ends_in_a_shell_or_in_nothing_is_not():
    assert not reap_machine.is_hosted(TABLE, 44)
    assert not reap_machine.is_hosted(TABLE, 60)


def test_shells_are_not_hosts_because_an_orphan_is_a_shell_whose_owner_left():
    for shell in ("bash", "pwsh", "cmd", "powershell", "python"):
        assert shell not in reap_machine.HOSTS
    for host in ("claude", "code", "windowsterminal", "svchost"):
        assert host in reap_machine.HOSTS


# --- Remote Control rows -------------------------------------------------------------


def test_a_named_server_is_recognised_by_its_mode_and_name():
    assert reap_machine.rc_daemon_name(TABLE[7]) == "carameli"
    assert reap_machine.daemons(TABLE) == {20: "carameli", 30: "devkit"}


@pytest.mark.parametrize(
    "cmdline",
    [
        f"{CLAUDE} -p 'fix remote-control'",  # the word, not the mode
        f"{CLAUDE} remote-control --spawn worktree",  # no --name: not devkit's
        f"{CLAUDE} remote-control --name",  # truncated
        f"{NODE} remote-control --name x",  # not claude
    ],
)
def test_anything_else_is_not_a_server(cmdline):
    name = "node" if cmdline.startswith(NODE) else "claude"
    assert reap_machine.rc_daemon_name(P(1, 0, name, cmdline)) == ""


def test_a_spawned_session_is_recognised_by_sdk_url_and_session_id():
    assert reap_machine.spawned_session_id(TABLE[8]) == "cse_AAA"
    assert reap_machine.spawned_session_id(TABLE[4]) == ""  # interactive: no --sdk-url
    assert reap_machine.spawned_session_id(P(1, 0, "claude", f"{CLAUDE} --sdk-url u")) == ""
    assert reap_machine.spawned_session_id(P(1, 0, "node", "node --sdk-url u --session-id s")) == ""


def test_sessions_are_only_the_children_of_named_servers():
    found = reap_machine.sessions(TABLE)
    assert found == [
        reap_machine.Session(TABLE[8], "cse_AAA", "carameli"),
        reap_machine.Session(TABLE[10], "cse_BBB", "devkit"),
    ]
    assert 70 not in {s.row.pid for s in found}


# --- activity ----------------------------------------------------------------------


def touch(path, age_seconds, now):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    os.utime(path, (now - age_seconds, now - age_seconds))


def test_activity_finds_a_worktree_session_by_the_id_in_its_store_directory(tmp_path):
    now = time.time()
    store = tmp_path / "projects"
    bridge = store / "C--ws-devkit--claude-worktrees-bridge-cse-01ABC"
    touch(bridge / "a.jsonl", 3600, now)
    touch(bridge / "b.jsonl", 60, now)
    touch(store / "C--ws-devkit" / "c.jsonl", 5, now)  # the project's own, not this session's
    latest = reap_machine.session_activity("cse_01ABC", tmp_path / "devkit", store)
    assert latest == pytest.approx(now - 60, abs=2)


def test_activity_falls_back_to_the_project_directory_for_an_in_place_session(tmp_path):
    now = time.time()
    store = tmp_path / "projects"
    project = tmp_path / "devkit"
    touch(rc_machine.transcript_dir(project, store) / "c.jsonl", 300, now)
    latest = reap_machine.session_activity("cse_NOPE", project, store)
    assert latest == pytest.approx(now - 300, abs=2)


def test_activity_is_unknown_without_a_store_or_without_anywhere_to_look(tmp_path):
    assert reap_machine.session_activity("cse_X", tmp_path, tmp_path / "missing") is None
    (tmp_path / "projects").mkdir()
    assert reap_machine.session_activity("cse_X", None, tmp_path / "projects") is None


def test_a_project_the_store_has_never_seen_is_infinitely_idle(tmp_path):
    (tmp_path / "projects").mkdir()
    assert reap_machine.session_activity("cse_X", tmp_path / "new", tmp_path / "projects") == 0.0


def test_newest_transcript_ignores_a_directory_with_none(tmp_path):
    assert reap_machine.newest_transcript(tmp_path) == 0.0


# --- dev servers -------------------------------------------------------------------


def test_orphaned_dev_servers_are_the_top_of_each_unowned_tree():
    assert [row.pid for row in reap_machine.dev_servers(TABLE)] == [41, 60]


def test_a_dev_server_under_an_editor_or_a_session_is_owned():
    owned = [
        *TABLE,
        P(80, 12, "node", f'{NODE} "C:\\proj\\node_modules\\vitest\\vitest.mjs" run'),
    ]
    assert 50 not in {row.pid for row in reap_machine.dev_servers(owned)}
    assert 80 not in {row.pid for row in reap_machine.dev_servers(owned)}


def test_only_a_runtime_image_can_be_a_dev_server():
    """An editor names the file it has open on its command line; `vite.config.ts` is not
    a vite server."""
    editor = [P(1, 999, "code", r'"C:\Code.exe" C:\proj\vite.config.ts')]
    assert reap_machine.dev_servers(editor) == []


@pytest.mark.parametrize(
    "cmdline",
    [
        r'"C:\nvm\node.exe" "C:\p\node_modules\vite\bin\vite.js" --host',
        r'"node" "C:\p\node_modules\.bin\\..\vitest\vitest.mjs" run',
        r'"C:\nvm\node.exe" "C:\nvm\node_modules\npm\bin\npm-cli.js" run dev -- --port 1',
        r'"C:\nvm\npm.exe" run dev',
        "node node_modules/.bin/next dev",
        "node webpack serve",
    ],
)
def test_the_default_pattern_names_the_servers_agents_start(cmdline):
    assert reap_machine.dev_servers([P(1, 999, "node", cmdline)]) == [P(1, 999, "node", cmdline)]


@pytest.mark.parametrize(
    "cmdline",
    [
        r'"C:\nvm\node.exe" chrome-devtools-mcp.js',
        r'"C:\nvm\node.exe" C:\p\scripts\invite.js',  # `invite` is not `vite`
        "node C:/next-app/server.js",  # `next` in a path, no `dev`
    ],
)
def test_the_default_pattern_leaves_other_node_processes_alone(cmdline):
    assert reap_machine.dev_servers([P(1, 999, "node", cmdline)]) == []


def test_the_pattern_is_a_parameter():
    custom = [P(1, 999, "node", "node my-server.js")]
    assert reap_machine.dev_servers(custom, pattern=r"my-server") == custom
    assert reap_machine.dev_servers(custom) == []


# --- stopping ----------------------------------------------------------------------


def test_alive_is_read_off_tasklist_and_unknown_is_alive():
    row = '"claude.exe","123","Console","1","100 K"\n'
    assert reap_machine.pid_alive(123, lambda argv: completed(row), windows=True)
    assert not reap_machine.pid_alive(123, lambda argv: completed("INFO: none"), windows=True)
    assert reap_machine.pid_alive(123, lambda argv: completed(returncode=1), windows=True)

    def boom(argv):
        raise OSError("no tasklist")

    assert reap_machine.pid_alive(123, boom, windows=True)


def test_alive_off_windows_asks_the_kernel():
    assert reap_machine.pid_alive(os.getpid(), windows=False)
    assert not reap_machine.pid_alive(2**22 - 1, windows=False) or True  # may exist; no crash


def test_stop_is_polite_first_and_forces_only_what_survives():
    calls = []

    def run(argv):
        calls.append(list(argv))
        if argv[0] == "tasklist":
            return completed("INFO: No tasks are running")
        return completed()

    error = reap_machine.stop_tree(
        41, run, sleep=lambda s: calls.append(("sleep", s)), windows=True
    )
    assert error == ""
    assert calls[0] == ["taskkill", "/PID", "41", "/T"]
    assert ("sleep", reap_machine.STOP_GRACE_SECONDS) in calls
    assert not any("/F" in call for call in calls if isinstance(call, list))


def test_stop_escalates_when_the_polite_ask_is_ignored():
    calls = []

    def run(argv):
        calls.append(list(argv))
        if argv[0] == "tasklist":
            return completed('"node.exe","41","Console","1","100 K"\n')
        return completed()

    assert reap_machine.stop_tree(41, run, sleep=lambda s: None, windows=True) == ""
    assert ["taskkill", "/PID", "41", "/T", "/F"] in calls


def test_stop_reports_a_force_that_failed_and_a_runner_that_raised():
    def refuse(argv):
        if argv[0] == "tasklist":
            return completed('"node.exe","41","Console","1","100 K"\n')
        return completed("", returncode=1) if "/F" in argv else completed()

    assert "taskkill failed" in reap_machine.stop_tree(
        41, refuse, sleep=lambda s: None, windows=True
    )

    def boom(argv):
        raise subprocess.TimeoutExpired(argv, 1)

    assert reap_machine.stop_tree(41, boom, sleep=lambda s: None, windows=True)


def test_stop_off_windows_says_so_rather_than_pretending():
    assert "Windows" in reap_machine.stop_tree(41, lambda argv: completed(), windows=False)
