"""`scripts/git_trust.py`: the workspace's trees trusted by git, whoever created them.

The pure halves are tested on their own; one test drives a real git against a
repository whose ownership check it is told to fail, which is the refusal 5025d284 and
e1463857 filed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from support import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import git_trust

ROOT = Path("C:/Users/alexa/vs-code")


def test_the_pattern_is_git_s_prefix_form_in_forward_slashes():
    assert git_trust.pattern(ROOT) == "C:/Users/alexa/vs-code/*"
    assert git_trust.pattern(Path("/home/a/ws/")) == "/home/a/ws/*"


def test_covered_is_the_root_s_pattern_or_git_s_wildcard():
    assert git_trust.covered(["D:/x", "C:/Users/alexa/vs-code/*"], ROOT)
    assert git_trust.covered(["*"], ROOT)
    assert not git_trust.covered(["C:/Users/alexa/vs-code", "C:/Users/alexa/*/x"], ROOT)


def test_a_trusted_env_appends_after_entries_already_there():
    env = git_trust.trusted_env(
        {"PATH": "p", "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.pager"}, ROOT
    )
    assert env["GIT_CONFIG_COUNT"] == "3"
    assert (env["GIT_CONFIG_KEY_1"], env["GIT_CONFIG_VALUE_1"]) == (
        "safe.directory",
        "C:/Users/alexa/vs-code/*",
    )
    assert env["GIT_CONFIG_KEY_2"] == git_trust.AUTH_SETTING
    assert env["GIT_CONFIG_KEY_0"] == "core.pager" and env["PATH"] == "p"


def test_a_trusted_env_adds_nothing_twice_or_under_a_broken_count():
    once = git_trust.trusted_env({}, ROOT)
    assert once["GIT_CONFIG_COUNT"] == "2"
    assert git_trust.trusted_env(once, ROOT) == once
    everything = {"GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "safe.directory"}
    everything["GIT_CONFIG_VALUE_0"] = "*"
    everything |= {"GIT_CONFIG_KEY_1": git_trust.AUTH_SETTING, "GIT_CONFIG_VALUE_1": "basic"}
    assert git_trust.trusted_env(everything, ROOT) == everything
    broken = {"GIT_CONFIG_COUNT": "two"}
    assert git_trust.trusted_env(broken, ROOT) == broken


def test_a_trusted_env_asks_github_s_credentials_up_front_unless_already_set():
    """902dad3f: GitHub answered this machine's anonymous fetches 403, which git never
    takes to its credential helper. An operator's own value, any spelling, is kept."""
    env = git_trust.trusted_env({}, ROOT)
    assert (env["GIT_CONFIG_KEY_1"], env["GIT_CONFIG_VALUE_1"]) == (
        "http.https://github.com/.proactiveAuth",
        "basic",
    )
    own = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "HTTP.https://github.com/.proactiveauth"}
    own["GIT_CONFIG_VALUE_0"] = "none"
    kept = git_trust.trusted_env(own, ROOT)
    assert kept["GIT_CONFIG_COUNT"] == "2" and kept["GIT_CONFIG_KEY_1"] == "safe.directory"


class FakeGit:
    """Global config as a safe.directory list and a proactiveAuth value; each call
    recorded."""

    def __init__(
        self,
        listed: list[str],
        auth: str = "",
        add_fails: bool = False,
        said: str = "git version 2.47.1.windows.1",
    ) -> None:
        self.listed, self.auth, self.add_fails, self.said = listed, auth, add_fails, said
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str) -> subprocess.CompletedProcess[str]:
        if args == ("version",):
            return subprocess.CompletedProcess(args, 0 if self.said else 1, self.said, "")
        self.calls.append(args)
        if "--get-all" in args:
            code = 0 if self.listed else 1
            return subprocess.CompletedProcess(
                args, code, "".join(f"{v}\n" for v in self.listed), ""
            )
        if "--get" in args:
            return subprocess.CompletedProcess(args, 0 if self.auth else 1, self.auth, "")
        if "--unset-all" in args:
            self.listed = [value for value in self.listed if value != args[-1]]
            return subprocess.CompletedProcess(args, 0, "", "")
        if self.add_fails:
            return subprocess.CompletedProcess(args, 255, "", "error: could not lock config file")
        if "--add" in args:
            self.listed.append(args[-1])
        else:
            self.auth = args[-1]
        return subprocess.CompletedProcess(args, 0, "", "")


AUTH_SET = "git-trust: set git's global http.https://github.com/.proactiveAuth=basic"


