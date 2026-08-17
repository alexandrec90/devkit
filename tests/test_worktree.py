"""Tests for the ephemeral-worktree tier.

The destructive half is what these are for. `reap` deletes a worktree, its branch and
its Docker volumes, so every one of those steps is asserted as *argv* against a
hand-built `sweep.State` — no repo, no daemon, no network. That is the same contract
`tests/test_sweep.py` keeps for the same reason: a safety property nobody can check
cheaply is a safety property nobody checks.

`test_reap_refuses_while_work_is_only_in_the_box` is the reversion check for the whole
design. If it passed with `SAFE_TO_REAP` widened to include `ready`, the tier would be
back to stranding work — just faster than before.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

import pytest
from support import devkit_ports, sweep, worktree

TODAY = _dt.date(2026, 8, 6)


def registry(max_slots: int = 8, **slots: int) -> devkit_ports.Registry:
    return devkit_ports.from_dict(
        {
            "registry": {"max_slots": max_slots},
            "services": {"app": 8000, "db": 5432},
            "slots": dict(slots),
        }
    )


def box(name: str = "carameli--voicemail-0806", **kwargs) -> worktree.Box:
    defaults = {
        "project": "carameli",
        "branch": "claude/voicemail-0806",
        "slot": 3,
    }
    return worktree.Box(name=name, **{**defaults, **kwargs})


def _completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    """A `subprocess.CompletedProcess`, for the tests that stub the runner out."""
    return worktree.subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def state(**kwargs) -> sweep.State:
    defaults = {
        "name": "carameli--voicemail-0806",
        "is_git": True,
        "host": "github",
        "default_branch": "master",
        "branch": "claude/voicemail-0806",
        "linked": True,
    }
    return sweep.State(**{**defaults, **kwargs})


# --- naming -----------------------------------------------------------------


def test_box_name_joins_project_and_branch_topic():
    assert worktree.box_name("carameli", "claude/voicemail-0806") == "carameli--voicemail-0806"
    assert worktree.box_name("carameli", "codex/voicemail-0806") == "carameli--voicemail-0806"


def test_box_name_keeps_a_non_task_branch_whole():
    """No managed prefix to strip means the branch name is the topic, not a slice of it."""
    assert worktree.box_name("carameli", "hotfix") == "carameli--hotfix"


def test_box_name_is_a_legal_compose_project_name():
    """It becomes COMPOSE_PROJECT_NAME, which compose restricts to [a-z0-9][a-z0-9_-]*."""
    for project, branch in (
        ("apt-finder", "claude/rent-caps-0806"),
        ("ibkr_trader", "claude/oos-0806-2"),
    ):
        name = worktree.box_name(project, branch)
        assert name[0].isalnum()
        assert all(c.isalnum() or c in "-_" for c in name)
        assert name.lower() == name


def test_project_of_recovers_the_source_checkout():
    assert worktree.project_of("apt-finder--rent-caps-0806") == "apt-finder"
    assert worktree.project_of("not-a-box") == ""


# --- leases -----------------------------------------------------------------


def test_leases_round_trip():
    boxes = {"carameli--x-0806": box("carameli--x-0806")}
    assert worktree.parse_leases(worktree.render_leases(boxes)) == boxes


@pytest.mark.parametrize("text", ["", "{", "null", '{"boxes": []}', '{"boxes": {"a": 3}}'])
def test_unreadable_leases_are_no_boxes_not_a_crash(text):
    """A truncated lease file must cost a slot, never the whole tier."""
    assert worktree.parse_leases(text) == {}


def test_next_lease_slot_skips_both_pinned_checkouts_and_live_boxes():
    """The bug this exists to prevent: handing a box a slot a pinned checkout already holds."""
    reg = registry(carameli=0, ibkr_trader=1)
    boxes = {"carameli--a-0806": box("carameli--a-0806", slot=2)}
    assert worktree.next_lease_slot(reg, boxes) == 3


def test_next_lease_slot_reuses_a_gap_left_by_a_reaped_box():
    reg = registry(carameli=0, ibkr_trader=2)
    assert worktree.next_lease_slot(reg, {}) == 1


def test_next_lease_slot_ignores_boxes_with_no_stack():
    """slot=-1 means "no Docker tier", not "slot minus one"."""
    reg = registry(carameli=0)
    boxes = {"devkit--x-0806": box("devkit--x-0806", project="devkit", slot=-1)}
    assert worktree.next_lease_slot(reg, boxes) == 1


def test_next_lease_slot_raises_when_the_registry_is_full():
    reg = registry(max_slots=2, carameli=0, ibkr_trader=1)
    with pytest.raises(devkit_ports.RegistryError, match="all 2 port slots"):
        worktree.next_lease_slot(reg, {})


def test_find_session_box_is_scoped_to_one_project():
    boxes = {
        "carameli--ws-abc-0806": box("carameli--ws-abc-0806", session="abc"),
        "ibkr_trader--ws-abc-0806": box(
            "ibkr_trader--ws-abc-0806", project="ibkr_trader", session="abc"
        ),
    }
    found = worktree.find_session_box(boxes, "ibkr_trader", "abc")
    assert found is not None and found.name == "ibkr_trader--ws-abc-0806"


def test_find_session_box_without_a_session_id_finds_nothing():
    """An empty session must not match every unstamped lease and reuse a stranger's box."""
    boxes = {"carameli--x-0806": box("carameli--x-0806", session="")}
    assert worktree.find_session_box(boxes, "carameli", "") is None


# --- env seeding ------------------------------------------------------------


def test_managed_env_always_carries_the_compose_project_name():
    env = worktree.managed_env("carameli--x-0806", registry(), 3)
    assert env["COMPOSE_PROJECT_NAME"] == "carameli--x-0806"
    assert env["DB_HOST_PORT"] == "5435"
    assert env["APP_HOST_PORT"] == "8003"


def test_managed_env_without_a_registry_still_namespaces_the_stack():
    assert worktree.managed_env("devkit--x-0806", None, -1) == {
        "COMPOSE_PROJECT_NAME": "devkit--x-0806"
    }


def test_render_env_appends_so_the_managed_keys_win():
    """Compose's dotenv parser takes the last assignment, which is why nothing above is edited."""
    rendered = worktree.render_env("DB_HOST_PORT=5432\nSECRET=keepme\n", {"DB_HOST_PORT": "5435"})
    assert rendered.index("SECRET=keepme") < rendered.index("DB_HOST_PORT=5435")
    assert "DB_HOST_PORT=5432" in rendered


def test_render_env_is_idempotent():
    """Re-spawning must not stack a second managed block on the first."""
    once = worktree.render_env("SECRET=keepme\n", {"DB_HOST_PORT": "5435"})
    twice = worktree.render_env(once, {"DB_HOST_PORT": "5435"})
    assert once == twice
    assert twice.count(worktree.MANAGED_BEGIN) == 1


def test_render_env_replaces_a_stale_block_rather_than_leaving_both_ports():
    once = worktree.render_env("SECRET=keepme\n", {"DB_HOST_PORT": "5435"})
    twice = worktree.render_env(once, {"DB_HOST_PORT": "5437"})
    assert "DB_HOST_PORT=5437" in twice
    assert "DB_HOST_PORT=5435" not in twice


def test_a_tracked_env_is_never_seeded():
    """Regression: seeding rewrites the file, so a box whose repo *tracks* `.env` was
    dirty from birth -- never `spent`, never reapable, and a `/ship` from inside it
    would have committed devkit's managed block as the task's work."""
    assert worktree.should_seed_env(stack=True, env_tracked=True) is False


def test_an_untracked_env_is_seeded_when_there_is_a_stack():
    assert worktree.should_seed_env(stack=True, env_tracked=False) is True


def test_a_project_with_no_stack_gets_no_env_at_all():
    assert worktree.should_seed_env(stack=False, env_tracked=False) is False


def test_render_env_handles_an_empty_source():
    """A project whose `.env` is gitignored gives a fresh worktree nothing to seed from."""
    rendered = worktree.render_env("", {"COMPOSE_PROJECT_NAME": "x"})
    assert rendered.startswith("\n" + worktree.MANAGED_BEGIN) or rendered.lstrip().startswith(
        worktree.MANAGED_BEGIN
    )
    assert "COMPOSE_PROJECT_NAME=x" in rendered


# --- spawn ------------------------------------------------------------------


def spawn(**kwargs) -> worktree.SpawnPlan:
    from pathlib import Path

    defaults = {
        "project": "carameli",
        "workspace_root": Path("/ws"),
        "slug": "voicemail",
        "default_branch": "master",
        "existing_branches": set(),
        "boxes": {},
        "registry": registry(carameli=0),
        "today": TODAY,
    }
    return worktree.spawn_plan(**{**defaults, **kwargs})


def test_spawn_cuts_from_origin_not_from_the_source_checkouts_head():
    """The one place this differs from `sweep.branch_plan`, and the reason is the tier.

    Sweep branches from HEAD because it is rescuing a dirty tree it must not clobber.
    A box starts empty, so anywhere but the tip of the default branch is a stale base
    for no benefit.
    """
    steps = spawn().steps
    assert ("fetch", "--quiet", "origin") in steps
    add = next(s for s in steps if s[0] == "worktree")
    assert add[-1] == "origin/master"


def test_spawn_never_tracks_the_base():
    """`--no-track` for the reason `tb.checkout_argv` documents: otherwise a bare
    `git push` from the box lands the task's commits on the default branch."""
    add = next(s for s in spawn().steps if s[0] == "worktree")
    assert "--no-track" in add


def test_spawn_names_the_branch_the_way_the_hooks_do():
    plan = spawn()
    assert plan.box.branch == "agent/voicemail-0806"
    assert plan.box.name == "carameli--voicemail-0806"
    assert plan.path.replace("\\", "/").endswith(".worktrees/carameli--voicemail-0806")


def test_spawn_disambiguates_against_existing_branches():
    plan = spawn(existing_branches={"agent/voicemail-0806"})
    assert plan.box.branch == "agent/voicemail-0806-2"


def test_spawn_without_a_registry_leases_no_slot():
    """A project with no Docker tier must not spend a slot from a 16-entry ceiling."""
    plan = spawn(registry=None)
    assert plan.box.slot == -1
    assert plan.env == {"COMPOSE_PROJECT_NAME": "carameli--voicemail-0806"}


