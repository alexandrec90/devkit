"""`scripts/machine_role.py`: whether this PC takes heavy jobs first, or only as fallback."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import devkit_jsonc
import devkit_project
import machine_role as mr

LIVE = """{
\t"folders": [{"path": "devkit"}],
\t// "devkit.machineRole": "worker" in a comment is not the setting
\t"settings": {
\t\t"devkit.fixPass": "dispatch"
\t}
}
"""


@pytest.mark.parametrize(
    ("text", "role"),
    [
        ('{"settings": {"devkit.machineRole": "worker"}}', mr.WORKER),
        ('{"settings": {"devkit.machineRole": "workstation"}}', mr.WORKSTATION),
        ('{"settings": {"devkit.machineRole": ""}}', mr.UNSET),
        ('{"settings": {"devkit.machineRole": "laptop"}}', mr.UNSET),
        ('{"settings": {}}', mr.UNSET),
        ('{"settings": []}', mr.UNSET),
        ("not a workspace file", mr.UNSET),
        (LIVE, mr.UNSET),
    ],
)
def test_the_role_is_read_off_the_file_and_anything_else_is_today_s_behaviour(text, role):
    assert mr.role_of(text) == role


def test_a_file_that_cannot_be_read_has_no_role(tmp_path):
    assert mr.role_here(tmp_path / "missing.code-workspace") == mr.UNSET
    workspace = tmp_path / "w.code-workspace"
    workspace.write_text('{"settings": {"devkit.machineRole": "worker"}}', encoding="utf-8")
    assert mr.role_here(workspace) == mr.WORKER


def test_the_role_beside_a_devkit_root_is_that_root_s_file_not_the_live_one(tmp_path):
    root = tmp_path / "devkit"
    assert mr.role_beside(root) == mr.UNSET
    (tmp_path / mr.sweep.WORKSPACE_FILE_NAME).write_text(
        '{"settings": {"devkit.machineRole": "workstation"}}', encoding="utf-8"
    )
    assert mr.role_beside(root) == mr.WORKSTATION


def test_setting_a_role_adds_it_keeping_every_comment():
    edited = mr.with_role(LIVE, mr.WORKER)
    assert edited is not None
    assert mr.role_of(edited) == mr.WORKER
    assert "// \"devkit.machineRole\": \"worker\" in a comment" in edited
    assert devkit_jsonc.loads(edited)["settings"]["devkit.fixPass"] == "dispatch"


def test_setting_a_role_again_replaces_it_where_it_stands():
    once = mr.with_role(LIVE, mr.WORKER)
    twice = mr.with_role(once, mr.WORKSTATION)
    assert twice is not None and mr.role_of(twice) == mr.WORKSTATION
    assert twice.count('"devkit.machineRole":') == once.count('"devkit.machineRole":')
    unset = mr.with_role(twice, mr.UNSET)
    assert unset is not None and mr.role_of(unset) == mr.UNSET
    assert '"devkit.machineRole": ""' in unset


def test_a_file_with_nowhere_to_put_it_or_a_role_that_is_not_one_is_refused():
    assert mr.with_role('{"folders": []}', mr.WORKER) is None
    assert mr.with_role(LIVE, "laptop") is None


def test_the_cli_shows_and_sets_this_machine_s_role(tmp_path, capsys):
    workspace = tmp_path / "w.code-workspace"
    workspace.write_text(LIVE, encoding="utf-8")
    assert mr.main(["--workspace", str(workspace)]) == mr.EXIT_OK
    assert "machine role: unset" in capsys.readouterr().out
    assert mr.main(["--workspace", str(workspace), "--set", "worker"]) == mr.EXIT_OK
    assert "machine role: worker" in capsys.readouterr().out
    assert mr.role_here(workspace) == mr.WORKER
    assert mr.main(["--workspace", str(workspace), "--set", "unset"]) == mr.EXIT_OK
    assert mr.role_here(workspace) == mr.UNSET


def test_each_role_says_what_it_does_and_where_it_is_set(tmp_path):
    workspace = tmp_path / "w.code-workspace"
    for role in (*mr.ROLES, mr.UNSET):
        line = mr.describe(role, workspace)
        assert line.startswith(f"machine role: {role or mr.UNSET_WORD} -- {mr.MEANING[role]}")
        assert str(workspace) in line and mr.SETTING in line


def test_the_cli_refuses_a_file_it_cannot_read_or_write_into(tmp_path, capsys):
    missing = tmp_path / "missing.code-workspace"
    assert mr.main(["--workspace", str(missing), "--set", "worker"]) == mr.EXIT_USAGE
    bare = tmp_path / "bare.code-workspace"
    bare.write_text('{"folders": []}', encoding="utf-8")
    assert mr.main(["--workspace", str(bare), "--set", "worker"]) == mr.EXIT_USAGE
    assert "no `settings` object" in capsys.readouterr().err


def test_the_role_is_a_machine_setting_a_render_carries_and_an_adopt_never_records():
    assert mr.SETTING in devkit_project.MACHINE_SETTINGS
    declared = devkit_project.machine_settings(
        devkit_jsonc.loads(devkit_project.canonical_text())
    )
    assert declared[mr.SETTING] == mr.UNSET, "a fresh machine starts with no role"
