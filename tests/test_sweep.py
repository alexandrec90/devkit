"""Tests for the cross-project ship sweep.

The two contract tests at the bottom (`test_classify_is_total`,
`test_every_actionable_verdict_has_a_plan`) are the ones that matter: together
they are the machine-checkable form of "nothing gets stranded". Everything above
them pins the individual decisions.
"""

import datetime as dt
import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from support import LIVE_WORKSPACE, needs_live_workspace, sweep

# Pinned so branch names are assertable: tb.branch_name() stamps -<mmdd>.
DATE = dt.date(2026, 7, 29)

State = sweep.State
classify = sweep.classify
parse_workspace = sweep.parse_workspace
plan_for = sweep.plan_for


def on_default(**overrides) -> State:
    """A checkout sitting on its default branch."""
    base = {"name": "proj", "default_branch": "main", "branch": "main", "host": "github"}
    return State(**{**base, **overrides})


def on_feature(**overrides) -> State:
    """A checkout on a task branch, one commit ahead, pushed."""
    base = {
        "name": "proj",
        "default_branch": "main",
        "branch": "claude/thing-0727",
        "host": "github",
        "ahead": 1,
        "upstream": "origin/claude/thing-0727",
        "unpushed": 0,
    }
    return State(**{**base, **overrides})


def on_anchor(**overrides) -> State:
    """A linked worktree on its long-lived anchor branch (the `carameli-b` case)."""
    base = {
        "name": "proj-b",
        "default_branch": "main",
        "branch": "proj-b",
        "host": "github",
        "linked": True,
        "local_branches": ("main", "proj-b"),
    }
    return State(**{**base, **overrides})


# --- workspace parsing ------------------------------------------------------

WORKSPACE = json.dumps(
    {
        "folders": [
            {"path": "carameli"},
            {"path": "carameli-b"},
            {"path": "ibkr_trader"},
            {"path": "devkit"},
            {"path": "VanillaLand"},
        ]
    }
)


def test_workspace_folders_are_read_in_file_order():
    assert parse_workspace(WORKSPACE, frozenset()) == [
        "carameli",
        "carameli-b",
        "ibkr_trader",
        "devkit",
        "VanillaLand",
    ]


def test_excluded_checkouts_are_dropped():
    # The default exclusion: VanillaLand is Azure DevOps with a `develop` base and
    # is a reference checkout, not one we ship from.
    assert "VanillaLand" not in parse_workspace(WORKSPACE)
    assert len(parse_workspace(WORKSPACE)) == 4


def test_nested_paths_reduce_to_the_checkout_name():
    text = json.dumps({"folders": [{"path": "nested/dir/proj"}, {"path": "win\\style\\other"}]})
    assert parse_workspace(text, frozenset()) == ["proj", "other"]


def test_malformed_workspace_reports_nothing_rather_than_raising():
    # A root-level task should print "no checkouts" and move on, not crash.
    assert parse_workspace("{not json") == []
    assert parse_workspace("[]") == []


def test_commented_workspace_is_read_not_silently_skipped():
    """The real file carries comments and the shared task block.

    Under `json.loads` this returned [] — the sweep would report "no checkouts" and
    exit 0, which reads as "nothing to ship" rather than "I could not parse the
    registry". That failure is invisible in exactly the situation the sweep exists
    to catch, so it gets a regression test.
    """
    text = """{
        // the checkouts this sweep manages
        "folders": [
            { "path": "carameli" },
            { "path": "devkit" },
        ],
        "tasks": { "version": "2.0.0", "tasks": [] }
    }"""
    assert parse_workspace(text, frozenset()) == ["carameli", "devkit"]


@needs_live_workspace
def test_the_real_workspace_file_still_lists_checkouts():
    """Guards the live registry, not a fixture: sweep must never see it as empty."""
    text = LIVE_WORKSPACE.read_text(encoding="utf-8")
    assert parse_workspace(text), "sweep reads no checkouts from the real workspace file"


def test_only_restricts_the_sweep_to_the_named_checkouts():
    names = ["carameli", "carameli-b", "devkit"]
    assert sweep.select(names, ["devkit"]) == (["devkit"], [])
    assert sweep.select(names, ["devkit", "carameli"]) == (["carameli", "devkit"], [])


def test_only_accepts_a_comma_delimited_multi_pick_value():
    names = ["carameli", "carameli-b", "devkit"]
    assert sweep.select(names, ["devkit,carameli"]) == (["carameli", "devkit"], [])


def test_no_only_sweeps_everything():
    names = ["carameli", "devkit"]
    assert sweep.select(names, None) == (names, [])
    assert sweep.select(names, []) == (names, [])


def test_an_unknown_only_name_is_reported_not_ignored():
    """A typo'd --only that swept nothing would report "nothing stranded" -- the one
    answer this tool must never give wrongly."""
    kept, unknown = sweep.select(["carameli", "devkit"], ["caramelli"])
    assert kept == []
    assert unknown == ["caramelli"]


def test_duplicate_folder_entries_are_swept_once():
    text = json.dumps({"folders": [{"path": "proj"}, {"path": "proj"}]})
    assert parse_workspace(text, frozenset()) == ["proj"]


# --- host detection ---------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/alexandrec90/devkit.git", "github"),
        ("git@github.com:alexandrec90/devkit.git", "github"),
        ("https://x@dev.azure.com/Coll/Proj/_git/Proj", "azure"),
        ("https://gitlab.com/x/y.git", "other"),
        ("", "none"),
    ],
)
def test_remote_host_picks_the_pr_api(url, expected):
    assert sweep.remote_host(url) == expected


def test_porcelain_lines_reduce_to_the_changed_paths():
    text = " M scripts/sweep.py\n?? logs/new.txt\nA  tests/test_sweep.py\n"
    assert sweep.parse_porcelain(text) == (
        "scripts/sweep.py",
        "logs/new.txt",
        "tests/test_sweep.py",
    )


def test_a_renamed_path_is_recorded_where_it_landed():
    # The old path does not exist after the commit; listing it would be a lie.
    assert sweep.parse_porcelain("R  old.py -> new.py\n") == ("new.py",)


def test_a_quoted_path_loses_the_display_quotes():
    assert sweep.parse_porcelain(' M "with space.py"\n') == ("with space.py",)


def test_an_empty_porcelain_is_no_files():
    assert sweep.parse_porcelain("") == ()
    assert sweep.parse_porcelain("\n  \n") == ()


def test_ahead_behind_survives_unparseable_output():
    assert sweep.parse_ahead_behind("4\t2") == (4, 2)
    assert sweep.parse_ahead_behind("") == (0, 0)
    assert sweep.parse_ahead_behind("fatal: bad revision") == (0, 0)


# --- classification: the default branch is where work strands ---------------


def test_dirty_on_the_default_branch_needs_a_branch_not_a_ship():
    # ship.py's is_shippable() refuses on the default branch, so a sweep that
    # called /ship here would just collect exit 3 and leave the work sitting.
    verdict, reason = classify(on_default(dirty=58))
    assert verdict == sweep.NEEDS_BRANCH
    assert "58 uncommitted" in reason


def test_commits_made_straight_to_the_default_branch_also_need_a_branch():
    verdict, reason = classify(on_default(ahead=2))
    assert verdict == sweep.NEEDS_BRANCH
    assert "straight to main" in reason


def test_dirty_and_ahead_on_the_default_branch_reports_both():
    _, reason = classify(on_default(dirty=3, ahead=2))
    assert "3 uncommitted" in reason
    assert "2 unpushed" in reason


def test_clean_but_behind_on_the_default_branch_only_needs_a_pull():
    assert classify(on_default(behind=31))[0] == sweep.NEEDS_PULL


def test_clean_and_current_on_the_default_branch_is_clean():
    assert classify(on_default())[0] == sweep.CLEAN


# --- classification: feature branches ---------------------------------------


def test_dirty_feature_branch_is_ready_to_ship():
    assert classify(on_feature(dirty=4))[0] == sweep.READY


def test_never_pushed_feature_branch_is_ready_to_ship():
    assert classify(on_feature(upstream="", unpushed=-1))[0] == sweep.READY


def test_locally_ahead_of_upstream_is_ready_to_ship():
    assert classify(on_feature(ahead=3, unpushed=2))[0] == sweep.READY


def test_fully_pushed_feature_branch_needs_its_pr_confirmed():
    assert classify(on_feature())[0] == sweep.NEEDS_PR


