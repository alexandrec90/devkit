"""`scripts/tier_command.py`: what the medium tier runs at a commit, and where it cannot."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import tier_command


def test_the_override_is_read_off_the_commit_s_devkit_toml():
    text = '[delivery]\nflow = "direct"\nmedium_tier = "python scripts/fast.py --quick"\n'
    assert tier_command.medium_command(text) == "python scripts/fast.py --quick"


@pytest.mark.parametrize(
    "text",
    ["", "[delivery]\n", "[delivery\nbroken", "delivery = 3\n", "[delivery]\nmedium_tier = 3\n"],
)
def test_no_override_for_anything_absent_or_unreadable(text):
    assert tier_command.medium_command(text) == ""


def test_a_project_with_its_database_on_cannot_run_here():
    assert "[db]" in tier_command.not_applicable("[db]\nenabled = true\n", '"--all"')


@pytest.mark.parametrize(
    "toml", ["", "[db]\nenabled = false\n", "[project]\nname = 1\n", "[db\nbroken"]
)
def test_a_project_without_one_can(toml):
    assert tier_command.not_applicable(toml, '"--all"') == ""


@pytest.mark.parametrize(
    "script",
    [
        '_ARGV = [\n    "docker",\n    "compose",\n    "exec",\n]',
        "subprocess.run(['docker', 'compose', 'up'])",
        'run("docker compose exec app pytest")',
    ],
)
def test_a_runner_that_drives_the_stack_cannot_run_here(script):
    assert "Docker" in tier_command.not_applicable("", script)


@pytest.mark.parametrize(
    "script",
    ["# pytest directly on the runner (no Docker stack).", "uses docker in a comment", ""],
)
def test_a_runner_that_only_mentions_docker_can(script):
    assert tier_command.not_applicable("", script) == ""


def test_a_runner_with_a_whole_suite_flag_is_given_it():
    """Bare, the template's runner picks the tests for what changed since origin, which
    at origin's tip is nothing: a green from zero tests, every 15 minutes."""
    assert tier_command.suite_args('parser.add_argument("--all")') == ("--all",)
    assert tier_command.suite_args('parser.add_argument("--changed")') == ()


def test_the_argv_is_the_tree_s_script_under_the_given_python(tmp_path):
    script = tmp_path / "scripts" / "run-tests.py"
    script.parent.mkdir()
    script.write_text('"--all"', encoding="utf-8")
    assert tier_command.argv_for(tmp_path, "", "py") == ["py", str(script), "--all"]


def test_an_override_s_python_is_the_tree_s_and_anything_else_is_found_on_path(monkeypatch):
    monkeypatch.setattr(tier_command.shutil, "which", lambda name: rf"C:\bin\{name}.cmd")
    assert tier_command.argv_for(Path("t"), "python -m x --y", "py") == ["py", "-m", "x", "--y"]
    assert tier_command.argv_for(Path("t"), "npm test", "py") == [r"C:\bin\npm.cmd", "test"]
    monkeypatch.setattr(tier_command.shutil, "which", lambda _name: None)
    assert tier_command.argv_for(Path("t"), "absent go", "py") == ["absent", "go"]
