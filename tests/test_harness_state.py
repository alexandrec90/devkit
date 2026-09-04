"""Tests for the switch's record and the instruction files it moves.

The reversion risk worth naming: this module *moves files out of repositories*, and the
property that makes that safe is not "it works" but "restoring puts back exactly what
moving took, and git never saw either". Both halves are asserted against a real git
repository in `tmp_path`, because the skip-worktree bit is the whole mechanism and a fake
runner would assert only that the argv was built.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from support import harness_state as state


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Never devkit's own `logs/`. The module's constants are the state, so they are what
    a test has to move -- writing to the real ones would stash this checkout's own
    instruction files halfway through a test run."""
    monkeypatch.setattr(state, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(state, "LEDGER", tmp_path / "state" / "ledger.json")
    monkeypatch.setattr(state, "STASH", tmp_path / "state" / "files")
    return tmp_path / "state"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    ).stdout


@pytest.fixture
def repo(tmp_path):
    """A checkout with the whole instruction tier in it, committed."""
    root = tmp_path / "proj"
    (root / ".claude" / "rules").mkdir(parents=True)
    (root / ".claude" / "skills" / "ship").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "templates" / "core").mkdir(parents=True)
    (root / "CLAUDE.md").write_text("root memory\n", encoding="utf-8")
    (root / "scripts" / "CLAUDE.md").write_text("subtree memory\n", encoding="utf-8")
    (root / ".claude" / "rules" / "engineering.md").write_text("a rule\n", encoding="utf-8")
    (root / ".claude" / "skills" / "ship" / "SKILL.md").write_text("a skill\n", encoding="utf-8")
    (root / "templates" / "core" / "CLAUDE.md").write_text("output\n", encoding="utf-8")
    git(root, "init", "-q", ".")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    return root


# --- what counts as an instruction file ---------------------------------------------


def test_the_tier_is_memory_and_rules_at_every_depth(repo):
    found = {path.relative_to(repo).as_posix() for path in state.instruction_files(repo)}
    assert found == {"CLAUDE.md", "scripts/CLAUDE.md", ".claude/rules/engineering.md"}


def test_skills_are_not_part_of_the_tier(repo):
    """The operator asked for rules and CLAUDE.md and said skills need no switch. A skill
    costs nothing until it is invoked by name, which is the opposite of the always-loaded
    tier this group exists to stand down."""
    found = {path.relative_to(repo).as_posix() for path in state.instruction_files(repo)}
    assert not any("skills" in name for name in found)


def test_templates_are_not_part_of_the_tier(repo):
    """`templates/core/CLAUDE.md` is devkit's *output*. It is never loaded into a session,
    and moving it aside would make `new-project.py` generate a project with no memory
    file at all."""
    found = {path.relative_to(repo).as_posix() for path in state.instruction_files(repo)}
    assert "templates/core/CLAUDE.md" not in found


def test_a_root_that_is_not_a_directory_yields_nothing(tmp_path):
    assert state.instruction_files(tmp_path / "absent") == []


def test_root_keys_separate_two_roots_with_the_same_name(tmp_path):
    """A colliding key would restore one checkout's CLAUDE.md into another."""
    (tmp_path / "a" / "proj").mkdir(parents=True)
    (tmp_path / "b" / "proj").mkdir(parents=True)
    assert state.root_key(tmp_path / "a" / "proj") != state.root_key(tmp_path / "b" / "proj")


# --- the round trip ------------------------------------------------------------------


def test_switching_off_removes_the_files_and_leaves_git_clean(repo):
    ledger = state.Ledger()
    lines = state.switch_root(repo, ledger)

    assert not (repo / "CLAUDE.md").exists()
    assert not (repo / ".claude" / "rules" / "engineering.md").exists()
    assert len(ledger.instructions) == 3
    assert len(lines) == 3
    # The property the whole mechanism rests on: three tracked files are gone from the
    # working tree and `git status` says nothing, so `git add -A` in a box cannot carry
    # "delete CLAUDE.md" into a pull request.
    assert git(repo, "status", "--porcelain").strip() == ""