def test_spawn_can_skip_the_fetch():
    assert all(step[0] != "fetch" for step in spawn(fetch=False).steps)


def test_spawn_records_the_session_that_asked_for_it():
    assert spawn(session="abc123").box.session == "abc123"


def test_the_box_lives_outside_every_checkout():
    """Nested inside a project it would show up as untracked files there -- the exact
    `needs-branch` verdict this tier exists to stop manufacturing."""
    from pathlib import Path

    path = Path(spawn().path)
    assert path.parent.name == worktree.BOXES_DIR_NAME
    assert path.parent.parent == Path("/ws")


# --- reap: the safety property ----------------------------------------------


@pytest.mark.parametrize("verdict", sorted(worktree.SAFE_TO_REAP))
def test_reap_allows_a_box_whose_work_has_left_it(verdict):
    allowed, note = worktree.reap_decision(verdict, "", force=False)
    assert allowed and note == ""


@pytest.mark.parametrize(
    "verdict",
    sorted(sweep.ACTIONABLE - worktree.SAFE_TO_REAP),
)
def test_reap_refuses_while_work_is_only_in_the_box(verdict):
    """The reversion check for the whole design.

    Widen `SAFE_TO_REAP` to include `ready` and this fails -- which is what it is for:
    the tier's entire claim over `sweep.py` is that a box cannot be freed until its
    work has shipped, and `ready` is the verdict that means it has not.
    """
    allowed, note = worktree.reap_decision(verdict, "2 uncommitted file(s)", force=False)
    assert not allowed
    assert "/ship" in note


def test_ready_is_never_reapable():
    """Named explicitly as well as covered by the parametrised case: `ready` is the
    verdict for "the only copy is here", so it is the one that must never widen."""
    assert sweep.READY not in worktree.SAFE_TO_REAP


def test_force_says_what_it_will_destroy():
    allowed, note = worktree.reap_decision(sweep.READY, "2 uncommitted file(s)", force=True)
    assert allowed
    assert "discarded" in note and "2 uncommitted file(s)" in note


# --- reap: the squash-merged box --------------------------------------------
# A squash merge rewrites the branch's commits and `--delete-branch` removes the
# upstream, so `sweep.classify` reports `needs-rebranch` -- true of the refs, false of
# the work. Before `reapable` consulted the PR, that verdict outlived the merge and the
# box could only be freed with `--force`, the flag that also discards uncommitted edits.


def test_a_squash_merged_box_is_reapable_without_force():
    """Regression. This is the state every merged box reaches once the deleted remote
    branch is pruned, and it refused: "2 unmerged commit(s) ... the work is still only in
    this box" about work that was on `main` at the time it said so."""
    allowed, note = worktree.reap_decision(
        sweep.NEEDS_REBRANCH,
        "2 unmerged commit(s) on agent/x, whose remote branch is gone",
        force=False,
        pr_merged=True,
        holds_uncommitted=False,
    )
    assert allowed
    assert note == ""


def test_a_merged_pr_does_not_license_destroying_uncommitted_work():
    """The safety property, at the predicate rather than at the pass: the merge says
    where the *commits* are and says nothing about the edits sitting on top of them."""
    allowed, note = worktree.reap_decision(
        sweep.NEEDS_REBRANCH,
        "3 uncommitted file(s) on agent/x",
        force=False,
        pr_merged=True,
        holds_uncommitted=True,
    )
    assert not allowed
    assert "/ship" in note


def test_an_unmerged_box_stays_refused_however_clean_it_is():
    """`holds_uncommitted=False` is not itself permission. A retired branch carrying
    commits whose PR was *closed* rather than merged holds the only copy of them."""
    assert not worktree.reapable(sweep.NEEDS_REBRANCH, pr_merged=False, holds_uncommitted=False)


def test_a_ready_box_never_escapes_through_the_merge_path():
    """`ready` means uncommitted work by definition, so a zero count beside it is two
    fields disagreeing rather than a clean box. Keyed on the count alone this returned
    True, and `test_reconcile_never_reaps_a_box_holding_work` failed -- the merge escape
    is scoped to the verdict a squash can actually be stale about."""
    assert not worktree.reapable(sweep.READY, pr_merged=True, holds_uncommitted=False)
    assert sweep.READY not in worktree.MERGE_CAN_BE_STALE_ABOUT


def test_not_knowing_whether_a_box_is_dirty_holds_it():
    """Both flags default to the cautious answer, so a caller that forgets to pass them
    keeps a box it might have destroyed rather than the reverse."""
    assert not worktree.reapable(sweep.NEEDS_REBRANCH, pr_merged=True)


def test_the_two_classifiers_agree_about_a_squash_merged_box():
    """`reconcile` warns "reap refused" and stalls the box when its decision and
    `reap_decision` disagree, so the pair is asserted together rather than apart."""
    merged = worktree.parse_pr_view(pr_json(state="MERGED"))
    action, _ = worktree.reconcile_action(
        sweep.NEEDS_REBRANCH, "2 unmerged commit(s)", merged, holds_uncommitted=False
    )
    allowed, _ = worktree.reap_decision(
        sweep.NEEDS_REBRANCH,
        "2 unmerged commit(s)",
        force=False,
        pr_merged=True,
        holds_uncommitted=False,
    )
    assert action == worktree.REAP
    assert allowed


# --- reap: the plan ---------------------------------------------------------


def reap(verdict: str = sweep.NEEDS_PR, **kwargs) -> worktree.ReapPlan:
    from pathlib import Path

    defaults = {
        "box": box(),
        "workspace_root": Path("/ws"),
        "state": state(upstream="origin/claude/voicemail-0806", unpushed=0, ahead=2),
        "verdict": verdict,
        "reason": "2 commit(s) pushed",
        "has_stack": True,
    }
    return worktree.reap_plan(**{**defaults, **kwargs})


def test_reap_tears_the_stack_down_before_removing_the_worktree():
    """Order matters: the compose file lives in the worktree that is about to go."""
    plan = reap()
    assert plan.stack_down
    assert plan.steps[0][:2] == ("worktree", "remove")


def test_reap_skips_the_stack_for_a_project_that_has_none():
    assert not reap(has_stack=False).stack_down


def test_keep_stack_leaves_the_containers_running():
    assert not reap(keep_stack=True).stack_down


def test_reap_deletes_the_branch_from_the_source_checkout():
    """The worktree holding the branch is gone by then, so the delete has to run in the
    project, which is what `ReapPlan.project` is for."""
    plan = reap()
    assert plan.project == "carameli"
    assert plan.steps[-1][:1] == ("branch",)


def test_a_refused_reap_plans_nothing_at_all():
    plan = reap(sweep.READY, state=state(dirty=3), reason="3 uncommitted file(s)")
    assert plan.refusal
    assert plan.steps == () and not plan.stack_down and not plan.acts


def test_reap_never_forces_the_worktree_removal_by_default():
    assert "--force" not in reap().steps[0]


def test_force_reap_discards_the_worktree_but_not_the_commits():
    """The asymmetry the whole `--force` design rests on: `worktree remove --force`
    throws away uncommitted edits, and nothing in the plan can lose a commit."""
    plan = reap(sweep.READY, state=state(dirty=3, ahead=1), reason="3 files", force=True)
    assert "--force" in plan.steps[0]
    assert not [step for step in plan.steps if step[0] == "branch"]


def test_a_forced_reap_plans_no_delete_git_is_certain_to_refuse():
    """Lived, on `devkit--handoff-prompt-no-commit-0815`: its PR was closed rather than
    merged and the remote branch deleted, so `--force` was the only way out. The plan
    removed the worktree and then ran `branch -d` on a branch carrying a commit no
    remote had -- which `-d` refuses by definition. Every forced reap of a box like that
    ended `FAILED at git branch -d`, exit 1, with the worktree already gone and a git
    hint pushing the `-D` that `--force` deliberately withholds.

    Keeping the branch is the design; failing while keeping it is the defect."""
    plan = reap(
        sweep.NEEDS_REBRANCH,
        state=state(upstream="", ahead=1, unpushed=1),
        reason="1 unmerged commit(s)",
        force=True,
    )
    assert plan.steps == (("worktree", "remove", plan.path, "--force"),)
    assert "is kept" in plan.warning
    assert f"branch -D {box().branch}" in plan.warning


def test_the_kept_branch_warning_still_carries_what_force_destroyed():
    """Two facts, one line: the uncommitted edits are gone, and the branch is not. The
    second used to arrive as a git error, so appending it must not drop the first."""
    plan = reap(
        sweep.NEEDS_REBRANCH,
        state=state(upstream="", ahead=1, unpushed=1, dirty=2),
        reason="1 unmerged commit(s)",
        force=True,
    )
    assert "forced past" in plan.warning
    assert "is kept" in plan.warning


def test_a_forced_reap_still_deletes_a_branch_that_is_safe_to_delete():
    """The skip is scoped to the flag git would refuse. A pushed branch resolves to
    `-D`, and forcing must not start leaving those behind -- that is the `agent/ws-*`
    accumulation `branch_delete_flag` was written to stop."""
    plan = reap(sweep.NEEDS_PR, force=True)
    assert plan.steps[-1] == ("branch", "-D", box().branch)


def test_no_reap_plan_ever_emits_a_capital_D_without_the_remote_having_it():
    """A blanket safety sweep over the plan surface, not one path through it.

    The state carries `ahead=2` — local-only commits, which is the only thing `-D` can
    actually destroy. It used to carry `dirty=1` alone, leaving `ahead` at its default of
    0, so every case in the sweep was a branch with no commits on it: the assertion could
    not have caught a `-D` that mattered, and it went green against a version of
    `branch_delete_flag` that force-deleted whenever the verdict said `spent`, regardless
    of what the state said was on the branch.
    """
    for verdict in sorted(sweep.ACTIONABLE | {sweep.CLEAN}):
        for force in (False, True):
            plan = reap(
                verdict, state=state(dirty=1, ahead=2), reason="x", force=force, pr_merged=False
            )
            assert all(step[1] != "-D" for step in plan.steps if step[0] == "branch")


# --- reap: which delete flag ------------------------------------------------


