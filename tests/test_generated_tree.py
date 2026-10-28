"""Checks that hold a generated project's tree as a whole.

A file of its own, not a section of `test_new_project.py`, because of who runs it:
`run-tests.py` names this file for **any** change under `templates/`, and no template's
own name leads to it. It sat in the generator's tests, which take minutes and are named
by no template, so a fixer that reshaped `run-tests.py.tmpl` ran that template's tests
green and handed the gate a project whose first `ruff format --check` failed (#479).
Keep it to checks that are cheap and read every rendered file.
"""

import configparser
import subprocess
import sys

import pytest
from support import generate, harness_config, load_script

new_project = load_script("scripts/new-project.py")
structure_check = load_script("scripts/hooks/structure_check.py")

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


@pytest.mark.parametrize("features", SHAPES)
def test_no_generated_function_or_class_is_past_the_structure_limits(tmp_path, features):
    """A project's gate holds the files devkit renders into it to `structure_check`'s limits.

    `run-tests.py` is the project's own, rendered from its template, and its structure
    baseline was seeded at adoption. #479 grew the template's `main` to complexity 21, and
    every project that refreshed it went red on `test_nothing_is_new_or_worse_than_the_baseline`
    (roguelike #52, social-scraper #23): a new finding that no fix in the project could
    reach. Only the limits a function or class is held to: the per-module counts are
    advisory, and a generated tree's dependencies are seeded into its baseline at adoption.
    The vendored files are devkit's own gate's.
    """
    root = generate(tmp_path, features)
    # What `sync-devkit.py --pull` stamps, and what makes the gate skip vendored files.
    (root / "DEVKIT_VERSION").write_text("0000000\n", encoding="utf-8")
    cfg = harness_config.load(root)
    worse, _, _ = structure_check.judge(root, cfg)
    held = set(structure_check.DEFAULT_LIMITS) - structure_check.ADVISORY_RULES
    lim = structure_check.limits(cfg)
    over = [structure_check.describe(f, lim) for f in worse if f.rule in held]
    assert over == [], "reshape the template, do not grow the baseline:\n" + "\n".join(over)


def test_alembic_finds_the_scripts_its_ini_points_at(tmp_path):
    """10a9810d: productivity-tracker's first PR Gate failed at `alembic upgrade head` with
    "Path doesn't exist: migrations": the `alembic` feature rendered an `alembic.ini` naming
    a `migrations/` folder, and its `env.py`, that no template wrote, so every project
    generated with the feature was red on its first commit."""
    root = generate(tmp_path, {"alembic": True})
    ini = configparser.ConfigParser(interpolation=None)
    ini.read(root / "alembic.ini", encoding="utf-8")
    scripts = root / ini["alembic"]["script_location"]
    assert (scripts / "script.py.mako").is_file(), "`alembic revision` renders from it"
    assert (scripts / "versions").is_dir(), "where revisions go, kept by its .gitkeep"
    env = (scripts / "env.py").read_text(encoding="utf-8")
    compile(env, "env.py", "exec")
    assert 'os.environ.get("DATABASE_URL"' in env, "the URL the gate and .env.example set"
