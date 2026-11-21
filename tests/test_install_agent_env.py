"""Tests for the installer that puts the agent-shell environment in the user tier.

The file it edits is Claude Code's own user settings, holding the operator's permissions
and the hooks switch beside `env`, so what is asserted here is what makes a rewrite of it
safe: only missing keys are added, a value the operator set is never touched, a file it
cannot read is refused rather than replaced, and the default mode writes nothing.

The regression it exists for is 5769209d: a session started at the workspace root loaded
no project's settings, so its Git Bash rewrote `origin/main:<path>` into a Windows path
list and `git show` called it an ambiguous argument.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from support import load_script

installer = load_script("scripts/install-agent-env.py")


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A `~/.claude` holding a settings file with the operator's own keys and no agent env."""
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps({"env": {"DEVKIT_HOOKS_OFF": "1"}, "permissions": {"allow": ["Bash"]}}),
        encoding="utf-8",
    )
    return path


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# --- the answer `installers.py` reads ----------------------------------------------


def test_the_path_conversion_switch_is_among_the_keys_installed():
    assert installer.AGENT_ENV["MSYS2_ARG_CONV_EXCL"].split(";")[0] == "origin/"


def test_a_missing_key_needs_installing(home, capsys):
    assert installer.run_check(home) == 1
    assert "MSYS2_ARG_CONV_EXCL" in capsys.readouterr().err


def test_every_key_present_is_current_whatever_its_value(home):
    home.write_text(
        json.dumps({"env": {key: "mine" for key in installer.AGENT_ENV}}), encoding="utf-8"
    )
    assert installer.run_check(home) == 0


def test_a_machine_without_claude_code_is_left_alone(tmp_path):
    assert installer.run_check(tmp_path / "absent" / "settings.json") == 2


def test_an_unreadable_file_is_left_alone_rather_than_replaced(home):
    home.write_text("{ not json", encoding="utf-8")
    assert installer.run_check(home) == 2
    assert installer.main(["--settings", str(home), "--yes"]) == 2
    assert home.read_text(encoding="utf-8") == "{ not json"


def test_an_env_that_is_not_an_object_is_left_alone(home):
    home.write_text(json.dumps({"env": ["PY_COLORS=0"]}), encoding="utf-8")
    assert installer.read_settings(home) is None


def test_no_settings_file_yet_reads_as_empty(tmp_path):
    assert installer.read_settings(tmp_path / "settings.json") == {}


# --- --yes ---------------------------------------------------------------------------


def test_the_default_mode_writes_nothing(home):
    before = home.read_text(encoding="utf-8")
    assert installer.main(["--settings", str(home)]) == 0
    assert home.read_text(encoding="utf-8") == before


def test_yes_adds_every_missing_key_and_keeps_the_rest(home):
    assert installer.main(["--settings", str(home), "--yes"]) == 0
    written = read(home)
    assert written["env"] == {"DEVKIT_HOOKS_OFF": "1", **installer.AGENT_ENV}
    assert written["permissions"] == {"allow": ["Bash"]}
    assert installer.run_check(home) == 0


def test_yes_never_overwrites_a_value_the_operator_set(home):
    home.write_text(json.dumps({"env": {"MSYS2_ARG_CONV_EXCL": "*"}}), encoding="utf-8")
    assert installer.main(["--settings", str(home), "--yes"]) == 0
    assert read(home)["env"]["MSYS2_ARG_CONV_EXCL"] == "*"


def test_a_second_yes_does_not_rewrite_the_file(home, capsys):
    installer.main(["--settings", str(home), "--yes"])
    before = home.read_text(encoding="utf-8")
    assert installer.main(["--settings", str(home), "--yes"]) == 0
    assert "nothing to add" in capsys.readouterr().out
    assert home.read_text(encoding="utf-8") == before


def test_yes_creates_the_file_when_claude_code_has_not_yet(tmp_path):
    path = tmp_path / "settings.json"
    assert installer.main(["--settings", str(path), "--yes"]) == 0
    assert read(path) == {"env": installer.AGENT_ENV}


def test_the_missing_keys_are_named_in_agent_env_order():
    assert installer.missing_keys({"env": {"PY_COLORS": "1"}}) == [
        key for key in installer.AGENT_ENV if key != "PY_COLORS"
    ]


# --- --uninstall ---------------------------------------------------------------------


def test_uninstall_removes_only_keys_still_holding_devkits_value(home):
    installer.main(["--settings", str(home), "--yes"])
    payload = read(home)
    payload["env"]["PY_COLORS"] = "1"
    home.write_text(json.dumps(payload), encoding="utf-8")
    assert installer.main(["--settings", str(home), "--uninstall", "--yes"]) == 0
    assert read(home)["env"] == {"DEVKIT_HOOKS_OFF": "1", "PY_COLORS": "1"}


def test_uninstall_is_a_dry_run_without_yes(home):
    installer.main(["--settings", str(home), "--yes"])
    before = home.read_text(encoding="utf-8")
    assert installer.main(["--settings", str(home), "--uninstall"]) == 0
    assert home.read_text(encoding="utf-8") == before


def test_uninstall_drops_an_env_it_empties():
    settings, removed = installer.without_agent_env({"env": dict(installer.AGENT_ENV), "x": 1})
    assert settings == {"x": 1}
    assert removed == list(installer.AGENT_ENV)


def test_the_parser_defaults_to_a_dry_run_on_the_user_settings():
    args = installer.build_parser().parse_args([])
    assert args.dry_run is True
    assert args.settings == installer.USER_SETTINGS
    assert installer.USER_SETTINGS.parts[-2:] == (".claude", "settings.json")


def test_write_settings_ends_in_one_newline(tmp_path):
    path = tmp_path / "settings.json"
    installer.write_settings(path, {"env": {}})
    assert path.read_bytes() == b'{\n  "env": {}\n}\n'