def test_switching_back_on_restores_every_byte_and_clears_the_index_bit(repo):
    before = {
        path.relative_to(repo).as_posix(): path.read_bytes()
        for path in state.instruction_files(repo)
    }
    ledger = state.Ledger()
    state.switch_root(repo, ledger)
    state.restore_root(ledger)

    after = {
        path.relative_to(repo).as_posix(): path.read_bytes()
        for path in state.instruction_files(repo)
    }
    assert after == before
    assert ledger.instructions == []
    assert git(repo, "status", "--porcelain").strip() == ""
    # `S` is skip-worktree. Left set, the file would stop tracking the branch it is on.
    assert not any(line.startswith("S ") for line in git(repo, "ls-files", "-v").splitlines())


def test_switching_off_twice_holds_each_file_once(repo):
    ledger = state.Ledger()
    state.switch_root(repo, ledger)
    assert state.switch_root(repo, ledger) == []
    assert len(ledger.instructions) == 3


def test_an_untracked_instruction_file_round_trips_too(tmp_path):
    """The workspace root is not a repository and `~/.claude` is not either, so the two
    outermost tiers have no index to hide anything in -- a plain move is all there is."""
    root = tmp_path / "loose"
    root.mkdir()
    (root / "CLAUDE.md").write_text("loose memory\n", encoding="utf-8")
    ledger = state.Ledger()
    state.switch_root(root, ledger)
    assert not (root / "CLAUDE.md").exists()
    assert ledger.instructions[0].tracked is False
    state.restore_root(ledger)
    assert (root / "CLAUDE.md").read_text(encoding="utf-8") == "loose memory\n"


def test_a_root_that_vanished_is_dropped_rather_than_failing(repo, tmp_path):
    """A reaped box is the ordinary way for a root to disappear; a ledger that can never
    be emptied is one nobody trusts."""
    ledger = state.Ledger()
    state.switch_root(repo, ledger)
    ledger.instructions[0] = state.StashedFile(
        root=str(tmp_path / "gone"), relpath="CLAUDE.md", stash="x/CLAUDE.md", tracked=False
    )
    lines = state.restore_root(ledger)
    assert any("gone, dropped" in line for line in lines)
    assert ledger.instructions == []


def test_a_missing_stash_still_clears_the_index_bit(repo):
    """The file is recoverable from git; the skip-worktree bit is the part that must not
    outlive the switch, because nothing else would ever put it back."""
    ledger = state.Ledger()
    state.switch_root(repo, ledger)
    for entry in ledger.instructions:
        entry.held().unlink()
    lines = state.restore_root(ledger)
    assert all("stash missing, index cleared" in line for line in lines)
    assert not any(line.startswith("S ") for line in git(repo, "ls-files", "-v").splitlines())


def test_restore_can_be_scoped_to_one_root(repo, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "CLAUDE.md").write_text("other\n", encoding="utf-8")
    ledger = state.Ledger()
    state.switch_root(repo, ledger)
    state.switch_root(other, ledger)
    state.restore_root(ledger, other)
    assert (other / "CLAUDE.md").is_file()
    assert not (repo / "CLAUDE.md").exists()
    assert {Path(entry.root) for entry in ledger.instructions} == {repo.resolve()}


# --- what reads a stood-down tier ----------------------------------------------------


def test_a_gate_still_sees_every_instruction_file_while_the_group_is_off(repo):
    """`instruction_sources` is why `test_doc_claims.py` stays honest with the switch on.
    A gate that globs finds nothing once the files have moved, and a check that silently
    stops checking is worse than one that fails."""
    live = state.instruction_sources(repo, state.Ledger())
    ledger = state.Ledger()
    state.switch_root(repo, ledger)
    stashed = state.instruction_sources(repo, ledger)

    assert [name for name, _ in live] == [name for name, _ in stashed]
    assert all(path.is_file() for _, path in stashed)
    assert dict(stashed)["CLAUDE.md"].read_text(encoding="utf-8") == "root memory\n"


def test_stashed_for_answers_by_the_live_path_and_only_for_a_held_file(repo):
    ledger = state.Ledger()
    assert state.stashed_for(repo / "CLAUDE.md", ledger) is None
    state.switch_root(repo, ledger)
    held = state.stashed_for(repo / "CLAUDE.md", ledger)
    assert held is not None and held.read_text(encoding="utf-8") == "root memory\n"


