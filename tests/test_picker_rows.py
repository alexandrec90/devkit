"""`scripts/picker_rows.py`: the one line a live picker's option is, and its containment.

Every assertion here is about a failure that is SILENT in the quick-pick -- a shifted
field, an extra row, a value nobody wrote -- so the shapes are asserted positionally
rather than by reading back through a parser that would make the same mistake twice.
"""

from __future__ import annotations

import pytest
from support import load_script

picker_rows = load_script("scripts/picker_rows.py")


def fields(line: str) -> list[str]:
    return line.split(picker_rows.FIELD_SEP)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("plain", "plain"),
        ("a|b", "a/b"),
        ("a|b|c", "a/b/c"),
        ("one" + chr(10) + "two", "one two"),
        ("  padded  ", "padded"),
        ("tabbed" + chr(9) + "out", "tabbed out"),
        (412, "412"),
        (None, "None"),
        ("", ""),
    ],
)
def test_a_cell_is_one_line_with_no_separator_left_in_it(text, expected):
    assert picker_rows.cell(text) == expected


def test_a_row_is_the_four_fields_in_the_order_the_extension_reads_them():
    """Positional and fixed: the extension returns the first and draws the other three."""
    line = picker_rows.row("devkit:301", "#301 a-branch", "devkit -- merge conflict", "Some title")
    assert fields(line) == ["devkit:301", "#301 a-branch", "devkit -- merge conflict", "Some title"]


def test_a_row_left_short_still_carries_four_fields():
    """A row with fewer fields is not a row with empty ones: the extension reads by
    position, so omitting the detail would make the description the detail."""
    assert fields(picker_rows.row("v", "l")) == ["v", "l", "", ""]


def test_no_field_can_add_a_fifth_or_a_second_line():
    """The two silent failures, exercised where they would actually come from: a title
    and a description are prose somebody else wrote."""
    line = picker_rows.row("v", "l|abel", "one" + chr(10) + "two", "a|b")
    assert len(fields(line)) == 4
    assert chr(10) not in line


def test_the_nothing_row_carries_the_sentinel_and_says_it_runs_nothing():
    line = picker_rows.nothing_row("nothing broken", "every open PR is green")
    assert fields(line)[0] == picker_rows.NOTHING
    assert fields(line)[3] == "picking this runs nothing"


def test_the_sentinel_cannot_be_read_as_an_option_by_argparse():
    """It reaches its script as `--picks <value>`; a leading `-` is a flag, and the task
    fails with a usage error on a click that meant `never mind`."""
    assert not picker_rows.NOTHING.startswith("-")


def test_emit_writes_one_line_per_row_and_nothing_else(capsys):
    """This stdout IS the quick-pick -- anything extra is an option a person can tick."""
    picker_rows.emit([picker_rows.row("a", "A"), picker_rows.row("b", "B")])
    assert capsys.readouterr().out.splitlines() == ["a|A||", "b|B||"]


def test_emit_of_nothing_writes_nothing(capsys):
    """An empty list is the caller's decision to draw no rows; `nothing_row` is how a
    scan says it found none. This must not invent a blank option out of it."""
    picker_rows.emit([])
    assert capsys.readouterr().out == ""
