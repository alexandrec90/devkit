"""Checks that hold a generated project's tree as a whole.

A file of its own, not a section of `test_new_project.py`, because of who runs it:
`run-tests.py` names this file for **any** change under `templates/`, and no template's
own name leads to it. It sat in the generator's tests, which take minutes and are named
by no template, so a fixer that reshaped `run-tests.py.tmpl` ran that template's tests
green and handed the gate a project whose first `ruff format --check` failed (#479).
Keep it to checks that are cheap and read every rendered file.
"""

import subprocess
import sys

import pytest
from support import generate, load_script

new_project = load_script("scripts/new-project.py")

# The two ends of the feature matrix: every template's `if` rendered false, then true.
SHAPES = [
    pytest.param({}, id="bare"),
    pytest.param({f: True for f in new_project.FEATURES}, id="everything"),
]


@pytest.mark.parametrize("features", SHAPES)
def test_generated_python_is_already_ruff_format_clean(tmp_path, features):
    """A new project's first `pre-commit run` must not rewrite its own files.

    devkit excludes `templates/` from its own format check (that Python is content, linted
    by the ruff.toml that ships beside it), which is right — and it meant
    `lint-all.py.tmpl` sat unformatted for the 100-column config it ships with. Nothing
    noticed until a generated project gained a `ruff-format` pre-commit hook, which
    reformatted the file on arrival: a brand-new repo, a failing hook, a dirty tree.
    """
    root = generate(tmp_path, features)
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "format", "--check", "."],
        cwd=root,
        capture_output=True,
        text=True,
    )
    # No skip for a missing ruff: it is a dev dependency in pyproject.toml, so its absence
    # is an unprovisioned tree, which this failure names in stderr.
    assert result.returncode == 0, (
        f"generated files are not format-clean:\n{result.stdout}{result.stderr}"
    )