def test_branch_delete_is_forced_once_the_pr_has_merged():
    """A squash merge leaves the branch a non-ancestor of the default branch, so `-d`
    refuses forever -- while GitHub holds every line of it."""
    assert worktree.branch_delete_flag(state(), pr_merged=True) == "-D"


def test_branch_delete_is_forced_when_everything_is_pushed():
    """An open PR: not merged, so not an ancestor, but the remote has every commit."""
    assert (
        worktree.branch_delete_flag(state(upstream="origin/x", unpushed=0), pr_merged=False) == "-D"
    )


def test_branch_delete_defers_to_git_when_commits_are_only_local():
    """`ahead` says the branch HAS commits; `unpushed` says two of them are not on the
    remote. Both are needed to describe the case -- an `unpushed` count above an `ahead`
    of zero is a state git cannot produce."""
    assert (
        worktree.branch_delete_flag(
            state(upstream="origin/x", ahead=2, unpushed=2), pr_merged=False
        )
        == "-d"
    )


def test_branch_delete_defers_to_git_when_there_is_no_upstream():
    """`unpushed` is -1 for a branch that was never pushed -- not 0, which would read
    as "nothing outstanding" and force-delete the only copy."""
    assert (
        worktree.branch_delete_flag(state(upstream="", ahead=3, unpushed=-1), pr_merged=False)
        == "-d"
    )


# --- the tiers stay separate ------------------------------------------------


def test_boxes_are_invisible_to_the_sweep():
    """A box is not a folder in the workspace file, so `sweep.py` never sees it. That
    separation is what lets the two tiers keep different lifecycles."""
    workspace = json.dumps({"folders": [{"path": "carameli"}, {"path": ".worktrees"}]})
    # Even a hand-added `.worktrees` entry is a directory of boxes, not a checkout:
    # nothing in the sweep would know what to do with it, which is why `new` never
    # registers one.
    assert "carameli--voicemail-0806" not in sweep.parse_workspace(workspace)


def test_a_failed_teardown_removes_the_box_but_does_not_report_success(tmp_path, monkeypatch):
    """Leaking a container and volume set per task is what makes the VHDX the next
    bottleneck, so a stack that would not come down has to be visible -- while still
    not stranding the box over a daemon that happened to be off."""
    # Any filename: `apply_reap` only reads `workspace.parent`. Deliberately not the
    # live registry's name -- `test_self_hosting.py` flags a test carrying that literal,
    # and a fixture that merely borrows the name is indistinguishable to it.
    workspace = tmp_path / "ws" / "registry.code-workspace"
    workspace.parent.mkdir()
    monkeypatch.setattr(worktree, "compose_down", lambda *a: (False, "docker is not on PATH"))
    monkeypatch.setattr(worktree, "run_steps", lambda *a, **k: ([], "", ""))
    monkeypatch.setattr(worktree, "read_leases", lambda root: {})
    monkeypatch.setattr(worktree, "write_leases", lambda root, boxes: None)

    ok, notes = worktree.apply_reap(
        worktree.ReapPlan(box="demo--x-0806", project="demo", stack_down=True), workspace
    )
    assert ok is False
    assert any("lease released" in note for note in notes)
    assert any("may survive" in note for note in notes)


# --- the CLI ----------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ["new", "demo", "--slug", "x", "--yes"],
        ["reap", "demo--x-0806", "--yes", "--force"],
        ["list", "--no-fetch"],
    ],
)
def test_flags_are_accepted_after_the_subcommand(args, tmp_path):
    """Regression: `--yes` used to be a top-level-only flag.

    argparse accepts a top-level option only *before* the subcommand, so
    `worktree.py new demo --yes` -- the spelling this module's docstring, its --help
    output and `worktree-guard.py`'s block message all use -- died with "unrecognized
    arguments: --yes". The dry run had already printed a plan by then, so it read as
    the tool refusing to do the thing it had just described.

    Reaching the "no workspace file" return proves argparse accepted the argv:
    an argparse rejection raises SystemExit instead of returning.
    """
    missing = tmp_path / "nope.code-workspace"
    assert worktree.main([*args, "--workspace", str(missing)]) == 2


def test_reap_uses_the_same_classifier_as_the_sweep():
    """One opinion about "does this hold unshipped work". Two would disagree exactly
    when it mattered."""
    assert worktree.SAFE_TO_REAP <= (sweep.ACTIONABLE | sweep.TERMINAL)


# --- provisioning -------------------------------------------------------------


def _python_installer(steps) -> list[str]:
    """The install commands among `steps`, minus venv creation and the frontend."""
    return [
        s.label
        for s in steps
        if s.label != "create .venv" and not s.label.startswith("npm install")
    ]


def test_a_uv_locked_project_syncs_and_makes_no_venv_of_its_own():
    """uv owns ./.venv when there is a lockfile; creating one first is wasted time."""
    steps = worktree.provision_steps({"uv.lock", "pyproject.toml"})
    assert [s.argv for s in steps] == [("uv", "sync", "--all-extras", "--all-groups")]


def test_the_manifest_install_command_beats_every_detected_marker():
    """`[python] install_command` is the documented escape hatch for a project that fits
    none of the shapes -- so a project that sets it must not also get the guess."""
    steps = worktree.provision_steps(
        {"uv.lock", "requirements-dev.txt"}, install_command="make dev"
    )
    assert [(s.label, s.shell_command) for s in steps] == [
        (".devkit.toml install_command", "make dev")
    ]
    assert all(not s.argv for s in steps)


def test_a_pip_tools_project_installs_both_locks_into_its_own_venv():
    steps = worktree.provision_steps({"requirements.txt", "requirements-dev.txt"}, windows=False)
    assert steps[0].argv[-3:] == ("-m", "venv", ".venv")
    assert steps[1].argv == (
        "uv",
        "pip",
        "install",
        "--python",
        ".venv/bin/python",
        "-r",
        "requirements.txt",
        "-r",
        "requirements-dev.txt",
    )


def test_a_missing_runtime_lock_is_left_out_rather_than_failing_the_install():
    """`-r requirements.txt` for a file that is not there aborts the whole install, and
    with it the dev toolchain that WAS available."""
    steps = worktree.provision_steps({"requirements-dev.txt"}, windows=False)
    assert "requirements.txt" not in steps[-1].argv
    assert "requirements-dev.txt" in steps[-1].argv


def test_an_unlocked_pyproject_installs_itself_editable():
    steps = worktree.provision_steps({"pyproject.toml"}, windows=False)
    assert steps[-1].argv[-2:] == ("-e", ".[dev]")


def test_a_project_with_no_python_markers_installs_nothing():
    assert worktree.provision_steps(set()) == ()


@pytest.mark.parametrize(
    "present",
    [
        {"uv.lock"},
        {"uv.lock", "pyproject.toml"},
        {"requirements-dev.txt", "pyproject.toml"},
        {"requirements.txt", "requirements-dev.txt", "pyproject.toml"},
        {"pyproject.toml"},
    ],
)
def test_exactly_one_python_installer_runs_however_many_markers_match(present):
    """The ladder is an `elif` chain for a reason: a project carrying both a lockfile and
    a pyproject would otherwise be installed twice, the second pass resolving fresh
    against the network and overwriting the pinned versions the first pass just placed."""
    assert len(_python_installer(worktree.provision_steps(present))) == 1


def test_the_frontend_toolchain_is_installed_alongside_the_python_one():
    steps = worktree.provision_steps({"uv.lock"}, frontend_dir="frontend", windows=False)
    assert steps[-1].argv == ("npm", "install", "--prefix", "frontend", "--no-audit", "--no-fund")


def test_the_npm_program_name_follows_the_platform():
    """A bare `npm` is unrunnable as argv on Windows -- npm is `npm.cmd`, a batch shim,
    and argv resolution does not consult PATHEXT. The step then dies with WinError 2,
    which `run_provision` downgrades to a `[warn]`, so the box comes out announcing
    itself provisioned with no `node_modules` and no frontend linter in it."""
    assert worktree.npm_executable(windows=True) == "npm.cmd"
    assert worktree.npm_executable(windows=False) == "npm"


def test_the_frontend_step_is_runnable_on_windows():
    """The reversion check for the above: with a bare `npm` this is what shipped, and
    every Windows box silently lost eslint, tsc, stylelint and markdownlint."""
    steps = worktree.provision_steps({"uv.lock"}, frontend_dir="frontend", windows=True)
    assert steps[-1].argv[0] == "npm.cmd"


def test_a_project_with_no_frontend_tier_runs_no_npm():
    steps = worktree.provision_steps({"uv.lock"})
    assert not any("npm" in s.argv for s in steps)


def test_the_interpreter_path_follows_the_platform():
    assert worktree.venv_python(windows=True) == ".venv/Scripts/python.exe"
    assert worktree.venv_python(windows=False) == ".venv/bin/python"


def test_plan_provision_reads_the_markers_and_the_manifest_off_disk(tmp_path):
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    (tmp_path / "frontend").mkdir()
    (tmp_path / ".devkit.toml").write_text(
        '[frontend]\nenabled = true\ndir = "frontend"\n', encoding="utf-8"
    )
    labels = [s.label for s in worktree.plan_provision(tmp_path)]
    assert labels == ["uv sync (uv.lock)", "npm install (frontend)"]


def test_a_declared_frontend_that_is_not_checked_out_is_skipped(tmp_path):
    """`[frontend] enabled` describes the project; the directory describes this box."""
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    (tmp_path / ".devkit.toml").write_text(
        '[frontend]\nenabled = true\ndir = "frontend"\n', encoding="utf-8"
    )
    assert not any("npm" in s.label for s in worktree.plan_provision(tmp_path))


def test_an_unreadable_manifest_still_yields_the_python_toolchain(tmp_path):
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    (tmp_path / ".devkit.toml").write_text("this is not toml {{", encoding="utf-8")
    assert [s.label for s in worktree.plan_provision(tmp_path)] == ["uv sync (uv.lock)"]


