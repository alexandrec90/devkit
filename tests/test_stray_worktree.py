"""`scripts/stray_worktree.py`: releasing a tree that holds a PR branch and no work.

The path through `fix-prs.existing_tree` is in `tests/test_fix_prs.py`; what is here is
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


def test_the_checkout_itself_is_never_released(monkeypatch, tmp_path):
    calls = recording_git(monkeypatch)
    assert not stray_worktree.release(tmp_path, tmp_path, "agent/x")
    assert calls == []


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
