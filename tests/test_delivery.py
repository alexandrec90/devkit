"""`scripts/delivery.py`: a project's flow, which is `gated` unless it clearly says not."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import delivery


def manifest(root: Path, text: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / delivery.MANIFEST).write_text(text, encoding="utf-8")
    return root


def test_a_project_that_says_direct_is_direct(tmp_path):
    root = manifest(tmp_path, '[delivery]\nflow = "direct"\n')
    assert delivery.flow(root) == delivery.DIRECT
    assert delivery.is_direct(root)


@pytest.mark.parametrize(
    "text",
    [
        "",
        '[project]\nenv_prefix = "X"\n',
        "[delivery]\n",
        '[delivery]\nflow = "gated"\n',
        '[delivery]\nflow = "DIRECT"\n',
        '[delivery]\nflow = "drect"\n',
        "[delivery]\nflow = 1\n",
        'delivery = "direct"\n',
        "this is not toml =",
    ],
)
def test_anything_else_is_gated(tmp_path, text):
    root = manifest(tmp_path, text)
    assert delivery.flow(root) == delivery.GATED
    assert not delivery.is_direct(root)


def test_a_missing_file_a_directory_and_bad_bytes_are_gated(tmp_path):
    assert delivery.flow(tmp_path / "absent") == delivery.GATED
    (tmp_path / "dir" / delivery.MANIFEST).mkdir(parents=True)
    assert delivery.flow(tmp_path / "dir") == delivery.GATED
    (tmp_path / "bytes").mkdir()
    (tmp_path / "bytes" / delivery.MANIFEST).write_bytes(b"\xff\xfe[delivery]")
    assert delivery.flow(tmp_path / "bytes") == delivery.GATED


def test_direct_projects_names_only_the_direct_ones(tmp_path):
    manifest(tmp_path / "roguelike", '[delivery]\nflow = "direct"\n')
    manifest(tmp_path / "carameli", '[delivery]\nflow = "gated"\n')
    names = ["roguelike", "carameli", "not-cloned-here"]
    assert delivery.direct_projects(tmp_path, names) == frozenset({"roguelike"})
