"""`scripts/agent_worktrees.py`: what git's output means, and what may be removed.

Every decision in that module is pure, so this suite drives the shapes `git worktree
list` and `git status` actually return and never a repository.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from support import load_script

# Loaded by path, like every other script module here, so this file needs no `sys.path`
# bootstrap of its own. The name it registers is the one `agent-worktree.py` imports, so
# both suites and the script under test share one copy.
aw = load_script("scripts/agent_worktrees.py")

CHECKOUT = Path("C:/ws/devkit")

# Codex reads its state directory from `CODEX_HOME`. Passed explicitly rather than
# monkeypatched, because these functions are pure and a suite that let one of them read
# the machine would pass or fail by whose desktop it ran on.
CODEX_ENV = {"CODEX_HOME": "C:/home/.codex"}


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


def test_a_new_worktree_is_cut_in_the_default_tier():
    """`create` and `fix-prs.cut_tree` share this answer with everything that later has
    to *find* what they cut, which is why it is one function rather than a literal in
    each. The default tier is the nested one: a detached tier's directory is keyed by a
    digest only that runtime knows how to compute."""
    assert aw.default_root(CHECKOUT) == Path("C:/ws/devkit/.claude/worktrees")


def test_the_tier_summary_names_every_directory_a_worktree_can_be_in():
    """Two messages tell somebody a worktree is *outside* the tiers, which is only a
    useful sentence if it says what they are -- and it is built from the tier list so it
    cannot fall behind one that gets added."""
    summary = aw.TIER_SUMMARY
    assert ".claude/worktrees" in summary
    assert "~/.codex/worktrees" in summary


def test_only_the_immediate_children_of_the_worktrees_directory_count():
    """The checkout itself, a box beside it and a nested worktree are all excluded.

    The box case is the one worth pinning: `<workspace>/.worktrees/` and
    `<checkout>/.claude/worktrees/` are different tiers with different lifecycles, and a
    `remove` that took a box would be stranding a port lease.
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


def test_owned_reads_membership_off_the_tier_and_never_re_derives_it():
    """The predicate `nested` and `holder` share. Git ran in the checkout, so a listed
    path already belongs to it -- what is still asked is which tier it is in, and, for a
    nested one only, that the checkout it hangs off is this one."""
    home = CODEX_ENV["CODEX_HOME"]
    assert aw.owned(CHECKOUT, "C:/ws/devkit/.claude/worktrees/topic", CODEX_ENV)
    assert aw.owned(CHECKOUT, f"{home}/worktrees/2e51/devkit", CODEX_ENV)
    # The checkout itself, a box of the other tier, and one cut by hand: none is ours.
    assert not aw.owned(CHECKOUT, "C:/ws/devkit", CODEX_ENV)
    assert not aw.owned(CHECKOUT, "C:/ws/.worktrees/devkit--topic-0905", CODEX_ENV)
    assert not aw.owned(CHECKOUT, "C:/ws/scratch/by-hand", CODEX_ENV)
    # A nested worktree hanging off SOMEBODY ELSE's checkout belongs in their menu.
    assert not aw.owned(CHECKOUT, "C:/ws/other/.claude/worktrees/topic", CODEX_ENV)


def test_a_codex_worktree_of_this_checkout_is_listed_and_labelled_by_its_agent():
    """`codex --worktree` cuts OUTSIDE the checkout, under its own home behind a digest,
    so no prefix of `project_dir` appears in the path at all. Git listed it, which is the
    whole proof of ownership this needs -- and re-deriving one would mean reading the
    worktree's `.git`, which this module may not do.
    """
    home = CODEX_ENV["CODEX_HOME"]
    text = porcelain(
        ("C:/ws/devkit", "main"),
        ("C:/ws/devkit/.claude/worktrees/topic", "agent/topic-0905"),
        (f"{home}/worktrees/2e51/devkit", "agent/codex-0905"),
    )
    assert aw.nested(CHECKOUT, text, CODEX_ENV) == [
        ("codex/devkit", f"{home}/worktrees/2e51/devkit", "agent/codex-0905"),
        ("topic", "C:/ws/devkit/.claude/worktrees/topic", "agent/topic-0905"),
    ]


def test_two_tiers_holding_one_directory_name_are_still_two_rows():
    """`remove` resolves a named token back to a worktree by its name, so a collision
    would remove whichever the scan happened to list first -- which is not a failure
    anybody would report, because both entries look right."""
    home = CODEX_ENV["CODEX_HOME"]
    text = porcelain(
        ("C:/ws/devkit/.claude/worktrees/fix-320", "agent/fix-320"),
        (f"{home}/worktrees/2e51/fix-320", "agent/other-320"),
    )
    names = [name for name, _, _ in aw.nested(CHECKOUT, text, CODEX_ENV)]
    assert names == ["codex/fix-320", "fix-320"]


def test_a_codex_shaped_path_under_some_other_directory_is_not_a_worktree_of_the_tier():
    """A project that happens to keep `worktrees/<x>/<y>` is not Codex's home, and a
    `remove` that took its contents would be removing somebody else's directories.
    """
    text = porcelain(("C:/ws/scratch/worktrees/2e51/thing", "agent/x"))
    assert aw.nested(CHECKOUT, text, CODEX_ENV) == []


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


def test_a_pick_keeps_a_tail_that_contains_slashes():
    """The tail may contain slashes -- a base branch is `agent/topic-0905` more often
    than not -- so only the first separator splits."""
    assert aw.parse_pick("devkit:agent/topic-0905") == ("devkit", "agent/topic-0905")


@pytest.mark.parametrize("token", ["", "devkit", ":topic", "devkit:"])
def test_a_malformed_pick_reads_as_nothing(token):
    """The caller reports that nothing was named and runs nothing."""
    assert aw.parse_pick(token) is None


def test_named_tokens_split_on_the_separator_and_lose_duplicates():
    assert aw.split_picks("devkit:a  carameli:b devkit:a") == ["devkit:a", "carameli:b"]
