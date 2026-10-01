"""The generated project's `scripts/run-tests.py`: its `[frontend]` half.

`tests/test_run_tests.py` covers what the template shares with devkit's own runner. This
file covers what only a project has: a vitest tier, which the template's bare run left
untested -- a TypeScript-only change printed "no test named for" every file and ran
nothing, with `[frontend]` switched on (roguelike, bfbdaba6).
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest
from support import REPO_ROOT, load_script

TEMPLATE = REPO_ROOT / "templates" / "core" / "scripts" / "run-tests.py.tmpl"


@pytest.fixture
def runner(monkeypatch) -> types.ModuleType:
    """The template's runner, loaded as-is (it has no `{{ }}`), with no bytecode written:
    a `__pycache__` beside it would be copied into every generated project."""
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    loader = importlib.machinery.SourceFileLoader("template_run_tests", str(TEMPLATE))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def project(root: Path, frontend: str | None = 'dir = "."\nsrc = "src/"') -> Path:
    """A generated project's skeleton: the vendored config reader and a `.devkit.toml`."""
    hooks = root / "scripts" / "hooks"
    hooks.mkdir(parents=True)
    shutil.copy(REPO_ROOT / "scripts" / "hooks" / "harness_config.py", hooks)
    if frontend is not None:
        (root / ".devkit.toml").write_text(
            f"[frontend]\nenabled = true\n{frontend}\n", encoding="utf-8"
        )
    return root


def with_vitest(base: Path) -> Path:
    """A `node_modules/.bin/vitest` `shutil.which` finds on this OS."""
    bin_dir = base / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    tool = bin_dir / ("vitest.cmd" if os.name == "nt" else "vitest")
    tool.write_text("", encoding="utf-8")
    tool.chmod(0o755)
    return tool


@pytest.fixture
def run_in(runner, tmp_path, monkeypatch):
    """Point the runner at `tmp_path` with a changed set, recording every command."""
    seen: list[list[str]] = []
    answers: dict[str, subprocess.CompletedProcess] = {}

    def fake_run(cmd, **_kwargs):
        seen.append(list(cmd))
        done = answers.get("vitest" if "related" in cmd else "pytest")
        return done or subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(runner, "ARTIFACT", tmp_path / "logs" / "test-failures.log")
    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    for name in runner.FULL_SUITE_ENV:
        monkeypatch.delenv(name, raising=False)

    def run(*changed: str) -> int:
        monkeypatch.setattr(runner, "changed_paths", lambda root, run=None: list(changed))
        return runner.main([])

    run.seen = seen
    run.answers = answers
    run.contracts = list(runner.CONTRACT_TESTS)
    return run


def test_frontend_sources_are_the_scripts_under_src(runner):
    changed = ["src/game/map.ts", "src/ui/App.tsx", "src/style.css", "tests/test_x.py", "x.ts"]
    assert runner.frontend_sources(changed, "src/") == ["src/game/map.ts", "src/ui/App.tsx"]
    assert runner.frontend_sources(["frontend\\src\\a.vue"], "frontend/src") == [
        "frontend/src/a.vue"
    ]
    assert runner.frontend_sources(["a.ts", "b.md"], ".") == ["a.ts"]


def test_the_tier_is_read_from_devkit_toml_through_the_vendored_reader(runner, tmp_path):
    assert runner.frontend_tier(project(tmp_path / "on")) == (".", "src/")
    assert runner.frontend_tier(project(tmp_path / "off", frontend=None)) is None
    assert runner.frontend_tier(tmp_path / "no-harness") is None


