"""`scripts/disposable_tree.py`: a detached tree at one commit, gone when the run is."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import disposable_tree as dt


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    checkout = tmp_path / "project"
    checkout.mkdir()
    _git(checkout, "init", "--quiet", "--initial-branch=main")
    # The machine's global git policy refuses `main`, and would provision each tree.
    _git(checkout, "config", "core.hooksPath", str(tmp_path / "no-hooks"))
    (checkout / "a.txt").write_text("a\n", encoding="utf-8")
    _git(checkout, "add", "a.txt")
    _git(checkout, "commit", "--quiet", "-m", "first")
    return checkout, _git(checkout, "rev-parse", "HEAD")


def test_a_tree_is_cut_detached_at_the_commit_in_the_default_tier(repo):
    """There, because a project's `../sibling` path dependency resolves there."""
    checkout, sha = repo
    tree, why = dt.cut(checkout, sha)
    assert why == ""
    assert tree == checkout / ".claude" / "worktrees" / f"disposable-{sha[:12]}"
    assert tree == dt.path_for(checkout, sha)
    assert (tree / "a.txt").is_file()
    assert _git(tree, "rev-parse", "HEAD") == sha
    assert _git(tree, "branch", "--show-current") == ""


def test_a_commit_git_cannot_find_says_why(repo):
    checkout, _sha = repo
    tree, why = dt.cut(checkout, "0" * 40)
    assert tree is None
    assert why


def test_destroy_leaves_neither_the_directory_nor_git_s_record(repo):
    checkout, sha = repo
    tree, _ = dt.cut(checkout, sha)
    (tree / "logs").mkdir()
    (tree / "logs" / "untracked.log").write_text("x", encoding="utf-8")
    assert dt.destroy(checkout, tree) == ""
    assert not tree.exists()
    assert "disposable-" not in _git(checkout, "worktree", "list")


def test_a_tree_git_will_not_remove_is_deleted_by_hand_and_pruned(repo):
    checkout, sha = repo
    tree, _ = dt.cut(checkout, sha)
    asked = []

    def refusing(argv, timeout):
        asked.append(argv[3:5])
        if argv[3:5] == ["worktree", "remove"]:
            return subprocess.CompletedProcess(argv, 1, "", "no")
        return dt.sweep.run_bounded(argv, timeout)

    assert dt.destroy(checkout, tree, refusing) == ""
    assert not tree.exists()
    assert asked[-1] == ["worktree", "prune"]
    assert "disposable-" not in _git(checkout, "worktree", "list")


def test_a_refused_delete_sets_aside_what_a_mapped_link_holds(repo, monkeypatch):
    """uv hard links plus a mapped `.pyd` defeat a plain delete: `box_teardown._set_aside`
    documents the trap, and only `set_aside=True` gets past it."""
    checkout, sha = repo
    tree, _ = dt.cut(checkout, sha)
    asked = []
    real = dt.box_teardown.remove_tree_longpath

    def remove(path, set_aside=False):
        asked.append(set_aside)
        return real(path, set_aside)

    monkeypatch.setattr(dt.box_teardown, "remove_tree_longpath", remove)

    def refusing(argv, timeout):
        if argv[3:5] == ["worktree", "remove"]:
            return subprocess.CompletedProcess(argv, 1, "", "no")
        return dt.sweep.run_bounded(argv, timeout)

    assert dt.destroy(checkout, tree, refusing) == ""
    assert asked == [True]


def test_a_tree_that_cannot_be_deleted_is_named_and_git_keeps_its_record(repo, monkeypatch):
    """`worktree prune` would forget the husk, and nothing could then find it."""
    checkout, sha = repo
    tree, _ = dt.cut(checkout, sha)
    monkeypatch.setattr(
        dt.box_teardown, "remove_tree_longpath", lambda _p, set_aside=False: "a.pyd: in use"
    )
    asked = []

    def refusing(argv, _timeout):
        asked.append(argv[3:5])
        return subprocess.CompletedProcess(argv, 1, "", "no")

    assert dt.destroy(checkout, tree, refusing) == f"could not remove {tree}: a.pyd: in use"
    assert ["worktree", "prune"] not in asked


