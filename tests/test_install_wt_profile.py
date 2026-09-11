"""Tests for the installer that registers the `Agent` Windows Terminal profile.

The file being written belongs to the operator and is read by every terminal on their
desktop, so the properties asserted here are the ones that make a rewrite of it safe: the
previous contents are always kept, nothing outside devkit's own profile changes, a second
`--yes` is a no-op rather than a second profile, and the default mode writes nothing at
all.

Exit codes are the other half -- `installers.py` reads nothing else, and a machine with no
Windows Terminal (every CI runner) has to answer 2 rather than fail.
"""

from __future__ import annotations

import datetime as _dt
import io
import json
import sys
from pathlib import Path

import pytest
from support import load_script
from support import wt_profile as wt

installer = load_script("scripts/install-wt-profile.py")


def settings(*profiles: dict) -> dict:
    return {"copyOnSelect": False, "profiles": {"defaults": {}, "list": list(profiles)}}


def other() -> dict:
    return {"guid": "{574e775e-4f2a-5b96-ac1e-a2962a402336}", "name": "PowerShell"}


@pytest.fixture
def live(tmp_path: Path) -> Path:
    """A settings.json with one ordinary profile and no devkit profile yet."""
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(settings(other()), indent=4), encoding="utf-8")
    return path


# --- the answer `installers.py` reads ----------------------------------------------


def test_no_windows_terminal_is_left_alone_rather_than_failed():
    assert installer.run_check(None) == 2


def test_an_unregistered_profile_needs_installing(live, capsys):
    assert installer.run_check(live) == 1
    assert "is missing from" in capsys.readouterr().err


def test_a_drifted_profile_says_so_rather_than_that_it_is_missing(live, capsys):
    live.write_text(
        json.dumps(settings(other(), {**wt.PROFILE, "tabColor": "#000000"})), encoding="utf-8"
    )
    assert installer.run_check(live) == 1
    assert "has drifted from" in capsys.readouterr().err


def test_a_registered_profile_is_current(live, capsys):
    assert installer.main(["--settings", str(live), "--yes"]) == 0
    assert installer.run_check(live) == 0
    assert "up to date" in capsys.readouterr().out


def test_check_never_writes(live):
    before = live.read_text(encoding="utf-8")
    installer.run_check(live)
    assert live.read_text(encoding="utf-8") == before
    assert list(live.parent.glob("*.bak")) == []


# --- the write ---------------------------------------------------------------------


def test_the_default_mode_prints_the_plan_and_changes_nothing(live, capsys):
    before = live.read_text(encoding="utf-8")
    assert installer.main(["--settings", str(live)]) == 0
    out = capsys.readouterr().out
    assert wt.PROFILE_NAME in out and "Dry run" in out
    assert live.read_text(encoding="utf-8") == before