def test_provisioning_stops_at_the_first_failure(tmp_path, monkeypatch):
    """The second step of a ladder assumes the first one placed an interpreter."""
    calls: list[tuple[str, ...]] = []

    def fake_run(argv, **kwargs):
        calls.append(tuple(argv))
        return _completed(returncode=1, stderr="no interpreter")

    monkeypatch.setattr(worktree.subprocess, "run", fake_run)
    ok, notes = worktree.run_provision(
        tmp_path,
        (
            worktree.ProvisionStep("create .venv", ("python", "-m", "venv", ".venv")),
            worktree.ProvisionStep("install", ("uv", "pip", "install", "-e", ".")),
        ),
    )
    assert ok is False
    assert len(calls) == 1
    assert any("no interpreter" in note for note in notes)


def test_a_timed_out_install_is_reported_rather_than_raised(tmp_path, monkeypatch):
    def fake_run(*args, **kwargs):
        raise worktree.subprocess.TimeoutExpired(cmd="uv", timeout=900)

    monkeypatch.setattr(worktree.subprocess, "run", fake_run)
    ok, notes = worktree.run_provision(
        tmp_path, (worktree.ProvisionStep("uv sync", ("uv", "sync")),), timeout=900
    )
    assert ok is False
    assert any("timed out" in note for note in notes)


def test_a_shell_install_command_runs_through_a_shell(tmp_path, monkeypatch):
    seen: dict = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["shell"] = kwargs.get("shell", False)
        return _completed()

    monkeypatch.setattr(worktree.subprocess, "run", fake_run)
    ok, _ = worktree.run_provision(
        tmp_path, (worktree.ProvisionStep("manifest", shell_command="make dev"),)
    )
    assert ok is True
    assert seen == {"command": "make dev", "shell": True}


# --- the lease file is a record, and records go stale -------------------------


def test_a_lease_whose_worktree_is_gone_is_dropped_on_the_next_write():
    recorded = {"a--x-0806": box("a--x-0806"), "b--y-0806": box("b--y-0806")}
    live = {"a--x-0806": recorded["a--x-0806"]}
    assert worktree.prune_leases(recorded, live) == ["b--y-0806"]


def test_pruning_keeps_every_lease_that_still_has_a_worktree():
    boxes = {"a--x-0806": box("a--x-0806")}
    assert worktree.prune_leases(boxes, boxes) == []


# --- reap's argument contract -------------------------------------------------


def test_reaping_by_name_and_all_at_once_is_refused():
    assert worktree.reap_argument_faults("demo--x-0806", every=True, force=False)


def test_reaping_nothing_at_all_is_refused():
    """`reap` with no box and no --all would otherwise look up a box called ""."""
    assert worktree.reap_argument_faults("", every=False, force=False)


def test_a_named_reap_is_accepted():
    assert worktree.reap_argument_faults("demo--x-0806", every=False, force=True) == []


def test_all_never_forces():
    """--force discards uncommitted work. Applied to a sweep, it discards uncommitted work
    in boxes the caller has not looked at -- the outcome `reap_decision` exists to make
    impossible."""
    faults = worktree.reap_argument_faults("", every=True, force=True)
    assert faults and "never forces" in faults[0]


# --- the lease file's read-modify-write is exclusive ---------------------------
#
# Two guard hooks spawning boxes seconds apart both read `leases.json` and the second
# write erased the first's entry; the erased box (`carameli--voicemail-hook-0816`,
# 2026-08-16) became a worktree no tool could see. `lease_lock` closes the window and
# `orphaned_boxes` (next section) adopts what an unlocked writer already lost.


def _lock_dir(tmp_path: Path) -> Path:
    return worktree.boxes_root(tmp_path) / worktree.LEASE_LOCK_NAME


def test_the_lease_lock_is_held_inside_and_released_after(tmp_path):
    with worktree.lease_lock(tmp_path):
        assert _lock_dir(tmp_path).is_dir()
    assert not _lock_dir(tmp_path).exists()


def test_a_held_lock_is_waited_on_then_stepped_past_not_stolen(tmp_path):
    """Timing out fails toward availability -- an unlocked write is the status quo
    ante -- but the *other* holder's lock must survive our exit untouched."""
    _lock_dir(tmp_path).mkdir(parents=True)
    with worktree.lease_lock(tmp_path, wait=0.2, stale=60.0):
        pass
    assert _lock_dir(tmp_path).is_dir()


def test_a_lock_whose_holder_died_is_broken(tmp_path):
    lock = _lock_dir(tmp_path)
    lock.mkdir(parents=True)
    stale = worktree.time.time() - 300
    worktree.os.utime(lock, (stale, stale))
    with worktree.lease_lock(tmp_path, wait=5.0, stale=60.0):
        assert lock.is_dir()
    assert not lock.exists()  # we owned it, so exit removed it