def test_persist_adds_the_global_entries_once():
    git = FakeGit(["D:/elsewhere"])
    assert git_trust.persist(ROOT, git).splitlines() == [
        "git-trust: added C:/Users/alexa/vs-code/* to git's global safe.directory",
        AUTH_SET,
    ]
    assert git.listed == ["D:/elsewhere", "C:/Users/alexa/vs-code/*"] and git.auth == "basic"
    assert git_trust.persist(ROOT, git) == ""
    assert len(git.calls) == 6, "the second call only reads"
    assert git_trust.persist(ROOT, FakeGit(["*"], auth="x")) == "", "a wildcard covers it"


def test_persist_keeps_an_operator_s_own_proactive_auth():
    git = FakeGit(["*"], auth="none")
    assert git_trust.persist(ROOT, git) == ""
    assert git.auth == "none"
    assert git_trust.persist(ROOT, FakeGit(["*"])) == AUTH_SET


def test_persist_says_a_failure_rather_than_raising():
    first, second = git_trust.persist(ROOT, FakeGit([], add_fails=True)).splitlines()
    assert first.startswith("git-trust: could not add C:/Users/alexa/vs-code/*")
    assert first.endswith("error: could not lock config file")
    assert second.startswith("git-trust: could not set git's global http.https://github.com/")
    assert second.endswith("error: could not lock config file")


def test_adopt_writes_only_when_asked():
    environ: dict[str, str] = {}
    git = FakeGit([])
    assert git_trust.adopt(ROOT, write=False, environ=environ, git=git) == ""
    assert environ["GIT_CONFIG_VALUE_0"] == "C:/Users/alexa/vs-code/*" and git.calls == []
    assert git_trust.adopt(ROOT, write=True, environ=environ, git=git).startswith(
        "git-trust: added"
    )
    assert environ["GIT_CONFIG_COUNT"] == "2"


@pytest.mark.parametrize(
    ("said", "leading"),
    [
        ("git version 2.46.0", True),
        ("git version 2.47.1.windows.1", True),
        ("git version 3.0.0", True),
        ("git version 2.40.0.windows.1", False),
        ("git version 2.45.2", False),
        ("", False),
        ("not git", False),
    ],
)
def test_only_a_git_from_2_46_reads_the_leading_path_form(said, leading):
    assert git_trust.reads_leading_path(FakeGit([], said=said)) is leading


def test_version_reads_major_and_minor_or_none():
    assert git_trust.version("git version 2.40.0.windows.1") == (2, 40)
    assert git_trust.version("git: command not found") is None


def test_an_older_git_is_trusted_with_everything_in_the_passs_own_scope_only():
    """2026-11-20: this machine's git 2.40 read `C:/Users/alexa/vs-code/*` as matching
    nothing, so the pass's ship of an elevated session's tree failed its first `git
    status` and the scheduled pass exited 1. The global entry stays the narrow one."""
    environ: dict[str, str] = {}
    git = FakeGit([], said="git version 2.40.0.windows.1")
    lines = git_trust.adopt(
        ROOT / "nowhere", write=True, environ=environ, git=git, probe=Probe({})
    ).splitlines()
    assert environ["GIT_CONFIG_VALUE_0"] == git_trust.EVERYTHING
    assert git.listed == ["C:/Users/alexa/vs-code/nowhere/*"], "never `*` globally"
    assert lines[-1].startswith("git-trust: this git predates safe.directory's `/*` form")
    assert git_trust.adopt(ROOT, write=False, environ={}, git=FakeGit([])) == ""


class Probe:
    """git refusing each tree in `foreign` over the directories it lists, in order, until
    the `-c safe.directory=` scope holds each."""

    def __init__(self, foreign: dict[str, list[str]]) -> None:
        self.foreign = foreign

    def __call__(self, *args: str) -> subprocess.CompletedProcess[str]:
        trusted = {arg.split("=", 1)[1] for arg in args if arg.startswith("safe.directory=")}
        tree = args[args.index("-C") + 1]
        for value in self.foreign.get(tree, []):
            if value not in trusted:
                said = f"fatal: detected dubious ownership in repository at '{value}'\n"
                return subprocess.CompletedProcess(args, 128, "", said)
        return subprocess.CompletedProcess(args, 0, ".git\n", "")


