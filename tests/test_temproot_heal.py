"""Tests for scripts/temproot_heal.py: a scratch root nobody can sweep no longer fails the run.

A real poisoned link needs an elevated process to make, so a plain file stands in for
it and the two probes -- is it a link, can it be resolved -- are passed in.
"""

import subprocess
import sys
import types
from pathlib import Path

from support import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import temproot_heal as th


def _scratch(base: Path, user: str = "alexa", current: bool = True) -> Path:
    root = base / f"pytest-of-{user}"
    root.mkdir(parents=True)
    for name in ("pytest-2904", "pytest-2905"):
        (root / name).mkdir()
    if current:
        (root / "pytest-current").write_text("")  # stands in for the link
    return root


def is_current(path: Path) -> bool:
    return path.name == "pytest-current"


def refused(path: Path) -> bool:
    return False


def test_a_link_this_user_cannot_resolve_is_poisoned_and_nothing_else_is(tmp_path):
    """The 2026-09-27 machine: an elevated pytest left `pytest-current` owned by
    Administrators, and every later run died resolving it at session end."""
    root = _scratch(tmp_path)
    assert th.poisoned_links(root, is_current, refused) == [root / "pytest-current"]
    assert th.poisoned_links(root, is_current, lambda path: True) == []
    assert th.poisoned_links(root, lambda path: False, refused) == []
    assert th.poisoned_links(tmp_path / "pytest-of-nobody", is_current, refused) == []


def test_an_entry_whose_lstat_is_refused_counts_as_a_link(tmp_path):
    root = _scratch(tmp_path)

    def lstat_refused(path: Path) -> bool:
        if is_current(path):
            raise PermissionError(5, "Access is denied")
        return False

    assert th.poisoned_links(root, lstat_refused, refused) == [root / "pytest-current"]


def test_readable_is_false_only_when_resolving_is_refused(tmp_path, monkeypatch):
    assert th.readable(tmp_path / "gone")
    assert th.readable(tmp_path)

    def refuse(self, strict=False):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(Path, "resolve", refuse)
    assert not th.readable(tmp_path)


def test_a_clean_default_is_kept_and_a_poisoned_one_moves_beside_it(tmp_path):
    _scratch(tmp_path, current=False)
    assert th.healthy_temproot(tmp_path, "alexa", is_current, refused) == (tmp_path, [])
    poisoned = _scratch(tmp_path / "other") / "pytest-current"
    base = tmp_path / "other"
    assert th.healthy_temproot(base, "alexa", is_current, refused) == (
        base / th.REHOMED,
        [poisoned],
    )
    # A rehome poisoned in turn is passed over, and running out of them is said.
    _scratch(base / th.REHOMED)
    assert th.healthy_temproot(base, "alexa", is_current, refused)[0] == base / f"{th.REHOMED}-2"
    for n in range(2, th.MAX_REHOMES + 1):
        _scratch(base / f"{th.REHOMED}-{n}")
    assert th.healthy_temproot(base, "alexa", is_current, refused) == (None, [poisoned])


def test_guard_moves_a_poisoned_run_and_leaves_a_healthy_one_alone(tmp_path):
    _scratch(tmp_path)
    env = {th.TEMPROOT: str(tmp_path)}
    said: list[str] = []
    option = types.SimpleNamespace(basetemp=None)
    th.guard(option, env, "alexa", said.append, is_current, refused)
    rehomed = tmp_path.resolve() / th.REHOMED
    assert env[th.TEMPROOT] == str(rehomed) and rehomed.is_dir()
    assert len(said) == 1 and "pytest-current" in said[0] and "elevated" in said[0]
    # The next process inherits the rehome, and finds nothing to move.
    said.clear()
    th.guard(option, env, "alexa", said.append, is_current, refused)
    assert env[th.TEMPROOT] == str(rehomed) and said == []


def test_guard_never_touches_an_explicit_basetemp(tmp_path):
    """The user's `--basetemp`, or the one xdist hands each worker, is never swept."""
    _scratch(tmp_path)
    env = {th.TEMPROOT: str(tmp_path)}
    said: list[str] = []
    th.guard(types.SimpleNamespace(basetemp="x"), env, "alexa", said.append, is_current, refused)
    assert env == {th.TEMPROOT: str(tmp_path)} and said == []


def test_guard_with_nowhere_left_to_go_says_so_and_changes_nothing(tmp_path):
    _scratch(tmp_path)
    for n in range(1, th.MAX_REHOMES + 1):
        _scratch(tmp_path / (th.REHOMED if n == 1 else f"{th.REHOMED}-{n}"))
    env = {th.TEMPROOT: str(tmp_path)}
    said: list[str] = []
    th.guard(types.SimpleNamespace(basetemp=None), env, "alexa", said.append, is_current, refused)
    assert env == {th.TEMPROOT: str(tmp_path)} and len(said) == 1


def test_current_user_names_the_scratch_root_or_is_empty(monkeypatch):
    monkeypatch.setattr(th.getpass, "getuser", lambda: "alexa")
    assert th.current_user() == "alexa"

    def no_user():
        raise OSError("no user")

    monkeypatch.setattr(th.getpass, "getuser", no_user)
    assert th.current_user() == ""


def test_the_repo_conftest_runs_the_guard_and_keeps_the_vendored_conftest_importable():
    """The root conftest is a parent of the vendored one, so pytest loads it first and
    `from conftest import load_module` still reaches the vendored module, in either order."""
    text = (REPO_ROOT / "conftest.py").read_text(encoding="utf-8")
    assert "temproot_heal.guard(config.option" in text
    for order in (
        [
            "scripts/hooks/tests/test_code_text.py",
            "tests/test_temproot_heal.py::test_readable_is_false_only_when_resolving_is_refused",
        ],
        [
            "tests/test_temproot_heal.py::test_readable_is_false_only_when_resolving_is_refused",
            "scripts/hooks/tests/test_code_text.py",
        ],
    ):
        done = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly", "-p", "no:xdist", *order],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