def test_the_tier_is_read_in_an_interpreter_that_never_imported_the_reader(tmp_path):
    """In a project the runner is the first to load `harness_config`, and `@dataclass`
    dies on a module run by path before it is in `sys.modules` (scripts/CLAUDE.md). The
    suite imports it long before, so only a fresh interpreter can show the difference."""
    root = project(tmp_path)
    probe = (
        "import importlib.machinery as m, sys; from pathlib import Path; "
        f"mod = m.SourceFileLoader('rt', {str(TEMPLATE)!r}).load_module(); "
        f"print(mod.frontend_tier(Path({str(root)!r})))"
    )
    done = subprocess.run(
        [sys.executable, "-B", "-c", probe], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "('.', 'src/')"


def test_vitest_related_runs_the_trees_own_vitest_on_paths_relative_to_it(runner, tmp_path):
    front = tmp_path / "frontend"
    assert runner.vitest_related(tmp_path, "frontend", ["frontend/src/a.ts"]) is None
    with_vitest(front)
    cmd = runner.vitest_related(tmp_path, "frontend", ["frontend/src/a.ts"])
    assert cmd is not None
    assert Path(cmd[0]).parent == front / "node_modules" / ".bin"
    assert cmd[1:] == ["related", "--run", os.path.join("src", "a.ts")]


def test_a_typescript_change_runs_vitest_related_and_no_pytest(run_in, tmp_path, capsys):
    """bfbdaba6's exact shape: `[frontend]` on at `dir = "."`, only `src/**/*.ts` changed."""
    project(tmp_path)
    with_vitest(tmp_path)
    assert run_in("src/game/map.ts", "src/ui/hud.ts", "README.md") == 0
    [cmd] = run_in.seen
    assert cmd[1:] == [
        "related",
        "--run",
        os.path.join("src", "game", "map.ts"),
        os.path.join("src", "ui", "hud.ts"),
    ]
    out = capsys.readouterr().out
    assert "vitest related for 2 frontend file(s)" in out
    assert "no test named for README.md" in out and "no test named for src/" not in out


def test_a_failing_vitest_run_is_the_artifact(run_in, tmp_path):
    project(tmp_path)
    with_vitest(tmp_path)
    tail = "\n".join(f"line {i}" for i in range(300)) + "\n FAIL src/game/map.test.ts"
    run_in.answers["vitest"] = subprocess.CompletedProcess([], 1, tail, "")
    assert run_in("src/game/map.ts") == 1
    body = (tmp_path / "logs" / "test-failures.log").read_text(encoding="utf-8")
    assert body.startswith("# source: vitest related")
    assert "FAIL src/game/map.test.ts" in body and "line 100\n" not in body


def test_a_python_and_a_typescript_change_run_both_and_both_failures_are_kept(run_in, tmp_path):
    project(tmp_path)
    with_vitest(tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_game.py").write_text("", encoding="utf-8")
    run_in.answers["pytest"] = subprocess.CompletedProcess(
        [], 1, "FAILED tests/test_game.py::t", ""
    )
    run_in.answers["vitest"] = subprocess.CompletedProcess([], 1, "FAIL src/a.test.ts", "")
    assert run_in("app/game.py", "src/a.ts") == 1
    assert [cmd[-1] for cmd in run_in.seen] == ["tests/test_game.py", os.path.join("src", "a.ts")]
    body = (tmp_path / "logs" / "test-failures.log").read_text(encoding="utf-8")
    assert "FAILED tests/test_game.py::t" in body and "FAIL src/a.test.ts" in body


def test_an_unprovisioned_tree_owes_the_frontend_tests_and_says_how_to_run_them(run_in, tmp_path):
    """ "Nothing to run" for a change nobody tested read as a pass."""
    project(tmp_path)
    assert run_in("src/game/map.ts") == 1
    assert run_in.seen == []
    body = (tmp_path / "logs" / "test-failures.log").read_text(encoding="utf-8")
    assert "has no vitest" in body and "npm ci" in body


def test_with_the_tier_off_a_typescript_change_still_runs_nothing(run_in, tmp_path, capsys):
    project(tmp_path, frontend=None)
    assert run_in("src/game/map.ts") == 0
    assert run_in.seen == []
    assert "nothing to run" in capsys.readouterr().out


def test_an_instruction_file_change_runs_the_vendored_contract_tests(run_in, tmp_path, capsys):
    """roguelike, 2026-10-01: CLAUDE.md names no test, so a targeted run never met the
    500-line contract in `test_repo_contract.py` and main went red in the gate on it."""
    project(tmp_path, frontend=None)
    for rel in ("tests/test_game.py", *run_in.contracts):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("", encoding="utf-8")
    assert run_in("CLAUDE.md", "app/game.py") == 0
    [cmd] = run_in.seen
    assert cmd[-1 - len(run_in.contracts) :] == ["tests/test_game.py", *run_in.contracts]
    out = capsys.readouterr().out
    assert "no test named for CLAUDE.md" in out and "the contract tests" in out


def test_a_contract_test_the_project_does_not_hold_is_skipped(runner, tmp_path):
    held = runner.CONTRACT_TESTS[0]
    (tmp_path / held).parent.mkdir(parents=True)
    (tmp_path / held).write_text("", encoding="utf-8")
    assert runner.with_contracts(["tests/test_a.py", held], tmp_path) == ["tests/test_a.py", held]


def test_every_template_contract_test_is_vendored(runner):
    """A generated project holds only what the MANIFEST ships it; a contract test outside
    it would be skipped in every project without a word."""
    manifest = set(load_script("scripts/sync-devkit.py").MANIFEST)
    assert [t for t in runner.CONTRACT_TESTS if t not in manifest] == []
