"""`scripts/stray_worktree.py`: releasing a tree that holds a PR branch and no work.

The path through `fix_trees.existing_tree` is in `tests/test_fix_prs.py`; what is here is
the two guards that never ask git at all, and the refusal's own wording.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from support import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import stray_worktree

CLEAN_AND_PUSHED = {
    ("status", "--porcelain"): (0, ""),
    ("rev-list", "--count", "agent/x@{u}..agent/x"): (0, "0\n"),
    ("worktree", "remove"): (0, ""),
}


def recording_git(monkeypatch) -> list[tuple[str, tuple[str, ...]]]:
    """Answer every tree as clean and pushed; return every `(dir, argv)` git was asked."""
    calls: list[tuple[str, tuple[str, ...]]] = []

    def git_for(path):
        def git(*args):
            calls.append((Path(path).name, args))
            code, out = CLEAN_AND_PUSHED.get(args[:2] if args[0] == "worktree" else args, (1, ""))
            return subprocess.CompletedProcess(list(args), code, stdout=out, stderr="")

        return git

    monkeypatch.setattr(stray_worktree.sweep, "git_for", git_for)
    return calls


def test_a_clean_pushed_tree_is_released(monkeypatch, tmp_path):
    calls = recording_git(monkeypatch)
    held = tmp_path / "scratchpad" / "unwire"
    assert stray_worktree.release(tmp_path / "carameli", held, "agent/x")
    assert ("carameli", ("worktree", "remove", str(held))) in calls


def checkout_on_pr_branch(tmp_path, *, dirty: bool = False) -> Path:
    """A real clone parked on `agent/x`, pushed; `dirty` leaves a file only it has."""

    def git(where, *args):
        identity = ["-c", "user.name=t", "-c", "user.email=t@t"]
        identity += ["-c", f"core.hooksPath={tmp_path / 'no-hooks'}"]
        subprocess.run(["git", "-C", str(where), *identity, *args], check=True, capture_output=True)

    origin, checkout = tmp_path / "origin.git", tmp_path / "tvshell"
    origin.mkdir()
    git(origin, "init", "--bare", "-b", "main")
    git(tmp_path, "clone", str(origin), str(checkout))
    git(checkout, "checkout", "-b", "main")
    git(checkout, "commit", "--allow-empty", "-m", "one")
    git(checkout, "push", "-u", "origin", "main")
    git(checkout, "remote", "set-head", "origin", "main")
    git(checkout, "checkout", "-b", "agent/x")
    git(checkout, "push", "-u", "origin", "agent/x")
    if dirty:
        (checkout / "new.txt").write_text("work", encoding="utf-8")
    return checkout


def head_of(checkout: Path) -> str:
    return (stray_worktree.sweep.git_for(checkout)("branch", "--show-current").stdout or "").strip()


def test_a_clean_pushed_checkout_is_parked_on_its_default_branch(monkeypatch, tmp_path):
    """aeee2754: tvshell's checkout sat on #16's branch, so #16's conflict got no resolver."""
    monkeypatch.setattr(stray_worktree.fix_reports, "active_transcript", lambda *_: None)
    checkout = checkout_on_pr_branch(tmp_path)
    assert stray_worktree.refusal(checkout, checkout.as_posix(), "agent/x") == ""
    assert head_of(checkout) == "main"
    assert checkout.is_dir(), "parked, never removed"


def test_a_checkout_holding_work_is_not_parked(monkeypatch, tmp_path):
    monkeypatch.setattr(stray_worktree.fix_reports, "active_transcript", lambda *_: None)
    checkout = checkout_on_pr_branch(tmp_path, dirty=True)
    assert not stray_worktree.release(checkout, checkout, "agent/x")
    assert head_of(checkout) == "agent/x"


def test_a_checkout_a_session_is_at_work_in_is_not_parked(monkeypatch, tmp_path):
    monkeypatch.setattr(stray_worktree.fix_reports, "active_transcript", lambda *_: tmp_path)
    checkout = checkout_on_pr_branch(tmp_path)
    assert not stray_worktree.park(checkout, "agent/x")
    assert head_of(checkout) == "agent/x"


def test_a_box_is_never_released_so_its_lease_does_not_leak(monkeypatch, tmp_path):
    calls = recording_git(monkeypatch)
    box = tmp_path / stray_worktree.wt.BOXES_DIR_NAME / "carameli-x"
    assert not stray_worktree.release(tmp_path / "carameli", box, "agent/x")
    assert calls == []


def test_the_refusal_is_empty_once_released_and_names_the_directory_otherwise(
    monkeypatch, tmp_path
):
    recording_git(monkeypatch)
    checkout = tmp_path / "carameli"
    assert stray_worktree.refusal(checkout, "C:/scratch/unwire", "agent/x") == ""
    box = (tmp_path / stray_worktree.wt.BOXES_DIR_NAME / "x").as_posix()
    refused = stray_worktree.refusal(checkout, box, "agent/x")
    assert box in refused
    assert stray_worktree.aw.TIER_SUMMARY in refused
