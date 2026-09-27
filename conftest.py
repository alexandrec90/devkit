"""Repo-wide pytest setup, for `tests/` and `scripts/hooks/tests/` alike.

Only what must hold for every run lives here, and nothing a test imports: the vendored
`scripts/hooks/tests/conftest.py` is imported by name (`from conftest import
load_module`), and pytest always loads this parent before it, so that name stays bound
to the vendored one. `tests/support.py` says why `tests/` has no conftest of its own.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
import temproot_heal


def pytest_configure(config):
    """Before any scratch dir exists: a root an elevated run poisoned fails every run."""
    temproot_heal.guard(config.option, say=lambda line: print(line, file=sys.__stderr__))
