"""`scripts/after_merge_tidy.py`: one PR a day takes finished task files out of the repo."""

from __future__ import annotations

import datetime as _dt
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from support import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import after_merge as am
import after_merge_tidy as tidy
import job_claims as jc
import sweep

NOW = 1_800_000_000.0
TODAY = _dt.datetime.fromtimestamp(NOW, _dt.UTC).date().isoformat()
LATER = NOW + 2 * am.DAY_SECONDS


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def out(cwd: Path, *args: str) -> str:
    return git(cwd, *args).stdout.strip()


@dataclass
class Seams:
    now_at: float = NOW
    host: str = "pc-a"
    gh_calls: list[tuple[str, ...]] = field(default_factory=list)
    open_prs: str = "[]"
    create_code: int = 0
    git_for = staticmethod(sweep.git_for)

    def now(self) -> float:
        return self.now_at

    def gh_for(self, path: Path):
        def gh(*args):
            self.gh_calls.append(args)
            if args[:2] == ("pr", "list"):
                return subprocess.CompletedProcess(args, 0, self.open_prs, "")
            if args[:2] == ("pr", "create"):
                said = "https://github.com/x/y/pull/9\n"
                return subprocess.CompletedProcess(args, self.create_code, said, "no")
            return subprocess.CompletedProcess(args, 0, "", "")

        return gh


@dataclass
class Cycle:
    seams: Seams
    lines: list[str] = field(default_factory=list)
    hosts: frozenset[str] = frozenset()

    def say(self, line: str) -> None:
        self.lines.append(line)


