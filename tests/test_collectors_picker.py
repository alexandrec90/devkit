"""`collectors_picker.py`: the one quick-pick behind *Machine: Ingestion Collectors*.

Pinned: the read-only row comes first, every row's value is something `collectors.py`
parses, each row says what this PC is set to now, and an empty declaration draws a row
that runs nothing rather than an empty (and so failed-looking) list.
"""

from __future__ import annotations

import json

from support import load_script

picker = load_script("scripts/collectors_picker.py")
collectors = load_script("scripts/collectors.py")
picker_rows = load_script("scripts/picker_rows.py")
config = picker.config

DECLARED = [config.Collector("ibkr_trader", "app"), config.Collector("sports_betting", "collector")]


def values(lines):
    return [line.split(picker_rows.FIELD_SEP)[0] for line in lines]


def test_status_is_first_so_a_misclick_changes_nothing():
    assert values(picker.rows(DECLARED, {}))[0] == "status"


def test_every_verb_is_offered_for_all_and_for_each_collector():
    got = values(picker.rows(DECLARED, {}))
    for verb in collectors.VERBS:
        assert verb in got
        assert f"{verb}:ibkr_trader" in got and f"{verb}:sports_betting" in got
    assert len(got) == 1 + len(collectors.VERBS) * (1 + len(DECLARED))


def test_every_value_is_one_collectors_parses():
    for value in values(picker.rows(DECLARED, {})):
        args = collectors.parse_args([value])
        assert args.mode in {"status", *collectors.VERBS}
        assert args.projects in ([], ["ibkr_trader"], ["sports_betting"])


def test_each_row_says_what_this_pc_is_set_to():
    lines = picker.rows(DECLARED, {"ibkr_trader": config.RUN, "sports_betting": config.STOP})
    by_value = {line.split("|")[0]: line for line in lines}
    assert "this PC: runs it" in by_value["stop-here:ibkr_trader"]
    assert "this PC: stops it" in by_value["run-here:sports_betting"]
    assert "ibkr_trader (this PC: runs it)" in by_value["status"]
    assert "hands off" in picker.now({}, "ibkr_trader")


def test_nothing_declared_draws_the_row_that_runs_nothing():
    (line,) = picker.rows([], {})
    assert line.split("|")[0] == picker_rows.NOTHING == collectors.NOTHING
    assert config.SETTING in line


def test_every_line_has_exactly_four_fields():
    for line in picker.rows(DECLARED, {}):
        assert line.count(picker_rows.FIELD_SEP) == 3, line


def test_main_prints_only_rows(tmp_path, monkeypatch, capsys):
    root = tmp_path / "devkit"
    root.mkdir()
    monkeypatch.setenv("DEVKIT_DIR", str(root))
    (tmp_path / "alex-projects.code-workspace").write_text(
        json.dumps({"settings": {config.SETTING: {"ibkr_trader": {"service": "app"}}}}),
        encoding="utf-8",
    )
    assert picker.main() == 0
    out = capsys.readouterr().out.splitlines()
    assert values(out) == [
        "status",
        "run-here",
        "run-here:ibkr_trader",
        "stop-here",
        "stop-here:ibkr_trader",
        "release",
        "release:ibkr_trader",
    ]