def test_stashed_for_ignores_an_entry_whose_held_copy_is_gone(repo):
    """A half-cleaned stash must read as "not held" rather than as a path that does not
    open: every caller treats the answer as somewhere it can read from."""
    ledger = state.Ledger()
    state.switch_root(repo, ledger)
    for entry in ledger.instructions:
        entry.held().unlink()
    assert state.stashed_for(repo / "CLAUDE.md", ledger) is None


def test_a_switched_off_file_is_read_by_its_live_path(repo):
    """The lookup is by logical path so a caller never learns the stash layout -- the
    gates that use it also relativise those paths, and a stash path is not under the repo."""
    ledger = state.Ledger()
    state.switch_root(repo, ledger)
    assert state.instruction_text(repo / "CLAUDE.md", ledger) == "root memory\n"
    assert state.instruction_exists(repo / "CLAUDE.md", ledger) is True


def test_a_file_that_is_neither_live_nor_held_reads_as_empty(tmp_path):
    assert state.instruction_text(tmp_path / "nope.md", state.Ledger()) == ""
    assert state.instruction_exists(tmp_path / "nope.md", state.Ledger()) is False


# --- the ledger ----------------------------------------------------------------------


def test_the_ledger_round_trips(tmp_path):
    ledger = state.Ledger(
        hooks=True,
        jobs=("devkit-release",),
        instructions=[state.StashedFile("/r", "CLAUDE.md", "r/CLAUDE.md", True)],
    )
    ledger.save(tmp_path / "l.json")
    back = state.Ledger.load(tmp_path / "l.json")
    assert back.hooks is True
    assert back.jobs == ("devkit-release",)
    assert back.instructions == ledger.instructions


@pytest.mark.parametrize("text", ["", "{", "[]", '{"instructions": [1, 2]}'])
def test_an_unreadable_ledger_reads_as_nothing_switched_off(tmp_path, text):
    """The state that makes `--off` safe to re-run and `--on` a no-op. A ledger that
    raised would leave the operator with files moved and no verb that puts them back."""
    path = tmp_path / "l.json"
    path.write_text(text, encoding="utf-8")
    assert state.Ledger.load(path).instructions == []


def test_a_ledger_entry_missing_its_fields_is_dropped(tmp_path):
    path = tmp_path / "l.json"
    path.write_text(json.dumps({"instructions": [{"root": "/r"}]}), encoding="utf-8")
    assert state.Ledger.load(path).instructions == []


def test_the_skip_worktree_call_names_the_repo_and_the_flag(tmp_path):
    assert state.skip_worktree_argv(tmp_path, "CLAUDE.md", True)[-3:] == [
        "--skip-worktree",
        "--",
        "CLAUDE.md",
    ]
    assert "--no-skip-worktree" in state.skip_worktree_argv(tmp_path, "CLAUDE.md", False)


def test_the_state_follows_devkit_dir_rather_than_the_copy_it_runs_from(tmp_path, monkeypatch):
    """An ephemeral box is a devkit checkout too. A switch run from one would stash six
    repositories' instruction files into a directory `reconcile` deletes when the box's PR
    merges -- and the untracked ones (the workspace root's `CLAUDE.md`, `~/.claude`) are
    not in any git history to recover them from."""
    static = tmp_path / "devkit"
    static.mkdir()
    monkeypatch.setenv("DEVKIT_DIR", str(static))
    assert state._state_root() == static


def test_an_unset_or_absent_devkit_dir_falls_back_to_this_checkout(monkeypatch):
    monkeypatch.delenv("DEVKIT_DIR", raising=False)
    assert state._state_root() == state.REPO_ROOT
    monkeypatch.setenv("DEVKIT_DIR", str(state.REPO_ROOT / "no-such-directory"))
    assert state._state_root() == state.REPO_ROOT


def test_a_directory_that_is_not_a_repo_tracks_nothing(tmp_path):
    assert state.is_tracked(tmp_path, "CLAUDE.md") is False


def test_git_missing_entirely_tracks_nothing(tmp_path):
    def explode(*_a, **_k):
        raise OSError("no git")

    assert state.is_tracked(tmp_path, "CLAUDE.md", explode) is False
