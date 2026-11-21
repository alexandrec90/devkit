"""Tests for scripts/ledger-pool.py -- sharing the harness-events ledger between machines.

Every path is a `tmp_path` passed as `--root`, so no test reads or moves this machine's
real ledger.
"""

from support import load_script

pool = load_script("scripts/ledger-pool.py")


def _shard(root, host, text):
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / f"harness-events-{host}.log").write_text(text, encoding="utf-8")
    return logs / f"harness-events-{host}.log"


class TestMoveShard:
    def test_moves_rows_and_removes_the_source(self, tmp_path):
        source = tmp_path / "a.log"
        source.write_text("one\ntwo\n", encoding="utf-8")
        assert pool.move_shard(source, tmp_path / "pool" / "a.log") == 2
        assert not source.exists()
        assert (tmp_path / "pool" / "a.log").read_text(encoding="utf-8") == "one\ntwo\n"

    def test_appends_to_rows_already_there(self, tmp_path):
        """A pool set twice, or rows filed locally while it was offline: nothing is lost."""
        source = tmp_path / "a.log"
        source.write_text("new\n", encoding="utf-8")
        dest = tmp_path / "b.log"
        dest.write_text("old", encoding="utf-8")
        pool.move_shard(source, dest)
        assert dest.read_text(encoding="utf-8") == "old\nnew\n"

    def test_missing_source_or_same_file_is_a_no_op(self, tmp_path):
        same = tmp_path / "a.log"
        same.write_text("x\n", encoding="utf-8")
        assert pool.move_shard(tmp_path / "missing.log", same) == 0
        assert pool.move_shard(same, same) == 0
        assert same.read_text(encoding="utf-8") == "x\n"


class TestOwnShards:
    def test_local_only_until_a_pool_is_reachable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEVKIT_HOST", "desktop")
        local = tmp_path / "logs" / "harness-events-desktop.log"
        assert pool.own_shards(tmp_path) == [local]
        shared = tmp_path / "shared"
        shared.mkdir()
        (tmp_path / "logs").mkdir()
        (tmp_path / "logs" / "ledger-pool").write_text(f"{shared}\n", encoding="utf-8")
        assert pool.own_shards(tmp_path) == [local, shared / "harness-events-desktop.log"]


class TestSetAndUnset:
    def test_set_moves_this_machines_shard_into_the_pool(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEVKIT_HOST", "desktop")
        root = tmp_path / "devkit"
        _shard(root, "desktop", "row\n")
        shared = tmp_path / "shared"
        assert "1 row(s)" in pool.set_pool(root, shared)
        assert (shared / "harness-events-desktop.log").read_text(encoding="utf-8") == "row\n"
        assert not (root / "logs" / "harness-events-desktop.log").exists()
        assert pool.harness_events.ledger_path(root) == shared / "harness-events-desktop.log"

    def test_set_leaves_the_legacy_file_and_other_hosts_alone(self, tmp_path, monkeypatch):
        """Every machine has a `harness-events.log`; moving it would collide in the pool."""
        monkeypatch.setenv("DEVKIT_HOST", "desktop")
        root = tmp_path / "devkit"
        legacy = root / "logs" / "harness-events.log"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("old\n", encoding="utf-8")
        pool.set_pool(root, tmp_path / "shared")
        assert legacy.exists()
        assert not (tmp_path / "shared" / "harness-events.log").exists()

    def test_set_again_moves_from_the_old_pool(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEVKIT_HOST", "desktop")
        root = tmp_path / "devkit"
        _shard(root, "desktop", "row\n")
        pool.set_pool(root, tmp_path / "first")
        pool.set_pool(root, tmp_path / "second")
        assert (tmp_path / "second" / "harness-events-desktop.log").is_file()
        assert not (tmp_path / "first" / "harness-events-desktop.log").exists()

    def test_unset_brings_the_shard_back(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEVKIT_HOST", "desktop")
        root = tmp_path / "devkit"
        _shard(root, "desktop", "row\n")
        pool.set_pool(root, tmp_path / "shared")
        pool.unset_pool(root)
        assert (root / "logs" / "harness-events-desktop.log").read_text(encoding="utf-8") == (
            "row\n"
        )
        assert pool.harness_events.pool_named(root) is None


class TestStatus:
    def test_unpooled_says_how_to_share(self, tmp_path):
        code, line = pool.status(tmp_path)
        assert code == 0
        assert "not pooled" in line
        assert "--set" in line

    def test_pooled_names_the_machines_it_holds(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEVKIT_HOST", "desktop")
        root = tmp_path / "devkit"
        _shard(root, "desktop", "row\n")
        pool.set_pool(root, tmp_path / "shared")
        (tmp_path / "shared" / "harness-events-laptop.log").write_text("x\n", encoding="utf-8")
        code, line = pool.status(root)
        assert code == 0
        assert "desktop, laptop" in line

    def test_unreachable_is_exit_one(self, tmp_path):
        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / "ledger-pool").write_text(f"{tmp_path / 'offline'}\n", encoding="utf-8")
        code, line = pool.status(tmp_path)
        assert code == pool.UNREACHABLE
        assert "unreachable" in line


class TestMain:
    def test_set_then_status(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("DEVKIT_HOST", "desktop")
        shared = tmp_path / "shared"
        assert pool.main(["--root", str(tmp_path), "--set", str(shared)]) == 0
        assert "pooled at" in capsys.readouterr().out
        assert pool.main(["--root", str(tmp_path), "--unset"]) == 0
        assert "not pooled" in capsys.readouterr().out

    def test_no_checkout_is_exit_two(self, monkeypatch):
        monkeypatch.setattr(pool.harness_events, "ledger_root", lambda root=None: None)
        assert pool.main([]) == 2