@dataclass
class Board:
    project: str
    path: Path
    base: str
    flow: str
    tasks: tuple
    refs: tuple
    done: frozenset[str]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A checkout whose `origin` holds two queue files, one of them done."""
    config = tmp_path / "gitconfig"
    config.write_text("[user]\n\tname = t\n\temail = t@example.com\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    path = tmp_path / "checkout"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "remote", "add", "origin", remote.as_posix())
    queue = path / am.QUEUE_DIR
    queue.mkdir()
    (queue / "finished.toml").write_text('run = "a"\n', encoding="utf-8")
    (queue / "waiting.toml").write_text('run = "b"\n', encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "--no-verify", "-m", "queue")
    git(path, "push", "-q", "--no-verify", "origin", "main")
    git(path, "fetch", "-q", "origin", "+refs/heads/*:refs/remotes/origin/*")
    return path


def board(path: Path, flow: str = am.GATED, refs: tuple = ()) -> Board:
    finished = am.parse("p", f"{am.QUEUE_DIR}/finished.toml", "a" * 40, 'run = "a"')
    waiting = am.parse("p", f"{am.QUEUE_DIR}/waiting.toml", "b" * 40, 'run = "b"')
    return Board("p", path, "main", flow, (finished, waiting), refs, frozenset({finished.id}))


def branch_on_remote(path: Path) -> str:
    listed = out(path, "ls-remote", "--heads", "origin", "agent/*")
    return listed.split("refs/heads/")[1] if listed else ""


def test_it_opens_one_pr_removing_only_what_is_finished(repo):
    cycle = Cycle(Seams())
    tidy.daily(board(repo), cycle, {})
    head = branch_on_remote(repo)
    assert head.startswith("agent/after-merge-tidy")
    git(repo, "fetch", "-q", "origin", head)
    files = out(repo, "ls-tree", "-r", "--name-only", "FETCH_HEAD").splitlines()
    assert files == [f"{am.QUEUE_DIR}/waiting.toml"]
    assert out(repo, "merge-base", "origin/main", "FETCH_HEAD") == out(
        repo, "rev-parse", "origin/main"
    )
    created = next(call for call in cycle.seams.gh_calls if call[:2] == ("pr", "create"))
    assert "automerge" in created and f"{am.QUEUE_DIR}/finished.toml" in " ".join(created)
    assert "pull/9" in cycle.lines[0]


def test_a_direct_project_labels_it_to_merge_at_once(repo):
    cycle = Cycle(Seams())
    tidy.daily(board(repo, am.DIRECT), cycle, {})
    created = next(call for call in cycle.seams.gh_calls if call[:2] == ("pr", "create"))
    assert "merge-now" in created
    assert any(call[:3] == ("label", "create", "merge-now") for call in cycle.seams.gh_calls)


def test_it_runs_at_most_daily_unless_asked(repo):
    tidied: dict[str, str] = {}
    cycle = Cycle(Seams())
    tidy.daily(board(repo), cycle, tidied)
    assert tidied == {"p": TODAY}
    calls = len(cycle.seams.gh_calls)
    tidy.daily(board(repo), cycle, tidied)
    assert len(cycle.seams.gh_calls) == calls
    tidy.daily(board(repo), cycle, tidied, force=True)
    assert len(cycle.seams.gh_calls) > calls


def test_an_open_tidy_is_left_to_land_rather_than_joined(repo):
    listing = json.dumps([{"headRefName": "agent/after-merge-tidy-0101"}])
    cycle = Cycle(Seams(open_prs=listing))
    tidy.daily(board(repo), cycle, {})
    assert "waits on its open PR" in cycle.lines[0]
    assert not branch_on_remote(repo)


def test_someone_elses_open_pr_does_not_stop_it():
    assert not tidy.open_tidy(json.dumps([{"headRefName": "agent/fix-thing-0101"}]))
    assert not tidy.open_tidy("[]")
    assert not tidy.open_tidy(None)


def test_a_second_pc_the_same_day_opens_nothing(repo):
    first = Cycle(Seams())
    tidy.daily(board(repo), first, {})
    second = Cycle(Seams(host="pc-b"))
    tidy.daily(board(repo), second, {})
    assert "tidy not claimed" in second.lines[0]
    assert not [call for call in second.seams.gh_calls if call[:2] == ("pr", "create")]


def test_nothing_finished_opens_nothing(repo):
    unfinished = board(repo)
    unfinished.done = frozenset()
    cycle = Cycle(Seams())
    tidy.daily(unfinished, cycle, {})
    assert cycle.lines == [] and cycle.seams.gh_calls == []


def test_a_failed_pr_is_said_rather_than_swallowed(repo):
    cycle = Cycle(Seams(create_code=1))
    tidy.daily(board(repo), cycle, {})
    assert "could not open its PR" in cycle.lines[0]


def test_a_push_that_fails_is_said(repo):
    git(repo, "remote", "set-url", "--push", "origin", "../gone.git")
    cycle = Cycle(Seams())
    tidy.daily(board(repo), cycle, {})
    assert "tidy" in cycle.lines[0] and "pull/9" not in cycle.lines[0]


def test_a_broken_commit_is_said(repo):
    gone = am.parse("p", f"{am.QUEUE_DIR}/finished.toml", "a" * 40, 'run = "a"')
    assert tidy.removal_commit(sweep.git_for(repo), "no-such-branch", [gone]) == ""


def test_claims_past_use_are_pruned_on_the_same_daily_step(repo):
    git_call = sweep.git_for(repo)
    assert jc.claim(git_call, "old", jc.RUN, "pc-a", NOW).won
    stale = (jc.Ref(jc.claim_ref("old"), "x", NOW - jc.PRUNE_AFTER.total_seconds() - 1),)
    tidy.daily(board(repo, refs=stale), Cycle(Seams()), {})
    assert not out(repo, "ls-remote", "origin", jc.claim_ref("old"))


def test_open_pr_returns_the_line_for_the_record(repo):
    cycle = Cycle(Seams())
    finished = list(board(repo).tasks[:1])
    assert "pull/9" in tidy.open_pr(board(repo), finished, TODAY, cycle)


def test_the_pr_body_names_each_file_and_its_task():
    finished = am.parse("p", f"{am.QUEUE_DIR}/finished.toml", "a" * 40, 'run = "a"')
    body = tidy.pr_body([finished])
    assert finished.path in body and finished.id in body