def test_task_branch_with_nothing_on_it_is_spent_not_clean():
    # Merged, or cut and never used. Either way the worktree is still parked on it,
    # so there is something for --sync to do -- reporting `clean` here is what made
    # "nothing stranded" print while a checkout sat on a dead branch.
    verdict, reason = classify(on_feature(ahead=0, unpushed=0))
    assert verdict == sweep.SPENT
    assert "spent" in reason


# --- classification: retired task branches ----------------------------------
# The branch policy permanently retires a branch whose PR merged, so git refuses
# every commit to it. Work left on one is stranded exactly like work on a home
# branch -- and before `needs-rebranch` existed it classified as READY, so `--ship`
# planned a commit that was rejected on every single run.


def retired(**overrides) -> State:
    """A task branch whose PR merged: pushed once, remote branch since deleted."""
    return on_feature(upstream="", unpushed=-1, upstream_gone=True, **overrides)


def test_a_retired_branch_carrying_work_needs_a_rebranch():
    verdict, reason = classify(retired(dirty=3))
    assert verdict == sweep.NEEDS_REBRANCH
    assert "3 uncommitted file(s)" in reason
    assert "retired" in reason


def test_a_retired_branch_with_unmerged_commits_also_needs_a_rebranch():
    # Pushing would recreate the deleted branch, and the policy's pre-push hook
    # refuses that for the same reason pre-commit refuses the commit.
    assert classify(retired(dirty=0, ahead=2))[0] == sweep.NEEDS_REBRANCH


def test_a_never_pushed_branch_is_not_retired():
    """The ordinary mid-task state -- cut by the branch-per-task hook, edited, not
    yet pushed. It has no upstream either, and shipping it is exactly right."""
    assert classify(on_feature(dirty=3, ahead=0, upstream="", unpushed=-1))[0] == sweep.READY


def test_a_live_task_branch_is_not_retired():
    assert classify(on_feature(dirty=3))[0] == sweep.READY


def test_a_retired_branch_with_nothing_on_it_is_still_spent():
    """Nothing to preserve, so cutting a branch would only leave a second dead one.
    `--sync` deletes this."""
    assert classify(retired(dirty=0, ahead=0))[0] == sweep.SPENT


def test_a_gone_upstream_on_a_home_branch_is_still_needs_branch():
    """`is_retired` is about task branches. A home branch whose upstream was deleted
    is the original stranding case, and cutting from HEAD is still the fix."""
    assert classify(on_anchor(dirty=2, upstream_gone=True))[0] == sweep.NEEDS_BRANCH


def test_a_merged_pr_retires_a_branch_whose_upstream_config_is_gone():
    """The regression. `branch.<name>.remote` is the cheap signal for "pushed, then
    deleted", but it is only a *convention* that git leaves it behind -- an explicit
    `--unset-upstream`, a re-`checkout -b` over the same name, or any tool that
    rewrites branch config clears it. devkit hit exactly this: PR #22 merged, the
    config entry was empty, so `upstream_gone` was False and the checkout classified
    as READY. `--ship` then planned a commit the pre-commit hook rejects every run.

    The merged PR is what the *hook* keys on, so keying on it here too is what makes
    classification and enforcement unable to disagree.
    """
    state = on_feature(dirty=9, ahead=0, upstream="", unpushed=-1, upstream_gone=False)
    assert classify(state)[0] == sweep.READY, "precondition: the cheap signal misses this"

    verdict, reason = classify(replace(state, pr_merged=True))
    assert verdict == sweep.NEEDS_REBRANCH
    # "remote branch is gone" would be false here: nothing went missing, the config
    # entry was never there. The reason has to name the signal that actually fired.
    assert "whose PR merged" in reason
    assert "remote branch is gone" not in reason


def test_a_merged_pr_with_nothing_carried_is_still_spent():
    """Same rule as `upstream_gone`: retirement only matters when work rides on it."""
    assert classify(on_feature(dirty=0, ahead=0, pr_merged=True))[0] == sweep.SPENT


def test_a_merged_pr_on_a_home_branch_does_not_retire_it():
    """A merged PR whose branch name happens to match a home branch is not this case.
    `is_retired` stays gated on `is_task_branch` for the same reason it always was."""
    assert classify(on_anchor(dirty=2, pr_merged=True))[0] == sweep.NEEDS_BRANCH


# --- classification: long-lived worktree anchors ----------------------------
# `carameli-b` and `ibkr-b` are permanent worktree branches, not task branches.
# Agents without the branch-per-task hook leave work sitting on them.


def test_dirty_on_a_worktree_anchor_needs_a_branch_not_a_ship():
    # ship.py's is_shippable() only refuses the *default* branch, so /ship would
    # happily open a PR from `proj-b` and turn a permanent branch into a PR branch.
    verdict, reason = classify(on_anchor(dirty=4))
    assert verdict == sweep.NEEDS_BRANCH
    assert "4 uncommitted" in reason
    assert "proj-b" in reason
    assert "not a agent/ task branch" in reason


def test_commits_straight_to_an_anchor_also_need_a_branch():
    assert classify(on_anchor(ahead=2))[0] == sweep.NEEDS_BRANCH


def test_a_clean_anchor_behind_the_base_just_needs_a_fast_forward():
    assert classify(on_anchor(behind=5))[0] == sweep.NEEDS_PULL


def test_a_clean_current_anchor_is_clean():
    assert classify(on_anchor())[0] == sweep.CLEAN


def test_a_checkout_already_on_a_task_branch_is_left_alone():
    # The user's "don't cut a second branch for carameli" case: it is already on a
    # managed task branch, so it is ready to ship, not stranded.
    assert classify(on_feature(dirty=9))[0] == sweep.READY


def test_codex_task_branches_are_managed_like_existing_claude_branches():
    assert classify(on_feature(branch="codex/fix-shipping-0813", dirty=1))[0] == sweep.READY


# --- classification: blocked and skipped ------------------------------------


def test_non_git_directory_is_skipped():
    assert classify(State(name="sports_betting", is_git=False))[0] == sweep.SKIPPED


def test_detached_head_is_blocked():
    assert classify(on_default(branch=""))[0] == sweep.BLOCKED


def test_unresolvable_default_branch_is_blocked():
    assert classify(on_default(default_branch=""))[0] == sweep.BLOCKED


def test_missing_origin_is_blocked():
    assert classify(on_feature(host="none"))[0] == sweep.BLOCKED


# --- plans ------------------------------------------------------------------


def test_stranded_work_is_told_to_cut_a_branch_before_shipping():
    state = on_default(dirty=7)
    plan = plan_for(state, sweep.NEEDS_BRANCH)
    assert "agent/" in plan[0]
    assert plan[-1].startswith("/ship")


def test_a_retired_branch_is_told_to_rebranch_before_shipping():
    plan = plan_for(retired(dirty=3), sweep.NEEDS_REBRANCH)
    assert any("--branch" in step for step in plan)
    assert any("retired" in step for step in plan)


def test_a_stale_branch_rebases_before_it_ships():
    plan = plan_for(on_feature(dirty=1, behind=4), sweep.READY)
    assert any("rebase origin/main" in step for step in plan)
    assert not any("merge origin/main" in step for step in plan)


def test_a_stale_branch_with_a_pr_merges_instead_of_rebasing():
    # Rebasing a branch with an open PR detaches its review threads.
    plan = plan_for(on_feature(behind=4), sweep.NEEDS_PR)
    assert any("merge origin/main" in step for step in plan)


def test_an_up_to_date_branch_gets_no_sync_step():
    assert not any("origin/main" in step for step in plan_for(on_feature(dirty=1), sweep.READY))


def test_conflicts_are_never_auto_resolved():
    plan = plan_for(on_feature(dirty=1, behind=2), sweep.READY)
    assert any("never auto-resolve" in step for step in plan)


# --- home_ref: where a worktree parks between tasks -------------------------


def test_a_recorded_anchor_wins():
    assert sweep.home_ref(on_feature(anchor="proj-b", linked=True)) == "proj-b"


def test_a_checkout_already_on_a_home_branch_is_already_there():
    assert sweep.home_ref(on_anchor()) == "proj-b"
    assert sweep.home_ref(on_default()) == "main"


def test_a_primary_worktree_falls_back_to_the_default_branch():
    assert sweep.home_ref(on_feature()) == "main"


def test_a_linked_worktree_falls_back_to_a_branch_named_after_its_directory():
    # It cannot use the default branch -- the primary worktree has it checked out.
    state = on_feature(name="proj-b", linked=True, local_branches=("main", "proj-b"))
    assert sweep.home_ref(state) == "proj-b"


