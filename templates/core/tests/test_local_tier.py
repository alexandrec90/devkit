"""The local tier's first test, so `scripts/e2e.py` has something real to run from day one.

Marked `local`: `addopts` deselects it everywhere but `scripts/e2e.py`, which devkit's
nightly job runs in this checkout. Add this project's workstation-only tests beside it.
"""

import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.local
def test_the_checkouts_venv_runs_the_python_devkit_toml_pins():
    """`uv run` uses this checkout's `.venv`, so this is the interpreter every agent session
    here runs. A bare `uv sync` takes any version `requires-python` allows; the pin is
    `[python] version`, and `worktree.py provision .` rebuilds a `.venv` that drifted."""
    pinned = tomllib.loads((REPO_ROOT / ".devkit.toml").read_text(encoding="utf-8"))["python"][
        "version"
    ]
    running = f"{sys.version_info.major}.{sys.version_info.minor}"
    assert running == pinned, f".venv runs Python {running}; .devkit.toml pins {pinned}"
