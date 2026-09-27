"""Where `-p devkit_temproot` is loaded, the pytest floor is one that can import it, and
`scripts/temproot_wiring.py` loads it in a consumer that adopted it unwired.

pytest before 9.1 loads `addopts`' `-p` plugins before it applies the ini `pythonpath`,
so on an older pytest `-p devkit_temproot` is a `ModuleNotFoundError` at startup, and a
floor that allows one lets a fresh `uv sync` pick it. The plugin itself is tested in
`scripts/hooks/tests/test_devkit_temproot.py`.
"""

from __future__ import annotations

import configparser
import re
import subprocess
import tomllib

import pytest
from support import REPO_ROOT, load_script

FLOOR = (9, 1)
LOADERS = ["pyproject.toml", "templates/core/pyproject.toml.tmpl"]


def pytest_floor(text: str) -> tuple[int, ...] | None:
    """The `pytest>=X.Y` floor declared in a pyproject's text; None when there is none."""
    found = re.search(r'"pytest>=([\d.]+)"', text)
    return tuple(int(part) for part in found.group(1).split(".")) if found else None


def test_pytest_floor_reads_the_pin_and_only_pytest():
    assert pytest_floor('dev = [\n  "pytest>=9.1",\n]') == (9, 1)
    assert pytest_floor('"pytest-xdist>=3.6"') is None
    assert pytest_floor("") is None


@pytest.mark.parametrize("path", LOADERS)
def test_a_pyproject_loading_the_plugin_pins_a_pytest_that_can(path):
    text = (REPO_ROOT / path).read_text(encoding="utf-8")
    assert "-p devkit_temproot" in text, f"{path} no longer loads the plugin"
    floor = pytest_floor(text)
    assert floor is not None and floor >= FLOOR, f"{path}: pytest floor {floor} < {FLOOR}"


# --- wiring a consumer that adopted the plugin but never loaded it (7f011bf8) ----------

wiring = load_script("scripts/temproot_wiring.py")

# roguelike and social-scraper's table, as generated before the plugin existed.
ROGUELIKE = """[project]
name = "roguelike"
requires-python = ">=3.12"

[dependency-groups]
dev = [
  "pytest>=8.3",
  "pytest-xdist>=3.6",
]

[tool.pytest.ini_options]
# `testpaths` keeps the vendored suite out.
testpaths = ["tests/"]
addopts = "-q"

[tool.mypy]
strict = true
"""

# carameli's: pytest.ini, a multi-line value, a quoted marker expression.
CARAMELI_INI = """[pytest]
asyncio_mode = auto
testpaths = tests
addopts = --ignore=tests/e2e -m "not paid"
timeout = 60
markers =
    slow: slow tests
    paid: costs money
"""


def options(text: str) -> dict:
    return tomllib.loads(text)["tool"]["pytest"]["ini_options"]


def test_a_pre_plugin_table_is_wired_in_place_and_keeps_everything_else():
    wired, why = wiring.wire_pyproject(ROGUELIKE)
    assert why == ""
    table = options(wired)
    assert table["addopts"] == "-q -p devkit_temproot"
    assert table["pythonpath"] == ["scripts/pytest-plugins"]
    assert table["testpaths"] == ["tests/"]
    assert "# `testpaths` keeps the vendored suite out." in wired
    assert tomllib.loads(wired)["tool"]["mypy"] == {"strict": True}


def test_a_project_with_no_pytest_table_gets_one():
    wired, why = wiring.wire_pyproject('[project]\nname = "x"\n')
    assert why == "" and wiring.loads_plugin(**_halves(options(wired)))


def _halves(table: dict) -> dict:
    return {"addopts": table.get("addopts", ""), "pythonpath": table.get("pythonpath", [])}


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ('pythonpath = ["src"]', ["src", "scripts/pytest-plugins"]),
        ("pythonpath = []", ["scripts/pytest-plugins"]),
        ('pythonpath = "src"', ["src", "scripts/pytest-plugins"]),
    ],
)
def test_an_existing_pythonpath_is_extended_not_replaced(line, expected):
    text = f'[tool.pytest.ini_options]\naddopts = "-q"\n{line}\n'
    wired, why = wiring.wire_pyproject(text)
    assert why == "" and options(wired)["pythonpath"] == expected


