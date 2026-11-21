"""A reasoned `# noqa` survives `ruff format` followed by `ruff check --fix` (13d68a2b).

`ruff format` moves a trailing `# noqa: S603 - <reason>` onto a wrapped call's closing
paren, where it no longer covers the finding; RUF100 then called it unused and its fix
deleted it, reason and all, leaving S603 live. Both devkit's own `ruff.toml` and the one
every generated project starts from must keep RUF100 report-only.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib

import pytest
from support import REPO_ROOT, TEMPLATES

CONFIGS = (REPO_ROOT / "ruff.toml", TEMPLATES / "core" / "ruff.toml.tmpl")

WRAPPED = (
    "import subprocess\n\n\n"
    "def run(cmd):\n"
    "    return subprocess.run(cmd, check=False, capture_output=True, text=True, "
    'cwd="somewhere/quite/long")  # noqa: S603 - argv list, not a shell\n'
)


@pytest.mark.parametrize("config", CONFIGS, ids=lambda p: p.name)
def test_unused_noqa_is_reported_never_deleted(config):
    lint = tomllib.loads(config.read_text(encoding="utf-8"))["lint"]
    assert "RUF100" in lint.get("unfixable", []), f"{config.name}: RUF100's fix deletes reasons"


@pytest.mark.parametrize("config", CONFIGS, ids=lambda p: p.name)
def test_a_reason_survives_format_then_fix(tmp_path, config):
    (tmp_path / "ruff.toml").write_text(config.read_text(encoding="utf-8"), encoding="utf-8")
    target = tmp_path / "a.py"
    target.write_text(WRAPPED, encoding="utf-8")
    for args in (["format", "-q"], ["check", "--fix", "-q"]):
        subprocess.run(
            [sys.executable, "-m", "ruff", *args, str(target)], cwd=tmp_path, check=False
        )
    assert "noqa: S603 - argv list, not a shell" in target.read_text(encoding="utf-8")
