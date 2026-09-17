"""`memory-inventory.py`: each section against a hand-written table, and the page they make.

Read-only is the property: `main` spawns nothing but `reap-stale.py status` and writes
nothing but its artifact. The sections are pure over `reap_machine.Process` rows, so
the table here carries the memory and age fields the classifications ignore.
"""

from __future__ import annotations

import datetime as _dt
import os
import subprocess
import time

import pytest
from support import load_script

rc_machine = load_script("scripts/rc_machine.py")
reap_machine = load_script("scripts/reap_machine.py")
reclaim = load_script("scripts/reclaim.py")
inventory = load_script("scripts/memory-inventory.py")

P = reap_machine.Process
MB = inventory.MB
NOW = 1_700_000_000.0
CLAUDE = r'"C:\bin\claude.EXE"'


def table():
    return [
        P(1, 0, "explorer", "explorer.exe", private=50 * MB, created=NOW - 86400 * 3),
        P(10, 1, "code", "Code.exe", private=900 * MB, created=NOW - 7200),
        P(12, 10, "claude", f"{CLAUDE} -w", private=400 * MB, created=NOW - 3600),
        P(13, 12, "node", "node chrome-devtools-mcp.js", private=120 * MB),
        P(14, 13, "node", "node mcp/watchdog.js", private=60 * MB),
        P(
            20,
            999,
            "claude",
            f"{CLAUDE} remote-control --name carameli --spawn worktree",
            private=100 * MB,
            created=NOW - 40000,
        ),
        P(
            21,
            20,
            "claude",
            f"{CLAUDE} --print --sdk-url u --session-id cse_ABCDEFGHIJKLMNOP",
            private=200 * MB,
            created=NOW - 30000,
        ),
        P(
            30,
            998,
            "claude",
            f"{CLAUDE} remote-control --name devkit --spawn same-dir",
            private=90 * MB,
        ),
        P(
            40,
            997,
            "node",
            r'"C:\nvm\node.exe" C:/p/node_modules/vite/bin/vite.js --host',
            private=80 * MB,
        ),
        P(
            50,
            10,
            "node",
            r'"node" "C:\p\node_modules\vite\bin\vite.js" --port 5300',
            private=70 * MB,
        ),
    ]


# --- sections -------------------------------------------------------------------------


def test_holders_rank_images_by_private_bytes_and_drop_the_empty():
    found = inventory.holders(table(), limit=3)
    assert found == [("code", 1, 900 * MB), ("claude", 4, 790 * MB), ("node", 4, 330 * MB)]
    assert ("explorer", 1, 50 * MB) in inventory.holders(table())
    assert all(private for _name, _count, private in inventory.holders([P(1, 0, "idle", "")]))


@pytest.mark.parametrize(
    ("created", "shown"),
    [
        (0.0, "?"),
        (NOW - 120, "2m"),
        (NOW - 3 * 3600 - 300, "3h 05m"),
        (NOW - 2 * 86400 - 3600, "2d 01h"),
    ],
)
def test_age_reads_as_a_person_says_it(created, shown):
    assert inventory.age(created, NOW) == shown


def test_session_roles_name_servers_phone_sessions_and_interactive_sessions():
    rows = {row.pid: row for row in table()}
    servers = reap_machine.daemons(table())
    assert inventory.session_role(rows[20], servers) == "server carameli (worktree)"
    assert inventory.session_role(rows[21], servers) == "phone session cse_ABCDEFGH... in carameli"
    assert inventory.session_role(rows[12], servers) == "interactive (worktree)"
    assert inventory.session_role(P(1, 0, "claude", f"{CLAUDE}"), {}) == "interactive"
    assert inventory.session_role(P(1, 0, "claude", f"{CLAUDE} -p hi"), {}) == "print"


def test_the_sessions_section_carries_idle_time_and_the_stray_flag(tmp_path):
    store = tmp_path / "projects"
    name = "C--ws-carameli--claude-worktrees-bridge-cse-ABCDEFGHIJKLMNOP"  # pragma: allowlist secret - fabricated session id, not a credential
    bridge = store / name
    bridge.mkdir(parents=True)
    (bridge / "a.jsonl").write_text("{}", encoding="utf-8")
    os.utime(bridge / "a.jsonl", (NOW - 600, NOW - 600))
    lines = inventory.sessions_section(table(), store, tmp_path, frozenset({20}), NOW)
    assert lines[0].startswith("  pid 12 ") and "interactive (worktree)" in lines[0]
    phone = next(line for line in lines if "phone session" in line)
    assert phone.endswith("idle 10 min")
    stray = next(line for line in lines if "server devkit" in line)
    assert stray.endswith("NOT in rc-servers state")
    served = next(line for line in lines if "server carameli" in line)
    assert "NOT in" not in served


