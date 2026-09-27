"""Give each checkout its own pytest temp root, so one stuck entry cannot fail them all.

pytest keeps every run's `tmp_path` under one directory per *user* --
`%TEMP%/pytest-of-<user>` -- and at the end of every run it walks that directory and
resolves each `pytest-current` link in it. On a machine running several sessions at once,
a run cut off by a tool timeout left two orphaned interpreters holding that link open, in
a delete-pending state nothing can stat. From then on every pytest run by that user, in
any checkout, crashed in its own teardown with `PermissionError: [WinError 5]` after a
green suite and exited 1: `posix-rehearsal.py` reported FAILED over a passing rehearsal
(97d20f01), and a targeted run in the fixer sent at it did the same.

So the root is keyed on the checkout: `<tempdir>/pytest-trees/<digest of rootdir>`. What
another checkout leaves behind is no longer in the directory this run cleans up. It is
still under the system temp directory, so nothing lands in the tree and nothing needs
ignoring.

Loaded with `-p` from `addopts` rather than as a `conftest.py`, which this directory
cannot have (`support.py` says why), and because the cleanup that crashed runs in
xdist's controller, which imports no test module. `pythonpath = ["tests"]` is what makes
it importable that early: pytest applies it before it loads `-p` plugins.
`PYTEST_DEBUG_TEMPROOT` is pytest's own switch; one already set wins.

Tested in `tests/test_pytest_temproot.py`.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

import pytest

ENV = "PYTEST_DEBUG_TEMPROOT"


def temproot(rootdir: Path, base: str | None = None) -> Path:
    """The temp root for the checkout at `rootdir`: one per checkout, stable across runs.

    Case-folded before hashing, since Windows reports one directory under several
    spellings and two roots for one checkout would share nothing but the cost.
    """
    digest = hashlib.sha256(str(rootdir.resolve()).casefold().encode("utf-8")).hexdigest()
    return Path(base or tempfile.gettempdir()) / "pytest-trees" / digest[:12]


def pytest_configure(config: pytest.Config) -> None:
    """Point pytest at this checkout's root before anything asks for a temp directory.

    The base temp directory is computed lazily, at the first `tmp_path` or when xdist
    starts its workers, both after every plugin is configured. Workers inherit the
    environment, and are handed a base temp directory under the controller's anyway.
    """
    if os.environ.get(ENV):
        return
    root = temproot(config.rootpath)
    root.mkdir(parents=True, exist_ok=True)
    os.environ[ENV] = str(root)
