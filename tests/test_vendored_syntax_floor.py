"""Every vendored Python file must parse at the oldest consumer's target version.

The MANIFEST ships byte-identical into repos devkit does not control, and each
consumer lints the copy with its *own* ruff `target-version`. A construct that is
valid here but newer than a consumer's floor is invisible in devkit's CI and fails in
theirs -- at the worst possible moment, as a lint refusal of the very commit that
adopts the release. That is not hypothetical: v0.10.0 shipped
`report-workflow-failure.py` with line breaks inside f-string replacement fields
(Python 3.12 syntax, PEP 701), and data-lake's py311-targeted ruff refused the
adopting commit of the upgrade run.

The floor is py311 because that is the lowest `target-version` any consumer carries.
Raising it is a real decision: it means every consumer's interpreter and ruff floor
moved first, and the commit doing it should say so.

Checked with ruff rather than `ast.parse(feature_version=...)` because the ast
best-effort flag does not cover the PEP 701 f-string grammar -- the exact class that
slipped through.
"""

import subprocess
import sys
from pathlib import Path

from support import load_script

REPO_ROOT = Path(__file__).resolve().parents[1]

# The lowest ruff `target-version` in any consuming repo.
SYNTAX_FLOOR = "py311"


def test_every_vendored_python_file_parses_at_the_syntax_floor():
    manifest = load_script("scripts/sync-devkit.py").MANIFEST
    vendored_py = [rel for rel in manifest if rel.endswith(".py")]
    assert vendored_py, "the MANIFEST lists no Python files -- the test is vacuous"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--isolated",
            "--target-version",
            SYNTAX_FLOOR,
            # Syntax errors are reported regardless of selection; E9 narrows the
            # selectable rules to the syntax family so no style rule can redden this
            # test for reasons the floor has nothing to do with.
            "--select",
            "E9",
            *vendored_py,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"a vendored file uses syntax newer than {SYNTAX_FLOOR}; a consumer whose ruff "
        f"targets {SYNTAX_FLOOR} will refuse the commit that adopts it:\n{result.stdout}"
    )
