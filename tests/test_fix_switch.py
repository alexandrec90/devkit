"""`scripts/fix_switch.py`: the fix pass's switch, read with nothing of the pass imported."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fix_cycle
import fix_switch
import project_registry


@pytest.mark.parametrize(
    ("text", "mode"),
    [
        ('{"settings": {\n  "devkit.fixPass": "dispatch"\n}}', "dispatch"),
        ('{"settings": {"devkit.fixPass": "plan"}}', "plan"),
        ('{"settings": {"devkit.fixPass" : "off"}}', "off"),
        ('{"settings": {}}', "off"),
        ("not a workspace file", "off"),
    ],
)
def test_the_switch_is_read_off_the_file(tmp_path, text, mode):
    workspace = tmp_path / "w.code-workspace"
    workspace.write_text(text, encoding="utf-8")
    assert fix_switch.workspace_mode(workspace) == mode


def test_a_file_that_cannot_be_read_is_off(tmp_path):
    assert fix_switch.workspace_mode(tmp_path / "missing") == fix_switch.OFF


def test_the_machine_file_beside_the_workspace_file_wins(tmp_path):
    workspace = tmp_path / "w.code-workspace"
    workspace.write_text('{"settings": {"devkit.fixPass": "off"}}', encoding="utf-8")
    machine = tmp_path / fix_switch.MACHINE_FILE_NAME
    machine.write_text('{\n  "devkit.fixPass": "dispatch"\n}\n', encoding="utf-8")
    assert fix_switch.workspace_mode(workspace) == "dispatch"


def test_a_machine_file_without_the_switch_leaves_the_workspace_file_to_answer(tmp_path):
    workspace = tmp_path / "w.code-workspace"
    workspace.write_text('{"settings": {"devkit.fixPass": "plan"}}', encoding="utf-8")
    (tmp_path / fix_switch.MACHINE_FILE_NAME).write_text(
        '{"devkit.machineRole": "worker"}', encoding="utf-8"
    )
    assert fix_switch.workspace_mode(workspace) == "plan"
    assert fix_switch.workspace_mode(tmp_path / "absent.code-workspace") == fix_switch.OFF


def test_the_machine_file_is_the_one_the_registry_writes(tmp_path):
    """Spelled, not imported, so the watchdog survives the module; held equal here."""
    assert fix_switch.MACHINE_FILE_NAME == project_registry.MACHINE_FILE_NAME
    workspace = tmp_path / "w.code-workspace"
    project_registry.set_machine_setting(fix_cycle.SETTING, "plan", workspace=workspace)
    assert fix_switch.workspace_mode(workspace) == "plan" == fix_cycle.mode_here(workspace)


def test_the_spellings_are_the_ones_the_pass_uses():
    assert (fix_switch.OFF, fix_switch.DISPATCH) == (fix_cycle.OFF, fix_cycle.DISPATCH)
    assert fix_switch.MODE.search(f'"{fix_cycle.SETTING}": "x"')


def test_it_imports_nothing_the_watchdog_could_not_survive_losing():
    """The watchdog has to run when the pass cannot be imported."""
    tree = ast.parse(Path(fix_switch.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}
