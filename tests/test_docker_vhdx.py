"""Tests for `scripts/docker_vhdx.py`, the compaction half of `docker-maint.py prune`.

The prune's decisions around it -- skip when unelevated, report a failed compaction --
are tested with the prune in `tests/test_docker_maint.py`.
"""

from __future__ import annotations

from support import load_script

docker_vhdx = load_script("scripts/docker_vhdx.py")


def test_elevation_is_only_asked_of_windows(monkeypatch):
    monkeypatch.setattr(docker_vhdx.sys, "platform", "linux")
    assert docker_vhdx.is_elevated() is True


def test_the_older_disk_layout_is_still_found(tmp_path):
    assert docker_vhdx.find(tmp_path) is None
    old = tmp_path / docker_vhdx.LAYOUTS[1]
    old.parent.mkdir(parents=True)
    old.write_bytes(b"")
    assert docker_vhdx.find(tmp_path) == old


def test_compact_with_no_disk_stops_docker_and_says_there_was_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(docker_vhdx.Path, "home", staticmethod(lambda: tmp_path))
    stopped: list[bool] = []
    verdict = docker_vhdx.compact(lambda *a, **k: 0, lambda: stopped.append(True))
    assert verdict == "none" and stopped == [True]
