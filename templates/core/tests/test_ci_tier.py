"""The CI end-to-end tier's first test, so the PR Gate's `E2E` job has something to run.

Marked `ci_e2e`: `addopts` deselects it everywhere but `scripts/e2e.py --ci`, which the
`E2E` job runs on the default branch. Add this project's end-to-end tests a hosted runner
can run beside it -- the ones needing a service container, Docker, a headless browser --
and keep `local` for what only this PC has.
"""

import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.ci_e2e
def test_the_gate_runs_the_python_devkit_toml_pins():
    """The workflow passes its own `python-version` literal to every job, beside the pin in
    `.devkit.toml`; this is what fails when one is bumped and the other is not."""
    pinned = tomllib.loads((REPO_ROOT / ".devkit.toml").read_text(encoding="utf-8"))["python"][
        "version"
    ]
    running = f"{sys.version_info.major}.{sys.version_info.minor}"
    assert running == pinned, f"the gate runs Python {running}; .devkit.toml pins {pinned}"
