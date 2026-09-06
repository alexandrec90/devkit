"""`scripts/agent_worktrees.py`: what git's output means, and what the dropdowns draw.

Every decision in that module is pure, so this suite drives the shapes `git worktree
list` and `git status` actually return and never a repository.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

import pytest
from support import load_script

# Loaded by path, like every other script module here, so this file needs no `sys.path`
# bootstrap of its own. The name it registers is the one `agent-worktree.py` imports, so
# both suites and the script under test share one copy.
aw = load_script("scripts/agent_worktrees.py")

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


def test_the_worktrees_root_is_a_lowercased_posix_prefix():
    """What every path comparison in this module is made against. A trailing slash so
    `.claude/worktrees-old/` cannot prefix-match `.claude/worktrees/`, and the case fold
    because the filesystem this runs on has one."""
    assert aw.worktrees_root(CHECKOUT) == "c:/ws/devkit/.claude/worktrees/"
    assert aw.worktrees_root(Path("C:/ws/DevKit/")) == "c:/ws/devkit/.claude/worktrees/"


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


# --- finding, naming and cutting one on a branch that already exists -----------------


def test_a_worktree_of_this_checkouts_own_tier_is_reported_as_reusable():
    """`fix-prs`'s ordinary second click on a PR: the worktree from the first is still
    there, and cutting again would fail on the very thing that means "ready"."""
    text = porcelain(
        ("C:/ws/devkit", "main"),
        ("C:/ws/devkit/.claude/worktrees/pr-256", "agent/fix-pr-256"),
    )
    assert aw.holder(CHECKOUT, text, "agent/fix-pr-256") == (
        "C:/ws/devkit/.claude/worktrees/pr-256",
        True,
    )


@pytest.mark.parametrize(
    "path",
    [
        "C:/ws/devkit",  # the checkout itself, sitting on the PR branch
        "C:/ws/.worktrees/devkit--fix-pr-256-0905",  # a box of the other tier
        "C:/ws/scratch/by-hand",  # somebody's own worktree, anywhere at all
    ],
)
def test_a_branch_held_outside_the_tier_is_reported_as_held_not_as_reusable(path):
    """The bool is the whole point: adopting one of these would put an agent in a tree
    it does not own, and cutting anyway fails talking about the branch rather than about
    the directory that is the actual obstacle."""
    text = porcelain(("C:/ws/devkit", "main"), (path, "agent/fix-pr-256"))
    assert aw.holder(CHECKOUT, text, "agent/fix-pr-256") == (path, False)


def test_a_branch_nothing_holds_is_the_empty_answer():
    text = porcelain(("C:/ws/devkit", "main"))
    assert aw.holder(CHECKOUT, text, "agent/fix-pr-256") == ("", False)


def test_a_detached_worktree_never_matches_a_branch():
    """`parse_worktree_list` keeps detached entries so they stay deletable, and an empty
    branch must not compare equal to a caller that asked for one."""
    text = porcelain(("C:/ws/devkit/.claude/worktrees/loose", ""))
    assert aw.holder(CHECKOUT, text, "") == ("", False)


def test_the_holder_comparison_ignores_case_like_the_listing_does():
    text = porcelain(("c:/WS/DevKit/.claude/worktrees/Topic", "agent/topic-0905"))
    assert aw.holder(CHECKOUT, text, "agent/topic-0905")[1] is True


@pytest.mark.parametrize(
    ("branch", "expected"),
    [
        ("agent/voicemail-0905", "voicemail-0905"),  # `create`'s own spelling
        ("pr304", "pr304"),  # a head branch with no prefix at all
        ("dependabot/npm_and_yarn/ws-8.18.0", "ws-8.18.0"),  # several segments
        ("feature/it works!", "it-works"),  # characters a directory cannot hold
        ("///", "worktree"),  # nothing survives sanitising
    ],
)
def test_the_directory_name_is_the_branchs_last_segment_made_safe(branch, expected):
    """A PR head branch is written by whoever opened the PR, so this cannot assume the
    `agent/<slug>-<mmdd>` shape the `new` verb mints."""
    assert aw.tree_name(branch, []) == expected


def test_a_name_already_on_disk_takes_a_counter():
    """Two PRs whose heads end in the same segment want two worktrees; the second one
    landing in the first one's directory is what this prevents."""
    assert aw.tree_name("alice/fix", ["fix"]) == "fix-2"
    assert aw.tree_name("alice/fix", ["fix", "fix-2", "FIX-3"]) == "fix-4"


def test_the_counter_compares_names_case_insensitively():
    """The filesystem this runs on does, so a name that differs only in case is the
    same directory and `git worktree add` would refuse it."""
    assert aw.tree_name("agent/Topic", ["topic"]) == "Topic-2"


def test_a_branch_this_checkout_already_has_is_checked_out_as_it_stands():
    """`worktree.resume_plan`'s reason: the local branch may carry commits no remote has
    -- a box reaped while its work was open leaves exactly that -- and re-creating it
    from origin is the one move that discards them."""
    assert aw.add_steps("agent/x", "C:/p/.claude/worktrees/x", True) == (
        "worktree",
        "add",
        "C:/p/.claude/worktrees/x",
        "agent/x",
    )


