"""`scripts/agent_worktrees.py`: what git's output means, and what the dropdowns draw.

Every decision in that module is pure, so this suite drives the shapes `git worktree
list` and `git status` actually return and never a repository.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest
from support import load_script

# Loaded by path, like every other script module here, so this file needs no `sys.path`
# bootstrap of its own. The name it registers is the one `agent-worktree.py` imports, so
# both suites and the script under test share one copy.
aw = load_script("scripts/agent_worktrees.py")
picker_rows = load_script("scripts/picker_rows.py")

NOW = _dt.datetime(2026, 9, 5, 12, 0, tzinfo=_dt.UTC)
CHECKOUT = Path("C:/ws/devkit")


def porcelain(*entries: tuple[str, str]) -> str:
    """`git worktree list --porcelain` output for `(path, branch)` pairs.

    Reproduced rather than abbreviated: the parser reads two of the three line kinds and
    has to step over the third, and a fixture that omitted `HEAD` would never prove it.
    """
    blocks = []
    for path, branch in entries:
        lines = [f"worktree {path}", "HEAD 1a2b3c4d"]
        lines.append(f"branch refs/heads/{branch}" if branch else "detached")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


def test_the_parser_reads_every_worktree_including_the_detached_one():
    """A detached worktree still occupies the directory, so it is still deletable."""
    text = porcelain(
        ("C:/ws/devkit", "main"),
        ("C:/ws/devkit/.claude/worktrees/topic", "agent/topic-0905"),
        ("C:/ws/devkit/.claude/worktrees/loose", ""),
    )
    assert aw.parse_worktree_list(text) == [
        ("C:/ws/devkit", "main"),
        ("C:/ws/devkit/.claude/worktrees/topic", "agent/topic-0905"),
        ("C:/ws/devkit/.claude/worktrees/loose", ""),
    ]


def test_the_parser_survives_empty_and_junk_input():
    """`trees_for` hands it whatever git wrote, including nothing at all."""
    assert aw.parse_worktree_list("") == []
    assert aw.parse_worktree_list("bare\nHEAD 1a2b\n") == []


def test_only_the_immediate_children_of_the_worktrees_directory_count():
    """The checkout itself, a box beside it and a nested worktree are all excluded.

    The box case is the one worth pinning: `<workspace>/.worktrees/` and
    `<checkout>/.claude/worktrees/` are different tiers with different lifecycles, and a
    delete menu that offered a box would be offering to strand a port lease.
    """
    text = porcelain(
        ("C:/ws/devkit", "main"),
        ("C:/ws/.worktrees/devkit--topic-0905", "agent/topic-0905"),
        ("C:/ws/devkit/.claude/worktrees/topic", "agent/topic-0905"),
        ("C:/ws/devkit/.claude/worktrees/topic/.claude/worktrees/deeper", "agent/deeper-0905"),
    )
    assert aw.nested(CHECKOUT, text) == [
        ("topic", "C:/ws/devkit/.claude/worktrees/topic", "agent/topic-0905")
    ]


def test_the_path_comparison_ignores_case_and_slash_direction():
    """Git prints forward slashes on Windows and the drive letter's case is not fixed.

    A `Path.resolve()` comparison would have handled both and touched the filesystem;
    this keeps the function pure, so the fold has to be asserted rather than assumed.
    """
    text = porcelain(("c:/WS/DevKit/.claude/worktrees/Topic", "agent/topic-0905"))
    assert [name for name, _, _ in aw.nested(CHECKOUT, text)] == ["Topic"]


@pytest.mark.parametrize(
    ("dirty", "unpushed", "forced", "expected"),
    [
        (0, 0, False, aw.REMOVE),
        (3, 0, False, aw.KEEP),
        (0, 2, False, aw.KEEP),
        (3, 2, True, aw.FORCE),
        (0, 0, True, aw.FORCE),
    ],
)
def test_removal_refuses_anything_that_exists_in_one_place_only(dirty, unpushed, forced, expected):
    """Unpushed commits are refused as firmly as uncommitted files, and that is the half
    `git worktree remove` cannot do for itself: a clean tree three commits ahead of the
    remote is one it removes without a word."""
    tree = aw.Tree("topic", "C:/w", "agent/topic-0905", dirty, unpushed)
    verdict, _ = aw.removal_decision(tree, forced)
    assert verdict == expected


def test_a_refusal_names_what_would_have_been_lost():
    """The whole point of refusing rather than removing: the operator has to be able to
    tell which of five ticked rows stopped, and why, without opening any of them."""
    tree = aw.Tree("topic", "C:/w", "agent/topic-0905", dirty=2, unpushed=1)
    _, reason = aw.removal_decision(tree, forced=False)
    assert "topic" in reason
    assert "2 uncommitted path(s)" in reason
    assert "1 unpushed commit(s)" in reason


def test_a_clean_tree_says_so_rather_than_saying_nothing():
    assert aw.Tree("t", "C:/w", "b").state() == "clean and pushed"


def test_a_pick_survives_a_round_trip_through_the_dropdown():
    """The value is one token because a VS Code input resolves to one string, and the
    tail may contain slashes — a base branch is `agent/topic-0905` more often than not."""
    token = aw.pick_value("devkit", "agent/topic-0905")
    assert aw.parse_pick(token) == ("devkit", "agent/topic-0905")


@pytest.mark.parametrize("token", ["", "devkit", ":topic", "devkit:", f"devkit:{aw.NOTHING}"])
def test_the_sentinel_and_the_malformed_pick_both_read_as_nothing(token):
    """One `None` for both, because the caller does the same thing with either: report
    that nothing was chosen and run nothing."""
    assert aw.parse_pick(token) is None


def test_ticked_rows_split_on_the_separator_and_lose_duplicates():
    assert aw.split_picks("devkit:a  carameli:b devkit:a") == ["devkit:a", "carameli:b"]


def fields(line: str) -> list[str]:
    return line.split(picker_rows.FIELD_SEP)


def test_a_worktree_row_leads_with_the_name_and_the_cost_of_ticking_it():
    """The label is what the checkbox shows; the description is the whole reason the row
    exists rather than a bare list of directories, and it names the checkout because the
    list is flat."""
    row = aw.tree_row("devkit", aw.Tree("topic", "C:/w/topic", "agent/topic-0905", 1, 2))
    assert fields(row) == [
        "devkit:topic",
        "topic",
        "devkit -- 1 uncommitted path(s), 2 unpushed commit(s)",
        "agent/topic-0905 -- C:/w/topic",
    ]


def test_a_detached_worktree_says_so_where_its_branch_would_be():
    """`detail` is the only field that names the branch, so an empty one there would be a
    row that looks like it lost half its text."""
    assert "detached HEAD" in fields(aw.tree_row("devkit", aw.Tree("loose", "C:/w", "")))[3]


def test_a_base_row_carries_the_branch_name_not_the_remote_ref():
    """The CLI takes a branch and resolves which ref it means, so the same string works
    whether it was ticked here or typed. `detail` is where the resolution is shown."""
    row = aw.base_row("devkit", "agent/topic-0905", "last commit 2 hours ago")
    assert fields(row) == [
        "devkit:agent/topic-0905",
        "agent/topic-0905",
        "devkit -- last commit 2 hours ago",
        "cut the new branch from origin/agent/topic-0905",
    ]


def test_a_machine_with_no_worktrees_draws_the_sentinel_rather_than_no_rows():
    """An empty quick-pick cannot be told apart from a command that failed to run, so
    "there are none" is stated as a row -- and `parse_pick` answers it with nothing."""
    drawn = aw.tree_rows({"devkit": [], "carameli": []})
    assert len(drawn) == 1
    assert aw.parse_pick(fields(drawn[0])[0]) is None
    assert fields(drawn[0])[3] == "picking this runs nothing"


def test_no_branches_anywhere_draws_the_sentinel_too():
    drawn = aw.base_rows({"devkit": []})
    assert len(drawn) == 1
    assert aw.parse_pick(fields(drawn[0])[0]) is None


def test_the_checkout_with_the_most_worktrees_is_offered_first():
    """Whoever opened the delete list wants to delete something, so the rows of the
    checkout that has several belong above the one that has none. Alphabetical order gets
    that right only by luck."""
    trees = {
        "alpha": [],
        "zulu": [aw.Tree("a", "C:/a", "b"), aw.Tree("c", "C:/c", "d")],
        "mike": [aw.Tree("e", "C:/e", "f")],
    }
    drawn = [fields(line)[0] for line in aw.tree_rows(trees)]
    assert drawn == ["zulu:a", "zulu:c", "mike:e"]


def test_the_base_rows_are_alphabetical_by_checkout():
    """Where `tree_rows` sorts by count, and the difference is the question: this list is
    read to find a branch whose name you already know, so a stable position is worth more
    than putting the busiest checkout on top."""
    bases = {"zulu": [("main", "the default branch")], "alpha": [("main", "the default branch")]}
    assert [fields(line)[0] for line in aw.base_rows(bases)] == ["alpha:main", "zulu:main"]


def test_every_row_of_either_list_carries_four_fields():
    """The extension reads by position, so a row one field short makes the next reader
    take a description for a detail. `tests/test_picker_rows.py` owns what `cell` does to
    each field; this asserts both builders hand it four."""
    trees = {"devkit": [aw.Tree("topic", "C:/w", "agent/topic-0905", 1, 0)], "carameli": []}
    bases = {"devkit": [("main", "the default branch")], "carameli": []}
    for line in [*aw.tree_rows(trees), *aw.base_rows(bases)]:
        assert len(fields(line)) == 4
