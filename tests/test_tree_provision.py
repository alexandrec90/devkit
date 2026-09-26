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


def test_a_background_session_is_opened_only_in_a_provisioned_tree(monkeypatch, tmp_path):
    """`agent_tabs.launch_background` is the one place the pass opens a session, so it
    provisions first -- with the same runner, before the session's own spawn."""
    spawned = []

    def runner(argv, **_kwargs):
        spawned.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(agent_tabs.shutil, "which", lambda _cli: "claude")
    launch = agent_tabs.agent_models.Launch("claude-bg")
    assert agent_tabs.launch_background(launch, tmp_path, "fix it", False, runner) == 0
    assert spawned[0] == tree_provision.argv(tmp_path) and spawned[1][-1] == "fix it"