def test_a_linked_worktree_with_no_resolvable_home_refuses_to_guess():
    # `ibkr_trader-b` the directory vs `ibkr-b` the branch: the names do not match,
    # so there is nothing to infer and sync must say so rather than pick.
    state = on_feature(name="ibkr_trader-b", linked=True, local_branches=("main", "ibkr-b"))
    assert sweep.home_ref(state) == ""
    assert sweep.sync_plan(state, sweep.SPENT).refusal


# --- step 1: --branch --------------------------------------------------------


def test_branching_cuts_a_task_branch_off_head_carrying_the_dirty_tree():
    plan = sweep.branch_plan(on_default(dirty=29), slug="sweep", today=DATE)
    assert plan.steps[0] == ("checkout", "-b", "agent/sweep-0729")
    # No base ref: branching off HEAD is what carries uncommitted work across.
    assert not any("origin/main" in step[-1] for step in plan.steps if step[0] == "checkout")


def test_branching_records_where_the_work_came_from():
    assert sweep.branch_plan(on_anchor(dirty=4), today=DATE).anchor == "proj-b"


def test_branching_resets_a_home_branch_that_carried_commits():
    # Safe only here: the commits are already on the new branch. Leaving it would
    # diverge the home branch forever once the PR merges as a squash.
    plan = sweep.branch_plan(on_default(ahead=2), today=DATE)
    assert plan.steps[1] == ("branch", "-f", "main", "origin/main")


def test_branching_leaves_an_unmoved_home_branch_alone():
    plan = sweep.branch_plan(on_default(dirty=3), today=DATE)
    assert not any(step[0] == "branch" for step in plan.steps)


def test_branching_refuses_a_checkout_already_on_a_task_branch():
    assert sweep.branch_plan(on_feature(dirty=9), today=DATE).refusal


def test_rebranching_cuts_a_fresh_branch_under_a_retired_one():
    plan = sweep.branch_plan(retired(dirty=3), today=DATE)
    assert not plan.refusal
    assert plan.steps[0][:2] == ("checkout", "-b")
    # Off HEAD, like every other cut here: a retired branch is normally behind the
    # base, and starting from the base could clobber the work being rescued.
    assert len(plan.steps[0]) == 3


def test_rebranching_keeps_the_topic_the_branch_was_named_for():
    """The retired branch was named from the task's own prompt, which is a better
    topic than any slug one workspace-wide sweep could supply."""
    plan = sweep.branch_plan(retired(dirty=1), slug="sweep", today=DATE)
    assert plan.steps[0][-1] == "agent/thing-0729"


def test_rebranching_never_records_the_dead_branch_as_home():
    """`anchor` is where `--sync` parks the worktree. Recording a retired task
    branch would send it straight back to the branch this cut exists to leave."""
    assert sweep.branch_plan(retired(dirty=1), today=DATE).anchor == ""


def test_rebranching_never_rewrites_the_retired_branch():
    """The home-branch reset is safe only because a home branch is not merged. This
    one is, so it belongs to `--sync`, which deletes it."""
    plan = sweep.branch_plan(retired(dirty=1, ahead=2), today=DATE)
    assert not any(step[0] == "branch" for step in plan.steps)


def test_the_slug_names_the_branch():
    # There is no prompt here to derive a topic from, so the caller supplies one;
    # `sweep` is the honest default rather than a fabricated description.
    plan = sweep.branch_plan(on_default(dirty=29), slug="ingestion connector settings", today=DATE)
    assert plan.steps[0][-1] == "agent/ingestion-connector-settings-0729"


def test_branch_names_do_not_collide_with_existing_ones():
    state = on_default(dirty=1, local_branches=("main", "agent/sweep-0729"))
    assert sweep.branch_plan(state, today=DATE).steps[0][-1] == "agent/sweep-0729-2"


def branch_sibling_pair(store="/repo/.git", **overrides):
    """Two worktrees of one repo, both stranded, both about to cut a branch.

    The `ibkr_trader`/`ibkr_trader-b` shape: one ref store, so `local_branches` is
    the same list for both and `branch_plan` independently picks the same free
    name for each.
    """
    primary = on_default(name="proj", dirty=9, git_common_dir=store, **overrides)
    linked = on_anchor(name="proj-b", dirty=3, git_common_dir=store, **overrides)
    return [(s, sweep.branch_plan(s, today=DATE)) for s in (primary, linked)]


def test_siblings_sharing_a_ref_store_plan_the_same_branch_name():
    """The bug, before the fix: nothing in `branch_plan` can see the other worktree."""
    (_, first), (_, second) = branch_sibling_pair()
    assert first.steps[0] == second.steps[0] == ("checkout", "-b", "agent/sweep-0729")


def test_dedupe_branch_names_bumps_the_second_sibling():
    """The regression test for `fatal: a branch named 'agent/sweep-0802' already
    exists` -- the second worktree was left stranded while the run reported OK."""
    first, second = sweep.dedupe_branch_names(branch_sibling_pair())
    assert first.steps[0] == ("checkout", "-b", "agent/sweep-0729")
    assert second.steps[0] == ("checkout", "-b", "agent/sweep-0729-2")


def test_dedupe_branch_names_keeps_bumping_past_a_taken_suffix():
    pairs = branch_sibling_pair()
    third = on_anchor(name="proj-c", dirty=1, git_common_dir="/repo/.git")
    pairs.append((third, sweep.branch_plan(third, today=DATE)))
    names = [plan.steps[0][-1] for plan in sweep.dedupe_branch_names(pairs)]
    assert names == ["agent/sweep-0729", "agent/sweep-0729-2", "agent/sweep-0729-3"]


def test_dedupe_branch_names_leaves_separate_clones_alone():
    """Two clones have two ref stores, so the same name in each is not a collision
    -- bumping one would name it after a conflict that does not exist."""
    pairs = branch_sibling_pair()
    pairs[1] = (replace(pairs[1][0], git_common_dir="/other/.git"), pairs[1][1])
    first, second = sweep.dedupe_branch_names(pairs)
    assert first.steps[0] == second.steps[0] == ("checkout", "-b", "agent/sweep-0729")


def test_dedupe_branch_names_does_not_pool_checkouts_git_would_not_identify():
    first, second = sweep.dedupe_branch_names(branch_sibling_pair(store=""))
    assert first.steps[0] == second.steps[0] == ("checkout", "-b", "agent/sweep-0729")


def test_dedupe_branch_names_preserves_the_rest_of_the_plan():
    # The bumped sibling still resets its home branch and records its anchor.
    pairs = branch_sibling_pair(ahead=2)
    _, second = sweep.dedupe_branch_names(pairs)
    assert ("branch", "-f", "proj-b", "origin/main") in second.steps
    assert second.anchor == "proj-b"


# --- step 2: --ship ----------------------------------------------------------


def test_shipping_commits_pushes_and_opens_a_pr():
    plan = sweep.ship_plan(on_feature(dirty=14), sweep.READY)
    assert plan.steps[0] == ("add", "-A")
    assert plan.steps[1][:2] == ("commit", "-m")
    assert plan.steps[2] == ("push", "-u", "origin", "claude/thing-0727")
    assert plan.pr_head == "claude/thing-0727"
    assert plan.pr_base == "main"
    assert plan.pr_title


def test_the_commit_message_describes_the_sweep_not_the_diff():
    """Nothing read the diff. A subject claiming to summarise it would be invented."""
    message = sweep.commit_message(on_feature(dirty=14, branch="claude/sweep-0802"))
    assert message == "sweep: park stranded work (14 file(s))"


def test_the_branch_supplies_the_scope_not_a_slug():
    """The branch was named from the task's own prompt when the work started. One
    sweep spans every repo, so a slug passed here would claim a shared topic that
    does not exist."""
    message = sweep.commit_message(on_feature(dirty=3, branch="claude/voicemail-drops-0802"))
    assert message == "sweep(voicemail-drops): park stranded work (3 file(s))"


@pytest.mark.parametrize(
    ("branch", "expected"),
    [
        ("claude/voicemail-drops-0802", "voicemail-drops"),
        # The -N collision suffix goes with the date stamp, not the topic.
        ("claude/voicemail-drops-0802-2", "voicemail-drops"),
        ("claude/sweep-0802", "sweep"),
        # A digit-heavy topic must not be mistaken for the stamp.
        ("claude/s-364-changes-file-branch-0802", "s-364-changes-file-branch"),
        ("main", ""),
        ("proj-b", ""),
    ],
)
def test_the_topic_is_recovered_from_the_branch_name(branch, expected):
    assert sweep.branch_topic(branch) == expected


