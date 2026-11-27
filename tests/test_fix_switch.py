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
import projects_root


@pytest.mark.parametrize(
    ("text", "mode"),
    [
        ('{\n  "devkit.fixPass": "dispatch"\n}', "dispatch"),
        ('{"devkit.fixPass": "plan"}', "plan"),
        ('{"devkit.fixPass" : "off"}', "off"),
        ('{"devkit.machineRole": "worker"}', "off"),
        ("not a machine file", "off"),
    ],
)
def test_the_switch_is_read_off_the_machine_file(tmp_path, text, mode):
    (tmp_path / fix_switch.MACHINE_FILE_NAME).write_text(text, encoding="utf-8")
    assert fix_switch.root_mode(tmp_path) == mode


def test_a_root_with_no_machine_file_is_off(tmp_path):
    assert fix_switch.root_mode(tmp_path / "missing") == fix_switch.OFF


def test_the_retired_workspace_file_is_never_read(tmp_path):
    (tmp_path / projects_root.WORKSPACE_FILE_NAME).write_text(
        '{"settings": {"devkit.fixPass": "dispatch"}}', encoding="utf-8"
    )
    assert fix_switch.root_mode(tmp_path) == fix_switch.OFF


def test_the_machine_file_is_the_one_the_registry_writes(tmp_path):
    """Spelled, not imported, so the watchdog survives the module; held equal here."""
    assert fix_switch.MACHINE_FILE_NAME == project_registry.MACHINE_FILE_NAME
    project_registry.set_machine_setting(fix_cycle.SETTING, "plan", root=tmp_path)
    assert fix_switch.root_mode(tmp_path) == "plan" == fix_cycle.mode_here(tmp_path)


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
