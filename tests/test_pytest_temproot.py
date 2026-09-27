"""`tests/pytest_temproot.py`: each checkout's pytest temp root is its own."""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

from support import REPO_ROOT

import pytest_temproot


def test_one_checkout_has_one_root_and_two_checkouts_two(tmp_path):
    base = str(tmp_path)
    one, other = tmp_path / "devkit", tmp_path / "devkit-box"
    assert pytest_temproot.temproot(one, base) == pytest_temproot.temproot(one, base)
    assert pytest_temproot.temproot(one, base) != pytest_temproot.temproot(other, base)
    assert pytest_temproot.temproot(one, base).parent == tmp_path / "pytest-trees"


def test_configure_sets_the_root_and_a_root_already_chosen_wins(tmp_path, monkeypatch):
    monkeypatch.delenv(pytest_temproot.ENV, raising=False)
    monkeypatch.setattr(pytest_temproot.tempfile, "gettempdir", lambda: str(tmp_path))
    pytest_temproot.pytest_configure(SimpleNamespace(rootpath=tmp_path / "tree"))
    chosen = Path(os.environ[pytest_temproot.ENV])
    assert chosen.is_dir() and chosen.parent == tmp_path / "pytest-trees"
    monkeypatch.setenv(pytest_temproot.ENV, str(tmp_path / "mine"))
    pytest_temproot.pytest_configure(SimpleNamespace(rootpath=tmp_path / "tree"))
    assert os.environ[pytest_temproot.ENV] == str(tmp_path / "mine")


def test_the_suite_loads_it_before_anything_asks_for_a_temp_directory():
    options = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    ini = options["tool"]["pytest"]["ini_options"]
    assert "-p pytest_temproot" in ini["addopts"]
    assert "tests" in ini["pythonpath"], "a -p plugin is imported before collection"


def test_a_real_run_never_touches_the_shared_root(tmp_path):
    """97d20f01, run rather than read: pytest's teardown walks the root it made its temp
    directories in, so a run that never makes `<tempdir>/pytest-of-<user>` cannot crash on
    what another checkout left there. The real poison was a delete-pending link, which no
    test can make portably, so this asserts where the run went instead.
    """
    shared = tmp_path / "shared"
    shared.mkdir()
    project = tmp_path / "project"
    (project / "tests").mkdir(parents=True)
    (project / "tests" / "pytest_temproot.py").write_text(
        (REPO_ROOT / "tests" / "pytest_temproot.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (project / "tests" / "test_x.py").write_text(
        "def test_x(tmp_path):\n    assert tmp_path.is_dir()\n", encoding="utf-8"
    )
    (project / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts = "-p pytest_temproot"\npythonpath = ["tests"]\n',
        encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items() if k != pytest_temproot.ENV}
    env.update({"TMP": str(shared), "TEMP": str(shared), "TMPDIR": str(shared)})
    run = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"]
    done = subprocess.run(run, cwd=project, env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr
    own = pytest_temproot.temproot(project, str(shared))
    assert [p.name for p in shared.iterdir()] == ["pytest-trees"], "the shared root is untouched"
    assert any(own.glob("pytest-of-*/pytest-*/test_x0")), "the run's tmp_path was this tree's"
