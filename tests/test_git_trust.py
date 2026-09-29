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
    assert env["GIT_CONFIG_COUNT"] == "2"
    assert (env["GIT_CONFIG_KEY_1"], env["GIT_CONFIG_VALUE_1"]) == (
        "safe.directory",
        "C:/Users/alexa/vs-code/*",
    )
    assert env["GIT_CONFIG_KEY_0"] == "core.pager" and env["PATH"] == "p"


def test_a_trusted_env_adds_nothing_twice_or_under_a_broken_count():
    once = git_trust.trusted_env({}, ROOT)
    assert once["GIT_CONFIG_COUNT"] == "1"
    assert git_trust.trusted_env(once, ROOT) == once
    everything = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory"}
    everything["GIT_CONFIG_VALUE_0"] = "*"
    assert git_trust.trusted_env(everything, ROOT) == everything
    broken = {"GIT_CONFIG_COUNT": "two"}
    assert git_trust.trusted_env(broken, ROOT) == broken


class FakeGit:
    """Global config as a list; each call recorded."""

    def __init__(self, listed: list[str], add_fails: bool = False) -> None:
        self.listed, self.add_fails = listed, add_fails
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        if "--get-all" in args:
            code = 0 if self.listed else 1
            return subprocess.CompletedProcess(
                args, code, "".join(f"{v}\n" for v in self.listed), ""
            )
        if self.add_fails:
            return subprocess.CompletedProcess(args, 255, "", "error: could not lock config file")
        self.listed.append(args[-1])
        return subprocess.CompletedProcess(args, 0, "", "")


def test_persist_adds_the_global_entry_once():
    git = FakeGit(["D:/elsewhere"])
    assert git_trust.persist(ROOT, git) == (
        "git-trust: added C:/Users/alexa/vs-code/* to git's global safe.directory"
    )
    assert git.listed == ["D:/elsewhere", "C:/Users/alexa/vs-code/*"]
    assert git_trust.persist(ROOT, git) == ""
    assert len(git.calls) == 3, "the second call only reads"
    assert git_trust.persist(ROOT, FakeGit(["*"])) == "", "a wildcard already covers it"


def test_persist_says_a_failure_rather_than_raising():
    line = git_trust.persist(ROOT, FakeGit([], add_fails=True))
    assert line.startswith("git-trust: could not add C:/Users/alexa/vs-code/*")
    assert line.endswith("error: could not lock config file")


def test_adopt_writes_only_when_asked():
    environ: dict[str, str] = {}
    git = FakeGit([])
    assert git_trust.adopt(ROOT, write=False, environ=environ, git=git) == ""
    assert environ["GIT_CONFIG_VALUE_0"] == "C:/Users/alexa/vs-code/*" and git.calls == []
    assert git_trust.adopt(ROOT, write=True, environ=environ, git=git).startswith(
        "git-trust: added"
    )
    assert environ["GIT_CONFIG_COUNT"] == "1"


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
    assert status(git_trust.trusted_env(base, tmp_path / "ws")).returncode == 0
    assert status(git_trust.trusted_env(base, tmp_path / "other")).returncode != 0