def test_write_leases_replaces_the_file_leaving_no_partial_state(tmp_path):
    worktree.write_leases(tmp_path, {"a--x-0806": box("a--x-0806")})
    assert "a--x-0806" in worktree.read_leases(tmp_path)
    leftovers = [p for p in worktree.boxes_root(tmp_path).iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def _spawn_plan(name: str = "demo--x-0806", slot: int = -1) -> worktree.SpawnPlan:
    spawned = box(name, project="demo", branch="agent/x-0806", slot=slot, session="s1")
    return worktree.SpawnPlan(box=spawned, path=name, steps=())


def test_apply_new_reads_and_writes_the_leases_under_the_lock(tmp_path, monkeypatch):
    """The reversion check for the lost-update race: the fresh read and the write
    happen inside one critical section, so a concurrent spawn's entry cannot be
    read-before and overwritten-after."""
    workspace = tmp_path / "ws" / "registry.code-workspace"
    workspace.parent.mkdir()
    monkeypatch.setattr(worktree, "run_steps", lambda *a, **k: ([], "", ""))
    locked_during: list[bool] = []
    real_read, real_write = worktree.read_leases, worktree.write_leases
    monkeypatch.setattr(
        worktree,
        "read_leases",
        lambda root: (locked_during.append(_lock_dir(root).is_dir()), real_read(root))[1],
    )
    monkeypatch.setattr(
        worktree,
        "write_leases",
        lambda root, boxes: (
            locked_during.append(_lock_dir(root).is_dir()),
            real_write(root, boxes),
        )[1],
    )

    ok, _ = worktree.apply_new(_spawn_plan(), workspace, provision=False)

    assert ok
    assert locked_during and all(locked_during)
    assert "demo--x-0806" in real_read(workspace.parent)


def test_apply_reap_releases_the_lease_under_the_same_lock(tmp_path, monkeypatch):
    workspace = tmp_path / "ws" / "registry.code-workspace"
    workspace.parent.mkdir()
    worktree.write_leases(workspace.parent, {"demo--x-0806": box("demo--x-0806", project="demo")})
    worktree.box_path(workspace.parent, "demo--x-0806").mkdir()  # live: the dir exists
    monkeypatch.setattr(worktree, "run_steps", lambda *a, **k: ([], "", ""))
    locked_during: list[bool] = []
    real_write = worktree.write_leases
    monkeypatch.setattr(
        worktree,
        "write_leases",
        lambda root, boxes: (
            locked_during.append(_lock_dir(root).is_dir()),
            real_write(root, boxes),
        )[1],
    )

    ok, _ = worktree.apply_reap(worktree.ReapPlan(box="demo--x-0806", project="demo"), workspace)

    assert ok
    assert locked_during and all(locked_during)
    assert "demo--x-0806" not in worktree.read_leases(workspace.parent)


def test_apply_new_re_leases_a_slot_taken_while_the_box_was_cut(tmp_path, monkeypatch):
    """The slot is chosen at plan time and the fetch + worktree-add between plan and
    lease write are seconds wide; a concurrent spawn recording the same slot first
    must not end with two stacks publishing the same host ports."""
    workspace = tmp_path / "ws" / "registry.code-workspace"
    workspace.parent.mkdir()
    rival = box("demo--rival-0806", project="demo", slot=3)
    worktree.boxes_root(workspace.parent).mkdir(parents=True)
    worktree.box_path(workspace.parent, rival.name).mkdir()  # live: the dir exists
    worktree.write_leases(workspace.parent, {rival.name: rival})
    monkeypatch.setattr(worktree, "run_steps", lambda *a, **k: ([], "", ""))
    monkeypatch.setattr(worktree, "load_registry", lambda root: registry())

    ok, notes = worktree.apply_new(_spawn_plan(slot=3), workspace, provision=False)

    assert ok
    recorded = worktree.read_leases(workspace.parent)
    assert recorded["demo--x-0806"].slot == 0
    assert recorded["demo--rival-0806"].slot == 3
    assert any("re-leased slot 0" in note for note in notes)


# --- a worktree the lease file forgot is adopted, not leaked -------------------


def _orphan(
    tmp_path: Path, name: str = "carameli--lost-0806", branch: str = "agent/lost-0806"
) -> Path:
    """A linked worktree the lease file has never heard of, built from files alone."""
    gitdir = tmp_path / "carameli" / ".git" / "worktrees" / name
    gitdir.mkdir(parents=True)
    (gitdir / "HEAD").write_text(f"ref: refs/heads/{branch}\n", encoding="utf-8")
    home = worktree.boxes_root(tmp_path) / name
    home.mkdir(parents=True)
    (home / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    return home


def test_a_worktree_the_lease_file_forgot_is_adopted_on_read(tmp_path):
    _orphan(tmp_path)
    boxes = worktree.live_boxes(tmp_path)
    adopted = boxes["carameli--lost-0806"]
    assert adopted.project == "carameli"
    assert adopted.branch == "agent/lost-0806"
    assert adopted.session == ""  # never re-found by find_session_box, only managed


def test_a_recorded_box_is_not_re_adopted_over_its_lease(tmp_path):
    _orphan(tmp_path, "carameli--kept-0806", "agent/kept-0806")
    kept = box("carameli--kept-0806", branch="agent/kept-0806", slot=5, session="s9")
    worktree.write_leases(tmp_path, {kept.name: kept})
    assert worktree.live_boxes(tmp_path)["carameli--kept-0806"] == kept


def test_a_plain_directory_beside_the_boxes_is_not_adopted(tmp_path):
    (worktree.boxes_root(tmp_path) / "carameli--not-a-worktree").mkdir(parents=True)
    (worktree.boxes_root(tmp_path) / "slugs").mkdir()
    assert worktree.live_boxes(tmp_path) == {}


def test_a_detached_worktree_is_not_adopted(tmp_path):
    home = _orphan(tmp_path, "carameli--detached-0806")
    gitdir = Path((home / ".git").read_text(encoding="utf-8")[len("gitdir:") :].strip())
    (gitdir / "HEAD").write_text("0" * 40 + "\n", encoding="utf-8")
    assert worktree.live_boxes(tmp_path) == {}


def test_adoption_recovers_the_slot_from_the_seeded_env(tmp_path):
    reg = registry()
    home = _orphan(tmp_path)
    (home / ".env").write_text(
        worktree.render_env("", worktree.managed_env("carameli--lost-0806", reg, 3)),
        encoding="utf-8",
    )
    adopted = worktree.orphaned_boxes(tmp_path, {}, reg)["carameli--lost-0806"]
    assert adopted.slot == 3


def test_an_adopted_box_without_an_env_spends_no_slot(tmp_path):
    _orphan(tmp_path)
    adopted = worktree.orphaned_boxes(tmp_path, {}, registry())["carameli--lost-0806"]
    assert adopted.slot == -1


def test_the_next_lease_write_persists_an_adopted_box(tmp_path, monkeypatch):
    """Adoption is passive on read; any apply that writes the file makes it durable,
    which is what puts the orphan back in reach of `reap --all` and `reconcile`."""
    workspace = tmp_path / "ws" / "registry.code-workspace"
    workspace.parent.mkdir()
    _orphan(workspace.parent)
    monkeypatch.setattr(worktree, "run_steps", lambda *a, **k: ([], "", ""))

    ok, _ = worktree.apply_new(_spawn_plan(), workspace, provision=False)

    assert ok
    recorded = worktree.read_leases(workspace.parent)
    assert "carameli--lost-0806" in recorded
    assert recorded["carameli--lost-0806"].branch == "agent/lost-0806"


# --- two defects the first real lifecycle found -------------------------------


def test_an_unused_box_can_actually_have_its_branch_deleted():
    """Regression, found by the first end-to-end reap. `-d` asks "is this branch merged
    into the CURRENT checkout's HEAD", and the current checkout is the source repo,
    parked on whatever it was already on.

    A box branch sitting exactly on `origin/<default>` -- every box the guard cuts for a
    session that turns out not to write anything -- is therefore "not fully merged", so
    the last step of every such reap failed, the command exited non-zero, and the branch
    stayed. `ahead == 0` means there are no commits on it at all, so there is nothing
    `-D` can destroy.
    """
    spent = state(upstream="", unpushed=-1, ahead=0)
    assert worktree.branch_delete_flag(spent, pr_merged=False) == "-D"
    plan = worktree.reap_plan(
        box=box(), workspace_root=_root(), state=spent, verdict=sweep.SPENT, reason="no commits"
    )
    assert ("branch", "-D", "claude/voicemail-0806") in plan.steps


def test_a_box_holding_local_only_commits_still_defers_to_git():
    """The widening must not reach the case `-d` is actually guarding."""
    unpushed = state(upstream="", unpushed=-1, ahead=2)
    assert worktree.branch_delete_flag(unpushed, pr_merged=False) == "-d"


def test_a_state_nothing_could_be_read_from_is_not_an_empty_branch():
    """`ahead` is 0 by default, so "no commits" and "no answer" look identical in the
    field. Only a real git read may unlock `-D`."""
    unknown = state(is_git=False, upstream="", unpushed=-1)
    assert worktree.branch_delete_flag(unknown, pr_merged=False) == "-d"


def test_the_port_registry_is_read_from_devkit_not_from_the_workspace_root(tmp_path, monkeypatch):
    """Regression: `ports.toml` lives in devkit's repo root, which is where
    `new-project.py` allocates from -- not beside the checkouts.

    Reading it from the workspace root meant `load_registry` returned None every time,
    so every box got `slot -1` and published its stack on the *source checkout's* ports.
    Nothing said so: `slot -` renders as "no Docker tier", which is a legitimate answer
    for a project that has none.
    """
    devkit_root = tmp_path / "devkit"
    devkit_root.mkdir()
    (devkit_root / "ports.toml").write_text(
        "[registry]\nmax_slots = 8\n\n[services]\napp = 8000\n\n[slots]\ncarameli = 0\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(worktree, "REPO_ROOT", devkit_root)

    loaded = worktree.load_registry(tmp_path)
    assert loaded is not None
    assert loaded.slots == {"carameli": 0}


def test_a_workspace_that_keeps_its_own_registry_still_works(tmp_path, monkeypatch):
    monkeypatch.setattr(worktree, "REPO_ROOT", tmp_path / "no-devkit-here")
    (tmp_path / "ports.toml").write_text(
        "[registry]\nmax_slots = 4\n\n[services]\napp = 8000\n\n[slots]\ndemo = 1\n",
        encoding="utf-8",
    )
    loaded = worktree.load_registry(tmp_path)
    assert loaded is not None and loaded.slots == {"demo": 1}


def test_no_registry_anywhere_is_a_box_with_no_slot(tmp_path, monkeypatch):
    monkeypatch.setattr(worktree, "REPO_ROOT", tmp_path / "no-devkit-here")
    assert worktree.load_registry(tmp_path) is None


# --- creating a box, for real (the git and install calls stubbed) -------------


def _root() -> Path:
    """A workspace root that never gets written to -- for pure planners only."""
    return Path("C:/ws") if worktree.os.name == "nt" else Path("/ws")


@pytest.fixture
def workspace(tmp_path):
    """A workspace file whose parent holds the checkouts, as on a real workstation."""
    root = tmp_path / "ws"
    (root / "demo").mkdir(parents=True)
    path = root / "registry.code-workspace"
    path.write_text(json.dumps({"folders": [{"path": "demo"}]}), encoding="utf-8")
    return path


def _spawned(workspace, **kwargs) -> worktree.SpawnPlan:
    """A plan whose worktree `apply_new` will find already on disk."""
    root = workspace.parent
    name = "demo--x-0806"
    (root / worktree.BOXES_DIR_NAME / name).mkdir(parents=True)
    return worktree.SpawnPlan(
        box=worktree.Box(name=name, project="demo", branch="claude/x-0806"),
        path=str(root / worktree.BOXES_DIR_NAME / name),
        steps=(("worktree", "add", "-b", "claude/x-0806"),),
        **kwargs,
    )


def test_a_box_is_provisioned_when_it_is_created(workspace, monkeypatch):
    ran: list[tuple[worktree.ProvisionStep, ...]] = []
    monkeypatch.setattr(worktree, "run_steps", lambda *a, **k: ([], "", ""))
    monkeypatch.setattr(worktree, "has_stack", lambda path: False)
    monkeypatch.setattr(
        worktree, "run_provision", lambda path, steps, **k: (ran.append(steps), (True, []))[1]
    )
    plan = _spawned(workspace, provision=(worktree.ProvisionStep("uv sync", ("uv", "sync")),))

    ok, _ = worktree.apply_new(plan, workspace)
    assert ok is True
    assert ran == [plan.provision]


def test_a_box_cut_by_the_hook_names_the_install_it_skipped(workspace, monkeypatch):
    """The guard cannot wait minutes for an install, so the message has to carry the
    command -- otherwise the agent's first `/ship` hits the lint gate with no ruff."""
    monkeypatch.setattr(worktree, "run_steps", lambda *a, **k: ([], "", ""))
    monkeypatch.setattr(worktree, "has_stack", lambda path: False)
    monkeypatch.setattr(
        worktree, "run_provision", lambda *a, **k: pytest.fail("the hook must not install")
    )
    plan = _spawned(workspace, provision=(worktree.ProvisionStep("uv sync", ("uv", "sync")),))

    ok, notes = worktree.apply_new(plan, workspace, provision=False)
    assert ok is True
    assert any("provision demo--x-0806 --yes" in note for note in notes)


def test_creating_a_box_releases_the_leases_of_boxes_that_are_gone(workspace, monkeypatch):
    monkeypatch.setattr(worktree, "run_steps", lambda *a, **k: ([], "", ""))
    monkeypatch.setattr(worktree, "has_stack", lambda path: False)
    root = workspace.parent
    worktree.write_leases(root, {"demo--dead-0805": box("demo--dead-0805", project="demo")})

    ok, notes = worktree.apply_new(_spawned(workspace), workspace, provision=False)
    assert ok is True
    assert any("stale lease" in note for note in notes)
    assert set(worktree.read_leases(root)) == {"demo--x-0806"}


def test_a_box_that_could_not_be_cut_leaves_no_lease_behind(workspace, monkeypatch):
    monkeypatch.setattr(worktree, "run_steps", lambda *a, **k: ([], "git worktree add", "boom"))
    plan = worktree.SpawnPlan(
        box=worktree.Box(name="demo--x-0806", project="demo", branch="claude/x-0806"),
        path=str(workspace.parent / worktree.BOXES_DIR_NAME / "demo--x-0806"),
        steps=(("worktree", "add"),),
    )
    ok, notes = worktree.apply_new(plan, workspace, provision=False)
    assert ok is False
    assert any("FAILED" in note for note in notes)
    assert worktree.read_leases(workspace.parent) == {}


# --- reading what exists ------------------------------------------------------


def test_a_lease_without_a_directory_is_not_a_live_box(workspace):
    root = workspace.parent
    worktree.write_leases(
        root,
        {
            "demo--here-0806": box("demo--here-0806", project="demo"),
            "demo--gone-0806": box("demo--gone-0806", project="demo"),
        },
    )
    (root / worktree.BOXES_DIR_NAME / "demo--here-0806").mkdir(parents=True)
    assert set(worktree.live_boxes(root)) == {"demo--here-0806"}


def test_the_survey_carries_what_the_status_line_needs(workspace, monkeypatch):
    root = workspace.parent
    made = box("demo--here-0806", project="demo", created="2026-08-06T10:00:00+00:00")
    worktree.write_leases(root, {"demo--here-0806": made})
    (root / worktree.BOXES_DIR_NAME / "demo--here-0806").mkdir(parents=True)
    monkeypatch.setattr(
        worktree, "inspect_box", lambda *a, **k: (state(), sweep.READY, "2 files changed")
    )

    rows = worktree.survey(workspace)
    assert rows[0]["created"] == "2026-08-06T10:00:00+00:00"
    assert rows[0]["reapable"] is False


def test_a_worktree_with_no_lease_can_still_be_reaped(workspace, monkeypatch):
    """A box created by hand, or one whose lease file was lost. Refusing would leave
    `rm -rf` as the only way out."""
    root = workspace.parent
    (root / worktree.BOXES_DIR_NAME / "demo--orphan-0806").mkdir(parents=True)
    monkeypatch.setattr(
        worktree, "inspect_box", lambda *a, **k: (state(), sweep.SPENT, "no commits")
    )
    monkeypatch.setattr(worktree, "has_stack", lambda path: False)

    plan = worktree.plan_reap("demo--orphan-0806", workspace, fetch=False)
    assert plan.refusal == ""
    # No lease means no recorded branch, so there is no branch step to run.
    assert [step[0] for step in plan.steps] == ["worktree"]


def test_reaping_a_box_that_never_existed_names_the_ones_that_do(workspace):
    worktree.write_leases(workspace.parent, {"demo--x-0806": box("demo--x-0806", project="demo")})
    with pytest.raises(worktree.WorktreeError, match="demo--x-0806"):
        worktree.plan_reap("demo--typo-0806", workspace, fetch=False)


# --- what the operator reads --------------------------------------------------


def test_the_survey_renders_every_box_and_singles_out_the_ones_holding_work():
    rows = [
        {
            "box": "demo--busy-0806",
            "branch": "claude/busy-0806",
            "slot": 3,
            "verdict": sweep.READY,
            "reason": "2 files changed",
            "reapable": False,
        },
        {
            "box": "demo--done-0806",
            "branch": "claude/done-0806",
            "slot": -1,
            "verdict": sweep.SPENT,
            "reason": "no commits",
            "reapable": True,
        },
    ]
    rendered = worktree.render_survey(rows)
    assert "1 box(es) still holding work" in rendered
    assert "demo--busy-0806 [ready] -- 2 files changed" in rendered
    assert "demo--done-0806" in rendered


def test_an_empty_survey_says_how_to_make_a_box():
    assert "new <project>" in worktree.render_survey([])


def test_a_dry_run_shows_the_install_before_it_costs_three_minutes():
    plan = worktree.SpawnPlan(
        box=box("demo--x-0806", project="demo"),
        path="C:/ws/.worktrees/demo--x-0806",
        provision=(worktree.ProvisionStep("uv sync (uv.lock)", ("uv", "sync")),),
    )
    rendered = worktree.render_spawn(plan, applied=False, notes=[])
    assert "uv sync (uv.lock): uv sync" in rendered
    assert "Dry run" in rendered


def test_provisioning_a_project_with_nothing_to_install_says_so():
    assert "nothing to install" in worktree.render_provision("demo--x-0806", (), False, [])


# --- the CLI, over more than one box ------------------------------------------


def test_reap_all_steps_over_the_boxes_that_are_holding_work(workspace, monkeypatch, capsys):
    """The whole point of a sweep mode: one pass, the reapable ones gone, the rest named
    and left alone -- and an exit code that does not call that outcome a failure."""
    root = workspace.parent
    for name in ("demo--busy-0806", "demo--done-0806"):
        (root / worktree.BOXES_DIR_NAME / name).mkdir(parents=True)
    worktree.write_leases(
        root,
        {
            "demo--busy-0806": box("demo--busy-0806", project="demo", branch="claude/busy-0806"),
            "demo--done-0806": box("demo--done-0806", project="demo", branch="claude/done-0806"),
        },
    )
    verdicts = {
        "demo--busy-0806": (sweep.READY, "2 files changed"),
        "demo--done-0806": (sweep.SPENT, "no commits"),
    }
    monkeypatch.setattr(
        worktree,
        "inspect_box",
        lambda b, root, fetch=False: (state(), *verdicts[b.name]),
    )
    monkeypatch.setattr(worktree, "has_stack", lambda path: False)
    reaped: list[str] = []
    monkeypatch.setattr(
        worktree, "apply_reap", lambda plan, ws: (reaped.append(plan.box), (True, []))[1]
    )

    code = worktree.main(["reap", "--all", "--no-fetch", "--yes", "--workspace", str(workspace)])
    out = capsys.readouterr().out
    assert code == 0
    assert reaped == ["demo--done-0806"]
    assert "refused" in out and "demo--busy-0806" in out


def test_reaping_one_box_by_name_still_fails_when_it_refuses(workspace, monkeypatch):
    """The other direction: the caller named THIS box, so being told no is a failure."""
    root = workspace.parent
    (root / worktree.BOXES_DIR_NAME / "demo--busy-0806").mkdir(parents=True)
    worktree.write_leases(root, {"demo--busy-0806": box("demo--busy-0806", project="demo")})
    monkeypatch.setattr(
        worktree, "inspect_box", lambda *a, **k: (state(), sweep.READY, "2 files changed")
    )
    monkeypatch.setattr(worktree, "has_stack", lambda path: False)

    code = worktree.main(
        ["reap", "demo--busy-0806", "--no-fetch", "--yes", "--workspace", str(workspace)]
    )
    assert code == 1


def test_reap_refuses_an_argument_pair_it_cannot_honour(workspace):
    assert worktree.main(["reap", "demo--x-0806", "--all", "--workspace", str(workspace)]) == 2


def test_provisioning_an_unknown_box_is_an_error_not_a_no_op(workspace):
    assert worktree.main(["provision", "demo--ghost-0806", "--workspace", str(workspace)]) == 2


# --- reconcile: reading GitHub ----------------------------------------------


def pr_json(**kwargs) -> str:
    payload = {
        "number": 42,
        "url": "https://github.com/o/r/pull/42",
        "state": "OPEN",
        "labels": [],
        "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
    }
    return json.dumps({**payload, **kwargs})


def test_rollup_is_green_only_when_every_check_concluded_successfully():
    rollup = [
        {"status": "COMPLETED", "conclusion": "SUCCESS"},
        {"status": "COMPLETED", "conclusion": "SKIPPED"},
    ]
    assert worktree.rollup_conclusion(rollup) == worktree.CHECKS_SUCCESS


def test_rollup_takes_the_worst_check_not_the_last():
    """A green check reported after a red one must not read as green."""
    rollup = [
        {"status": "COMPLETED", "conclusion": "FAILURE"},
        {"status": "COMPLETED", "conclusion": "SUCCESS"},
    ]
    assert worktree.rollup_conclusion(rollup) == worktree.CHECKS_FAILURE


def test_a_running_check_is_pending_whatever_its_conclusion_field_says():
    assert worktree.rollup_conclusion([{"status": "IN_PROGRESS", "conclusion": ""}]) == (
        worktree.CHECKS_PENDING
    )


def test_legacy_commit_statuses_are_read_through_state():
    """`gh` returns `state` for commit statuses and `conclusion` for check runs."""
    assert worktree.rollup_conclusion([{"state": "FAILURE"}]) == worktree.CHECKS_FAILURE
    assert worktree.rollup_conclusion([{"state": "SUCCESS"}]) == worktree.CHECKS_SUCCESS


def test_an_empty_rollup_is_not_success():
    """No checks at all is a gate that never ran, never a licence to merge."""
    assert worktree.rollup_conclusion([]) == worktree.CHECKS_NONE
    assert worktree.rollup_conclusion(None) == worktree.CHECKS_NONE


def test_an_unparseable_rollup_entry_is_pending_never_success():
    assert worktree.rollup_conclusion(["nonsense"]) == worktree.CHECKS_PENDING


def test_parse_pr_view_reads_number_state_labels_and_checks():
    pr = worktree.parse_pr_view(pr_json(labels=[{"name": "automerge"}]))
    assert (pr.number, pr.state, pr.checks) == (42, "OPEN", worktree.CHECKS_SUCCESS)
    assert pr.labels == ("automerge",)
    assert pr.is_open and not pr.merged


def test_parse_pr_view_degrades_to_no_pr_on_junk():
    for raw in ("", "not json", "[]", "{}", '{"state": ""}'):
        assert not worktree.parse_pr_view(raw).exists


def test_pr_for_fails_closed_when_gh_is_missing_or_errors():
    """An offline `gh` must make reconcile do LESS, never more."""

    def exploding(*args):
        raise OSError("gh not found")

    assert not worktree.pr_for(exploding, "claude/x-0806").exists
    assert not worktree.pr_for(lambda *a: _completed(1, "", "no pr"), "claude/x-0806").exists
    assert not worktree.pr_for(lambda *a: _completed(0, pr_json()), "").exists


# --- reconcile: the merge gate ----------------------------------------------


def test_mergeable_requires_open_green_and_the_label_when_one_is_set():
    green = worktree.parse_pr_view(pr_json(labels=[{"name": "automerge"}]))
    assert worktree.mergeable(green) == (True, "")
    assert worktree.mergeable(green, "automerge") == (True, "")
    assert worktree.mergeable(green, "release-me")[0] is False


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"statusCheckRollup": [{"status": "COMPLETED", "conclusion": "FAILURE"}]}, "red"),
        ({"statusCheckRollup": [{"status": "IN_PROGRESS"}]}, "still running"),
        ({"statusCheckRollup": []}, "no checks"),
        ({"state": "MERGED"}, "not open"),
        ({"state": "CLOSED"}, "not open"),
    ],
)
def test_mergeable_names_the_reason_it_refused(payload, expected):
    allowed, why = worktree.mergeable(worktree.parse_pr_view(pr_json(**payload)))
    assert allowed is False
    assert expected in why


