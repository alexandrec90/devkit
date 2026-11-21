"""`scripts/fix_switch.py`: the fix pass's switch, read with nothing of the pass imported."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fix_cycle
import fix_switch


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