def test_the_default_slug_is_not_repeated_as_a_scope():
    """`sweep` is --branch's "no topic given", not a topic named sweep."""
    assert not sweep.commit_message(on_feature(dirty=1, branch="claude/sweep-0802")).startswith(
        "sweep(sweep)"
    )


def test_a_clean_task_branch_with_nothing_unpushed_only_needs_its_pr():
    # NEEDS_PR: already pushed. Re-pushing is noise; the PR is the missing piece.
    plan = sweep.ship_plan(on_feature(), sweep.NEEDS_PR)
    assert plan.steps == ()
    assert plan.pr_title
    assert plan.acts


def test_a_never_pushed_branch_is_pushed_without_a_commit():
    state = on_feature(dirty=0, ahead=2, upstream="", unpushed=-1)
    steps = sweep.ship_plan(state, sweep.READY).steps
    assert steps == (("push", "-u", "origin", "claude/thing-0727"),)


def test_shipping_refuses_work_still_on_a_home_branch():
    """`--branch` is what moves work onto a task branch. Doing it implicitly here
    would let one --yes take an untouched home branch all the way to a PR."""
    plan = sweep.ship_plan(on_default(dirty=9), sweep.NEEDS_BRANCH)
    assert plan.refusal and "--branch first" in plan.refusal
    assert not plan.acts


def test_shipping_refuses_a_retired_branch_and_names_the_real_problem():
    """The generic refusal says "nothing on a task branch to ship", which is false
    here and sends the reader looking for the wrong thing."""
    plan = sweep.ship_plan(retired(dirty=3), sweep.NEEDS_REBRANCH)
    assert plan.refusal and "--branch first" in plan.refusal
    assert "retired" in plan.refusal
    assert "nothing on a task branch" not in plan.refusal
    assert not plan.acts


def test_shipping_refuses_anything_with_nothing_to_ship():
    for verdict in (sweep.SPENT, sweep.NEEDS_PULL, sweep.CLEAN):
        plan = sweep.ship_plan(on_feature(ahead=0), verdict)
        assert plan.refusal, verdict
        assert not plan.acts, verdict


def test_shipping_refuses_a_blocked_checkout():
    assert sweep.ship_plan(on_default(branch=""), sweep.BLOCKED).refusal


def test_shipping_never_publishes_a_home_branch():
    """The one unrecoverable mistake available to this mode."""
    state = on_default(dirty=5, branch="main")
    assert sweep.ship_plan(state, sweep.READY).refusal
    anchor = on_anchor(dirty=5)
    assert sweep.ship_plan(anchor, sweep.READY).refusal


def test_shipping_never_forces_and_never_bypasses_a_hook():
    """A gate that rejects the tree must stop the ship, not be argued past."""
    banned = {"--force", "-f", "--force-with-lease", "--no-verify", "-n", "--amend"}
    for verdict in (sweep.READY, sweep.NEEDS_PR):
        for state in (on_feature(dirty=7), on_feature(), on_feature(upstream="", unpushed=-1)):
            for step in sweep.ship_plan(state, verdict).steps:
                assert not (banned & set(step)), (state, step)
                assert step[0] not in {"reset", "rebase", "clean", "restore"}, step
                # Only ever this worktree's own task branch, only ever to origin.
                if step[0] == "push":
                    assert step[1:] == ("-u", "origin", state.branch), step


def test_the_pr_body_says_where_the_work_came_from_and_that_nothing_read_it():
    state = on_feature(dirty=2, dirty_files=("a.py", "b.py"), anchor="proj-b")
    body = sweep.pr_body(state)
    assert "proj-b" in body
    assert "Nothing has reviewed this" in body
    assert "- `a.py`" in body and "- `b.py`" in body


def test_the_pr_body_truncates_a_very_long_file_list():
    files = tuple(f"file{i}.py" for i in range(364))
    body = sweep.pr_body(on_feature(dirty=364, dirty_files=files), limit=50)
    assert "- `file0.py`" in body
    assert "- `file49.py`" in body
    assert "- `file50.py`" not in body
    assert "and 314 more" in body


def test_the_pr_body_degrades_to_the_count_without_a_file_list():
    body = sweep.pr_body(on_feature(dirty=5))
    assert "Changed paths" not in body
    assert body.strip()


# --- step 3: --sync ----------------------------------------------------------


def test_sync_returns_a_spent_worktree_home_and_deletes_the_branch():
    state = on_feature(ahead=0, unpushed=0, local_branches=("main", "claude/thing-0727"))
    steps = sweep.sync_plan(state, sweep.SPENT).steps
    assert ("checkout", "main") in steps
    assert ("merge", "--ff-only", "origin/main") in steps
    assert ("branch", "-d", "claude/thing-0727") in steps


def test_sync_deletes_the_branch_only_after_moving_off_it():
    # Order is the safety property: `branch -d` on a checked-out branch fails.
    steps = sweep.sync_plan(on_feature(ahead=0, unpushed=0), sweep.SPENT).steps
    assert steps.index(("checkout", "main")) < steps.index(("branch", "-d", "claude/thing-0727"))


def test_sync_never_force_deletes():
    state = on_feature(ahead=0, unpushed=0, merged_task_branches=("claude/old-0701",))
    assert all(step[:2] != ("branch", "-D") for step in sweep.sync_plan(state, sweep.SPENT).steps)


def test_sync_never_rewrites_history():
    # --ff-only is the whole safety story: a diverged branch errors, never merges.
    steps = sweep.sync_plan(on_default(behind=3), sweep.NEEDS_PULL).steps
    merges = [step for step in steps if step[0] == "merge"]
    assert merges and all("--ff-only" in step for step in merges)
    assert not any(step[0] in {"rebase", "reset", "push"} for step in steps)


def test_sync_reaps_merged_task_branches_it_is_not_standing_on():
    state = on_default(merged_task_branches=("claude/a-0701", "claude/b-0702"))
    steps = sweep.sync_plan(state, sweep.CLEAN).steps
    assert ("branch", "-d", "claude/a-0701") in steps
    assert ("branch", "-d", "claude/b-0702") in steps


def test_worktree_branches_are_parsed_from_the_porcelain_listing():
    text = (
        "worktree C:/x/carameli\nHEAD abc\nbranch refs/heads/claude/thing-0727\n\n"
        "worktree C:/x/carameli-b\nHEAD abc\nbranch refs/heads/carameli-b\n\n"
        "worktree C:/x/detached\nHEAD abc\ndetached\n"
    )
    assert sweep.parse_worktree_branches(text) == ("claude/thing-0727", "carameli-b")


def test_branch_upstreams_are_parsed_from_the_for_each_ref_listing():
    """`%(upstream:track)` is git's own answer to the question `branch -d` asks.
    Only `ahead` means `-d` will refuse: `behind` and an equal upstream make the
    branch an ancestor of it, and `[gone]` means there is no upstream ref left to
    consult, so `-d` falls back to HEAD."""
    text = (
        "main\t\n"
        "claude/rebased-0804\t[ahead 1]\n"
        "claude/diverged-0805\t[ahead 2, behind 3]\n"
        "claude/behind-0806\t[behind 8]\n"
        "claude/merged-0807\t[gone]\n"
        "proj-b\t\n"
    )
    names, ahead = sweep.parse_branch_upstreams(text)
    assert names == (
        "main",
        "claude/rebased-0804",
        "claude/diverged-0805",
        "claude/behind-0806",
        "claude/merged-0807",
        "proj-b",
    )
    assert ahead == ("claude/rebased-0804", "claude/diverged-0805")


def test_branch_upstreams_survive_a_listing_with_no_upstream_column():
    # `_out` returns "" for a git that would not answer, and a bare name is what
    # a branch with no upstream looks like once the trailing tab is stripped.
    assert sweep.parse_branch_upstreams("") == ((), ())
    assert sweep.parse_branch_upstreams("main\n\nproj-b\n") == (("main", "proj-b"), ())


def test_sync_never_deletes_a_branch_a_sibling_worktree_is_on():
    """Reaping is repo-wide, a checkout is per-worktree. `carameli-b` sees the
    branch `carameli` is mid-task on -- merged into the base, so it reads as
    abandoned -- and git would refuse the delete it proposes."""
    state = on_anchor(
        merged_task_branches=("claude/live-0729", "claude/dead-0701"),
        worktree_branches=("claude/live-0729", "proj-b"),
    )
    steps = sweep.sync_plan(state, sweep.CLEAN).steps
    assert ("branch", "-d", "claude/dead-0701") in steps
    assert ("branch", "-d", "claude/live-0729") not in steps


