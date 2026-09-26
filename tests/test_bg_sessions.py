"""`scripts/bg_sessions.py`: a finished fixer's idle process is stopped, nothing else is."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import bg_sessions

ROWS = [
    {
        "id": "a1",
        "kind": "background",
        "status": "idle",
        "state": "done",
        "cwd": "C:\\ws\\devkit\\.claude\\worktrees\\x",
    },
    {
        "id": "b2",
        "kind": "background",
        "status": "busy",
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
        "status": "idle",
        "state": "blocked",
        "cwd": "C:\\ws\\carameli\\.claude\\worktrees\\y",
    },
]


def runner_for(rows, stops: list, listing_code: int = 0):
    def runner(argv, **_kwargs):
        if argv[1] == "agents":
            return subprocess.CompletedProcess(argv, listing_code, json.dumps(rows), "")
        stops.append(argv[2])
        return subprocess.CompletedProcess(argv, 0, f"stopped {argv[2]}", "")

    return runner


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


def test_finished_in_matches_paths_whatever_their_slashes_and_case():
    assert [
        r["id"] for r in bg_sessions.finished_in(ROWS, ["c:/WS/carameli/.claude/worktrees/y"])
    ] == ["d4"]
    assert bg_sessions.finished_in(ROWS, []) == []