@pytest.mark.parametrize(
    "text",
    [
        '[tool.pytest.ini_options]\naddopts = [\n  "-q",\n]\n',
        '[tool.pytest.ini_options]\npythonpath = [\n  "src",\n]\n',
        '[tool.pytest]\naddopts = ["-q"]\n',
    ],
    ids=["multi-line-addopts", "multi-line-pythonpath", "native-table"],
)
def test_a_shape_it_cannot_edit_with_certainty_is_left_and_said(text):
    wired, why = wiring.wire_pyproject(text)
    assert wired == text and "left to a person" in why


def test_a_pytest_ini_is_wired_after_its_values_and_parses():
    wired, why = wiring.wire_pytest_ini(CARAMELI_INI)
    assert why == ""
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(wired)
    assert parser["pytest"]["addopts"] == '--ignore=tests/e2e -m "not paid" -p devkit_temproot'
    assert parser["pytest"]["pythonpath"] == "scripts/pytest-plugins"
    assert parser["pytest"]["markers"].split("\n")[-1] == "paid: costs money"


def test_a_continued_pytest_ini_value_is_extended_on_its_last_line():
    text = "[pytest]\naddopts = -q\n    --strict-markers\npythonpath = src\n"
    wired, why = wiring.wire_pytest_ini(text)
    assert why == ""
    assert "    --strict-markers -p devkit_temproot\n" in wired
    assert "pythonpath = src scripts/pytest-plugins\n" in wired


def test_a_pytest_ini_without_a_pytest_section_is_left():
    assert wiring.wire_pytest_ini("[tool:pytest]\n")[1] == "pytest.ini has no [pytest] section"


@pytest.mark.parametrize(
    ("text", "expected", "found"),
    [
        ('"pytest>=8.3",', '"pytest>=9.1",', True),
        ('"pytest>=9.1.1",', '"pytest>=9.1.1",', True),
        ("pytest>=9.0.3\npytest-xdist>=3.5\n", "pytest>=9.1\npytest-xdist>=3.5\n", True),
        ('"pytest-xdist>=3.6",', '"pytest-xdist>=3.6",', False),
    ],
)
def test_the_floor_is_raised_only_where_it_is_below(text, expected, found):
    assert wiring.raise_floor(text) == (expected, found)


