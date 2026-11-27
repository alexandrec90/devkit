"""`scripts/machine_role.py`: whether this PC takes heavy jobs first, or only as fallback."""

from __future__ import annotations

import json
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


def _machine_file(tmp_path: Path) -> Path:
    return tmp_path / mr.project_registry.MACHINE_FILE_NAME


def test_the_machine_file_wins_over_the_workspace_file(tmp_path):
    workspace = tmp_path / "w.code-workspace"
    workspace.write_text('{"settings": {"devkit.machineRole": "worker"}}', encoding="utf-8")
    _machine_file(tmp_path).write_text('{"devkit.machineRole": "workstation"}', encoding="utf-8")
    assert mr.role_here(workspace) == mr.WORKSTATION


def test_a_malformed_machine_file_has_no_role(tmp_path):
    workspace = tmp_path / "w.code-workspace"
    workspace.write_text('{"settings": {"devkit.machineRole": "worker"}}', encoding="utf-8")
    _machine_file(tmp_path).write_text("{", encoding="utf-8")
    assert mr.role_here(workspace) == mr.UNSET


@pytest.mark.parametrize("value", ["laptop", 3, None])
def test_a_stored_value_that_is_not_a_role_is_unset(value):
    assert mr.role_value(value) == mr.UNSET


def test_the_cli_shows_and_sets_this_machine_s_role_in_the_machine_file(tmp_path, capsys):
    workspace = tmp_path / "w.code-workspace"
    workspace.write_text(LIVE, encoding="utf-8")
    assert mr.main(["--workspace", str(workspace)]) == mr.EXIT_OK
    assert "machine role: unset" in capsys.readouterr().out
    assert mr.main(["--workspace", str(workspace), "--set", "worker"]) == mr.EXIT_OK
    assert "machine role: worker" in capsys.readouterr().out
    assert mr.role_here(workspace) == mr.WORKER
    assert workspace.read_text(encoding="utf-8") == LIVE, "the workspace file is not edited"
    assert json.loads(_machine_file(tmp_path).read_text(encoding="utf-8")) == {
        mr.SETTING: mr.WORKER
    }
    assert mr.main(["--workspace", str(workspace), "--set", "unset"]) == mr.EXIT_OK
    assert mr.role_here(workspace) == mr.UNSET


def test_setting_a_role_keeps_the_machine_file_s_other_settings(tmp_path):
    workspace = tmp_path / "w.code-workspace"
    _machine_file(tmp_path).write_text('{"devkit.fixPass": "dispatch"}', encoding="utf-8")
    assert mr.main(["--workspace", str(workspace), "--set", "workstation"]) == mr.EXIT_OK
    assert json.loads(_machine_file(tmp_path).read_text(encoding="utf-8")) == {
        "devkit.fixPass": "dispatch",
        mr.SETTING: mr.WORKSTATION,
    }


def test_each_role_says_what_it_does_and_where_it_is_set(tmp_path):
    machine = _machine_file(tmp_path)
    for role in (*mr.ROLES, mr.UNSET):
        line = mr.describe(role, machine)
        assert line.startswith(f"machine role: {role or mr.UNSET_WORD} -- {mr.MEANING[role]}")
        assert str(machine) in line and mr.SETTING in line


def test_the_cli_refuses_a_malformed_machine_file_and_leaves_it(tmp_path, capsys):
    workspace = tmp_path / "w.code-workspace"
    _machine_file(tmp_path).write_text("{", encoding="utf-8")
    assert mr.main(["--workspace", str(workspace), "--set", "worker"]) == mr.EXIT_USAGE
    assert "cannot write" in capsys.readouterr().err
    assert _machine_file(tmp_path).read_text(encoding="utf-8") == "{"


def test_migrate_copies_the_machine_settings_out_of_the_workspace_file(tmp_path, capsys):
    workspace = tmp_path / "w.code-workspace"
    workspace.write_text(
        '{"settings": {"devkit.machineRole": "worker", "devkit.fixPass": "plan",'
        ' "devkit.collectors": {}}}',
        encoding="utf-8",
    )
    assert mr.main(["--workspace", str(workspace), "--migrate"]) == mr.EXIT_OK
    assert "copied devkit.fixPass, devkit.machineRole" in capsys.readouterr().out
    assert json.loads(_machine_file(tmp_path).read_text(encoding="utf-8")) == {
        "devkit.fixPass": "plan",
        mr.SETTING: mr.WORKER,
    }


def test_the_daily_line_is_silent_with_nothing_to_copy_and_never_raises(tmp_path):
    workspace = tmp_path / "w.code-workspace"
    workspace.write_text('{"settings": {"devkit.onHold": ["x"]}}', encoding="utf-8")
    assert mr.migrate_line(workspace).startswith("moved devkit.onHold into")
    assert mr.migrate_line(workspace) == ""
    _machine_file(tmp_path).write_text("[]", encoding="utf-8")
    assert mr.migrate_line(workspace).startswith("machine settings not migrated")


def test_migrate_and_say_prints_only_a_move(tmp_path, capsys):
    workspace = tmp_path / "w.code-workspace"
    workspace.write_text('{"settings": {"devkit.fixPass": "off"}}', encoding="utf-8")
    mr.migrate_and_say(workspace, "[x] ")
    assert capsys.readouterr().out.startswith("[x] moved devkit.fixPass into")
    mr.migrate_and_say(workspace, "[x] ")
    assert capsys.readouterr().out == ""


def test_the_role_is_a_machine_setting_a_render_carries_and_an_adopt_never_records():
    assert mr.SETTING in devkit_project.MACHINE_SETTINGS
    declared = devkit_project.machine_settings(devkit_jsonc.loads(devkit_project.canonical_text()))
    assert declared[mr.SETTING] == mr.UNSET, "a fresh machine starts with no role"
