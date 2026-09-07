"""Tests for the two-stage picker handoff.

Two properties, and the file-backed menus this replaced failed the first of them. A
reader must never be able to serve rows some earlier click wrote, so every "not this
scan" case has to come back as a miss and none of them may come back as content. And
the rows come back RANKED across the machine rather than grouped by checkout, which is
the property a mapping payload would have silently lost the moment two were ticked.
"""

import json

import pytest
from support import load_script

picker_scan = load_script("scripts/picker_scan.py")

# One scan of three checkouts, ranked newest-first across all of them -- deliberately
# NOT in checkout order, because that is exactly what a grouped payload would restore.
ENTRIES = [
    ("devkit", "devkit:9|newest"),
    ("carameli", "carameli:4|middle"),
    ("devkit", "devkit:2|older"),
    ("roguelike", "roguelike:1|oldest"),
]


@pytest.fixture(autouse=True)
def scans_in_tmp(tmp_path, monkeypatch):
    """Point the module's one directory at a fresh one per test."""
    monkeypatch.setattr(picker_scan, "SCANS_DIR", tmp_path)
    return tmp_path


# --- writing and reading back ------------------------------------------------


def test_a_write_reads_back_under_its_own_token():
    token = picker_scan.write("preview", ENTRIES)
    assert picker_scan.read("preview", token) == ENTRIES


def test_each_picker_has_its_own_file():
    first = picker_scan.write("preview", [("carameli", "one")])
    picker_scan.write("fix-prs", [("carameli", "two")])
    assert picker_scan.read("preview", first) == [("carameli", "one")]


def test_two_writes_do_not_share_a_token():
    assert picker_scan.write("preview", []) != picker_scan.write("preview", [])


def test_a_second_write_retires_the_first_ones_token():
    """The overwrite is the point: one click's rows, and the click before it is gone."""
    stale = picker_scan.write("preview", [("carameli", "old")])
    picker_scan.write("preview", [("carameli", "new")])
    assert picker_scan.read("preview", stale) is None


# --- every way of not being that scan ----------------------------------------


def test_a_token_from_another_scan_is_a_miss():
    picker_scan.write("preview", ENTRIES)
    assert picker_scan.read("preview", "deadbeefcafe") is None


def test_an_empty_token_is_a_miss_without_touching_the_file():
    picker_scan.write("preview", ENTRIES)
    assert picker_scan.read("preview", "") is None


def test_a_missing_file_is_a_miss_not_an_error():
    assert picker_scan.read("never-written", "abc123") is None


def test_an_unreadable_payload_is_a_miss_not_an_error():
    picker_scan.scan_path("preview").write_text("{not json", encoding="utf-8")
    assert picker_scan.read("preview", "abc123") is None


@pytest.mark.parametrize(
    "payload",
    [
        json.dumps([1, 2, 3]),
        json.dumps({"token": "abc123"}),
        json.dumps({"token": "abc123", "entries": {"carameli": ["a row"]}}),
        json.dumps({"token": "abc123", "entries": [["carameli"]]}),
    ],
    ids=["not-an-object", "no-entries", "entries-not-a-list", "pair-of-the-wrong-width"],
)
def test_a_payload_of_the_wrong_shape_is_a_miss(payload):
    """A shape check rather than trust, because the file is on disk and this is the
    one reader: a payload that parses but is not what `write` writes is somebody
    else's file, and serving it would be exactly the failure the token exists for."""
    picker_scan.scan_path("preview").write_text(payload, encoding="utf-8")
    assert picker_scan.read("preview", "abc123") is None


# --- selecting, and the ranking that survives it ------------------------------


def test_selecting_one_checkout_keeps_only_its_rows():
    assert picker_scan.select(ENTRIES, ["carameli"]) == ["carameli:4|middle"]


def test_selecting_several_keeps_the_scans_ranking_not_checkout_order():
    """The property a mapping payload would have lost: two ticked checkouts give ONE
    menu ranked the way the whole-machine menu was, not one checkout's rows and then
    the other's."""
    assert picker_scan.select(ENTRIES, ["carameli", "devkit"]) == [
        "devkit:9|newest",
        "carameli:4|middle",
        "devkit:2|older",
    ]


def test_selecting_a_checkout_the_scan_never_saw_contributes_nothing():
    assert picker_scan.select(ENTRIES, ["carameli", "not-a-checkout"]) == ["carameli:4|middle"]


def test_selecting_nothing_selects_nothing():
    assert picker_scan.select(ENTRIES, []) == []


# --- the stage-one value ------------------------------------------------------


def test_a_project_value_round_trips():
    value = picker_scan.project_value("carameli", "abc123")
    assert picker_scan.parse_projects(value) == (["carameli"], "abc123")


def test_several_ticked_checkouts_share_one_token():
    text = picker_scan.LIST_SEP.join(
        [picker_scan.project_value("carameli", "abc"), picker_scan.project_value("devkit", "abc")]
    )
    assert picker_scan.parse_projects(text) == (["carameli", "devkit"], "abc")


def test_a_repeated_checkout_is_listed_once():
    text = picker_scan.LIST_SEP.join(["carameli@abc", "carameli@abc"])
    assert picker_scan.parse_projects(text) == (["carameli"], "abc")


def test_mixed_tokens_keep_the_checkouts_and_refuse_the_shortcut():
    """Two tokens means the values were not one draw of the picker, so no write is the
    one they name -- but they are still what the reader ticked, so the caller scans
    them rather than being told nothing was picked."""
    projects, token = picker_scan.parse_projects("carameli@abc,devkit@def")
    assert projects == ["carameli", "devkit"]
    assert token == ""


@pytest.mark.parametrize("text", ["", "carameli", "@abc", "   "])
def test_a_value_with_no_token_half_names_no_checkout(text):
    """An unsplittable value is not half-read: `@` is not the separator stage *two*
    uses, so a token that reached this parser by mistake yields nothing rather than a
    plausible wrong checkout."""
    assert picker_scan.parse_projects(text) == ([], "")


def test_a_stage_one_row_carries_the_value_both_stages_agree_on():
    line = picker_scan.project_row("carameli", "abc123", "2 broken PRs", "tick as many as you want")
    value = line.split("|")[0]
    assert picker_scan.parse_projects(value) == (["carameli"], "abc123")
