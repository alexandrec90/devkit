"""`scripts/branch_facts.py`: the tip, the behind check and the tag, off a git table."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import branch_facts as bf


def git_with(tip_code=0, known_code=0, ancestor_code=1):
    def git(*args):
        if args[0] == "rev-parse" and args[-1].startswith("refs/remotes/"):
            return subprocess.CompletedProcess(args, tip_code, "tip1\n", "")
        if args[0] == "rev-parse":
            return subprocess.CompletedProcess(args, known_code, "sha1\n", "")
        if args[0] == "merge-base":
            return subprocess.CompletedProcess(args, ancestor_code, "", "")
        raise AssertionError(args)

    return git


def test_the_tip_is_read_off_the_remote_ref_or_is_unknown():
    assert bf.branch_tip(git_with(), "main") == "tip1"
    assert bf.branch_tip(git_with(tip_code=128), "main") == ""


def test_behind_means_the_tip_is_not_an_ancestor_and_unknown_never_means_behind():
    """#379 was red on a pip-audit finding master had already fixed; the session sent
    at it did nothing but merge master in. A sha this checkout has not fetched, or a
    tip git cannot resolve, is not evidence of anything."""
    assert bf.is_behind(git_with(), "main", "sha1") is True
    assert bf.is_behind(git_with(ancestor_code=0), "main", "sha1") is False
    assert bf.is_behind(git_with(known_code=128), "main", "sha1") is False
    assert bf.is_behind(git_with(tip_code=128), "main", "sha1") is False
    assert bf.is_behind(git_with(), "main", "") is False


def repo_with(tmp_path, ours: str, theirs: str):
    """A repo whose `origin/main` and `feature` each rewrote one file from a shared root;
    `(git, feature sha)`."""
    tmp_path.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def commit(path: str, text: str) -> str:
        (tmp_path / path).write_text(text, encoding="utf-8")
        git("add", "-A")
        git("commit", "-q", "-m", path)
        return git("rev-parse", "HEAD").stdout.strip()

    git("init", "-q", "-b", "main")
    commit("a.txt", "1\n")
    root = commit("b.txt", "1\n")
    git("update-ref", "refs/remotes/origin/main", commit(theirs, "main\n"))
    git("checkout", "-q", "-b", "feature", root)
    return git, commit(ours, "feature\n")


def test_a_clean_merge_is_the_tree_git_writes_and_a_conflict_is_nothing(tmp_path):
    """#538 read `CONFLICTING` on GitHub while git merged it cleanly: git's verdict is
    the one asked for, against the remote-tracking base."""
    git, sha = repo_with(tmp_path / "clean", "a.txt", "b.txt")
    tree = bf.clean_merge(git, "main", sha)
    assert git("cat-file", "-t", tree).stdout.strip() == "tree"
    assert git("show", f"{tree}:b.txt").stdout == "main\n", "the base's side is in it"
    git, sha = repo_with(tmp_path / "torn", "a.txt", "a.txt")
    assert bf.clean_merge(git, "main", sha) == ""


def test_a_merge_with_an_unknown_side_is_never_called_clean():
    assert bf.clean_merge(git_with(tip_code=128), "main", "sha1") == ""
    assert bf.clean_merge(lambda *a: pytest.fail("no sha, no call"), "main", "") == ""

    def unknown_sha(*args):
        if args[0] == "rev-parse":
            return subprocess.CompletedProcess(args, 0, "tip1\n", "")
        return subprocess.CompletedProcess(args, 128, "", "fatal: not a valid object name")

    assert bf.clean_merge(unknown_sha, "main", "sha1") == ""


def test_a_tag_pointing_at_the_sha_is_the_release_workflows_verdict():
    assert bf.is_tagged(lambda *a: subprocess.CompletedProcess(a, 0, "v0.11.23\n", ""), "fb17")
    assert not bf.is_tagged(lambda *a: subprocess.CompletedProcess(a, 0, "\n", ""), "fb17")
    assert not bf.is_tagged(lambda *a: subprocess.CompletedProcess(a, 1, "", ""), "fb17")
    assert not bf.is_tagged(lambda *a: pytest.fail("no sha, no call"), "")