# --- reconcile: the decision ------------------------------------------------


def decide(verdict=sweep.NEEDS_PR, reason="pushed", pr=None, **kwargs):
    return worktree.reconcile_action(verdict, reason, pr or worktree.PullRequest(), **kwargs)


def test_a_box_holding_work_is_held_even_when_its_pr_merged():
    """The safety property: work that exists only in the box is never destroyed.

    Shipping a branch and then continuing to edit in the box is an ordinary thing to
    do, and the PR can merge while those edits are still uncommitted. Reaping on the
    strength of the merge alone would delete them.
    """
    merged = worktree.parse_pr_view(pr_json(state="MERGED"))
    action, why = decide(sweep.READY, "3 uncommitted file(s)", merged)
    assert action == worktree.HOLD
    assert "ready" in why


def test_a_box_holding_work_survives_disk_pressure_and_any_age():
    action, _ = decide(sweep.READY, "work", pressure=True, age_days=999.0)
    assert action == worktree.HOLD


def test_a_merged_pr_reaps_its_box():
    merged = worktree.parse_pr_view(pr_json(state="MERGED"))
    action, why = decide(sweep.NEEDS_PR, "pushed", merged)
    assert action == worktree.REAP
    assert "#42 merged" in why


def test_an_open_pr_waits_by_default():
    action, why = decide(pr=worktree.parse_pr_view(pr_json()))
    assert action == worktree.WAIT
    assert "auto-merge is off" in why