def sibling_pair(store="/repo/.git", **overrides):
    """Two linked worktrees of one repo, both seeing the same merged branch.

    The `carameli`/`carameli-b` shape: `--merged` is answered by the shared ref
    store, so the dead branch is in both states and lands in both plans.
    """
    primary = on_default(
        name="proj",
        merged_task_branches=("claude/dead-0701",),
        worktree_branches=("main", "proj-b"),
        git_common_dir=store,
        **overrides,
    )
    linked = on_anchor(
        merged_task_branches=("claude/dead-0701",),
        worktree_branches=("main", "proj-b"),
        git_common_dir=store,
        **overrides,
    )
    return [(s, sweep.sync_plan(s, sweep.CLEAN)) for s in (primary, linked)]


def test_dedupe_reaps_gives_a_shared_branch_to_the_first_checkout_only():
    """The carameli-b failure: both siblings plan the delete, the first one wins
    it, and the second no longer proposes a branch that will already be gone."""
    first, second = sweep.dedupe_reaps(sibling_pair())
    assert ("branch", "-d", "claude/dead-0701") in first.steps
    assert ("branch", "-d", "claude/dead-0701") not in second.steps


def test_dedupe_reaps_drops_the_unset_belonging_to_a_duplicate_delete():
    """The unset is part of the delete's claim, not a step of its own. Left behind
    it would run against a branch the winning sibling already deleted, and
    `--unset-upstream` on a branch that is gone is fatal -- aborting the loser's
    plan before it has parked."""
    pairs = sibling_pair(ahead_of_upstream=("claude/dead-0701",))
    first, second = sweep.dedupe_reaps(pairs)
    assert ("branch", "--unset-upstream", "claude/dead-0701") in first.steps
    assert not any(step[:2] == ("branch", "--unset-upstream") for step in second.steps)


def test_dedupe_reaps_leaves_the_loser_its_other_steps():
    # Only the duplicate delete is dropped -- the sibling still parks and fast-forwards.
    _, second = sweep.dedupe_reaps(sibling_pair())
    assert ("merge", "--ff-only", "origin/main") in second.steps


def test_dedupe_reaps_keeps_separate_clones_reaping_their_own_copy():
    """Two clones of one remote have two ref stores and two copies of the branch.
    Deduping them would leave the second copy undeleted forever."""
    pairs = sibling_pair()
    pairs[1] = (replace(pairs[1][0], git_common_dir="/other/.git"), pairs[1][1])
    first, second = sweep.dedupe_reaps(pairs)
    assert ("branch", "-d", "claude/dead-0701") in first.steps
    assert ("branch", "-d", "claude/dead-0701") in second.steps


def test_dedupe_reaps_does_not_pool_checkouts_git_would_not_identify():
    # An empty key is "unknown", not "same as every other unknown".
    first, second = sweep.dedupe_reaps(sibling_pair(store=""))
    assert ("branch", "-d", "claude/dead-0701") in first.steps
    assert ("branch", "-d", "claude/dead-0701") in second.steps


def test_sync_still_deletes_the_spent_branch_this_worktree_is_standing_on():
    # Our own branch is in worktree_branches too, but we check out `home` first.
    state = on_feature(
        ahead=0,
        unpushed=0,
        worktree_branches=("claude/thing-0727", "main"),
    )
    assert ("branch", "-d", "claude/thing-0727") in sweep.sync_plan(state, sweep.SPENT).steps


def test_sync_unsets_a_diverged_upstream_before_deleting_the_branch():
    """The data-lake/sports_betting failure: `--sync` proposed `branch -d` on a
    branch git had itself reported as merged into `origin/main`, and git refused
    it -- because `-d` consults the branch's *upstream* when it has one, and
    `origin/<branch>` still held the pre-rebase commits.

    Unsetting first is what makes `-d` ask the question the sweep actually cares
    about (is this work in the base branch?) instead of a question about pushing.
    """
    state = on_default(
        merged_task_branches=("claude/dead-0701",),
        ahead_of_upstream=("claude/dead-0701",),
    )
    steps = sweep.sync_plan(state, sweep.CLEAN).steps
    unset = steps.index(("branch", "--unset-upstream", "claude/dead-0701"))
    assert steps[unset + 1] == ("branch", "-d", "claude/dead-0701")


def test_sync_does_not_unset_an_upstream_that_would_not_refuse():
    """`--unset-upstream` is fatal on a branch that has none, and a failed step
    aborts the rest of the plan -- so the branches it is emitted for are exactly
    the ones `-d` would otherwise refuse, never every branch being reaped."""
    state = on_default(merged_task_branches=("claude/dead-0701",))
    steps = sweep.sync_plan(state, sweep.CLEAN).steps
    assert ("branch", "-d", "claude/dead-0701") in steps
    assert not any(step[:2] == ("branch", "--unset-upstream") for step in steps)


def test_sync_reaches_for_the_unset_rather_than_the_force():
    # -D would delete the branch whether or not the commits were anywhere else.
    # After the unset, `-d` still refuses unless the work is merged into HEAD.
    state = on_feature(
        ahead=0,
        unpushed=0,
        ahead_of_upstream=("claude/thing-0727",),
    )
    steps = sweep.sync_plan(state, sweep.SPENT).steps
    assert ("branch", "--unset-upstream", "claude/thing-0727") in steps
    assert all(step[:2] != ("branch", "-D") for step in steps)


def test_sync_never_deletes_the_home_branch():
    state = on_anchor(merged_task_branches=("proj-b", "claude/a-0701"))
    steps = sweep.sync_plan(state, sweep.CLEAN).steps
    assert not any(step == ("branch", "-d", "proj-b") for step in steps)


def test_sync_refuses_anything_with_unshipped_work():
    # The ordering the two steps depend on: syncing a PR still in review would move
    # the worktree off the branch under review.
    for verdict in (sweep.READY, sweep.NEEDS_PR, sweep.NEEDS_BRANCH):
        plan = sweep.sync_plan(on_feature(dirty=1), verdict)
        assert plan.refusal, verdict
        assert not plan.steps, verdict


def test_sync_refuses_a_blocked_checkout():
    assert sweep.sync_plan(on_default(branch=""), sweep.BLOCKED).refusal


def test_sync_records_the_home_branch_for_linked_worktrees_only():
    linked = on_feature(ahead=0, name="proj-b", linked=True, local_branches=("main", "proj-b"))
    assert sweep.sync_plan(linked, sweep.SPENT).anchor == "proj-b"
    # A primary can always resolve the default branch, so a stored value would only
    # be one more thing that can go stale.
    assert sweep.sync_plan(on_feature(ahead=0), sweep.SPENT).anchor == ""


def test_sync_skips_the_fetch_when_asked():
    steps = sweep.sync_plan(on_default(), sweep.CLEAN, fetch=False).steps
    assert not any(step[0] == "fetch" for step in steps)


# --- the "nothing stranded" contract ----------------------------------------

ALL_VERDICTS = {
    sweep.BLOCKED,
    sweep.NEEDS_BRANCH,
    sweep.NEEDS_REBRANCH,
    sweep.READY,
    sweep.NEEDS_PR,
    sweep.NEEDS_PULL,
    sweep.SPENT,
    sweep.CLEAN,
    sweep.SKIPPED,
}

# Every combination of the axes classify() actually branches on. `proj-b` is the
# third branch case -- neither the default branch nor a task branch -- and `linked`
# is the axis home_ref() turns on.
STATES = [
    State(
        name="proj",
        is_git=is_git,
        host=host,
        default_branch=default_branch,
        branch=branch,
        dirty=dirty,
        behind=behind,
        ahead=ahead,
        upstream=upstream,
        unpushed=unpushed,
        upstream_gone=upstream_gone,
        linked=linked,
    )
    for is_git in (True, False)
    for host in ("github", "azure", "none")
    for default_branch in ("main", "")
    for branch in ("main", "claude/x", "proj-b", "")
    for dirty in (0, 5)
    for behind in (0, 3)
    for ahead in (0, 2)
    for linked in (True, False)
    for upstream_gone in (True, False)
    for upstream, unpushed in (("", -1), ("origin/claude/x", 0), ("origin/claude/x", 2))
]


def test_classify_is_total():
    """No state falls through to an unknown verdict -- nothing can go unclassified."""
    for state in STATES:
        verdict, reason = classify(state)
        assert verdict in ALL_VERDICTS, state
        assert reason, state


