"""Tests for scripts/collector_build.py: the staged build context a collector with a
pinned sibling is redeployed from (8e3c4a67)."""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

from support import load_script

build = load_script("scripts/collector_build.py")


def test_compose_files_is_the_first_base_file_then_its_override(tmp_path):
    assert build.compose_files(tmp_path) == []
    (tmp_path / "docker-compose.yml").write_text("", encoding="utf-8")
    assert build.compose_files(tmp_path) == [tmp_path / "docker-compose.yml"]
    (tmp_path / "compose.yaml").write_text("", encoding="utf-8")
    assert build.compose_files(tmp_path) == [tmp_path / "compose.yaml"], "compose's own order"
    (tmp_path / "docker-compose.override.yml").write_text("", encoding="utf-8")
    assert build.compose_files(tmp_path) == [
        tmp_path / "compose.yaml",
        tmp_path / "docker-compose.override.yml",
    ], "naming files with -f turns compose's own override lookup off"


def test_override_moves_only_the_build_context(tmp_path):
    assert json.loads(build.override_text("app", tmp_path)) == {
        "services": {"app": {"build": {"context": str(tmp_path)}}}
    }


def _tar(path: Path, members: dict[str, bytes]) -> Path:
    with tarfile.open(path, "w") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


def test_unpack_extracts_into_the_destination(tmp_path):
    archive = _tar(tmp_path / "a.tar", {"src/m.py": b"x = 1\n"})
    assert build.unpack(archive, tmp_path / "out") == ""
    assert (tmp_path / "out" / "src" / "m.py").read_text(encoding="utf-8") == "x = 1\n"


def test_unpack_refuses_a_member_outside_the_destination(tmp_path):
    archive = _tar(tmp_path / "a.tar", {"../escape.txt": b"no"})
    assert build.unpack(archive, tmp_path / "out").startswith("could not unpack a.tar")
    assert not (tmp_path / "escape.txt").exists()


def test_unpack_names_an_archive_that_is_not_one(tmp_path):
    bad = tmp_path / "a.tar"
    bad.write_bytes(b"not a tar")
    assert build.unpack(bad, tmp_path / "out").startswith("could not unpack a.tar")


def _checkout(tmp_path: Path) -> Path:
    checkout = tmp_path / "ws" / "ibkr_trader"
    checkout.mkdir(parents=True)
    (checkout / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    return checkout


def test_stage_exports_each_source_beside_the_others_and_overrides_the_context(tmp_path):
    checkout = _checkout(tmp_path)
    sibling = checkout.parent / "data-lake"
    into = tmp_path / "stage"
    into.mkdir()
    exported = []

    def export(root, rev, dest):
        exported.append((root, rev, dest))
        return ""

    files, why = build.stage(checkout, "app", [(checkout, "HEAD"), (sibling, "abc")], into, export)
    assert why == ""
    assert exported == [
        (checkout, "HEAD", into / "ibkr_trader"),
        (sibling, "abc", into / "data-lake"),
    ], "laid out as the siblings sit in the workspace, which the Dockerfile COPYs from"
    assert files == [checkout / "docker-compose.yml", into / build.OVERRIDE_NAME]
    said = json.loads((into / build.OVERRIDE_NAME).read_text(encoding="utf-8"))
    assert said["services"]["app"]["build"]["context"] == str(into)


def test_stage_stops_at_the_first_export_that_fails(tmp_path):
    checkout = _checkout(tmp_path)
    sibling = checkout.parent / "data-lake"
    seen = []

    def export(root, rev, dest):
        seen.append(root)
        return "fatal: not a valid object name" if root == checkout else ""

    files, why = build.stage(
        checkout, "app", [(checkout, "HEAD"), (sibling, "abc")], tmp_path, export
    )
    assert (
        files == [] and why == "could not stage ibkr_trader at HEAD: fatal: not a valid object name"
    )
    assert seen == [checkout]


def test_stage_without_a_compose_file_exports_nothing(tmp_path):
    files, why = build.stage(
        tmp_path, "app", [(tmp_path, "HEAD")], tmp_path, lambda *a: "should not run"
    )
    assert files == [] and why == f"{tmp_path.name} has no compose file to build from"


def test_stage_names_an_override_it_could_not_write(tmp_path):
    checkout = _checkout(tmp_path)
    missing = tmp_path / "no-such-dir"
    files, why = build.stage(checkout, "app", [], missing, lambda *a: "")
    assert files == [] and why.startswith(f"could not write {build.OVERRIDE_NAME}")