def test_refused_names_every_directory_git_refuses_a_tree_over():
    tree = "C:/ws/devkit/.claude/worktrees/x"
    probe = Probe({str(Path(tree)): [tree, "C:/ws/devkit"]})
    assert git_trust.refused(Path(tree), probe) == [tree, "C:/ws/devkit"]
    assert git_trust.refused(Path("C:/ws/mine"), probe) == [], "the user's own tree"


def test_an_old_git_trusts_each_foreign_tree_by_its_own_path_and_drops_gone_ones(tmp_path):
    """d2f2c4b0, 7a8de777, 46d64a5a: on git 2.40 the `/*` entry trusts nothing, so a fixer
    in an elevated session's tree failed every bare git call and six of its tests."""
    root = tmp_path / "ws"
    mine = root / "devkit"
    theirs = mine / ".claude" / "worktrees" / "fix-0928"
    box = root / ".worktrees" / "carameli--x"
    for tree in (mine, theirs, box):
        (tree / ".git").mkdir(parents=True)
    (root / "notes").mkdir()
    assert git_trust.trees(root) == [box, mine, theirs]
    gone = (root / "devkit" / ".claude" / "worktrees" / "old").as_posix()
    git = FakeGit([gone, "D:/elsewhere", git_trust.pattern(root)])
    probe = Probe({str(theirs): [theirs.as_posix()], str(box): [box.as_posix()]})
    lines = git_trust.persist_exact(root, git, probe).splitlines()
    assert git.listed == [
        "D:/elsewhere",
        git_trust.pattern(root),
        box.as_posix(),
        theirs.as_posix(),
    ]
    assert lines[0] == f"git-trust: dropped {gone} from git's global safe.directory: it is gone"
    assert lines[1].startswith(f"git-trust: added {box.as_posix()} to git's global")
    assert git_trust.persist_exact(root, git, probe) == "", "nothing twice"


def test_a_pass_that_does_not_write_touches_no_global_entry_on_an_old_git(tmp_path):
    git = FakeGit([], said="git version 2.40.0.windows.1")

    def probe(*args):
        raise AssertionError("no write, no probe")

    git_trust.adopt(tmp_path, write=False, environ={}, git=git, probe=probe)
    assert git.calls == []


def test_the_probe_carries_none_of_the_passs_own_scope(monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "safe.directory")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "*")
    seen = {}

    def run(argv, **kwargs):
        seen.update(kwargs["env"])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(git_trust.subprocess, "run", run)
    git_trust._bare_git("version")
    assert not [key for key in seen if key.startswith("GIT_CONFIG_")]


def test_git_reads_the_proactive_auth_the_env_carries(tmp_path):
    """The key is the one git reads, URL-scoped to GitHub: `git config --get-urlmatch`
    resolves it for a GitHub remote and for no other host."""
    empty = tmp_path / "gitconfig"
    empty.write_text("", encoding="utf-8")
    base = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    base |= {"GIT_CONFIG_GLOBAL": str(empty), "GIT_CONFIG_NOSYSTEM": "1"}
    env = git_trust.trusted_env(base, tmp_path)

    def urlmatch(url: str) -> str:
        return subprocess.run(
            ["git", "config", "--get-urlmatch", "http.proactiveAuth", url],
            capture_output=True,
            text=True,
            env=env,
            cwd=tmp_path,
            check=False,
        ).stdout.strip()

    assert urlmatch("https://github.com/alexandrec90/devkit.git") == "basic"
    assert urlmatch("https://gitlab.com/someone/thing.git") == ""


def test_git_honours_the_trusted_env_over_a_dubious_owner(tmp_path):
    """The refusal itself, reproduced with git's own test knob: `GIT_TEST_ASSUME_DIFFERENT_OWNER`
    makes git treat every repository as someone else's, as an elevated process's are."""
    repo = tmp_path / "ws" / "project"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    empty = tmp_path / "gitconfig"
    empty.write_text("", encoding="utf-8")
    base = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    # This machine's own global trust must not decide the test either way.
    base |= {"GIT_CONFIG_GLOBAL": str(empty), "GIT_CONFIG_NOSYSTEM": "1"}
    base["GIT_TEST_ASSUME_DIFFERENT_OWNER"] = "1"

    def status(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(repo), "status", "--short"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    refused = status(base)
    assert refused.returncode != 0 and "dubious ownership" in refused.stderr
    adopted = dict(base)
    git_trust.adopt(tmp_path / "ws", write=False, environ=adopted)
    assert status(adopted).returncode == 0, "whichever git this machine has"
    if git_trust.reads_leading_path(git_trust._git):
        assert status(git_trust.trusted_env(base, tmp_path / "other")).returncode != 0