def test_every_actionable_verdict_has_a_plan():
    """Anything the sweep flags comes with a concrete next action."""
    for state in STATES:
        verdict, _ = classify(state)
        plan = plan_for(state, verdict)
        if verdict in sweep.TERMINAL:
            assert plan == [], state
        else:
            assert plan, state


def test_actionable_and_terminal_verdicts_partition_the_space():
    assert sweep.ACTIONABLE | sweep.TERMINAL == ALL_VERDICTS
    assert not (sweep.ACTIONABLE & sweep.TERMINAL)


# --- the two-step contract ---------------------------------------------------
# Step 1 ships work out; step 2 tidies up after it merges. Together they have to
# cover every actionable verdict, or "ship them all, then sync them all" leaves
# something behind -- which is the whole point of the sweep.


def test_the_three_steps_cover_every_actionable_verdict():
    handled = sweep.BRANCHABLE | sweep.SHIPPABLE | sweep.SYNCABLE | {sweep.BLOCKED}
    assert handled >= sweep.ACTIONABLE
    # BLOCKED belongs to a human and is deliberately not a mode.
    assert sweep.BLOCKED not in (sweep.BRANCHABLE | sweep.SHIPPABLE | sweep.SYNCABLE)


def test_the_three_modes_never_claim_the_same_verdict():
    """Pairwise disjoint, so `--branch && --ship && --sync` is a pipeline rather
    than three modes fighting over one checkout."""
    assert not (sweep.BRANCHABLE & sweep.SHIPPABLE)
    assert not (sweep.BRANCHABLE & sweep.SYNCABLE)
    assert not (sweep.SHIPPABLE & sweep.SYNCABLE)


def test_every_mutating_plan_either_acts_or_says_why_not():
    """No silent no-ops: a plan with no steps and no refusal reads as 'done' and
    would let a checkout drop out of both steps unnoticed."""
    for state in STATES:
        verdict, _ = classify(state)
        if verdict == sweep.SKIPPED:
            continue
        plan = sweep.sync_plan(state, verdict)
        assert plan.steps or plan.refusal, (state, verdict)
        shipped = sweep.ship_plan(state, verdict)
        assert shipped.acts or shipped.refusal, (state, verdict)
        if verdict in sweep.BRANCHABLE:
            cut = sweep.branch_plan(state, today=DATE)
            assert cut.steps or cut.refusal, state


def test_no_mutating_plan_ever_emits_a_destructive_git_command():
    """The safety envelope, asserted over the whole state space rather than by
    reading the source: every step is one git refuses to run destructively."""
    banned = {"reset", "rebase", "push", "clean", "restore"}
    for state in STATES:
        verdict, _ = classify(state)
        plans = [sweep.sync_plan(state, verdict)]
        if verdict in sweep.BRANCHABLE:
            plans.append(sweep.branch_plan(state, today=DATE))
        for plan in plans:
            for step in plan.steps:
                assert step[0] not in banned, (state, step)
                assert step[:2] != ("branch", "-D"), (state, step)
                if step[0] == "merge":
                    assert "--ff-only" in step, (state, step)
                # `branch -f` only ever retargets the branch the work just left,
                # and only onto the remote's own tip.
                if step[:2] == ("branch", "-f"):
                    assert step[2] == state.branch, (state, step)
                    assert step[3] == f"origin/{state.default_branch}", (state, step)


# --- running a plan ----------------------------------------------------------


class ScriptedGit:
    """A `git(*args)` stand-in answering a canned map of argv prefix -> stdout.

    Anything unmatched exits non-zero, which is what `_out` reads as "git would not
    say" -- the same shape as a real repo that has no such ref or config entry.
    """

    def __init__(self, replies: dict[tuple[str, ...], str]):
        self.replies = replies

    def __call__(self, *args: str):
        for prefix, stdout in self.replies.items():
            if args[: len(prefix)] == prefix:
                return subprocess.CompletedProcess(["git", *args], 0, stdout, "")
        return subprocess.CompletedProcess(["git", *args], 1, "", "fatal: nope")


# The subset of `inspect`'s reads that decide the upstream question. Everything
# else it asks for is left unmatched and degrades to empty, which is fine here.
INSPECT_REPLIES = {
    ("remote", "get-url", "origin"): "https://github.com/o/r.git",
    ("branch", "--show-current"): "claude/thing-0727",
    ("status", "--porcelain"): " M app.py",
}


def inspect_with(tmp_path, replies) -> sweep.State:
    (tmp_path / ".git").mkdir()
    return sweep.inspect("proj", tmp_path, git=ScriptedGit(replies), fetch=False)


def test_inspect_reads_a_deleted_remote_branch_as_a_gone_upstream(tmp_path):
    """A failing `@{u}` says only "no upstream resolves". Deleting the remote branch
    does not clear `branch.<name>.remote`, so the surviving config entry is what
    makes this "pushed, then deleted" rather than "never pushed"."""
    state = inspect_with(
        tmp_path,
        {**INSPECT_REPLIES, ("config", "--get", "branch.claude/thing-0727.remote"): "origin"},
    )
    assert state.upstream == ""
    assert state.upstream_gone


def test_inspect_reads_the_branch_list_and_the_ahead_set_from_one_listing(tmp_path):
    """One `for-each-ref` answers both: which branches exist, and which of them
    carry commits their upstream lacks. Two calls would be two chances to drift."""
    state = inspect_with(
        tmp_path,
        {
            **INSPECT_REPLIES,
            ("for-each-ref",): "main\t\nclaude/thing-0727\t[ahead 1]\n",
        },
    )
    assert state.local_branches == ("main", "claude/thing-0727")
    assert state.ahead_of_upstream == ("claude/thing-0727",)


def test_inspect_reads_a_never_pushed_branch_as_simply_having_no_upstream(tmp_path):
    state = inspect_with(tmp_path, INSPECT_REPLIES)
    assert state.upstream == ""
    assert not state.upstream_gone


class ScriptedGh:
    """A `gh(*args)` stand-in returning one canned `pr list` payload.

    Records every call so a test can assert the probe was *not* made: the whole point
    of gating it is that an ordinary sweep does not pay a network round trip per
    checkout, and an assertion on the result alone would not catch a lost gate.
    """

    def __init__(self, payload: str = "[]", returncode: int = 0):
        self.payload = payload
        self.returncode = returncode
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str):
        self.calls.append(args)
        return subprocess.CompletedProcess(["gh", *args], self.returncode, self.payload, "")


MERGED_PAYLOAD = json.dumps([{"number": 22, "url": "https://github.com/o/r/pull/22"}])

# The devkit case: a task branch carrying uncommitted work, no upstream, and *no*
# surviving `branch.<name>.remote` entry for `upstream_gone` to read.
STRANDED_REPLIES = {**INSPECT_REPLIES, ("rev-list", "--left-right", "--count"): "0\t0"}


def inspect_probing(tmp_path, replies, gh) -> sweep.State:
    (tmp_path / ".git").mkdir()
    return sweep.inspect("proj", tmp_path, git=ScriptedGit(replies), gh=gh, fetch=True)


def test_inspect_falls_back_to_a_merged_pr_when_the_upstream_config_is_gone(tmp_path):
    gh = ScriptedGh(MERGED_PAYLOAD)
    state = inspect_probing(tmp_path, STRANDED_REPLIES, gh)
    assert not state.upstream_gone, "the cheap signal cannot see this one"
    assert state.pr_merged
    assert gh.calls, "the probe is the only thing that can catch this state"


def test_inspect_reads_no_merged_pr_as_an_ordinary_mid_task_branch(tmp_path):
    gh = ScriptedGh("[]")
    state = inspect_probing(tmp_path, STRANDED_REPLIES, gh)
    assert not state.pr_merged


def test_inspect_does_not_probe_when_the_upstream_config_already_answered(tmp_path):
    gh = ScriptedGh(MERGED_PAYLOAD)
    state = inspect_probing(
        tmp_path,
        {**STRANDED_REPLIES, ("config", "--get", "branch.claude/thing-0727.remote"): "origin"},
        gh,
    )
    assert state.upstream_gone
    assert not gh.calls


def test_inspect_does_not_probe_a_branch_with_a_live_upstream(tmp_path):
    gh = ScriptedGh(MERGED_PAYLOAD)
    live = {**STRANDED_REPLIES, ("rev-parse", "--abbrev-ref"): "origin/claude/thing-0727"}
    assert not inspect_probing(tmp_path, live, gh).pr_merged
    assert not gh.calls