def test_a_husk_git_has_forgotten_is_swept_from_disk(repo):
    """A re-cut at that sha would otherwise fail every fire."""
    checkout, sha = repo
    husk = dt.path_for(checkout, sha)
    (husk / ".venv").mkdir(parents=True)
    (husk / ".venv" / "a.pyd").write_text("x", encoding="utf-8")
    assert dt.sweep_leftovers(checkout) == []
    assert not husk.exists()
    tree, why = dt.cut(checkout, sha)
    assert (tree, why) == (husk, "")


def test_a_cut_killed_mid_way_does_not_stop_the_next_one(repo):
    """`git worktree add` locks a tree `initializing`, and a killed one leaves that lock
    on a directory that is gone: only a second `--force` overrides it."""
    checkout, sha = repo
    tree = dt.path_for(checkout, sha)
    _git(checkout, "worktree", "add", "--quiet", "--detach", str(tree), sha)
    _git(checkout, "worktree", "lock", "--reason", "initializing", str(tree))
    shutil.rmtree(tree)
    cut, why = dt.cut(checkout, sha)
    assert (cut, why) == (tree, "")


def test_a_tree_locked_initializing_can_still_be_removed(repo):
    checkout, sha = repo
    tree, _ = dt.cut(checkout, sha)
    _git(checkout, "worktree", "lock", "--reason", "initializing", str(tree))
    assert dt.destroy(checkout, tree) == ""
    assert "disposable-" not in _git(checkout, "worktree", "list")


def test_leftovers_are_this_module_s_trees_and_no_one_else_s(tmp_path):
    root = tmp_path / ".claude" / "worktrees"
    root.mkdir(parents=True)
    ours = root / "disposable-abc"
    porcelain = "\n".join(
        [
            f"worktree {tmp_path}",
            "branch refs/heads/main",
            "",
            f"worktree {ours}",
            "detached",
            "",
            f"worktree {root / 'a-session'}",
            "detached",
            "",
            f"worktree {tmp_path / 'elsewhere' / 'disposable-abc'}",
            "detached",
        ]
    )
    assert dt.leftovers(tmp_path, porcelain) == [ours.resolve()]
    husk = root / "disposable-def"
    husk.mkdir()
    (root / "a-session-dir").mkdir()
    assert dt.leftovers(tmp_path, porcelain) == [ours.resolve(), husk.resolve()]


def test_what_a_crash_left_is_swept_and_anything_else_is_kept(repo):
    checkout, sha = repo
    tree, _ = dt.cut(checkout, sha)
    keeper = checkout / ".claude" / "worktrees" / "a-session"
    _git(checkout, "worktree", "add", "--quiet", "--detach", str(keeper), sha)
    assert dt.sweep_leftovers(checkout) == []
    assert not tree.exists()
    assert keeper.is_dir()


def test_a_sweep_git_cannot_answer_touches_nothing(repo):
    checkout, _sha = repo

    def failing(argv, _timeout):
        return subprocess.CompletedProcess(argv, 1, "", "no")

    assert dt.sweep_leftovers(checkout, failing) == []


def test_a_tree_the_hook_provisioned_is_not_provisioned_again(tmp_path, monkeypatch):
    monkeypatch.setattr(dt.tree_provision, "needs_provision", lambda _t: False)
    called = []
    monkeypatch.setattr(dt.tree_provision, "provision", lambda *a: called.append(a))
    assert dt.provision(tmp_path) == ""
    assert called == []


def test_provisioning_is_bounded_and_says_why_it_failed(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(dt.tree_provision, "needs_provision", lambda _t: True)
    seen = {}

    def runner(argv, timeout, **kwargs):
        seen.update(argv=argv, timeout=timeout, kwargs=kwargs)
        return subprocess.CompletedProcess(argv, 1, "", "uv: no network")

    assert dt.provision(tmp_path, runner) == "uv: no network"
    assert seen["timeout"] == dt.PROVISION_TIMEOUT
    assert "provision" in seen["argv"]
    assert "check" not in seen["kwargs"]
    assert "capture_output" not in seen["kwargs"]
    assert "uv: no network" in capsys.readouterr().err


def test_a_lock_that_does_not_resolve_is_named_over_the_last_line(tmp_path, monkeypatch):
    monkeypatch.setattr(dt.tree_provision, "needs_provision", lambda _t: True)
    said = "error: No solution found when resolving dependencies:\n  Because x needs y\n"

    def runner(argv, _timeout, **_kwargs):
        return subprocess.CompletedProcess(argv, 1, "", said)

    assert "No solution found" in dt.provision(tmp_path, runner)
