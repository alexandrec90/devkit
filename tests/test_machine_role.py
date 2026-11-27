"""`scripts/machine_role.py`: whether this PC takes heavy jobs first, or only as fallback."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import machine_role as mr

LIVE = """{
\t"folders": [{"path": "devkit"}],
\t// "devkit.machineRole": "worker" in a comment is not the setting
\t"settings": {
\t\t"devkit.fixPass": "dispatch"
\t}
}
"""


def _machine_file(tmp_path: Path) -> Path:
    return tmp_path / mr.project_registry.MACHINE_FILE_NAME


@pytest.mark.parametrize(
    ("text", "role"),
    [
        ('{"devkit.machineRole": "worker"}', mr.WORKER),
        ('{"devkit.machineRole": "workstation"}', mr.WORKSTATION),
        ('{"devkit.machineRole": ""}', mr.UNSET),
        ('{"devkit.machineRole": "laptop"}', mr.UNSET),
        ("{}", mr.UNSET),
        ("[]", mr.UNSET),
        ("not a machine file", mr.UNSET),
    ],
)
def test_the_role_is_read_off_the_machine_file_and_anything_else_is_unset(tmp_path, text, role):
    _machine_file(tmp_path).write_text(text, encoding="utf-8")
    assert mr.role_here(tmp_path) == role


def test_a_root_with_no_machine_file_has_no_role_whatever_the_workspace_file_says(tmp_path):
    assert mr.role_here(tmp_path / "missing") == mr.UNSET
    (tmp_path / mr.projects_root.WORKSPACE_FILE_NAME).write_text(
        '{"settings": {"devkit.machineRole": "worker"}}', encoding="utf-8"
    )
    assert mr.role_here(tmp_path) == mr.UNSET


def test_the_role_beside_a_devkit_root_is_that_root_s_file_not_the_live_one(tmp_path):
    root = tmp_path / "devkit"
    assert mr.role_beside(root) == mr.UNSET
    _machine_file(tmp_path).write_text('{"devkit.machineRole": "workstation"}', encoding="utf-8")
    assert mr.role_beside(root) == mr.WORKSTATION


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
    assert mr.role_here(tmp_path) == mr.WORKER
    assert workspace.read_text(encoding="utf-8") == LIVE, "the workspace file is not edited"
    assert json.loads(_machine_file(tmp_path).read_text(encoding="utf-8")) == {
        mr.SETTING: mr.WORKER
    }
    assert mr.main(["--root", str(tmp_path), "--set", "unset"]) == mr.EXIT_OK
    assert mr.role_here(tmp_path) == mr.UNSET


def test_the_cli_needs_no_workspace_file(tmp_path, capsys):
    assert mr.main(["--root", str(tmp_path), "--set", "worker"]) == mr.EXIT_OK
    assert "machine role: worker" in capsys.readouterr().out
    assert mr.main(["--root", str(tmp_path), "--migrate"]) == mr.EXIT_OK
    assert "copied nothing" in capsys.readouterr().out


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