def test_a_branch_only_origin_has_is_cut_tracking_its_own_remote():
    """`--track`, where `create` is emphatic about `--no-track`, and for the same reason
    read the other way round: the upstream is the branch's own remote, so a bare push
    from the worktree lands where the PR is looking."""
    assert aw.add_steps("agent/x", "C:/p/.claude/worktrees/x", False) == (
        "worktree",
        "add",
        "--track",
        "-b",
        "agent/x",
        "C:/p/.claude/worktrees/x",
        "origin/agent/x",
    )


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


def test_a_worktree_row_leads_with_the_name_and_the_cost_of_ticking_it():
    """The label is what the checkbox shows; the description is the whole reason the row
    exists rather than a bare list of directories."""
    row = aw.tree_row("devkit", aw.Tree("topic", "C:/w/topic", "agent/topic-0905", 1, 2))
    assert row["value"] == "devkit:topic"
    assert row["label"] == "topic"
    assert row["description"] == "1 uncommitted path(s), 2 unpushed commit(s)"
    assert row["detail"] == "agent/topic-0905 -- C:/w/topic"


def test_a_detached_worktree_says_so_where_its_branch_would_be():
    """`detail` is the only field that names the branch, so an empty one there would be a
    row that looks like it lost half its text."""
    assert "detached HEAD" in aw.tree_row("devkit", aw.Tree("loose", "C:/w", ""))["detail"]


def test_a_base_row_carries_the_branch_name_not_the_remote_ref():
    """The CLI takes a branch and resolves which ref it means, so the same string works
    whether it was ticked here or typed. `detail` is where the resolution is shown."""
    row = aw.base_row("devkit", "agent/topic-0905", "last commit 2 hours ago")
    assert row["value"] == "devkit:agent/topic-0905"
    assert row["label"] == "agent/topic-0905"
    assert row["description"] == "last commit 2 hours ago"
    assert row["detail"] == "cut the new branch from origin/agent/topic-0905"


def test_the_placeholder_row_resolves_to_the_sentinel_and_says_it_does_nothing():
    """It exists only to keep an array non-empty, so it has to be unmistakable in the
    dropdown and unmistakable to `parse_pick`."""
    row = aw.placeholder_row("devkit", "no worktrees", "nothing here")
    assert row["value"] == f"devkit:{aw.NOTHING}"
    assert aw.parse_pick(row["value"]) is None
    assert "runs nothing" in row["detail"]


def test_every_row_carries_every_field_as_a_string():
    """The extension appends options until an expression THROWS, and `undefined` does not
    throw — so a row missing one templated field draws ten thousand blank entries instead
    of ending the list. Asserted over every list the payload holds."""
    trees = {"devkit": [aw.Tree("topic", "C:/w", "agent/topic-0905", 1, 0)], "carameli": []}
    bases = {"devkit": [("main", "the default branch")], "carameli": []}
    payload = aw.menu_payload(trees, bases, NOW)
    lists = [*payload["rows"].values(), *payload["bases"].values()]
    for row in [entry for group in lists for entry in group]:
        assert set(row) == {"value", "label", "description", "detail"}
        assert all(isinstance(value, str) for value in row.values())


def test_a_checkout_with_nothing_still_draws_one_row_in_each_list():
    """An empty array would end the dropdown at the first empty checkout and hide every
    one after it — which is the failure mode, not a short list."""
    payload = aw.menu_payload({"devkit": []}, {"devkit": []}, NOW)
    assert [row["value"] for row in payload["rows"]["devkit"]] == [f"devkit:{aw.NOTHING}"]
    assert [row["value"] for row in payload["bases"]["devkit"]] == [f"devkit:{aw.NOTHING}"]


def test_the_checkout_with_the_most_worktrees_is_offered_first():
    """The delete dropdown's top entry should be the checkout that has something to
    delete; alphabetical order gets that right only by luck."""
    trees = {
        "alpha": [],
        "zulu": [aw.Tree("a", "C:/a", "b"), aw.Tree("c", "C:/c", "d")],
        "mike": [aw.Tree("e", "C:/e", "f")],
    }
    payload = aw.menu_payload(trees, {}, NOW)
    assert [entry["name"] for entry in payload["projects"]] == ["zulu", "mike", "alpha"]


def test_each_checkout_row_carries_a_count_for_both_dropdowns():
    """One file feeds two menus, so the first pick has to describe what each of them will
    offer — the delete list's size is not the base list's."""
    trees = {"devkit": [aw.Tree("topic", "C:/w", "agent/topic-0905")]}
    payload = aw.menu_payload(trees, {"devkit": [("main", "the default branch")]}, NOW)
    entry = payload["projects"][0]
    assert "1 worktree(s)" in entry["worktrees"]
    assert "1 branch(es)" in entry["branches"]
    assert payload["asOf"] in entry["worktrees"]


def test_the_menu_is_written_atomically_and_reads_back(tmp_path):
    target = tmp_path / "logs" / "agent-worktrees.json"
    payload = aw.menu_payload({"devkit": []}, {"devkit": []}, NOW)
    assert aw.write_menu(payload, target) == target
    assert json.loads(target.read_text(encoding="utf-8"))["asOf"] == payload["asOf"]
    assert not list(target.parent.glob("*.tmp"))


def test_an_unwritable_menu_is_none_rather_than_a_raise(tmp_path):
    """It runs as a rider on `worktree.reconcile`; a stale dropdown is the cost of
    failing here, and a reconcile that stopped reaping boxes would be the cost of
    raising."""
    blocker = tmp_path / "logs"
    blocker.write_text("not a directory", encoding="utf-8")
    assert aw.write_menu({}, blocker / "agent-worktrees.json") is None