def test_inspect_does_not_probe_a_clean_checkout(tmp_path):
    """No work riding on the branch means retirement changes no verdict -- `SPENT`
    either way -- so the round trip would buy nothing."""
    gh = ScriptedGh(MERGED_PAYLOAD)
    clean = {**STRANDED_REPLIES, ("status", "--porcelain"): ""}
    assert not inspect_probing(tmp_path, clean, gh).pr_merged
    assert not gh.calls


def test_inspect_does_not_probe_a_home_branch(tmp_path):
    gh = ScriptedGh(MERGED_PAYLOAD)
    home = {**STRANDED_REPLIES, ("branch", "--show-current"): "main"}
    assert not inspect_probing(tmp_path, home, gh).pr_merged
    assert not gh.calls


def test_inspect_skips_the_probe_when_the_network_is_off(tmp_path):
    """`fetch=False` is the no-network mode. A `gh` call is a network call."""
    gh = ScriptedGh(MERGED_PAYLOAD)
    (tmp_path / ".git").mkdir()
    state = sweep.inspect("proj", tmp_path, git=ScriptedGit(STRANDED_REPLIES), gh=gh, fetch=False)
    assert not state.pr_merged
    assert not gh.calls


def test_a_failing_gh_leaves_the_branch_unretired(tmp_path):
    """Fail open, deliberately. An unauthenticated or offline `gh` must not be able to
    invent a retirement -- the cost of missing one is the status quo (git refuses the
    commit and says why), while the cost of inventing one is a rebranch nobody wanted.
    """
    gh = ScriptedGh("not json", returncode=1)
    assert not inspect_probing(tmp_path, STRANDED_REPLIES, gh).pr_merged


def test_a_garbled_gh_payload_leaves_the_branch_unretired(tmp_path):
    assert not inspect_probing(tmp_path, STRANDED_REPLIES, ScriptedGh("not json")).pr_merged


def test_the_probe_asks_only_about_merged_prs_on_this_branch(tmp_path):
    gh = ScriptedGh(MERGED_PAYLOAD)
    inspect_probing(tmp_path, STRANDED_REPLIES, gh)
    argv = gh.calls[0]
    assert argv[:2] == ("pr", "list")
    assert "--head" in argv and "claude/thing-0727" in argv
    assert argv[argv.index("--state") + 1] == "merged"


class FakeGit:
    """A `git(*args)` stand-in that fails on the first step matching `fail_on`."""

    def __init__(self, fail_on: str = ""):
        self.fail_on = fail_on
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str):
        self.calls.append(args)
        failed = bool(self.fail_on) and self.fail_on in " ".join(args)
        return subprocess.CompletedProcess(
            args=["git", *args],
            returncode=1 if failed else 0,
            stdout="",
            stderr="fatal: nope" if failed else "",
        )


PLAN = sweep.Plan(
    steps=(("checkout", "main"), ("merge", "--ff-only", "origin/main"), ("branch", "-d", "gone")),
    anchor="",
)


def test_a_plan_runs_its_steps_in_order(tmp_path):
    git = FakeGit()
    result = sweep.apply_plan("proj", tmp_path, PLAN, git=git)
    assert result.ok
    assert git.calls == list(PLAN.steps)


def test_a_failing_step_stops_the_rest():
    """The steps are ordered so a later one is only safe once the earlier ones
    landed -- deleting a branch after a checkout that never happened is the exact
    way a safe plan turns destructive."""
    git = FakeGit(fail_on="merge")
    result = sweep.apply_plan("proj", Path("."), PLAN, git=git)
    assert not result.ok
    assert result.failed == "git merge --ff-only origin/main"
    assert "nope" in result.error
    assert ("branch", "-d", "gone") not in git.calls


class FakeGh:
    """A `gh(*args)` stand-in. `existing` is the URL `pr view` reports, "" for none."""

    def __init__(self, existing: str = "", create_fails: str = ""):
        self.existing = existing
        self.create_fails = create_fails
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str):
        self.calls.append(args)
        if args[:2] == ("pr", "view"):
            return subprocess.CompletedProcess(
                args=["gh", *args],
                returncode=0 if self.existing else 1,
                stdout=self.existing,
                stderr="" if self.existing else "no pull requests found",
            )
        if self.create_fails:
            return subprocess.CompletedProcess(
                args=["gh", *args], returncode=1, stdout="", stderr=self.create_fails
            )
        return subprocess.CompletedProcess(
            args=["gh", *args],
            returncode=0,
            stdout="https://github.com/o/r/pull/7\n",
            stderr="",
        )


SHIP_PLAN = sweep.ship_plan(on_feature(dirty=3), sweep.READY)


def test_the_pr_is_opened_ready_for_review():
    """A draft claims "not finished yet"; these are finished and pushed. What is
    unreviewed is the description, and the body says so in words -- a status flag
    that also suppresses review requests is the wrong tool for saying it."""
    gh = FakeGh()
    url, created, error = sweep.ensure_pr(gh, SHIP_PLAN)
    assert (url, created, error) == ("https://github.com/o/r/pull/7", True, "")
    assert "--draft" not in gh.calls[-1]
    assert gh.calls[-1][2:4] == ("--base", "main")


def test_the_pr_body_still_says_nothing_reviewed_it():
    # The warning moved out of the draft flag and into the text; it must not vanish.
    assert "Nothing has reviewed this" in sweep.pr_body(on_feature(dirty=3))


def test_an_existing_pr_is_reused_rather_than_recreated():
    """--ship has to be re-runnable: `gh pr create` on a branch that already has a
    PR fails with something that reads like a real error."""
    gh = FakeGh(existing="https://github.com/o/r/pull/2")
    url, created, error = sweep.ensure_pr(gh, SHIP_PLAN)
    assert (url, created, error) == ("https://github.com/o/r/pull/2", False, "")
    assert not any(call[:2] == ("pr", "create") for call in gh.calls)


def test_a_failed_pr_creation_is_reported_not_swallowed():
    _, created, error = sweep.ensure_pr(FakeGh(create_fails="gh: not authenticated"), SHIP_PLAN)
    assert not created
    assert "not authenticated" in error


LABELED_PLAN = sweep.Plan(
    pr_title="Adopt devkit v9.9.9",
    pr_body="body",
    pr_head="agent/upgrade",
    pr_base="main",
    pr_labels=(sweep.AUTOMERGE_LABEL,),
)


def test_a_labelled_plan_creates_the_label_before_the_pr_that_wears_it():
    """`gh pr create --label` fails outright on a label the repo has never
    carried, which is every repo before its first labelled PR."""
    gh = FakeGh()
    url, created, error = sweep.ensure_pr(gh, LABELED_PLAN)
    assert (url, created, error) == ("https://github.com/o/r/pull/7", True, "")
    label_at = gh.calls.index(
        (
            "label",
            "create",
            "automerge",
            "--force",
            "--color",
            "0e8a16",
            "--description",
            sweep.LABEL_SPECS["automerge"][1],
        )
    )
    create_at = next(i for i, call in enumerate(gh.calls) if call[:2] == ("pr", "create"))
    assert label_at < create_at
    create = gh.calls[create_at]
    assert ("--label", "automerge") == create[create.index("--label") : create.index("--label") + 2]


def test_a_reused_pr_still_gets_its_labels():
    # A reused PR may predate the caller learning to label; re-running must
    # converge on the labelled state, not skip it as already-shipped.
    gh = FakeGh(existing="https://github.com/o/r/pull/2")
    url, created, error = sweep.ensure_pr(gh, LABELED_PLAN)
    assert (url, created, error) == ("https://github.com/o/r/pull/2", False, "")
    assert ("pr", "edit", "agent/upgrade", "--add-label", "automerge") in gh.calls


def test_an_unlabelled_plan_issues_no_label_calls():
    gh = FakeGh(existing="https://github.com/o/r/pull/2")
    sweep.ensure_pr(gh, SHIP_PLAN)
    assert not any(call[0] == "label" or call[:2] == ("pr", "edit") for call in gh.calls)


def test_a_label_failure_on_a_reused_pr_is_an_error_that_still_names_the_url():
    """A plan that asked for `automerge` and did not get it is a PR that silently
    waits for the review it was labelled to skip -- but the PR itself is real."""
    url, created, error = sweep.ensure_pr(
        FakeGh(existing="https://github.com/o/r/pull/2", create_fails="gh: boom"), LABELED_PLAN
    )
    assert url == "https://github.com/o/r/pull/2"
    assert not created
    assert "https://github.com/o/r/pull/2" in error
    assert "boom" in error


