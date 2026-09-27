"""Where `-p devkit_temproot` is loaded, the pytest floor is one that can import it.

pytest before 9.1 loads `addopts`' `-p` plugins before it applies the ini `pythonpath`,
so on an older pytest `-p devkit_temproot` is a `ModuleNotFoundError` at startup, and a
floor that allows one lets a fresh `uv sync` pick it. The plugin itself is tested in
`scripts/hooks/tests/test_devkit_temproot.py`.
"""

from __future__ import annotations

import re

import pytest
from support import REPO_ROOT

FLOOR = (9, 1)
LOADERS = ["pyproject.toml", "templates/core/pyproject.toml.tmpl"]


def pytest_floor(text: str) -> tuple[int, ...] | None:
    """The `pytest>=X.Y` floor declared in a pyproject's text; None when there is none."""
    found = re.search(r'"pytest>=([\d.]+)"', text)
    return tuple(int(part) for part in found.group(1).split(".")) if found else None


def test_pytest_floor_reads_the_pin_and_only_pytest():
    assert pytest_floor('dev = [\n  "pytest>=9.1",\n]') == (9, 1)
    assert pytest_floor('"pytest-xdist>=3.6"') is None
    assert pytest_floor("") is None


@pytest.mark.parametrize("path", LOADERS)
def test_a_pyproject_loading_the_plugin_pins_a_pytest_that_can(path):
    text = (REPO_ROOT / path).read_text(encoding="utf-8")
    assert "-p devkit_temproot" in text, f"{path} no longer loads the plugin"
    floor = pytest_floor(text)
    assert floor is not None and floor >= FLOOR, f"{path}: pytest floor {floor} < {FLOOR}"
