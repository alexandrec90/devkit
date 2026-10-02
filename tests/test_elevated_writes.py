"""`scripts/elevated_writes.py`: a directory an elevated process left in an agent tree is
filed for investigation once, and only when it was made after the move to unelevated.

Unopenable directories cannot be made portably without elevation, so `opens` and `born`
are injected; the scan, the window and the once-only record are what is under test.
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path

from support import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import elevated_writes as ew

AFTER = ew.UNELEVATED_SINCE + _dt.timedelta(days=1)
BEFORE = ew.UNELEVATED_SINCE - _dt.timedelta(days=3)


def workspace(tmp_path: Path) -> Path:
    """A workspace with one devkit session tree, one carameli box, and a devkit checkout."""
    (tmp_path / "devkit" / ".claude" / "worktrees" / "hazy" / ".pytest_cache").mkdir(parents=True)
    (tmp_path / "devkit" / ".claude" / "worktrees" / "hazy" / "scripts").mkdir()
    (tmp_path / ".worktrees" / "carameli--fix-0930" / ".pytest_cache").mkdir(parents=True)
    file = tmp_path / "devkit.code-workspace"
    file.write_text(json.dumps({"folders": [{"path": "devkit"}, {"path": "carameli"}]}), "utf-8")
    return file


def locked(names: set[str]):
    return lambda path: path.name not in names


def test_trees_are_every_session_tree_and_box_with_its_project(tmp_path):
    workspace(tmp_path)
    found = ew.trees(tmp_path, ["devkit", "carameli", "gone"])
    assert [(project, tree.name) for project, tree in found] == [
        ("devkit", "hazy"),
        ("carameli", "carameli--fix-0930"),
    ]


def test_only_an_unopenable_directory_made_after_the_move_is_a_sighting(tmp_path):
    workspace(tmp_path)
    found = ew.trees(tmp_path, ["devkit", "carameli"])
    after = ew.sightings(found, opens=locked({".pytest_cache"}), born=lambda e: AFTER)
    assert sorted((s.project, s.path.name) for s in after) == [
        ("carameli", ".pytest_cache"),
        ("devkit", ".pytest_cache"),
    ]
    assert ew.sightings(found, opens=locked({".pytest_cache"}), born=lambda e: BEFORE) == [], (
        "the seven from the elevated era are already known"
    )
    assert ew.sightings(found, opens=lambda p: True, born=lambda e: AFTER) == []
    assert ew.sightings(found, opens=locked({".pytest_cache"}), born=lambda e: None) == []


def test_the_real_probe_opens_an_ordinary_directory_and_dates_it(tmp_path):
    (tmp_path / "t" / "logs").mkdir(parents=True)
    assert ew._opens(tmp_path / "t" / "logs") and ew._opens(tmp_path / "missing")
    [entry] = list(__import__("os").scandir(tmp_path / "t"))
    made = ew._born(entry)
    assert made is not None and abs(made - _dt.datetime.now(_dt.UTC)) < _dt.timedelta(hours=1)


def test_a_new_one_is_filed_once_and_names_what_to_investigate(tmp_path):
    file = workspace(tmp_path)
    devkit = tmp_path / "devkit"
    filed: list[list] = []
    said: list[str] = []

    def scan(found):
        return ew.sightings(found, opens=locked({".pytest_cache"}), born=lambda e: AFTER)

    def record(findings, where):
        filed.append(findings)

    assert ew.check(file, devkit, True, said.append, scan, record) == 2
    [findings] = filed
    assert {f.kind for f in findings} == {ew.KIND}
    devkit_one = next(f for f in findings if f.project == "devkit")
    assert "hazy: .pytest_cache was made by an elevated process" in devkit_one.detail
    assert "find what ran elevated" in devkit_one.detail
    assert devkit_one.evidence.endswith(".pytest_cache")
    assert all(line.startswith("ELEVATED ") for line in said)

    said.clear()
    assert ew.check(file, devkit, True, said.append, scan, record) == 0, "once each"
    assert len(filed) == 1 and said == []


def test_status_mode_files_and_records_nothing(tmp_path):
    file = workspace(tmp_path)
    devkit = tmp_path / "devkit"
    filed: list = []

    def scan(found):
        return ew.sightings(found, opens=locked({".pytest_cache"}), born=lambda e: AFTER)

    said: list[str] = []
    assert ew.check(file, devkit, False, said.append, scan, lambda f, w: filed.append(f)) == 2
    assert filed == [] and not (devkit / ew.SEEN).exists()
    assert all("would file" in line for line in said)


def test_a_cleared_path_is_forgotten_so_a_tree_cut_again_is_flagged_again(tmp_path):
    file = workspace(tmp_path)
    devkit = tmp_path / "devkit"
    ew.write_seen(devkit / ew.SEEN, [str(tmp_path / "devkit" / "gone" / ".pytest_cache")])
    ew.check(file, devkit, True, lambda line: None, lambda found: [], lambda f, w: None)
    assert ew.read_seen(devkit / ew.SEEN) == set()
    assert ew.read_seen(tmp_path / "missing.json") == set()
    (tmp_path / "bad.json").write_text("{not json", "utf-8")
    assert ew.read_seen(tmp_path / "bad.json") == set()


def test_a_sightings_finding_is_stable_and_points_at_the_directory(tmp_path):
    """The detail is the ledger's grouping key, so it carries the tree and the moment,
    and the evidence is the directory a session will look at first."""
    one = ew.Sighting("devkit", tmp_path / "hazy", tmp_path / "hazy" / ".pytest_cache", AFTER)
    finding = one.finding()
    assert finding == one.finding()
    assert finding.kind == ew.KIND and finding.project == "devkit"
    assert AFTER.strftime("%Y-%m-%d %H:%M UTC") in finding.detail
    assert finding.evidence == str(tmp_path / "hazy" / ".pytest_cache")


def test_an_unreadable_workspace_checks_nothing(tmp_path):
    assert ew.check(tmp_path / "none.code-workspace", tmp_path, True, print) == 0