def test_the_pr_is_opened_only_after_every_git_step_lands():
    """A PR on a branch whose push failed points at a ref the remote does not have."""
    git, gh = FakeGit(fail_on="push"), FakeGh()
    result = sweep.apply_plan("proj", Path("."), SHIP_PLAN, git=git, gh=gh)
    assert not result.ok
    assert result.failed == "git push -u origin claude/thing-0727"
    assert gh.calls == []


def test_a_failed_pr_fails_the_checkout():
    git, gh = FakeGit(), FakeGh(create_fails="gh: not authenticated")
    result = sweep.apply_plan("proj", Path("."), SHIP_PLAN, git=git, gh=gh)
    assert not result.ok
    assert "not authenticated" in result.error


def test_a_shipped_checkout_reports_its_pr_url(tmp_path):
    result = sweep.apply_plan("proj", tmp_path, SHIP_PLAN, git=FakeGit(), gh=FakeGh())
    assert result.ok
    assert result.pr_url == "https://github.com/o/r/pull/7"
    assert result.pr_created
    assert "https://github.com/o/r/pull/7" in sweep.render_applied([result])


def test_a_pr_only_plan_is_still_applied():
    """NEEDS_PR has no git steps at all. Keying the runner off `steps` would drop
    it silently -- the exact shape of bug `acts` exists to prevent."""
    plan = sweep.ship_plan(on_feature(), sweep.NEEDS_PR)
    assert plan.steps == ()
    assert plan.acts
    result = sweep.apply_plan("proj", Path("."), plan, git=FakeGit(), gh=FakeGh())
    assert result.ok and result.pr_url


def test_the_ship_dry_run_shows_the_pr_it_would_open():
    result = sweep.Result(on_feature(dirty=3), sweep.READY, "3 uncommitted", [])
    dry = sweep.render_plans("ship", [(result, SHIP_PLAN)], applied=False)
    assert "gh pr create" in dry
    assert "--base main" in dry
    assert "Dry run" in dry


def test_a_refused_plan_runs_nothing():
    git = FakeGit()
    result = sweep.apply_plan("proj", Path("."), sweep.Plan(refusal="has unshipped work"), git=git)
    assert git.calls == []
    assert not result.ok


def test_the_dry_run_prints_the_same_steps_the_real_run_executes():
    """A dry run is only worth having if it is the truth: both renders come from
    the same Plan objects the runner consumes."""
    result = sweep.Result(on_feature(ahead=0), sweep.SPENT, "spent", [])
    plan = sweep.sync_plan(result.state, sweep.SPENT)
    dry = sweep.render_plans("sync", [(result, plan)], applied=False)
    wet = sweep.render_plans("sync", [(result, plan)], applied=True)
    for step in plan.steps:
        assert " ".join(step) in dry
        assert " ".join(step) in wet
    assert "Dry run" in dry and "Dry run" not in wet


def test_refusals_are_reported_not_hidden():
    result = sweep.Result(on_feature(dirty=2), sweep.READY, "2 uncommitted", [])
    plan = sweep.sync_plan(result.state, sweep.READY)
    assert "skipped" in sweep.render_plans("sync", [(result, plan)], applied=False)
    assert plan.refusal in sweep.render_plans("sync", [(result, plan)], applied=False)


# --- exit codes -------------------------------------------------------------


def result(verdict: str) -> sweep.Result:
    return sweep.Result(State(name="p"), verdict, "reason", [])


def test_exit_code_reports_clean_then_actionable_then_blocked():
    assert sweep.exit_code([result(sweep.CLEAN), result(sweep.SKIPPED)]) == 0
    assert sweep.exit_code([result(sweep.CLEAN), result(sweep.READY)]) == 1
    # Blocked outranks actionable: a human is needed either way.
    assert sweep.exit_code([result(sweep.READY), result(sweep.BLOCKED)]) == 2


def _gh(payload: str = "[]", returncode: int = 0):
    def gh(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(args), returncode, payload, "")

    return gh


def test_a_merged_pr_is_detected():
    assert sweep.has_merged_pr(_gh('[{"number": 22}]'), "claude/x-0806") is True


def test_no_merged_pr_reads_as_not_retired():
    assert sweep.has_merged_pr(_gh("[]"), "claude/x-0806") is False


@pytest.mark.parametrize(
    "gh",
    [
        _gh("[]", returncode=1),  # gh present but unauthenticated / offline
        _gh("not json"),  # a shape change upstream
        _gh('{"number": 22}'),  # an object where a list was promised
    ],
)
def test_an_unanswerable_gh_fails_open(gh):
    """Inventing a retirement would send `--branch` to cut a branch under work that
    did not need one, on the say-so of an offline `gh`."""
    assert sweep.has_merged_pr(gh, "claude/x-0806") is False


@pytest.mark.parametrize("boom", [FileNotFoundError("gh"), subprocess.TimeoutExpired("gh", 1)])
def test_a_gh_that_cannot_run_at_all_fails_open(boom):
    """Regression: `gh` missing from PATH used to raise straight out of `inspect()`.

    `gh_for` is a bare `subprocess.run`, so on a machine with no `gh` installed the
    call raised `FileNotFoundError` rather than returning non-zero, and every caller
    here reads a returncode. The docstring already promised to fail open on "every
    error path"; the path where the callable raises was not one of them, so a sweep
    on such a machine died with a traceback instead of reporting.
    """

    def gh(*args: str):
        raise boom

    assert sweep.has_merged_pr(gh, "claude/x-0806") is False


# --- resolving the workspace registry from wherever a script is run ----------


def test_the_registry_sits_beside_a_static_checkout(tmp_path):
    assert sweep.default_workspace(tmp_path / "devkit") == tmp_path / sweep.WORKSPACE_FILE_NAME


def test_the_registry_is_found_from_inside_a_box(tmp_path):
    """A box sits one level deeper, and a box is where an agent runs these scripts.

    Regression: every workspace-aware script but `worktree.py` had its own naive
    `REPO_ROOT.parent` copy, so from a box they resolved
    `<workspace>/.worktrees/alex-projects.code-workspace` -- a path nobody wrote.
    `devkit_project.py --adopt-tasks` exited 2 naming it; `workspace-status.py` just
    went quiet.
    """
    box = tmp_path / sweep.BOXES_DIR_NAME / "devkit--topic-0810"
    assert sweep.default_workspace(box) == tmp_path / sweep.WORKSPACE_FILE_NAME


def test_a_static_checkout_is_its_own_source(tmp_path):
    assert sweep.source_checkout(tmp_path / "devkit") == tmp_path / "devkit"


def test_a_box_resolves_to_the_checkout_it_was_cut_from(tmp_path):
    """Regression: `workspace-status.py` exempts devkit from its adoption check by
    comparing each registered checkout against its own root. From a box that comparison
    missed, so the session-start line reported devkit itself as "never vendored devkit".
    """
    box = tmp_path / sweep.BOXES_DIR_NAME / f"devkit{sweep.NAME_SEP}topic-0810"
    assert sweep.source_checkout(box) == tmp_path / "devkit"


def test_the_project_name_is_read_off_the_box_not_assumed(tmp_path):
    """Project names contain hyphens, which is why the separator is two of them."""
    box = tmp_path / sweep.BOXES_DIR_NAME / f"apt-finder{sweep.NAME_SEP}fix-thing-0810"
    assert sweep.source_checkout(box) == tmp_path / "apt-finder"


def test_every_workspace_aware_script_resolves_it_the_same_way():
    """The guard on the fix: a sixth naive copy must not be written.

    Scanning the source rather than importing each module, because the naive form is
    *correct* when the script runs from the static checkout — importing it there would
    produce the right answer and prove nothing.
    """
    scripts = (Path(sweep.__file__).resolve().parent).glob("*.py")
    offenders = [
        path.name
        for path in scripts
        if f'REPO_ROOT.parent / "{sweep.WORKSPACE_FILE_NAME}"' in path.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        f"{offenders} resolve the workspace registry naively; they break inside a box. "
        "Use sweep.default_workspace(REPO_ROOT)."
    )


def test_shared_remotes_are_called_out():
    url = "https://github.com/alexandrec90/carameli.git"
    results = [
        sweep.Result(State(name="carameli", remote_url=url), sweep.CLEAN, "", []),
        sweep.Result(State(name="carameli-b", remote_url=url), sweep.CLEAN, "", []),
        sweep.Result(State(name="devkit", remote_url="https://x/devkit.git"), sweep.CLEAN, "", []),
    ]
    assert sweep.dedupe_note(results) == {url: ["carameli", "carameli-b"]}