def project(tmp_path, pyproject=ROGUELIKE, plugin=True, lock=False):
    if plugin:
        (tmp_path / wiring.PLUGIN_FILE).parent.mkdir(parents=True)
        (tmp_path / wiring.PLUGIN_FILE).write_text("", encoding="utf-8")
    if pyproject is not None:
        (tmp_path / "pyproject.toml").write_bytes(pyproject.replace("\n", "\r\n").encode())
    if lock:
        (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    return tmp_path


def locker(code: int, calls: list):
    def run(argv, **kwargs):
        calls.append((argv, kwargs["cwd"]))
        return subprocess.CompletedProcess(argv, code, "", "resolution failed")

    return run


def test_wiring_a_uv_project_relocks_it_and_keeps_its_line_endings(tmp_path, monkeypatch):
    root = project(tmp_path, lock=True)
    monkeypatch.setattr(wiring.shutil, "which", lambda _n: "uv")
    calls: list = []
    assert wiring.wire(root, locker(0, calls)) == wiring.WIRED
    assert calls == [(["uv", "lock"], str(root))]
    raw = (root / "pyproject.toml").read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
    text = raw.decode().replace("\r\n", "\n")
    assert '"pytest>=9.1"' in text and wiring.loads_plugin(**_halves(options(text)))
    assert wiring.wire(root, locker(0, calls)) == wiring.ALREADY


def test_a_relock_that_fails_leaves_the_project_as_it_was(tmp_path, monkeypatch):
    root = project(tmp_path, lock=True)
    before = (root / "pyproject.toml").read_bytes()
    monkeypatch.setattr(wiring.shutil, "which", lambda _n: "uv")
    said = wiring.wire(root, locker(1, []))
    assert said.startswith("left unwired: `uv lock` failed") and "resolution failed" in said
    assert (root / "pyproject.toml").read_bytes() == before


def test_no_uv_is_no_relock_and_no_wiring(tmp_path, monkeypatch):
    root = project(tmp_path, lock=True)
    monkeypatch.setattr(wiring.shutil, "which", lambda _n: None)
    assert "uv is not on PATH" in wiring.wire(root, locker(0, []))
    assert "devkit_temproot" not in (root / "pyproject.toml").read_text(encoding="utf-8")


def test_a_pytest_ini_project_is_wired_there_and_its_floor_raised_in_the_in_file(tmp_path):
    root = project(tmp_path, pyproject=None)
    (root / "pytest.ini").write_text(CARAMELI_INI, encoding="utf-8")
    (root / "requirements-test.in").write_text("pytest>=9.0.3\n", encoding="utf-8")
    (root / "requirements-test.txt").write_text("pytest==9.1.1\n    # via -r x\n", encoding="utf-8")
    assert wiring.wire(root, locker(1, [])) == wiring.WIRED, "no uv.lock: nothing to relock"
    assert "-p devkit_temproot" in (root / "pytest.ini").read_text(encoding="utf-8")
    assert (root / "requirements-test.in").read_text(encoding="utf-8") == "pytest>=9.1\n"


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        (lambda root: (root / "pyproject.toml").unlink(), "no pytest.ini or pyproject.toml"),
        (lambda root: (root / wiring.PLUGIN_FILE).unlink(), "is not vendored here"),
        (
            lambda root: (root / "requirements-test.txt").write_text(
                "pytest==8.3.5\n", encoding="utf-8"
            ),
            "recompile it at pytest>=9.1 first",
        ),
        (
            lambda root: (root / "pyproject.toml").write_text(
                '[project]\nname = "x"\n', encoding="utf-8"
            ),
            "no `pytest>=` floor",
        ),
    ],
    ids=["no-config", "no-plugin", "old-pin", "no-floor"],
)
def test_a_project_that_cannot_be_wired_whole_is_left_and_said(tmp_path, setup, reason):
    root = project(tmp_path)
    setup(root)
    before = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
    assert reason in wiring.wire(root, locker(0, []))
    assert {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()} == before


def test_the_plan_reads_and_never_writes(tmp_path):
    root = project(tmp_path, lock=True)
    before = (root / "pyproject.toml").read_bytes()
    edits, why = wiring.plan(root)
    assert why == "" and list(edits) == [root / "pyproject.toml"]
    assert '"pytest>=9.1"' in edits[root / "pyproject.toml"]
    assert (root / "pyproject.toml").read_bytes() == before


def test_a_version_is_compared_by_its_numbers_not_its_spelling():
    assert wiring.version("9.1") == (9, 1)
    assert wiring.version("9.0.3") < wiring.FLOOR <= wiring.version("9.1.1")
    assert wiring.version("10") > wiring.FLOOR


@pytest.mark.parametrize(
    ("addopts", "pythonpath", "expected"),
    [
        ("-q -p devkit_temproot", ["scripts/pytest-plugins"], True),
        (["-pdevkit_temproot"], "scripts/pytest-plugins/", True),
        ("-p no:devkit_temproot", ["scripts/pytest-plugins"], False),
        ("-p devkit_temproot", None, False),
    ],
)
def test_loading_the_plugin_needs_both_halves_in_either_spelling(addopts, pythonpath, expected):
    assert wiring.loads_plugin(addopts, pythonpath) is expected


def test_the_command_line_says_what_it_did(tmp_path, capsys):
    root = project(tmp_path, pyproject=None)
    assert wiring.main([str(root)]) == 1
    assert "no pytest.ini or pyproject.toml" in capsys.readouterr().out
