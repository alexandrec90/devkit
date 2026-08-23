"""Tests for `scripts/git-sync-keep.py` — rebase the current branch, keep local work.

Everything worth asserting here is about what happens when a step *fails*, because the
script's reason for existing is that the naive spelling loses work. It stashes before
it rebases, so every early return after that point has to leave the user a sentence
saying where their changes went; a silent `return 1` between the stash and the pop is
a branch that ate uncommitted work and said nothing.

Git is stubbed rather than driven against a real repo. A real repo would test git, and
what can actually break here is the ordering and the messages — which a fake records
exactly and a real repo makes hard to provoke (`stash pop` conflicting on demand is
several fixtures of setup for one assertion).
"""

from __future__ import annotations

import subprocess

import pytest
from support import load_script

git_sync_keep = load_script("scripts/git-sync-keep.py")


class FakeGit:
    """Records the git argv it is handed and answers from a scripted table.

    `responses` maps the leading words of a command to `(returncode, stdout)`; anything
    unlisted succeeds with empty output, so a test only spells out what it cares about.
    """

    def __init__(self, responses: dict[tuple[str, ...], tuple[int, str]] | None = None):
        self.responses = responses or {}
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        for prefix, (code, stdout) in self.responses.items():
            if args[: len(prefix)] == prefix:
                return subprocess.CompletedProcess(list(args), code, stdout, "")
        return subprocess.CompletedProcess(list(args), 0, "", "")

    def ran(self, *prefix: str) -> bool:
        return any(call[: len(prefix)] == prefix for call in self.calls)


@pytest.fixture
def fake_git(monkeypatch):
    def install(responses=None):
        fake = FakeGit(responses)
        monkeypatch.setattr(git_sync_keep, "git", fake)
        return fake

    return install


HEALTHY = {
    ("rev-parse", "--show-toplevel"): (0, "/repo"),
    ("branch", "--show-current"): (0, "agent/thing-0821"),
    ("symbolic-ref",): (0, "refs/remotes/origin/main"),
    ("status", "--porcelain"): (0, ""),
}


# --- out ----------------------------------------------------------------------


def test_out_returns_the_code_and_the_stripped_stdout(fake_git):
    fake_git({("branch",): (0, "  agent/thing-0821 \n")})
    assert git_sync_keep.out("branch", "--show-current") == (0, "agent/thing-0821")


def test_out_forwards_stderr_to_the_terminal_on_failure(monkeypatch, capsys):
    """The caller only gets a code, so an unrelayed stderr is a failure with no reason."""

    def failing(*args, capture=False):
        return subprocess.CompletedProcess(list(args), 128, "", "fatal: not a git repository\n")

    monkeypatch.setattr(git_sync_keep, "git", failing)
    assert git_sync_keep.out("status")[0] == 128
    assert "fatal: not a git repository" in capsys.readouterr().err


# --- default_branch -----------------------------------------------------------


def test_default_branch_reads_origin_head(fake_git):
    fake_git({("symbolic-ref",): (0, "refs/remotes/origin/master")})
    assert git_sync_keep.default_branch() == "master"


def test_default_branch_populates_origin_head_before_giving_up(fake_git):
    """A fresh clone has no `origin/HEAD`; asking the remote is cheaper than guessing."""
    fake = fake_git({("symbolic-ref",): (1, ""), ("rev-parse", "--verify"): (1, "")})
    assert git_sync_keep.default_branch() is None
    assert fake.ran("remote", "set-head", "origin", "--auto")


def test_default_branch_probes_the_usual_suspects_last(fake_git):
    """The remote can refuse to answer offline; a repo whose default is `master` must
    not be rebased onto a `main` that does not exist."""
    fake_git(
        {
            ("symbolic-ref",): (1, ""),
            ("rev-parse", "--verify", "--quiet", "refs/remotes/origin/main"): (1, ""),
            ("rev-parse", "--verify", "--quiet", "refs/remotes/origin/master"): (0, "abc"),
        }
    )
    assert git_sync_keep.default_branch() == "master"


def test_default_branch_gives_up_rather_than_guessing(fake_git):
    fake_git({("symbolic-ref",): (1, ""), ("rev-parse", "--verify"): (1, "")})
    assert git_sync_keep.default_branch() is None


# --- main ---------------------------------------------------------------------


def test_a_clean_branch_is_fetched_and_rebased_without_a_stash(fake_git):
    fake = fake_git(HEALTHY)
    assert git_sync_keep.main() == 0
    assert fake.ran("fetch", "--prune", "origin")
    assert fake.ran("rebase", "origin/main")
    assert not fake.ran("stash", "push")


def test_dirty_work_is_stashed_and_restored_around_the_rebase(fake_git):
    fake = fake_git({**HEALTHY, ("status", "--porcelain"): (0, " M app/main.py")})
    assert git_sync_keep.main() == 0
    order = [call[0] for call in fake.calls]
    assert order.index("stash") < order.index("rebase") < len(order) - 1
    assert fake.ran("stash", "pop")


def test_outside_a_repository_it_refuses(fake_git, capsys):
    fake = fake_git({("rev-parse", "--show-toplevel"): (128, "")})
    assert git_sync_keep.main() == 1
    assert "Not inside a git repository" in capsys.readouterr().err
    assert not fake.ran("fetch")


def test_a_detached_head_is_refused_before_anything_is_fetched(fake_git, capsys):
    """Rebasing a detached HEAD onto the default branch is how the work disappears."""
    fake = fake_git({**HEALTHY, ("branch", "--show-current"): (0, "")})
    assert git_sync_keep.main() == 1
    assert "detached" in capsys.readouterr().err
    assert not fake.ran("fetch")


def test_an_unresolvable_default_branch_stops_before_the_stash(fake_git):
    fake = fake_git({**HEALTHY, ("symbolic-ref",): (1, ""), ("rev-parse", "--verify"): (1, "")})
    assert git_sync_keep.main() == 1
    assert not fake.ran("stash", "push")


def test_a_failed_stash_aborts_rather_than_rebasing_over_the_work(fake_git, capsys):
    fake = fake_git(
        {
            **HEALTHY,
            ("status", "--porcelain"): (0, " M app/main.py"),
            ("stash", "push"): (1, ""),
        }
    )
    assert git_sync_keep.main() == 1
    assert "aborting so nothing is lost" in capsys.readouterr().err
    assert not fake.ran("rebase")


def test_a_conflicting_rebase_says_where_the_stash_is(fake_git, capsys):
    """The regression this guards: the script returns 1 mid-flight with the user's work
    in a stash they were never told about."""
    fake_git(
        {
            **HEALTHY,
            ("status", "--porcelain"): (0, " M app/main.py"),
            ("rebase",): (1, ""),
        }
    )
    assert git_sync_keep.main() == 1
    err = capsys.readouterr().err
    assert git_sync_keep.STASH_MSG in err
    assert "git stash pop" in err


def test_a_conflicting_rebase_on_a_clean_tree_mentions_no_stash(fake_git, capsys):
    fake_git({**HEALTHY, ("rebase",): (1, "")})
    assert git_sync_keep.main() == 1
    err = capsys.readouterr().err
    assert "rebase --abort" in err
    assert git_sync_keep.STASH_MSG not in err


def test_a_conflicting_pop_leaves_the_work_in_the_tree_and_says_so(fake_git, capsys):
    fake_git(
        {
            **HEALTHY,
            ("status", "--porcelain"): (0, " M app/main.py"),
            ("stash", "pop"): (1, ""),
        }
    )
    assert git_sync_keep.main() == 1
    assert "preserved in the working tree" in capsys.readouterr().err
