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


def test_a_tag_pointing_at_the_sha_is_the_release_workflows_verdict():
    assert bf.is_tagged(lambda *a: subprocess.CompletedProcess(a, 0, "v0.11.23\n", ""), "fb17")
    assert not bf.is_tagged(lambda *a: subprocess.CompletedProcess(a, 0, "\n", ""), "fb17")
    assert not bf.is_tagged(lambda *a: subprocess.CompletedProcess(a, 1, "", ""), "fb17")
    assert not bf.is_tagged(lambda *a: pytest.fail("no sha, no call"), "")
