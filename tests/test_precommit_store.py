"""Tests for scripts/precommit_store.py.

The regression is the release job of 2026-09-26: an unelevated commit refused by a
pre-commit clone an elevated session had made. The ACL itself cannot be staged in a
test, so an unreadable clone is a path `can_read` says no to -- and, for `readable`, a
path that is not there, which fails the same `os.scandir`.
"""

import sqlite3

from support import load_script

store = load_script("scripts/precommit_store.py")

HOOKS = "https://github.com/pre-commit/pre-commit-hooks"
RUFF = "https://github.com/astral-sh/ruff-pre-commit"


def _store(tmp_path, rows):
    db = tmp_path / store.DB_NAME
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE repos (repo TEXT NOT NULL, ref TEXT NOT NULL, path TEXT NOT NULL, "
            "PRIMARY KEY (repo, ref))"
        )
        conn.executemany("INSERT INTO repos VALUES (?, ?, ?)", rows)
    return tmp_path


def _rows(root):
    conn = sqlite3.connect(root / store.DB_NAME)
    try:
        return conn.execute("SELECT repo, ref FROM repos ORDER BY repo").fetchall()
    finally:
        conn.close()


def test_an_unreadable_clone_is_dropped_so_pre_commit_clones_it_again(tmp_path):
    admin_only = str(tmp_path / "repo36xv5pz3")
    root = _store(
        tmp_path, [(HOOKS, "v6.0.0", admin_only), (RUFF, "v0.16.0", str(tmp_path / "repos5c"))]
    )

    dropped = store.heal(root, can_read=lambda path: str(path) != admin_only)

    assert dropped == [f"{HOOKS}@v6.0.0 ({admin_only})"]
    assert _rows(root) == [(RUFF, "v0.16.0")]


def test_a_readable_store_is_left_exactly_as_it_was(tmp_path):
    root = _store(tmp_path, [(HOOKS, "v6.0.0", "a"), (RUFF, "v0.16.0", "b")])

    assert store.heal(root, can_read=lambda path: True) == []
    assert _rows(root) == [(RUFF, "v0.16.0"), (HOOKS, "v6.0.0")]


def test_no_store_yet_is_nothing_to_heal(tmp_path):
    assert store.heal(tmp_path / "absent", can_read=lambda path: False) == []


def test_a_database_that_is_not_pre_commits_is_left_alone(tmp_path):
    (tmp_path / store.DB_NAME).write_bytes(b"not a database")

    assert store.heal(tmp_path, can_read=lambda path: False) == []


def test_readable_lists_the_clone_and_every_directory_directly_in_it(tmp_path):
    clone = tmp_path / "repo"
    (clone / "py_env-python3").mkdir(parents=True)
    (clone / ".pre-commit-hooks.yaml").write_text("[]\n", encoding="utf-8")

    assert store.readable(clone)
    assert not store.readable(tmp_path / "gone")


def test_readable_is_false_when_a_hook_environment_cannot_be_listed(tmp_path, monkeypatch):
    clone = tmp_path / "repo"
    (clone / "py_env-python3").mkdir(parents=True)
    real = store.os.listdir

    def refuse(path):
        if str(path).endswith("py_env-python3"):
            raise PermissionError(13, "Access is denied")
        return real(path)

    monkeypatch.setattr(store.os, "listdir", refuse)

    assert not store.readable(clone)


def test_the_store_is_found_where_pre_commit_looks_for_it(tmp_path):
    assert store.store_dir({"PRE_COMMIT_HOME": str(tmp_path)}) == tmp_path
    assert store.store_dir({"XDG_CACHE_HOME": str(tmp_path)}) == tmp_path / "pre-commit"
    assert store.store_dir({}).parts[-2:] == (".cache", "pre-commit")


def test_what_was_dropped_is_one_line_and_nothing_is_silence():
    assert store.say_healed([]) == ""
    line = store.say_healed([f"{HOOKS}@v6.0.0 (x)"])
    assert "\n" not in line
    assert HOOKS in line
