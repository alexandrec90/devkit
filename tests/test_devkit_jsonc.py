"""Tests for the JSON-with-comments loader.

The cases that matter are the ones where a naive regex stripper is wrong: comment
markers *inside* string values, and commas that only look trailing.
"""

import json

import pytest
from support import (
    LIVE_WORKSPACE,
    devkit_jsonc,
    devkit_project,
    needs_live_workspace,
)

blank_comments = devkit_jsonc.blank_comments
drop_trailing_commas = devkit_jsonc.drop_trailing_commas
loads = devkit_jsonc.loads


# --- comments ---------------------------------------------------------------


def test_line_and_block_comments_are_removed():
    text = """{
        // a line comment
        "a": 1, /* inline block */
        /* multi
           line */
        "b": 2
    }"""
    assert loads(text) == {"a": 1, "b": 2}


def test_comment_markers_inside_strings_survive():
    # The whole reason this is a state machine and not a regex.
    text = '{"url": "https://example.com/x", "path": "C:\\\\a/*b*/c"}'
    assert loads(text) == {"url": "https://example.com/x", "path": "C:\\a/*b*/c"}


def test_escaped_quote_does_not_end_the_string_early():
    text = r'{"a": "he said \"// not a comment\"", "b": 1}'
    assert loads(text) == {"a": 'he said "// not a comment"', "b": 1}


def test_blanking_preserves_offsets_and_line_numbers():
    text = '{\n  // comment\n  "a": 1\n}'
    blanked = blank_comments(text)
    assert len(blanked) == len(text)
    assert blanked.count("\n") == text.count("\n")


def test_block_comment_keeps_its_newlines():
    text = '{\n/* one\ntwo\nthree */\n"a": 1\n}'
    assert blank_comments(text).count("\n") == text.count("\n")
    assert loads(text) == {"a": 1}


# --- trailing commas --------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ('{"a": 1,}', {"a": 1}),
        ('{"a": [1, 2,],}', {"a": [1, 2]}),
        ('{"a": [1, 2, ] }', {"a": [1, 2]}),
        ('{"a": {"b": 1,},}', {"a": {"b": 1}}),
    ],
)
def test_trailing_commas_are_tolerated(text, expected):
    assert loads(text) == expected


def test_a_comma_inside_a_string_is_not_a_trailing_comma():
    text = '{"a": "x,", "b": "y,"}'
    assert loads(text) == {"a": "x,", "b": "y,"}
    assert drop_trailing_commas(text) == text


def test_a_comma_followed_only_by_a_comment_then_a_brace_is_trailing():
    # blank_comments runs first, which is what exposes this one.
    assert loads('{"a": 1, // note\n}') == {"a": 1}


def test_separating_commas_are_left_alone():
    assert loads('{"a": 1, "b": 2}') == {"a": 1, "b": 2}


# --- failure behaviour ------------------------------------------------------


def test_genuinely_malformed_text_still_raises():
    # The dialect excuses comments and trailing commas, nothing else. Callers that
    # must not crash (sweep) catch this themselves.
    with pytest.raises(json.JSONDecodeError):
        loads("{not json")


def test_plain_json_is_unchanged():
    payload = {"folders": [{"path": "a"}, {"path": "b"}], "n": [1, 2, 3]}
    assert loads(json.dumps(payload)) == payload


# --- the real files ---------------------------------------------------------


@needs_live_workspace
def test_the_workspace_file_parses():
    """The registry sweep.py reads. If this fails, `Ship: Sweep` reports nothing."""
    text = LIVE_WORKSPACE.read_text(encoding="utf-8")
    payload = loads(text)
    assert [f["path"] for f in payload["folders"]], "workspace lists no folders"


def test_the_canonical_shared_task_block_parses():
    """devkit owns no `.vscode/tasks.json` of its own any more — this is the block it
    does own, and the one `--check-tasks` compares the live workspace against. If it
    stops parsing, the drift gate silently compares against nothing."""
    text = devkit_project.canonical_tasks_text()
    assert loads(text)["version"] == "2.0.0"