def test_yes_registers_the_profile_and_keeps_the_previous_file(live):
    assert installer.main(["--settings", str(live), "--yes"]) == 0
    written = json.loads(live.read_text(encoding="utf-8"))
    assert wt.is_current(written)
    backups = list(live.parent.glob("*.bak"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8")) == settings(other())


def test_the_operators_other_settings_survive(live):
    installer.main(["--settings", str(live), "--yes"])
    written = json.loads(live.read_text(encoding="utf-8"))
    assert written["copyOnSelect"] is False
    assert other() in wt.profiles_list(written)


def test_a_second_yes_is_a_no_op_rather_than_a_second_profile(live, capsys):
    installer.main(["--settings", str(live), "--yes"])
    capsys.readouterr()
    assert installer.main(["--settings", str(live), "--yes"]) == 0
    assert "already current" in capsys.readouterr().out
    assert len(list(live.parent.glob("*.bak"))) == 1, "an unchanged file is not rewritten"
    assert len(wt.profiles_list(json.loads(live.read_text(encoding="utf-8")))) == 2


def test_a_drifted_field_is_repaired(live):
    live.write_text(
        json.dumps(settings(other(), {**wt.PROFILE, "icon": "\U0001f4a9"})), encoding="utf-8"
    )
    assert installer.main(["--settings", str(live), "--yes"]) == 0
    assert wt.is_current(json.loads(live.read_text(encoding="utf-8")))


def test_the_file_is_written_in_the_shape_windows_terminal_writes_it(live):
    """Four-space indent and a trailing newline, so `--yes` on an untouched file is a
    one-hunk diff rather than a whole-file reformat the operator has to review."""
    installer.main(["--settings", str(live), "--yes"])
    text = live.read_text(encoding="utf-8")
    assert text.endswith("}\n")
    assert '\n    "profiles"' in text


def test_the_emoji_icon_is_not_escaped_into_mojibake(live):
    """`ensure_ascii` would write `\\ud83e\\udd16`, which is valid JSON and unreadable in
    the Settings UI the operator edits this profile in."""
    installer.main(["--settings", str(live), "--yes"])
    assert wt.PROFILE["icon"] in live.read_text(encoding="utf-8")


def test_render_plan_says_add_or_repair_rather_than_one_word_for_both(live):
    """The plan is read before a file the operator did not write this tool for is
    rewritten, so which of the two it is about to do is the first thing it must say."""
    fresh = wt.read_settings(live)
    assert "add profile Agent" in installer.render_plan(live, fresh, comments=False)
    drifted = {**fresh, "profiles": {"list": [dict(wt.PROFILE)]}}
    assert "repair profile Agent" in installer.render_plan(live, drifted, comments=False)


def test_write_settings_returns_the_backup_it_made(live):
    """`main` reports that path, and a backup nobody can name is one nobody restores."""
    backup = installer.write_settings(live, wt.merged(wt.read_settings(live)))
    assert backup.is_file()
    assert json.loads(backup.read_text(encoding="utf-8")) == settings(other())
    assert wt.is_current(json.loads(live.read_text(encoding="utf-8")))


# --- comments, which a JSON rewrite cannot keep -------------------------------------


def test_comments_are_detected_and_the_plan_warns_before_they_are_dropped(live, capsys):
    live.write_text('{\n  // mine\n  "profiles": {"list": []}\n}', encoding="utf-8")
    assert installer.has_comments(live.read_text(encoding="utf-8"))
    installer.main(["--settings", str(live)])
    assert "WARNING" in capsys.readouterr().out


def test_an_ordinary_file_raises_no_warning(live, capsys):
    installer.main(["--settings", str(live)])
    assert "WARNING" not in capsys.readouterr().out


def test_the_backup_keeps_the_comments_the_rewrite_drops(live):
    live.write_text('{\n  // mine\n  "profiles": {"list": []}\n}', encoding="utf-8")
    installer.main(["--settings", str(live), "--yes"])
    backup = next(iter(live.parent.glob("*.bak")))
    assert "// mine" in backup.read_text(encoding="utf-8")
    assert "// mine" not in live.read_text(encoding="utf-8")


def test_the_plan_prints_on_a_windows_console(live, monkeypatch):
    """The regression: a real `--dry-run` died with UnicodeEncodeError before printing
    anything, because the plan lists the profile field by field and the icon is an emoji
    a cp1252 console cannot encode. The write itself was never at risk -- the file is
    written as UTF-8 -- which is exactly why the failure was in the half nobody tests."""
    console = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", console)
    monkeypatch.setattr(sys, "stderr", console)
    assert installer.main(["--settings", str(live)]) == 0
    console.flush()
    assert b"Agent" in console.buffer.getvalue()


# --- the odds and ends --------------------------------------------------------------


def test_the_backup_is_stamped_and_not_a_json_windows_terminal_would_read(live):
    when = _dt.datetime(2026, 9, 10, 14, 30, 5)
    assert installer.backup_path(live, when).name == "settings.json.devkit-20260910-143005.bak"


def test_a_settings_path_that_is_not_there_is_left_alone_rather_than_created(tmp_path, capsys):
    missing = tmp_path / "nope.json"
    assert installer.main(["--settings", str(missing), "--yes"]) == 2
    assert not missing.exists()
    assert "no Windows Terminal settings.json" in capsys.readouterr().err