def test_an_open_green_pr_merges_when_automerge_is_on():
    action, why = decide(pr=worktree.parse_pr_view(pr_json()), automerge=True)
    assert action == worktree.MERGE
    assert "#42 is green" in why


def test_a_red_pr_never_merges_and_still_waits():
    red = pr_json(statusCheckRollup=[{"status": "COMPLETED", "conclusion": "FAILURE"}])
    action, why = decide(pr=worktree.parse_pr_view(red), automerge=True)
    assert action == worktree.WAIT
    assert "red" in why


def test_disk_pressure_reaps_a_box_whose_pr_is_merely_open():
    """Its commits are all on the remote, so only the checkout is lost."""
    action, why = decide(pr=worktree.parse_pr_view(pr_json()), pressure=True)
    assert action == worktree.REAP
    assert "reclaiming disk" in why


def test_an_old_open_pr_is_reaped_without_waiting_for_pressure():
    action, why = decide(pr=worktree.parse_pr_view(pr_json()), age_days=9.0, max_age_days=3.0)
    assert action == worktree.REAP
    assert "9.0d" in why


def test_a_young_open_pr_is_not_reaped_by_age():
    action, _ = decide(pr=worktree.parse_pr_view(pr_json()), age_days=1.0, max_age_days=3.0)
    assert action == worktree.WAIT


def test_an_unused_box_with_no_pr_is_reaped_immediately():
    """The commonest kind: the guard cuts one per session whether or not it writes."""
    for verdict in (sweep.SPENT, sweep.CLEAN):
        action, why = decide(verdict, "nothing here")
        assert action == worktree.REAP
        assert "never used" in why


def test_a_pushed_branch_with_no_pr_is_reported_never_destroyed():
    """Safe on the remote, but nobody will look at it, and that is a person's call."""
    action, why = decide(sweep.NEEDS_PR, "pushed")
    assert action == worktree.WAIT
    assert "no PR" in why


def test_no_decision_destroys_a_box_outside_safe_to_reap():
    """Reversion check for the whole mode, swept over every input combination."""
    for verdict in (sweep.READY, sweep.BLOCKED, sweep.NEEDS_BRANCH, sweep.NEEDS_REBRANCH):
        for pr in (worktree.PullRequest(), worktree.parse_pr_view(pr_json(state="MERGED"))):
            for pressure in (True, False):
                action, _ = decide(
                    verdict, "x", pr, pressure=pressure, automerge=True, age_days=1e6
                )
                assert action == worktree.HOLD, (verdict, pr.state, pressure)


# --- reconcile: age and disk ------------------------------------------------


def test_box_age_is_measured_from_the_lease_timestamp():
    now = _dt.datetime(2026, 8, 9, tzinfo=_dt.UTC)
    assert worktree.box_age_days("2026-08-06T00:00:00+00:00", now) == pytest.approx(3.0)


def test_an_unreadable_timestamp_reads_as_brand_new():
    """Age only ever licenses destruction, so unparseable must not license it."""
    assert worktree.box_age_days("not a date") == 0.0
    assert worktree.box_age_days("") == 0.0


def test_a_naive_timestamp_is_read_as_utc_not_rejected():
    now = _dt.datetime(2026, 8, 8, tzinfo=_dt.UTC)
    assert worktree.box_age_days("2026-08-06T00:00:00", now) == pytest.approx(2.0)


def test_pressure_needs_a_readable_volume():
    """`free_gb` returns -1.0 when it cannot tell, and that must not escalate."""
    assert worktree.under_pressure(5.0, 20.0) is True
    assert worktree.under_pressure(50.0, 20.0) is False
    assert worktree.under_pressure(-1.0, 20.0) is False


def test_dir_size_sums_a_tree(tmp_path):
    (tmp_path / "a").write_bytes(b"x" * 100)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b").write_bytes(b"y" * 50)
    assert worktree.dir_size_bytes(tmp_path) == 150


def test_human_bytes_reads_as_a_disk_label():
    assert worktree.human_bytes(512) == "512 B"
    assert worktree.human_bytes(1_500_000_000) == "1.5 GB"


# --- reconcile: the whole pass ----------------------------------------------


def _reconcilable(workspace, monkeypatch, name, verdict, reason, pr_state):
    """Stand one box up on disk with its git and GitHub answers stubbed."""
    root = workspace.parent
    (root / worktree.BOXES_DIR_NAME / name).mkdir(parents=True)
    worktree.write_leases(root, {name: box(name, project="demo")})
    monkeypatch.setattr(worktree, "inspect_box", lambda *a, **k: (state(), verdict, reason))
    monkeypatch.setattr(worktree, "has_stack", lambda path: False)
    monkeypatch.setattr(
        worktree,
        "pr_for",
        lambda gh, branch: worktree.parse_pr_view(pr_json(state=pr_state) if pr_state else "{}"),
    )
    return root


def test_reconcile_plan_orders_by_box_name_and_decides_each():
    merged = worktree.parse_pr_view(pr_json(state="MERGED"))
    rows = [
        (box("b--two-0806"), sweep.READY, "work", merged, 3),
        (box("a--one-0806"), sweep.NEEDS_PR, "up", merged, 0),
    ]
    plan = worktree.reconcile_plan(rows)
    assert [p.box for p in plan] == ["a--one-0806", "b--two-0806"]
    assert [p.action for p in plan] == [worktree.REAP, worktree.HOLD]


def test_reconcile_plan_reads_dirtiness_from_the_row_not_from_the_verdict():
    """Two boxes on the same verdict and the same merged PR, told apart only by the
    uncommitted count the row carries. Drop that field and both decide the same way --
    either the clean one leaks forever or the dirty one is destroyed."""
    merged = worktree.parse_pr_view(pr_json(state="MERGED"))
    rows = [
        (box("a--clean-0806"), sweep.NEEDS_REBRANCH, "2 unmerged commit(s)", merged, 0),
        (box("b--dirty-0806"), sweep.NEEDS_REBRANCH, "2 uncommitted file(s)", merged, 2),
    ]
    assert [p.action for p in worktree.reconcile_plan(rows)] == [
        worktree.REAP,
        worktree.HOLD,
    ]


def test_reconcile_reaps_a_merged_box_end_to_end(workspace, monkeypatch):
    """The loop that replaces remembering to sweep: PR merged in, box gone out."""
    root = _reconcilable(
        workspace, monkeypatch, "demo--done-0806", sweep.NEEDS_PR, "pushed", "MERGED"
    )
    ran: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        worktree,
        "run_steps",
        lambda cwd, steps, timeout=300.0: (ran.extend(steps), (["ok"], "", ""))[1],
    )

    code, report = worktree.reconcile(workspace, apply=True)

    assert code == 0
    assert [row["action"] for row in report["boxes"]] == [worktree.REAP]
    assert any(step[:2] == ("worktree", "remove") for step in ran)
    assert worktree.read_leases(root) == {}


def test_reconcile_never_reaps_a_box_holding_work(workspace, monkeypatch):
    root = _reconcilable(
        workspace, monkeypatch, "demo--busy-0806", sweep.READY, "2 uncommitted", "MERGED"
    )
    monkeypatch.setattr(
        worktree, "run_steps", lambda *a, **k: pytest.fail("reconcile touched a held box")
    )

    code, report = worktree.reconcile(workspace, apply=True)

    assert code == 0
    assert report["boxes"][0]["action"] == worktree.HOLD
    assert "demo--busy-0806" in worktree.read_leases(root)


def test_reconcile_merges_then_reaps_in_one_pass(workspace, monkeypatch):
    """A box finishes its whole life in one pass, not one stage per interval."""
    root = _reconcilable(
        workspace, monkeypatch, "demo--green-0806", sweep.NEEDS_PR, "pushed", "OPEN"
    )
    merged: list[int] = []
    monkeypatch.setattr(
        worktree, "merge_pr", lambda gh, number: (merged.append(number), (True, "merged"))[1]
    )
    monkeypatch.setattr(worktree, "run_steps", lambda *a, **k: (["ok"], "", ""))

    code, report = worktree.reconcile(workspace, apply=True, automerge=True)

    assert code == 0
    assert merged == [42]
    assert report["boxes"][0]["action"] == worktree.REAP
    assert worktree.read_leases(root) == {}


def test_a_failed_merge_leaves_the_box_alone_and_reddens_the_pass(workspace, monkeypatch):
    root = _reconcilable(
        workspace, monkeypatch, "demo--stuck-0806", sweep.NEEDS_PR, "pushed", "OPEN"
    )
    monkeypatch.setattr(worktree, "merge_pr", lambda gh, number: (False, "merge conflict"))
    monkeypatch.setattr(
        worktree, "run_steps", lambda *a, **k: pytest.fail("reaped after a failed merge")
    )

    code, report = worktree.reconcile(workspace, apply=True, automerge=True)

    assert code == 1
    assert report["boxes"][0]["action"] == worktree.MERGE
    assert "demo--stuck-0806" in worktree.read_leases(root)


def test_reconcile_dry_run_changes_nothing(workspace, monkeypatch):
    root = _reconcilable(
        workspace, monkeypatch, "demo--done-0806", sweep.NEEDS_PR, "pushed", "MERGED"
    )
    monkeypatch.setattr(
        worktree, "run_steps", lambda *a, **k: pytest.fail("a dry run mutated the workspace")
    )

    code, report = worktree.reconcile(workspace, apply=False)

    assert code == 0
    assert report["applied"] is False
    assert report["boxes"][0]["action"] == worktree.REAP
    assert "demo--done-0806" in worktree.read_leases(root)


