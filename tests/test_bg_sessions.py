"""`scripts/bg_sessions.py`: a finished fixer's idle process is stopped, nothing else is."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import bg_sessions

# As `claude agents --json` (2.1.296) prints them: an interactive row carries `status`
# and a background row only `state` -- the fixture once gave background rows both.
ROWS = [
    {
        "id": "a1",
        "kind": "background",
        "state": "done",
        "cwd": "C:\\ws\\devkit\\.claude\\worktrees\\x",
    },
    {
        "id": "b2",
        "kind": "background",
        "state": "working",
        "cwd": "C:\\ws\\devkit\\.claude\\worktrees\\x",
    },
    {
        "id": "c3",
        "kind": "interactive",
        "status": "idle",
        "cwd": "C:\\ws\\devkit\\.claude\\worktrees\\x",
    },
    {
        "id": "d4",
        "kind": "background",
        "state": "blocked",
        "cwd": "C:\\ws\\carameli\\.claude\\worktrees\\y",
    },
]


def test_a_background_row_s_state_is_read_as_its_status():
    """c476bac3 sat `blocked` for a day in a finished rescue tree, holding the memory the
    pass then held a devkit fixer for: background rows carry no `status`, so nothing read
    as idle and nothing was ever stopped. An unknown state is busy -- never stopped and
    never raced on a guess."""
    assert [bg_sessions.status(row) for row in ROWS] == ["idle", "busy", "idle", "idle"]
    assert bg_sessions.status({"kind": "background", "state": "compacting"}) == "busy"
    assert bg_sessions.status({"kind": "background", "status": "idle", "state": "x"}) == "idle"
    assert bg_sessions.status({"kind": "background"}) == ""


def runner_for(rows, stops: list, listing_code: int = 0):
    def runner(argv, **_kwargs):
        if argv[1] == "agents":
            return subprocess.CompletedProcess(argv, listing_code, json.dumps(rows), "")
        stops.append(argv[2])
        return subprocess.CompletedProcess(argv, 0, f"stopped {argv[2]}", "")

    return runner


def test_a_tree_is_working_while_any_session_in_it_is_busy_whatever_its_kind():
    rows = [*ROWS, {"kind": "interactive", "status": "busy", "cwd": "C:\\ws\\carameli"}]
    dirs = bg_sessions.working(rows)
    assert bg_sessions.busy_in(dirs, "c:/WS/devkit/.claude/worktrees/x/")
    assert bg_sessions.busy_in(dirs, Path("C:/ws/carameli"))
    assert not bg_sessions.busy_in(dirs, "C:/ws/carameli/.claude/worktrees/y")
    assert bg_sessions.working([]) == frozenset()
    background = bg_sessions.working(ROWS, kinds=("background",))
    assert bg_sessions.busy_in(background, "C:/ws/devkit/.claude/worktrees/x"), "b2 is working"


def test_only_idle_background_sessions_in_finished_trees_are_stopped():
    """Never an interactive session, never one still busy, never one in a tree whose
    stamped session is still working."""
    stops: list = []
    tree = "C:/ws/devkit/.claude/worktrees/x/"
    assert bg_sessions.stop_finished([tree], runner_for(ROWS, stops)) == [
        "a1 in C:\\ws\\devkit\\.claude\\worktrees\\x"
    ]
    assert stops == ["a1"]


def test_nothing_is_stopped_when_the_listing_cannot_be_read():
    stops: list = []
    assert (
        bg_sessions.stop_finished(["C:/ws/devkit/.claude/worktrees/x"], runner_for(ROWS, stops, 1))
        == []
    )
    assert stops == []

    def missing(argv, **_kwargs):
        raise FileNotFoundError("claude")

    assert bg_sessions.listed(missing) == []
    garbage = lambda argv, **_k: subprocess.CompletedProcess(argv, 0, "not json", "")
    assert bg_sessions.listed(garbage) == []


def test_in_tree_is_every_session_in_the_tree_or_under_it_and_nothing_beside_it():
    rows = [*ROWS, {"id": "e5", "cwd": "C:\\ws\\devkit\\.claude\\worktrees\\x\\app"}]
    rows.append({"id": "f6", "cwd": "C:\\ws\\devkit\\.claude\\worktrees\\x-2"})
    found = bg_sessions.in_tree(rows, "c:/WS/devkit/.claude/worktrees/x/")
    assert [r["id"] for r in found] == ["a1", "b2", "c3", "e5"]


def test_only_an_idle_background_session_is_stoppable():
    assert [r["id"] for r in ROWS if bg_sessions.stoppable(r)] == ["a1", "d4"]


def test_stop_is_false_where_claude_refuses_or_is_missing():
    def refuses(argv, **_kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "no such session")

    def missing(argv, **_kwargs):
        raise FileNotFoundError("claude")

    assert bg_sessions.stop("a1", refuses) is False
    assert bg_sessions.stop("a1", missing) is False
    assert bg_sessions.stop("a1", lambda argv, **_k: subprocess.CompletedProcess(argv, 0)) is True


def test_finished_in_matches_paths_whatever_their_slashes_and_case():
    assert [
        r["id"] for r in bg_sessions.finished_in(ROWS, ["c:/WS/carameli/.claude/worktrees/y"])
    ] == ["d4"]
    assert bg_sessions.finished_in(ROWS, []) == []
