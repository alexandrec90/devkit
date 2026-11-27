"""`scripts/projects_root.py`: where the checkouts are, from any checkout."""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import projects_root as roots
import sweep

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def test_a_static_checkouts_root_is_its_parent(tmp_path):
    assert roots.projects_root(tmp_path / "devkit") == tmp_path


def test_a_box_resolves_to_the_root_above_its_tier(tmp_path):
    box = tmp_path / roots.BOXES_DIR_NAME / "devkit--topic-0810"
    assert roots.projects_root(box) == tmp_path
    assert roots.source_checkout(box) == tmp_path / "devkit"


def test_an_agent_worktree_resolves_to_its_checkouts_root(tmp_path):
    tree = tmp_path / "devkit" / ".claude" / "worktrees" / "task"
    assert roots.cli_worktree_checkout(tree) == tmp_path / "devkit"
    assert roots.projects_root(tree) == tmp_path
    assert roots.source_checkout(tree) == tmp_path / "devkit"


def test_an_ordinary_checkout_is_its_own_source_and_no_agent_tree(tmp_path):
    assert roots.cli_worktree_checkout(tmp_path / "devkit") is None
    assert roots.source_checkout(tmp_path / "devkit") == tmp_path / "devkit"


def test_the_retired_workspace_path_is_the_root_joined_to_its_name(tmp_path):
    box = tmp_path / roots.BOXES_DIR_NAME / "devkit--x"
    assert roots.default_workspace(box) == tmp_path / roots.WORKSPACE_FILE_NAME
    assert roots.default_workspace(box, "other.json") == tmp_path / "other.json"
    assert not roots.default_workspace(box).exists()


def test_from_flags_prefers_root_then_a_deprecated_workspace_then_the_resolved_one(tmp_path):
    repo = tmp_path / "devkit"
    flagged = tmp_path / "flagged"
    old = tmp_path / "old" / roots.WORKSPACE_FILE_NAME
    assert roots.from_flags(flagged, old, repo) == flagged
    assert roots.from_flags(None, old, repo) == tmp_path / "old"
    assert roots.from_flags(None, None, repo) == tmp_path
    assert roots.from_flags(repo_root=repo) == tmp_path


def test_legacy_workspace_keeps_a_given_one_and_otherwise_names_the_root(tmp_path):
    repo = tmp_path / "devkit"
    old = tmp_path / "old" / "x.code-workspace"
    assert roots.legacy_workspace(tmp_path / "r", old, repo) == old
    assert roots.legacy_workspace(tmp_path / "r", None, repo).parent == tmp_path / "r"
    assert roots.legacy_workspace(repo_root=repo) == tmp_path / roots.WORKSPACE_FILE_NAME


def test_add_flags_gives_a_parser_both_spellings_defaulting_to_none(tmp_path):
    parser = argparse.ArgumentParser()
    roots.add_flags(parser)
    assert vars(parser.parse_args([])) == {"root": None, "workspace": None}
    args = parser.parse_args(["--root", str(tmp_path), "--workspace", "w.code-workspace"])
    assert (args.root, args.workspace) == (tmp_path, Path("w.code-workspace"))
    assert roots.from_flags(args.root, args.workspace) == tmp_path


def test_sweep_re_exports_the_resolvers_rather_than_copying_them():
    assert sweep.source_checkout is roots.source_checkout
    assert sweep.default_workspace is roots.default_workspace
    assert sweep.cli_worktree_checkout is roots.cli_worktree_checkout
    assert sweep.WORKSPACE_FILE_NAME == roots.WORKSPACE_FILE_NAME
    assert sweep.NAME_SEP == roots.NAME_SEP


def _imports(name: str) -> set[str]:
    tree = ast.parse((SCRIPTS / name).read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_the_registry_does_not_import_the_sweep():
    """The cycle this module exists to break: `sweep` reads the registry."""
    assert "sweep" not in _imports("project_registry.py")
    assert "sweep" not in _imports("projects_root.py")
    assert "project_registry" not in _imports("projects_root.py")
