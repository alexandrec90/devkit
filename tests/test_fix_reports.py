"""`scripts/fix_reports.py`: the stamp a dispatch leaves and the report a fixer writes back."""

from __future__ import annotations

import datetime as _dt
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fix_reports

NOW = _dt.datetime(2026, 9, 19, 9, 0, tzinfo=_dt.UTC)


def test_the_stamp_round_trips_and_a_missing_or_corrupt_one_reads_as_empty(tmp_path):
    assert fix_reports.read_stamp(tmp_path) == {}
    fix_reports.stamp(tmp_path, "pr:carameli:412:abc:d:dispatch", "1 check failing", NOW)
    assert fix_reports.read_stamp(tmp_path) == {
        "key": "pr:carameli:412:abc:d:dispatch",
        "what": "1 check failing",
        "when": "2026-09-19T09:00:00+00:00",
        "owns_branch": False,
    }
    (tmp_path / fix_reports.STAMP_FILE).write_text("{not json", encoding="utf-8")
    assert fix_reports.read_stamp(tmp_path) == {}


def test_a_restamp_keeps_whose_branch_it_is_unless_told(tmp_path):
    """A fixer sent back at a fixer's branch is still on the pass's branch; one sent at a
    feature session's is still on the feature's. Only the dispatch that cut it says so."""
    assert fix_reports.fixer_owns_branch(tmp_path) is False
    fix_reports.stamp(tmp_path, "a", "n", NOW, owns_branch=True)
    fix_reports.stamp(tmp_path, "b", "n", NOW)
    assert fix_reports.fixer_owns_branch(tmp_path) is True
    fix_reports.stamp(tmp_path, "c", "n", NOW, owns_branch=False)
    fix_reports.stamp(tmp_path, "d", "n", NOW)
    assert fix_reports.fixer_owns_branch(tmp_path) is False


def test_a_new_stamp_clears_the_report_an_earlier_session_left_in_the_tree(tmp_path):
    """Read against the new key, the old report marked the new dispatch blocked before
    its session had started -- and a blocked entry never expires."""
    (tmp_path / "logs").mkdir()
    (tmp_path / fix_reports.BLOCKED_FILE).write_text("needs a database\n", encoding="utf-8")
    fix_reports.stamp(tmp_path, "pr:carameli:412:new:d:dispatch", "n", NOW)
    assert fix_reports.blocked_reason(tmp_path) == ""
    assert fix_reports.read_stamp(tmp_path)["key"] == "pr:carameli:412:new:d:dispatch"


def test_a_blocked_report_is_its_first_lines_trimmed_and_absent_is_empty(tmp_path):
    assert fix_reports.blocked_reason(tmp_path) == ""
    (tmp_path / "logs").mkdir()
    (tmp_path / fix_reports.BLOCKED_FILE).write_text(
        "# Blocked\n\nThe fixture needs a database the runner lacks.\n" + "x" * 900,
        encoding="utf-8",
    )
    reason = fix_reports.blocked_reason(tmp_path)
    assert reason.startswith("Blocked The fixture needs a database")
    assert len(reason) <= fix_reports.REASON_LIMIT


def listing(*entries: tuple[Path, str]) -> str:
    return "".join(
        f"worktree {path.as_posix()}\nHEAD 1\nbranch refs/heads/{branch}\n\n"
        for path, branch in entries
    )


def test_agent_trees_walks_every_worktree_of_every_checkout_on_disk(tmp_path):
    (tmp_path / "carameli").mkdir()
    tree = tmp_path / "carameli" / ".claude" / "worktrees" / "x"
    detached = tmp_path / "carameli" / ".claude" / "worktrees" / "d"
    text = listing((tmp_path / "carameli", "main"), (tree, "agent/x")) + (
        f"worktree {detached.as_posix()}\nHEAD 3\ndetached\n"
    )

    def git_for(project_dir):
        def git(*args):
            code = 0 if project_dir.name == "carameli" else 1
            return subprocess.CompletedProcess(args, code, text if code == 0 else "", "")

        return git

    found = list(fix_reports.agent_trees(tmp_path, ["carameli", "devkit", "ghost"], git_for))
    assert found == [
        ("carameli", tmp_path / "carameli", "main"),
        ("carameli", tree, "agent/x"),
        ("carameli", detached, ""),
    ]


def test_blocked_reports_are_found_with_the_key_they_were_dispatched_under(tmp_path):
    (tmp_path / "carameli").mkdir()
    stamped = tmp_path / "carameli" / ".claude" / "worktrees" / "x"
    unstamped = tmp_path / "carameli" / ".claude" / "worktrees" / "y"
    quiet = tmp_path / "carameli" / ".claude" / "worktrees" / "z"
    for tree in (stamped, unstamped, quiet):
        (tree / "logs").mkdir(parents=True)
    fix_reports.stamp(stamped, "pr:carameli:412:abc:d:dispatch", "n", NOW)
    (stamped / fix_reports.BLOCKED_FILE).write_text("needs a database\n", encoding="utf-8")
    (unstamped / fix_reports.BLOCKED_FILE).write_text("no stamp here\n", encoding="utf-8")
    text = listing((stamped, "agent/x"), (unstamped, "agent/y"), (quiet, "agent/z"))

    def git_for(_project_dir):
        return lambda *a: subprocess.CompletedProcess(a, 0, text, "")

    found = fix_reports.find_blocked(tmp_path, ["carameli"], git_for)
    assert [(b.project, b.branch, b.key, b.reason) for b in found] == [
        ("carameli", "agent/x", "pr:carameli:412:abc:d:dispatch", "needs a database"),
        ("carameli", "agent/y", "", "no stamp here"),
    ]
    assert found[0].tree == stamped