def test_a_session_whose_activity_is_unknowable_says_so(tmp_path):
    lines = inventory.sessions_section(table(), tmp_path / "missing", None, frozenset(), NOW)
    assert any(line.endswith("activity unknown") for line in lines)


def test_an_empty_sessions_section_says_none():
    assert inventory.sessions_section([], "x", None, frozenset(), NOW) == ["  none"]


def test_the_mcp_section_sums_the_node_processes_that_are_mcp():
    assert (
        inventory.mcp_section(table())
        == "  2 node processes, 180 MB -- one set per session, started with it"
    )
    assert inventory.mcp_section([]) == "  none"


def test_dev_servers_carry_their_owner_or_the_reap_verdict():
    lines = inventory.dev_servers_section(table())
    orphan = next(line for line in lines if "pid 40 " in line)
    owned = next(line for line in lines if "pid 50 " in line)
    assert (
        "NO LIVING OWNER -- reap-stale would stop it" in orphan
        and "`node.exe vite.js --host`" in orphan
    )
    assert "owned by code" in owned
    assert inventory.dev_servers_section([]) == ["  none"]


def test_totals_read_the_commit_picture_and_name_paging():
    healthy = reclaim.Snapshot(free_gb=100, avail_gb=6.0, committed_gb=17.0, limit_gb=27.0)
    assert inventory.totals_line(healthy).endswith("(63%) -- commit headroom available")
    paging = reclaim.Snapshot(free_gb=100, avail_gb=2.0, committed_gb=25.0, limit_gb=27.0)
    assert "HIGH COMMIT" in inventory.totals_line(paging)
    assert (
        inventory.totals_line(reclaim.Snapshot(1, -1.0, -1.0, -1.0)) == "memory: could not be read"
    )


def test_reap_status_hands_back_the_reapers_own_lines(tmp_path):
    calls = []

    def run(argv):
        calls.append(list(argv))
        return subprocess.CompletedProcess(
            argv, 0, "# reap-stale x\nsession a: idle 9 min -- kept\n", ""
        )

    lines = inventory.reap_status(tmp_path, tmp_path / "w.code-workspace", run, python="py")
    assert lines == ["  session a: idle 9 min -- kept"]
    assert calls[0][:3] == ["py", str(inventory.REPO_ROOT / "scripts" / "reap-stale.py"), "status"]
    assert "--workspace" in calls[0] and "--devkit" in calls[0]


def test_reap_status_reports_a_reaper_that_failed_or_could_not_run(tmp_path):
    failed = lambda argv: subprocess.CompletedProcess(argv, 2, "", "")
    assert inventory.reap_status(tmp_path, None, failed, python="py") == ["  (reap-stale exited 2)"]

    def boom(argv):
        raise OSError("no python")

    assert "could not be asked" in inventory.reap_status(tmp_path, None, boom, python="py")[0]


# --- the page -------------------------------------------------------------------------


def test_render_leads_with_the_stamp_and_the_totals_then_each_section():
    when = _dt.datetime(2026, 9, 7, 16, 40)
    text = inventory.render(when, "commit 1 / 2", [("A:", ["  a1"]), ("B:", ["  b1"])])
    assert text == "# memory-inventory 2026-09-07T16:40:00\ncommit 1 / 2\n\nA:\n  a1\n\nB:\n  b1\n"


def test_the_artifact_lands_under_logs(tmp_path):
    path = inventory.write_artifact("x\n", tmp_path)
    assert path == tmp_path / inventory.ARTIFACT and path.read_text(encoding="utf-8") == "x\n"


def test_parse_args_defaults():
    args = inventory.parse_args([])
    assert args.workspace is None and args.top == inventory.TOP


def test_main_writes_the_page_and_touches_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(reap_machine, "process_table", lambda: table())
    monkeypatch.setattr(reclaim, "snapshot", lambda: reclaim.Snapshot(1, 6.0, 17.0, 27.0))
    monkeypatch.setattr(rc_machine, "sessions_store", lambda: tmp_path / "projects")
    monkeypatch.setattr(inventory, "reap_status", lambda root, workspace: ["  nothing left behind"])
    code = inventory.main(["--devkit", str(tmp_path), "--top", "2"])
    assert code == 0
    out = capsys.readouterr().out
    assert "Top 2 holders" in out and "claude processes:" in out and "nothing left behind" in out
    assert (tmp_path / inventory.ARTIFACT).is_file()


def test_main_reports_an_unreadable_table_and_exits_red(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(reap_machine, "process_table", lambda: None)
    assert inventory.main(["--devkit", str(tmp_path)]) == 2
    assert "could not be read" in capsys.readouterr().out


def test_run_command_captures():
    import sys

    assert inventory.run_command([sys.executable, "-c", "print(1)"]).stdout.strip() == "1"


def test_the_clock_the_sections_use_is_seconds():
    assert abs(time.time() - NOW) > 0  # the fixture is a fixed instant, not the clock
