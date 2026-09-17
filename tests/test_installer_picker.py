"""`installer_picker.py`: the two quick-picks the *Machine: Scheduled Jobs* task draws.

The one that matters is `apply_rows`, because it is the only prompt in the workspace that
declines to be asked. VS Code resolves every `${input:...}` a task names, unconditionally,
so a third prompt only `uninstall` reads was still put to `status` and `maintain` -- and a
one-row answer is what makes it silent there. Every test below is about one of the two
halves of that: what a person reads, and which branch a verb lands in.
"""

from __future__ import annotations

import ast

import pytest
from support import REPO_ROOT, load_script

picker = load_script("scripts/installer_picker.py")
installers = load_script("scripts/installers.py")


def _values(rows: list[str]) -> list[str]:
    return [line.split("|")[0] for line in rows]


def test_every_verb_row_is_a_mode_the_parser_accepts():
    """A row is a `mode` argument, so a fourth one here -- or a renamed one -- is a
    usage error on a click that looked legitimate, reported as a task failure."""
    values = _values(picker.verb_rows())
    assert values == ["status", "maintain", "uninstall"]
    assert [installers.parse_args([value]).mode for value in values] == values


def test_a_verb_row_says_what_it_does_rather_than_only_naming_itself():
    """The three CLI words were the entire quick-pick, and `maintain` in particular told
    a reader nothing -- least of all that it is the verb that *installs*. The label is
    what a person reads first, so it has to carry the action; the bare word stays on the
    row too, beside it, because it is what `installers.py` and the logs call the mode."""
    for line in picker.verb_rows():
        value, label, description, detail = line.split("|")
        assert label.lower() != value, f"{value} is a label that only repeats the verb"
        assert len(label.split()) > 1, f"{value} has a one-word label again"
        assert description == value, f"{value}'s CLI word has left the row"
        assert detail, f"{value} explains itself nowhere"


def test_the_install_verb_says_that_it_installs():
    """The question this picker was failing to answer -- "what do status and maintain
    even do, and how do I get the jobs onto a fresh PC" -- has one answer, and `maintain`
    is it. A row for it that never says "install" is the defect, whatever else it says."""
    maintain = next(line for line in picker.verb_rows() if line.startswith("maintain|"))
    _, label, _, detail = maintain.split("|")
    assert "install" in label.lower()
    assert "fresh PC" in detail


def test_only_an_uninstall_is_asked_whether_to_apply():
    """The whole point of this picker: `status` and `maintain` return a single row, which
    the task's `useSingleResult` takes without drawing a quick-pick, so the question is
    never put to a run that removes nothing."""
    for verb in ("status", "maintain"):
        assert len(picker.apply_rows(verb)) == 1, verb
    assert len(picker.apply_rows("uninstall")) == 2


def test_the_quiet_branch_still_answers_dry_run():
    """A row with an empty value is dropped by the picker's `filterEmptyResults`, and a
    quick-pick left with no options is an error rather than a default -- so the branch
    that asks nothing still has to supply a real, safe token."""
    assert _values(picker.apply_rows("status")) == ["--dry-run"]
    assert installers.parse_args(["status", "--dry-run"]).dry_run is True


def test_the_uninstall_branch_offers_the_plan_before_the_removal():
    assert _values(picker.apply_rows("uninstall")) == ["--dry-run", "--yes"]


@pytest.mark.parametrize("verb", ["${input:installerVerb}", "", "UNINSTALL", "uninstall --yes"])
def test_a_verb_this_picker_does_not_recognise_takes_the_safe_branch(verb):
    """An escaped verb picker leaves its literal behind and the task starts anyway -- see
    `scripts/task_input.py`. Reading anything but `uninstall` as "removes nothing" is what
    keeps that path from offering `--yes` for a click nobody made."""
    assert _values(picker.apply_rows(verb)) == ["--dry-run"]


def test_a_verb_with_stray_whitespace_is_still_an_uninstall():
    assert len(picker.apply_rows("  uninstall\n")) == 2


def test_no_row_carries_a_separator_or_a_newline_into_the_quick_pick():
    """Every field is positional: one stray `|` shifts a row's fields, and one stray
    newline draws a second, unpickable row holding a value nobody wrote."""
    for row in [*picker.verb_rows(), *picker.apply_rows("uninstall")]:
        assert len(row.split("|")) == 4
        assert "\n" not in row


def test_drawing_a_picker_prints_the_rows_and_nothing_else(capsys):
    """A picker's stdout IS the quick-pick, so a status line, a warning or a progress
    message on this path is an extra option a person can tick."""
    assert picker.main(["verb"]) == 0
    assert capsys.readouterr().out.splitlines() == picker.verb_rows()


def test_the_picker_cannot_reach_the_machine_at_all():
    """The seam that took this out of `installers.py`, asserted rather than described:
    drawing a list resolves no checkout, spawns nothing and writes no artifact, and the
    way to keep that true is to import none of what could. A picker is a person watching
    an empty box, so the work it does is the wait they experience."""
    tree = ast.parse((REPO_ROOT / "scripts" / "installer_picker.py").read_text(encoding="utf-8"))
    imported = {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not imported & {"subprocess", "sweep", "installers", "devkit_schtasks", "harness_state"}


def test_drawing_the_apply_picker_reads_the_verb_it_was_handed(capsys):
    assert picker.main(["apply", "--verb", "uninstall"]) == 0
    assert capsys.readouterr().out.splitlines() == picker.apply_rows("uninstall")