def test_reconcile_cli_reports_and_exits_zero_on_an_empty_workspace(workspace, capsys):
    assert worktree.main(["reconcile", "--no-fetch", "--workspace", str(workspace)]) == 0
    assert "Nothing to reconcile" in capsys.readouterr().out


# --- the static half of the scheduled pass ----------------------------------


def _results(*pairs: tuple[str, str]) -> list:
    """`sweep.Result`s from `(checkout name, verdict)` pairs."""
    return [
        sweep.Result(state(name=name, branch="master"), verdict, f"because {verdict}")
        for name, verdict in pairs
    ]


def test_a_checkout_holding_work_is_reported_rather_than_parked():
    """The refusal is the safety property: `sweep.sync_plan` acts only on SYNCABLE, so
    anything else is unshipped work the scheduled pass must step over and name."""
    summary = worktree.checkout_sync_summary(
        _results(("carameli", sweep.CLEAN), ("devkit", sweep.NEEDS_BRANCH)), 0
    )
    assert [row["held"] for row in summary["rows"]] == [False, True]


def test_every_syncable_verdict_counts_as_synced():
    """spent-branch is the one the whole change is for -- a merged PR's branch, still
    checked out, keeping the local default branch from ever advancing."""
    rows = worktree.checkout_sync_summary(
        _results(("a", sweep.SPENT), ("b", sweep.NEEDS_PULL), ("c", sweep.CLEAN)), 0
    )["rows"]
    assert not any(row["held"] for row in rows)


def test_a_non_git_folder_is_left_out_of_the_report_entirely():
    """`skipped` is not a checkout with an opinion; a row for it is noise in a report
    that is read every fifteen minutes."""
    summary = worktree.checkout_sync_summary(_results(("notes", sweep.SKIPPED)), 0)
    assert summary["rows"] == []


def test_a_dry_run_that_found_work_is_not_a_failure():
    """`run_mode` returns 1 for "there is something to do", and a scheduled runner that
    reddens on a healthy pass is a runner whose alerts nobody reads."""
    assert worktree.checkout_sync_summary(_results(("carameli", sweep.CLEAN)), 1)["failed"] is False


def test_a_failed_git_step_is_a_failure():
    assert worktree.checkout_sync_summary(_results(("carameli", sweep.CLEAN)), 2)["failed"] is True


def test_sync_checkouts_asks_sweep_for_the_sync_mode_and_applies_it(workspace, monkeypatch):
    """The wiring test: reconcile must not invent its own plan for the static tier --
    `sweep` owns those checkouts and its `--sync` is the plan the workspace task has
    always printed."""
    seen: dict = {}
    monkeypatch.setattr(sweep, "sweep", lambda root, names, fetch=True: _results(("demo", "clean")))
    monkeypatch.setattr(
        sweep,
        "run_mode",
        lambda root, results, mode, apply, fetch=True, slug="sweep": (
            seen.update(mode=mode, apply=apply),
            ("sync: applied", 0),
        )[1],
    )

    code, summary = worktree.sync_checkouts(workspace, apply=True)

    assert code == 0
    assert seen == {"mode": "sync", "apply": True}
    assert summary["rows"][0]["checkout"] == "demo"


def test_the_reference_checkout_is_never_swept(tmp_path, monkeypatch):
    """VanillaLand is an Azure DevOps reference checkout on a `develop` base, and
    nothing in this workspace ships from it. Reading the exclusion off
    `sweep.parse_workspace` rather than re-listing it here is what keeps the scheduled
    pass and the hand-run sweep agreeing about which checkouts exist."""
    registry = tmp_path / "registry.code-workspace"
    registry.write_text(
        json.dumps({"folders": [{"path": "carameli"}, {"path": "VanillaLand"}]}), encoding="utf-8"
    )
    swept: list[list[str]] = []
    monkeypatch.setattr(
        sweep, "sweep", lambda root, names, fetch=True: (swept.append(names), [])[1]
    )
    monkeypatch.setattr(sweep, "run_mode", lambda *a, **k: ("", 0))

    worktree.sync_checkouts(registry, apply=True)

    assert swept == [["carameli"]]


def test_sync_checkouts_reports_a_registry_it_could_not_read(tmp_path):
    """Reporting a clean sweep of checkouts it never looked at is the one wrong answer
    -- it is indistinguishable from four checkouts that are genuinely current."""
    code, summary = worktree.sync_checkouts(tmp_path / "missing.code-workspace", apply=True)
    assert code == 1
    assert summary["failed"] is True


def test_reconcile_syncs_the_checkouts_by_default(workspace, monkeypatch):
    """The regression test for the whole change: a PR merges, and nothing local
    advances until someone remembers to sweep."""
    called: list[bool] = []
    monkeypatch.setattr(
        worktree,
        "sync_checkouts",
        lambda ws, apply, fetch: (called.append(apply), (0, {"rows": [], "failed": False}))[1],
    )

    worktree.reconcile(workspace, apply=True)

    assert called == [True]


def test_reconcile_can_be_scheduled_for_boxes_only(workspace, monkeypatch):
    monkeypatch.setattr(
        worktree,
        "sync_checkouts",
        lambda *a, **k: pytest.fail("--no-checkouts still touched the static tier"),
    )
    assert worktree.reconcile(workspace, apply=True, checkouts=False)[0] == 0


def test_a_checkout_that_will_not_sync_reddens_the_pass(workspace, monkeypatch):
    monkeypatch.setattr(
        worktree, "sync_checkouts", lambda *a, **k: (1, {"rows": [], "failed": True})
    )
    assert worktree.reconcile(workspace, apply=True)[0] == 1


def test_the_healthy_checkout_report_is_one_line(workspace, monkeypatch):
    """Read every fifteen minutes, so the answer it gives almost every time has to be
    skimmable -- otherwise the box section above it is what gets skipped."""
    summary = worktree.checkout_sync_summary(_results(("a", sweep.CLEAN), ("b", sweep.SPENT)), 0)
    assert worktree.render_checkout_sync(summary, applied=True) == [
        "\n  checkouts: 2 synced, 0 holding work"
    ]


def test_a_failing_sync_carries_sweeps_own_report_into_the_log():
    """The log is the only artifact a windowless run leaves, so the failing git command
    has to be in it -- a count of failures is not something you can diagnose from."""
    summary = worktree.checkout_sync_summary(_results(("a", sweep.CLEAN)), 2)
    summary["report"] = "  a: FAILED at `git merge --ff-only origin/master`"
    rendered = "\n".join(worktree.render_checkout_sync(summary, applied=True))
    assert "FAILED at `git merge --ff-only origin/master`" in rendered


def test_the_checkouts_holding_work_are_named_not_counted():
    summary = worktree.checkout_sync_summary(_results(("devkit", sweep.NEEDS_BRANCH)), 0)
    rendered = "\n".join(worktree.render_checkout_sync(summary, applied=True))
    assert "devkit [needs-branch]" in rendered


# --- the scheduled pass leaves a record -------------------------------------


def test_reconcile_writes_its_report_to_a_log(tmp_path):
    """The scheduled run is windowless, so stdout goes nowhere. Without this the one
    thing that destroys checkouts unattended would have no record of what it did."""
    path = worktree.write_reconcile_log("reaped demo--x-0806", 0, root=tmp_path)
    assert path == tmp_path / worktree.RECONCILE_LOG
    body = path.read_text(encoding="utf-8")
    assert "reaped demo--x-0806" in body
    assert "exit=0" in body


def test_the_log_is_written_on_success_too_not_only_on_failure(tmp_path):
    """A log that only appears on failure cannot be told apart from a job that never
    ran -- which is the failure mode a silent scheduled task actually has."""
    worktree.write_reconcile_log("Nothing to reconcile.", 0, root=tmp_path)
    assert (tmp_path / worktree.RECONCILE_LOG).is_file()


def test_the_log_is_overwritten_per_run_not_appended(tmp_path):
    worktree.write_reconcile_log("first pass", 0, root=tmp_path)
    worktree.write_reconcile_log("second pass", 1, root=tmp_path)
    body = (tmp_path / worktree.RECONCILE_LOG).read_text(encoding="utf-8")
    assert "second pass" in body
    assert "first pass" not in body


def test_an_unwritable_log_never_fails_the_pass(tmp_path, monkeypatch):
    """A reconcile that did its work must not report failure over a log file."""
    monkeypatch.setattr(
        worktree.Path, "mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only"))
    )
    assert worktree.write_reconcile_log("x", 0, root=tmp_path) is None


# --- the scheduled pass opens no windows ------------------------------------
# A process with no console of its own (pythonw.exe, which the scheduled reconcile
# runs under) gets Windows to allocate a NEW console window per console child. One
# pass spawns ~40 git/gh calls, so a single unflagged site is 40 flickering windows.


def test_no_window_is_windows_only():
    assert (sweep.NO_WINDOW != 0) is (worktree.os.name == "nt")


def test_every_spawn_in_the_reconcile_path_suppresses_its_console():
    """Source-level, because the symptom is only visible on a Windows desktop under
    pythonw -- there is no runtime assertion that would have caught this, and the bug
    it guards was shipped and noticed by a human watching windows flash."""
    import re

    for rel in ("scripts/sweep.py", "scripts/worktree.py", "scripts/worktree-guard.py"):
        source = (worktree.REPO_ROOT / rel).read_text(encoding="utf-8")
        calls = re.findall(r"subprocess\.run\((.*?)\n        \)", source, re.S)
        calls += re.findall(r"subprocess\.run\((.*?)\n            \)", source, re.S)
        for call in calls:
            assert "NO_WINDOW" in call, f"{rel}: a subprocess.run without NO_WINDOW:\n{call}"


def test_the_console_suppression_covers_gh_and_docker_not_only_git():
    """git is 39 of the ~42 spawns in a pass, so a fix that only covered git would
    look like it worked and still flash a window per box for `gh pr view` and one per
    stack for `compose down`."""
    source = (worktree.REPO_ROOT / "scripts" / "worktree.py").read_text(encoding="utf-8")
    for marker in ("docker", "compose", "-v", "--remove-orphans"):
        assert marker in source
    gh_call = (worktree.REPO_ROOT / "scripts" / "sweep.py").read_text(encoding="utf-8")
    gh_block = gh_call.split('["gh", *args]')[1][:300]
    assert "NO_WINDOW" in gh_block
