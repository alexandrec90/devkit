"""`scripts/tree_provision.py`: a worktree is provisioned before a session opens in it."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import agent_tabs
import tree_provision


def test_provisioning_runs_the_one_verb_with_yes_and_reports_a_failure(tmp_path, capsys):
    """Without `--yes` the verb only prints its plan -- the first session to follow the
    rule's "run the provisioning command" was left with no `.venv` that way."""
    seen = []

    def runner(argv, **_kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, len(seen) - 1, "", "uv: lockfile out of date")

    assert tree_provision.provision(tmp_path, runner) is True
    assert seen[0][1:] == [str(tree_provision.WORKTREE), "provision", str(tmp_path), "--yes"]
    assert tree_provision.WORKTREE.is_file()
    assert tree_provision.provision(tmp_path, runner) is False
    assert "lockfile out of date" in capsys.readouterr().err
    assert tree_provision.argv(tmp_path)[-1] == "--yes"


def test_the_childs_output_is_read_as_utf8_and_a_failure_is_left_as_the_trees_friction(
    tmp_path,
):
    """cp1252 could not decode a byte of `uv`'s output: the reader thread's traceback
    landed in the pass's output and the watchdog sent a rescue at a pass that had
    finished. And a failure only printed reached no ledger; the tree's friction file is
    what the next pass files."""
    kwargs = {}

    def runner(argv, **given):
        kwargs.update(given)
        return subprocess.CompletedProcess(argv, 1, "", "npm ci (frontend) could not run")

    assert tree_provision.provision(tmp_path, runner) is False
    assert kwargs["encoding"] == "utf-8" and kwargs["errors"] == "replace"
    friction = (tmp_path / "logs" / "friction.md").read_text(encoding="utf-8")
    assert friction.startswith("- provisioning this tree failed before the session opened: ")
    assert "npm ci (frontend) could not run" in friction
    assert tree_provision.provision(tmp_path, runner) is False
    assert (tmp_path / "logs" / "friction.md").read_text(encoding="utf-8").count("- ") == 2


def test_a_background_session_is_opened_only_in_a_provisioned_tree(monkeypatch, tmp_path):
    """`agent_tabs.launch_background` is the one place the pass opens a session, so it
    provisions first -- with the same runner, before the session's own spawn."""
    spawned = []

    def runner(argv, **_kwargs):
        spawned.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(agent_tabs.shutil, "which", lambda _cli: "claude")
    monkeypatch.setattr(agent_tabs, "is_elevated", lambda: False)
    launch = agent_tabs.agent_models.Launch("claude-bg")
    assert agent_tabs.launch_background(launch, tmp_path, "fix it", False, runner) == 0
    assert spawned[0] == tree_provision.argv(tmp_path) and spawned[1][-1] == "fix it"
