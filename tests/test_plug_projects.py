"""Tests for the plug/unplug checkbox list.

Three things are worth pinning, and they are the three that would fail quietly.

The **inventory** merges sources that disagree, and a project missing from one of them
is the whole reason the script exists — so every combination of on-disk/on-GitHub is
asserted, including the one that reads as "nothing to do" and is not.

The **plan** decides an outward-facing act (creating a private GitHub repo) from a
ticked checkbox, so it is pure and every step is asserted before anything can run it.

And `edit_verdict` is the gate between this script and the file every window on the
machine reads. Its three refusals are the ones that would otherwise publish an unmerged
proposal, so they are tested without a git tree at all.

A fourth since the checkboxes moved into VS Code, and it changed shape when the list
stopped being a cached file: `toggled_selection` reads a pick as a set of rows to
*flip*, so what is pinned is that a row nobody ticked is left exactly as it is and that
the same pick applied twice is a no-op. The two functions this replaced,
`selection_from_ticks` and `guarded_selection`, existed only to tell "unticked" from
"never offered" in an answer made against options some earlier pass had written down.
"""

import json
import subprocess

import pytest
from support import REPO_ROOT, devkit_project, load_script, worktree

picker_rows = load_script("scripts/picker_rows.py")

plug_projects = load_script("scripts/plug-projects.py")

Candidate = plug_projects.Candidate
PlugError = plug_projects.PlugError
Step = plug_projects.Step
apply_registry = plug_projects.apply_registry
clone_repo = plug_projects.clone_repo
create_repo = plug_projects.create_repo
describe = plug_projects.describe
disk_projects = plug_projects.disk_projects
edit_verdict = plug_projects.edit_verdict
gather = plug_projects.gather
github_repos = plug_projects.github_repos
in_ephemeral_box = plug_projects.in_ephemeral_box
interactive = plug_projects.interactive
inventory = plug_projects.inventory
live_carries_a_hand_edit = plug_projects.live_carries_a_hand_edit
main = plug_projects.main
toggle_detail = plug_projects.toggle_detail
rows = plug_projects.rows
parse_command = plug_projects.parse_command
parse_ticks = plug_projects.parse_ticks
picked_nothing = plug_projects.picked_nothing
toggled_selection = plug_projects.toggled_selection
plan = plug_projects.plan
render = plug_projects.render
scripted_env = plug_projects.scripted_env
unplug_hazards = plug_projects.unplug_hazards
write_artifact = plug_projects.write_artifact

REGISTRY = '{"folders": [{"path": "alpha"}, {"path": "beta"}]}'

# The same registry plus a reference checkout. Only the exclusion test below uses it,
# and only with `NOT_PROJECTS` monkeypatched: the set is empty today, so a registry that
# carried this name unconditionally would just add a candidate everywhere else.
REFERENCE = "reference-checkout"
REFERENCE_REGISTRY = (
    '{"folders": [{"path": "alpha"}, {"path": "beta"}, {"path": "reference-checkout"}]}'
)


def done(stdout: str = "", code: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], code, stdout, stderr)


# --- the three sources ------------------------------------------------------


def test_only_directories_that_look_like_a_checkout_are_offered(tmp_path):
    """The workspace root also holds `logs/`, the tool caches and `.worktrees/` — the
    last of which would offer a dozen rows that reap themselves."""
    for name in ("alpha", "beta"):
        (tmp_path / name / ".git").mkdir(parents=True)
    (tmp_path / "gamma").mkdir()
    (tmp_path / "gamma" / ".devkit.toml").write_text("")
    (tmp_path / "logs").mkdir()
    (tmp_path / "notes").mkdir()
    (tmp_path / ".worktrees" / "alpha--task-0101").mkdir(parents=True)
    (tmp_path / "loose.txt").write_text("")
    assert disk_projects(tmp_path) == ["alpha", "beta", "gamma"]


def test_a_worktree_is_a_checkout_even_though_its_dot_git_is_a_file(tmp_path):
    (tmp_path / "alpha").mkdir()
    (tmp_path / "alpha" / ".git").write_text("gitdir: ../.git/worktrees/alpha\n")
    assert disk_projects(tmp_path) == ["alpha"]


def test_the_repo_listing_is_read_from_gh_json():
    calls = []

    def runner(argv):
        calls.append(argv)
        return done('[{"name": "beta"}, {"name": "alpha"}]')

    assert github_repos(runner) == ["alpha", "beta"]
    assert "--no-archived" in calls[0]


def test_a_failed_repo_listing_raises_rather_than_returning_nothing():
    """ "You have no repos" and "gh is not logged in" produce the same list and mean
    opposite things — and the second would offer to *create* every repo that exists."""
    with pytest.raises(PlugError, match="could not list repos"):
        github_repos(lambda argv: done(code=1, stderr="gh: not logged in\n"))


def test_unreadable_gh_output_raises():
    with pytest.raises(PlugError, match="cannot read"):
        github_repos(lambda argv: done("not json at all"))


# --- the inventory ----------------------------------------------------------


def test_registered_projects_keep_the_registry_order():
    """That array is hand-arranged, and reordering it would make the checkbox numbers
    disagree with the workspace tree everyone reads."""
    found = inventory(REGISTRY, ["beta", "alpha"], ["alpha", "beta"])
    assert [c.name for c in found] == ["alpha", "beta"]
    assert all(c.plugged for c in found)


def test_a_project_in_only_one_source_is_still_offered():
    found = {c.name: c for c in inventory(REGISTRY, ["alpha", "beta", "onlydisk"], ["onlyrepo"])}
    assert found["onlydisk"].on_disk and not found["onlydisk"].on_github
    assert found["onlyrepo"].on_github and not found["onlyrepo"].on_disk
    assert not found["onlydisk"].plugged and not found["onlyrepo"].plugged


def test_the_reference_checkout_is_never_a_candidate(monkeypatch):
    """It is in `folders` on purpose and every consumer of the registry excludes it by
    name; a checkbox that could retire it would break the one task that resolves
    against the raw registry.

    `NOT_PROJECTS` is empty today, so the name has to be put in it for the exclusion to
    be exercised at all.
    """
    monkeypatch.setattr(devkit_project, "NOT_PROJECTS", frozenset({REFERENCE}))
    names = [c.name for c in inventory(REFERENCE_REGISTRY, [REFERENCE], [REFERENCE])]
    assert REFERENCE not in names


def test_unregistered_candidates_are_alphabetical_after_the_registered_ones():
    found = inventory(REGISTRY, ["alpha", "beta", "Zeta"], ["mid"])
    assert [c.name for c in found] == ["alpha", "beta", "mid", "Zeta"]


def test_the_source_column_names_which_sources_hold_it():
    found = {c.name: c for c in inventory(REGISTRY, ["alpha", "beta", "d"], ["alpha", "beta", "r"])}
    assert found["alpha"].where == "folder + repo"
    assert found["d"].where == "folder only"
    assert found["r"].where == "repo only"


# --- the checkbox list ------------------------------------------------------


def test_a_ticked_box_means_registered():
    lines = render(inventory(REGISTRY, ["alpha", "beta"], []), {"alpha"})
    assert lines[0].startswith("   1. [x] alpha")
    assert lines[1].startswith("   2. [ ] beta")


def test_a_box_that_no_longer_matches_the_registry_says_which_verb_will_run():
    candidates = inventory(REGISTRY, ["alpha", "beta", "gamma"], [])
    lines = render(candidates, {"beta", "gamma"})
    assert "will unplug" in lines[0]
    assert "will plug" in lines[2]
    assert "will" not in lines[1]


def test_an_empty_list_says_so_rather_than_rendering_nothing():
    assert "no projects found" in render([], set())[0]


def test_a_folder_with_no_harness_is_flagged():
    """A checkout with no `.devkit.toml` reaches no generic task, so plugging it in is
    a smaller act than the checkbox implies."""
    found = inventory(REGISTRY, ["alpha", "beta", "raw"], [], harnessed=frozenset({"alpha"}))
    assert "no .devkit.toml" in render(found, set())[-1]
    assert "no .devkit.toml" not in render(found, set())[0]


@pytest.mark.parametrize(
    ("line", "verb"),
    [
        ("q", "quit"),
        ("QUIT", "quit"),
        ("a", "apply"),
        ("yes", "apply"),
        ("l", "list"),
        ("?", "help"),
        ("", "noop"),
        ("   ", "noop"),
    ],
)
def test_the_single_letter_commands(line, verb):
    assert parse_command(line, ["alpha", "beta"])[0] == verb


def test_numbers_and_names_both_toggle():
    """The numbers move — plugging a project reorders the list on the next redraw — so
    a name is the only stable handle."""
    assert parse_command("2", ["alpha", "beta"]) == ("toggle", ("beta",))
    assert parse_command("Beta", ["alpha", "beta"]) == ("toggle", ("beta",))
    assert parse_command("1, 2", ["alpha", "beta"]) == ("toggle", ("alpha", "beta"))


def test_an_unrecognised_token_is_an_error_not_a_silent_no_op():
    """In a list of checkboxes, a silent no-op reads as "that one is not togglable"."""
    verb, why = parse_command("gamma", ["alpha", "beta"])
    assert verb == "error" and "gamma" in why[0]
    verb, why = parse_command("9", ["alpha", "beta"])
    assert verb == "error" and "line 9" in why[0]


def test_the_loop_toggles_then_applies():
    candidates = inventory(REGISTRY, ["alpha", "beta", "gamma"], [])
    answers = iter(["2", "gamma", "a"])
    assert interactive(candidates, {"alpha", "beta"}, lambda _: next(answers), lambda *a: None) == {
        "alpha",
        "gamma",
    }


def test_quitting_returns_nothing_rather_than_the_ticks_so_far():
    candidates = inventory(REGISTRY, ["alpha", "beta"], [])
    answers = iter(["1", "q"])
    assert interactive(candidates, {"alpha"}, lambda _: next(answers), lambda *a: None) is None


def test_an_eof_is_not_consent_to_edit_the_registry():
    """A task terminal that closed, or a stdin that was never a terminal."""

    def ask(_):
        raise EOFError

    candidates = inventory(REGISTRY, ["alpha", "beta"], [])
    assert interactive(candidates, {"alpha"}, ask, lambda *a: None) is None


def test_an_error_does_not_end_the_loop():
    candidates = inventory(REGISTRY, ["alpha", "beta"], [])
    answers = iter(["nope", "?", "2", "a"])
    assert interactive(candidates, set(), lambda _: next(answers), lambda *a: None) == {"beta"}


# --- the same list, drawn by VS Code ----------------------------------------


@pytest.mark.parametrize(
    "candidate, expected",
    [
        (Candidate("alpha", plugged=True, on_disk=True, on_github=True), "ticking retires it"),
        (
            Candidate("zeta", plugged=True, on_disk=False, on_github=True),
            "leaving it alone clones acme/zeta",
        ),
        (Candidate("gamma", plugged=False, on_disk=False, on_github=True), "clones acme/gamma"),
        (Candidate("delta", plugged=False, on_disk=True, on_github=False), "CREATES the private"),
        (
            Candidate("eps", plugged=False, on_disk=True, on_github=True),
            "registers it, and nothing",
        ),
    ],
)
def test_each_row_says_what_its_own_tick_costs(candidate, expected):
    """The task runs `--picks ... --yes`, so the quick-pick *is* the confirmation and
    this line is the last thing anyone reads before a private GitHub repo is created.
    A tick flips the row, so the two directions cost wildly different things and the
    sentence has to name the one THIS row is offering."""
    assert expected in toggle_detail(candidate, "acme")


def test_every_row_carries_the_four_fields_the_quick_pick_splits_on():
    """`shellCommand.execute` is positional: `value|label|description|detail`, and a
    field carrying the separator silently becomes two. `picker_rows.row` is what
    contains that, so this asserts the shape reaches it rather than re-testing it."""
    candidates = inventory(REGISTRY, ["alpha", "beta"], ["alpha", "beta", "gamma"])
    drawn = rows(candidates, "acme")
    assert len(drawn) == len(candidates)
    for line in drawn:
        assert len(line.split(picker_rows.FIELD_SEP)) == 4


def test_the_row_value_is_the_bare_project_name():
    """The value is the only field that comes back, and it reaches `--picks` as a name
    `main` looks up in the live candidate set."""
    candidates = inventory(REGISTRY, ["alpha", "beta"], ["alpha", "beta", "gamma"])
    assert [line.split(picker_rows.FIELD_SEP)[0] for line in rows(candidates, "acme")] == [
        "alpha",
        "beta",
        "gamma",
    ]


def test_a_folder_with_no_harness_says_so_in_the_row():
    """The same flag the terminal listing carries, in the only column a quick-pick has
    room for: plugging an unharnessed folder registers a checkout no task can run."""
    candidates = inventory('{"folders": []}', ["delta"], [])
    (line,) = rows(candidates, "acme")
    assert line.split(picker_rows.FIELD_SEP)[2] == "folder only (no .devkit.toml)"


def test_a_scan_that_found_nothing_draws_a_row_saying_so():
    """An empty stdout is indistinguishable in the quick-pick from a command that failed
    or a wrong path, so "nothing to do" is stated as an unpickable-in-effect row whose
    value `main` recognises."""
    (line,) = rows([], "acme")
    assert line.split(picker_rows.FIELD_SEP)[0] == picker_rows.NOTHING


def test_the_rows_carry_no_timestamp_because_they_are_not_a_cache():
    """The file-backed checklist had to say how stale it was, in a group label. A list
    built when the picker opens has no such claim to make, and a leftover "as of" line
    would be the most misleading thing on it."""
    candidates = inventory(REGISTRY, ["alpha"], ["alpha"])
    assert not any("as of" in line for line in rows(candidates, "acme"))


@pytest.mark.parametrize(
    "answer, expected",
    [
        ("alpha,beta", ("alpha", "beta")),
        ("alpha, beta", ("alpha", "beta")),
        ("alpha", ("alpha",)),
        ("", ()),
        (",,", ()),
    ],
)
def test_the_one_string_answer_splits_back_into_names(answer, expected):
    """A VS Code input resolves to exactly one string, so `separator` joins the ticked
    values on the way out and this is the other half of that."""
    assert parse_ticks(answer) == expected


def test_escaping_the_quick_pick_is_recognised_rather_than_parsed():
    """Escape leaves the input unresolved and VS Code passes the literal through. Read
    as a name it would be a tick for a project called `${input:plugSelection}`, every
    other row unticked -- which is a plan to retire the entire registry."""
    assert picked_nothing("${input:plugSelection}")
    assert not picked_nothing("alpha,beta")
    assert not picked_nothing("")


def test_ticking_a_registered_row_retires_it():
    assert toggled_selection(("alpha",), {"alpha", "beta"}) == {"beta"}


def test_ticking_an_unregistered_row_adds_it():
    assert toggled_selection(("gamma",), {"alpha"}) == {"alpha", "gamma"}


def test_ticking_nothing_changes_nothing():
    """The property the checklist could not have, and needed a special-cased error to
    survive: an empty answer there asked to retire the whole registry."""
    assert toggled_selection((), {"alpha", "beta"}) == {"alpha", "beta"}


def test_a_row_nobody_ticked_is_left_exactly_as_it_is():
    """The regression the two staleness guards existed for, now a property of the shape.

    A pick says nothing whatsoever about a row it does not name, so a project registered
    (or retired) between the scan and the click cannot be reverted by a click that never
    mentioned it -- there is no `offered` set to subtract it from.
    """
    assert toggled_selection(("alpha",), {"alpha", "beta"}) == {"beta"}
    assert toggled_selection(("gamma",), {"alpha", "beta"}) == {"alpha", "beta", "gamma"}


def test_toggling_is_its_own_inverse():
    """What makes the answer safe to apply against a registry that moved under it: the
    same pick, applied twice, is the state it started from."""
    plugged = {"alpha", "beta"}
    picks = ("alpha", "gamma")
    assert toggled_selection(picks, toggled_selection(picks, plugged)) == plugged


# --- the plan ---------------------------------------------------------------


def test_an_unchanged_box_is_not_a_step():
    candidates = inventory(REGISTRY, ["alpha", "beta"], ["alpha", "beta"])
    assert plan(candidates, {"alpha", "beta"}) == []


def test_a_project_in_both_sources_is_registered_and_nothing_else():
    candidates = inventory(
        REGISTRY, ["alpha", "beta", "gamma"], ["gamma"], git_dirs=frozenset({"gamma"})
    )
    (step,) = plan(candidates, {"alpha", "beta", "gamma"})
    assert step == Step(plug_projects.PLUG, "gamma")


def test_a_repo_only_project_is_cloned_first():
    candidates = inventory(REGISTRY, ["alpha", "beta"], ["gamma"])
    (step,) = plan(candidates, {"alpha", "beta", "gamma"})
    assert step.clone and not step.create_repo and not step.init_git


def test_a_folder_only_project_gets_its_repo_created():
    candidates = inventory(REGISTRY, ["alpha", "beta", "gamma"], [], git_dirs=frozenset({"gamma"}))
    (step,) = plan(candidates, {"alpha", "beta", "gamma"})
    assert step.create_repo and not step.clone and not step.init_git


def test_a_folder_that_is_not_a_git_repo_is_initialised_first():
    """`gh repo create --source` needs a commit to push."""
    candidates = inventory(REGISTRY, ["alpha", "beta", "gamma"], [])
    (step,) = plan(candidates, {"alpha", "beta", "gamma"})
    assert step.init_git and step.create_repo


def test_a_registered_project_missing_from_disk_is_planned_as_a_clone():
    """The fresh-workstation case, and the one row where the tick and the registry
    already agree -- so both halves of `plan` skipped it and no verb offered the clone.
    Four registered projects were missing from disk, none was offered, and the answer was
    a hand-written `gh repo clone` loop."""
    candidates = inventory(REGISTRY, [], ["alpha", "beta"])
    steps = plan(candidates, {"alpha", "beta"})

    assert [(s.action, s.name, s.clone) for s in steps] == [
        (plug_projects.CLONE, "alpha", True),
        (plug_projects.CLONE, "beta", True),
    ]
    assert "clone from GitHub" in describe(steps[0])


def test_the_clone_step_leaves_the_registry_alone():
    """It is already registered. Re-registering would be a no-op edit to the file every
    window on the machine reads, and `apply_registry` is the one place that says so."""
    candidates = inventory(REGISTRY, [], ["alpha", "beta"])
    assert apply_registry(REGISTRY, plan(candidates, {"alpha", "beta"})) == REGISTRY


def test_a_registered_project_with_no_repo_to_clone_from_is_left_alone():
    """Nothing to clone from, so the step would only fail. A registry entry naming a
    repo that is gone is an unplug's problem, not a clone's."""
    assert plan(inventory(REGISTRY, [], []), {"alpha", "beta"}) == []
    assert not plug_projects.needs_clone(
        Candidate("a", plugged=True, on_disk=False, on_github=False)
    )


def test_needs_clone_is_only_true_for_a_registered_row_absent_from_disk():
    """The predicate the three surfaces share -- the plan, the listing and the quick-pick
    detail -- so it is asserted once here rather than three times through them."""
    assert plug_projects.needs_clone(Candidate("a", plugged=True, on_disk=False, on_github=True))
    assert not plug_projects.needs_clone(Candidate("a", plugged=True, on_disk=True, on_github=True))
    assert not plug_projects.needs_clone(
        Candidate("a", plugged=False, on_disk=False, on_github=True)
    )


def test_the_listing_says_which_registered_rows_are_not_on_disk():
    """The row has to carry it: nothing else distinguishes a registered project that is
    present from one that only exists in the registry, and both draw as ticked."""
    lines = render(inventory(REGISTRY, ["alpha"], ["alpha", "beta"]), {"alpha", "beta"})
    assert "registered, not on disk: will clone" in lines[1]
    assert "will clone" not in lines[0]


def test_unticking_plans_an_unplug_and_nothing_on_disk():
    candidates = inventory(REGISTRY, ["alpha", "beta"], ["alpha", "beta"])
    (step,) = plan(candidates, {"alpha"})
    assert step == Step(plug_projects.UNPLUG, "beta")
    assert "nothing on disk is touched" in describe(step)


def test_retirements_are_planned_before_additions():
    candidates = inventory(REGISTRY, ["alpha", "beta", "gamma"], ["alpha", "beta", "gamma"])
    steps = plan(candidates, {"alpha", "gamma"})
    assert [(s.action, s.name) for s in steps] == [
        (plug_projects.UNPLUG, "beta"),
        (plug_projects.PLUG, "gamma"),
    ]


def test_hazards_ride_on_the_unplug_step():
    candidates = inventory(REGISTRY, ["alpha", "beta"], [])
    (step,) = plan(candidates, {"alpha"}, {"beta": ("2 live box(es)",)})
    assert step.hazards == ("2 live box(es)",)
    assert "2 live box(es)" in describe(step)


def test_the_preview_names_every_outward_facing_act():
    """Creating a private GitHub repo is the only step here anyone outside can see, so
    it must be legible in the line printed before the confirmation."""
    candidates = inventory(REGISTRY, ["alpha", "beta", "gamma"], [])
    line = describe(plan(candidates, {"alpha", "beta", "gamma"})[0])
    assert "create the PRIVATE GitHub repo" in line and "git init" in line


# --- the gate ---------------------------------------------------------------


def test_the_registry_may_be_edited_from_a_clean_default_branch():
    assert edit_verdict(branch="main", default="main", in_box=False, live_unstamped=False) == ""


def test_a_box_may_not_publish():
    """Its `workspace.jsonc` is a proposal nobody has even opened a PR for yet."""
    verdict = edit_verdict(branch="main", default="main", in_box=True, live_unstamped=False)
    assert "ephemeral box" in verdict


def test_a_task_branch_may_not_publish():
    verdict = edit_verdict(branch="agent/x", default="main", in_box=False, live_unstamped=False)
    assert "not main" in verdict


def test_a_live_file_carrying_a_hand_edit_is_refused_before_anything_is_written():
    """`publish_workspace` would refuse it too, but only *after* the canonical copy has
    changed — leaving a registry edit stranded in a file nothing renders."""
    verdict = edit_verdict(branch="main", default="main", in_box=False, live_unstamped=True)
    assert "--adopt-workspace" in verdict


def test_the_box_check_matches_the_workspace_layout(tmp_path):
    """Keyed off `worktree.BOXES_DIR_NAME`, and asserted against a fixture rather than
    against `REPO_ROOT` — the suite itself runs from a box as a matter of course, so a
    test written against the live layout would assert the opposite half the time."""
    assert in_ephemeral_box(tmp_path / worktree.BOXES_DIR_NAME / "devkit--task-0101")
    assert not in_ephemeral_box(tmp_path / "devkit")
    assert in_ephemeral_box(REPO_ROOT) == (REPO_ROOT.parent.name == worktree.BOXES_DIR_NAME)


# Both of these build a *fixture* file that merely bears the registry's name; neither
# reads the real one, so neither wants `@needs_live_workspace`. The name comes off
# `DEFAULT_WORKSPACE` rather than being spelled out because
# `test_self_hosting.test_tests_reading_the_live_workspace_are_marked_to_skip_without_it`
# recognises a live-file read by that literal, and a literal here reads to it as the
# sixth CI-breaking test it exists to prevent.
REGISTRY_NAME = devkit_project.DEFAULT_WORKSPACE.name


def test_an_unstamped_but_identical_live_file_is_not_a_hand_edit(tmp_path):
    """The state right after an adopt. Refusing there would block a run over an edit
    that never happened."""
    canonical = devkit_project.canonical_text()
    live = tmp_path / REGISTRY_NAME
    live.write_text(canonical, encoding="utf-8", newline="\n")
    assert not live_carries_a_hand_edit(live)


def test_a_live_file_that_differs_and_is_unstamped_is_a_hand_edit(tmp_path):
    live = tmp_path / REGISTRY_NAME
    live.write_text('{"folders": [{"path": "nothing-like-it"}]}', encoding="utf-8", newline="\n")
    assert live_carries_a_hand_edit(live)


# --- the hazards ------------------------------------------------------------


def test_unplugging_a_project_with_no_boxes_and_nothing_local_costs_nothing(tmp_path):
    (tmp_path / "alpha").mkdir()
    assert unplug_hazards("alpha", tmp_path, lambda *a: done()) == ()


def test_live_boxes_are_a_hazard(tmp_path):
    """An unregistered project is outside `known_projects`, so `reconcile` stops reaping
    its boxes and their port slots and volume sets leak with nothing left to notice."""
    (tmp_path / "alpha").mkdir()
    for slug in ("alpha--one-0101", "alpha--two-0102"):
        (tmp_path / ".worktrees" / slug).mkdir(parents=True)
    (tmp_path / ".worktrees" / "beta--three-0103").mkdir()
    assert unplug_hazards("alpha", tmp_path, lambda *a: done()) == (
        "2 live box(es) reconcile would stop reaping",
    )


def test_uncommitted_and_unpushed_work_are_hazards(tmp_path):
    (tmp_path / "alpha" / ".git").mkdir(parents=True)

    def git(*args):
        if args[0] == "status":
            return done(" M one.py\n?? two.py\n")
        return done("abc123 a commit\n")

    assert unplug_hazards("alpha", tmp_path, git) == (
        "2 uncommitted path(s)",
        "1 unpushed commit(s)",
    )


def test_a_folder_that_is_not_a_checkout_is_not_asked_about_its_commits(tmp_path):
    """`@{u}` on a non-repo is an error, not an answer."""
    (tmp_path / "alpha").mkdir()

    def git(*args):
        raise AssertionError("git must not be run against a folder with no .git")

    assert unplug_hazards("alpha", tmp_path, git) == ()


# --- carrying it out --------------------------------------------------------


def test_the_registry_halves_are_applied_in_one_pass_each_way():
    text = """{
        "folders": [{"path": "alpha"}, {"path": "beta"}],
        "tasks": {"inputs": [{"id": "project", "options": ["alpha", "beta"], "default": "beta"}]}
    }"""
    steps = [Step(plug_projects.UNPLUG, "alpha"), Step(plug_projects.PLUG, "gamma")]
    assert devkit_project.known_projects(apply_registry(text, steps)) == ["beta", "gamma"]


def test_applying_no_steps_changes_nothing():
    assert apply_registry(REGISTRY, []) == REGISTRY


def test_a_clone_targets_the_workspace_root(tmp_path):
    calls = []
    clone_repo("owner", "gamma", tmp_path, lambda argv, **kw: calls.append(argv) or done())
    assert calls == [["gh", "repo", "clone", "owner/gamma", str(tmp_path / "gamma")]]


def test_a_failed_clone_raises_rather_than_registering_a_folder_that_is_not_there(tmp_path):
    with pytest.raises(PlugError, match="could not clone"):
        clone_repo(
            "owner", "gamma", tmp_path, lambda argv, **kw: done(code=1, stderr="no such repo")
        )


def test_creating_a_repo_initialises_commits_and_pushes(tmp_path):
    calls = []

    def runner(argv, cwd=None):
        calls.append(argv)
        # `rev-parse HEAD` fails until something has been committed.
        if argv[:2] == ["git", "rev-parse"]:
            return done(code=1)
        return done()

    create_repo("owner", "gamma", tmp_path, runner, init=True)
    assert calls[0] == ["git", "init", "-b", "main"]
    assert ["git", "commit", "-m", "Initial commit"] in calls
    assert calls[-1][:4] == ["gh", "repo", "create", "owner/gamma"]
    assert "--private" in calls[-1] and "--push" in calls[-1]


def test_an_existing_repo_with_commits_is_only_pushed(tmp_path):
    calls = []
    create_repo(
        "owner", "gamma", tmp_path, lambda argv, **kw: calls.append(argv) or done(), init=False
    )
    assert [c for c in calls if c[:2] == ["git", "commit"]] == []
    assert calls[-1][0] == "gh"


def test_a_failed_repo_creation_raises(tmp_path):
    def runner(argv, cwd=None):
        return done(code=0 if argv[0] == "git" else 1)

    with pytest.raises(PlugError, match="could not create"):
        create_repo("owner", "gamma", tmp_path, runner, init=False)


def test_the_push_waives_the_branch_policy():
    """Seeding a repo and pushing its default branch is the "scripted repo setup" case
    that hatch exists for; without it the policy's pre-push hook blocks the create,
    after the private GitHub repo already exists."""
    assert scripted_env()["DEVKIT_SKIP_BRANCH_POLICY"] == "1"


# --- the artifact -----------------------------------------------------------


def test_failures_are_persisted_under_logs(tmp_path, monkeypatch):
    """The task writes its own artifact rather than wearing `log-wrap.py`: this names
    the registry the run ended with instead of transcribing what scrolled past. The
    wrapper is also still wrong for the bare CLI, which does prompt — it pipes the
    child's stdout and reads it a line at a time, so a prompt that has not ended its
    line never reaches the terminal and the list hangs on input nobody can see."""
    monkeypatch.setattr(plug_projects, "ARTIFACT", tmp_path / "logs" / "plug-projects.log")
    write_artifact(["alpha: 2 live box(es)"])
    assert (tmp_path / "logs" / "plug-projects.log").read_text() == "alpha: 2 live box(es)\n"


def test_a_clean_run_empties_the_artifact(tmp_path, monkeypatch):
    """The way `log-wrap.py` empties its own log, so it never describes a failure that
    is already fixed."""
    artifact = tmp_path / "logs" / "plug-projects.log"
    monkeypatch.setattr(plug_projects, "ARTIFACT", artifact)
    write_artifact(["something went wrong"])
    write_artifact([])
    assert artifact.read_text() == ""


# --- the shell --------------------------------------------------------------


@pytest.fixture
def listed(monkeypatch, tmp_path):
    monkeypatch.setattr(plug_projects, "ARTIFACT", tmp_path / "logs" / "plug-projects.log")
    candidates = inventory(REGISTRY, ["alpha", "beta"], ["alpha", "beta", "gamma"])
    monkeypatch.setattr(plug_projects, "gather", lambda: (candidates, []))
    return candidates


def test_listing_is_read_only_and_works_from_anywhere(listed, capsys):
    """`--list` runs before the gate, so a box or a task branch can still answer "what
    is plugged in" — the question that needs no write at all."""
    assert main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "[x] alpha" in out and "[ ] gamma" in out


def test_the_json_listing_carries_every_source(listed, capsys):
    assert main(["--list", "--json"]) == 0
    rows = {r["name"]: r for r in json.loads(capsys.readouterr().out)}
    assert rows["gamma"] == {
        "name": "gamma",
        "plugged": False,
        "on_disk": False,
        "on_github": True,
        "is_git": False,
        "harnessed": False,
        "where": "repo only",
    }


def test_a_name_from_none_of_the_three_sources_is_refused(listed, capsys):
    """Otherwise `--plug typo` clones nothing, creates nothing, and registers a folder
    that is not there — which `resolve_project` then reports for every task."""
    assert main(["--plug", "nosuchthing"]) == 2
    assert "nosuchthing" in capsys.readouterr().err


def test_a_warning_from_a_missing_gh_does_not_stop_the_listing(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(plug_projects, "ARTIFACT", tmp_path / "logs" / "plug-projects.log")
    candidates = inventory(REGISTRY, ["alpha", "beta"], [])
    monkeypatch.setattr(plug_projects, "gather", lambda: (candidates, ["gh could not list repos"]))
    assert main(["--list"]) == 0
    assert "gh could not list repos" in capsys.readouterr().out


# --- the shell, driven by the quick-pick ------------------------------------


@pytest.fixture
def ticking(listed, monkeypatch):
    """`--picks` with the gate open and the registry edit stubbed.

    What these tests read is the selection `main` computed from the answer, which is
    the half that decides an unplug -- not the git tree it would otherwise have to
    build to get past `edit_verdict`.
    """
    monkeypatch.setattr(plug_projects, "edit_verdict", lambda **kwargs: "")
    monkeypatch.setattr(plug_projects, "live_carries_a_hand_edit", lambda path: False)
    monkeypatch.setattr(plug_projects.sweep, "git_for", lambda root: lambda *a, **k: done("main\n"))
    monkeypatch.setattr(plug_projects.task_branch, "detect_default_branch", lambda git: "main")
    monkeypatch.setattr(plug_projects, "unplug_hazards", lambda *a, **kwargs: ())
    applied: list = []
    monkeypatch.setattr(plug_projects, "_apply", lambda steps, owner, out: applied.extend(steps))
    return applied


def test_the_rows_are_printed_for_the_quick_pick_to_read(listed, capsys):
    assert main(["--rows"]) == 0
    printed = capsys.readouterr().out.splitlines()
    assert [line.split(picker_rows.FIELD_SEP)[0] for line in printed] == ["alpha", "beta", "gamma"]


def test_nothing_but_rows_reaches_stdout_on_the_rows_path(monkeypatch, capsys):
    """stdout *is* the quick-pick, so a `NOTE` line becomes an option somebody can tick.

    A degraded `gh` listing is the case that would produce one, and it draws the refusal
    as a row instead: `gather` falls back to the folder half alone, which makes every
    repo that exists read as one to CREATE.
    """
    candidates = inventory(REGISTRY, ["alpha", "beta"], [])
    monkeypatch.setattr(plug_projects, "gather", lambda: (candidates, ["gh could not list repos"]))

    assert main(["--rows"]) == 0
    printed = capsys.readouterr().out.splitlines()
    assert len(printed) == 1
    assert printed[0].split(picker_rows.FIELD_SEP)[0] == picker_rows.NOTHING
    assert "GitHub listing failed" in printed[0]


def test_the_picks_are_read_as_toggles_of_the_live_registry(ticking, capsys):
    """`alpha` is registered, so ticking it retires it -- and nothing else moves, because
    a pick says nothing about a row it does not name."""
    assert main(["--picks", "alpha", "--yes"]) == 0
    assert ticking == [Step(plug_projects.UNPLUG, "alpha")]
    assert "unplug  alpha" in capsys.readouterr().out


def test_ticking_a_row_that_was_not_registered_plugs_it(ticking):
    assert main(["--picks", "gamma", "--yes"]) == 0
    assert ticking == [Step(plug_projects.PLUG, "gamma", clone=True)]


def test_a_project_registered_since_the_scan_survives_a_click(ticking, capsys):
    """The regression the two staleness guards existed for, end to end and now free.

    The scan drew alpha, beta and gamma; the click names only gamma. Under the checklist
    an untouched row was an assertion about the registry, so a project registered after
    the file was written could be retired by a click that never mentioned it. A toggle
    cannot: `beta` is not in the answer, so nothing about it is being said.
    """
    assert main(["--picks", "gamma", "--yes"]) == 0
    assert ticking == [Step(plug_projects.PLUG, "gamma", clone=True)]
    assert "unplug" not in capsys.readouterr().out


def test_a_name_that_is_no_longer_a_candidate_is_dropped_with_a_note(ticking, capsys):
    """A row can only have come from the scan a moment ago, so this is a hand-typed
    `--picks`. A note rather than a failure: the *other* ticks are still true."""
    assert main(["--picks", "alpha ghost", "--yes"]) == 0
    assert ticking == [Step(plug_projects.UNPLUG, "alpha")]
    assert "no longer on disk, on GitHub or in the registry: ghost" in capsys.readouterr().out


def test_ticking_nothing_changes_nothing_rather_than_retiring_everything(ticking, capsys):
    """A quick-pick with every box cleared resolves to the empty string. Under the
    checklist that read as a plan to unplug the whole workspace and needed an error of
    its own; a tick is a toggle now, so no ticks is simply no change."""
    assert main(["--picks", "", "--yes"]) == 0
    assert ticking == []
    assert "nothing was picked" in capsys.readouterr().out


def test_the_nothing_row_is_recognised_rather_than_looked_up(ticking, capsys):
    """A scan with nothing to offer draws `picker_rows.NOTHING`, which is a real value
    the quick-pick can return. Read as a name it would be a project nobody has."""
    assert main(["--picks", picker_rows.NOTHING, "--yes"]) == 0
    assert ticking == []
    assert "nothing was picked" in capsys.readouterr().out


def test_escaping_the_quick_pick_costs_a_line_even_where_the_gate_would_refuse(
    listed, monkeypatch, capsys
):
    """Escape leaves the literal `${input:...}` in the argument, and that is resolved
    before `edit_verdict` on purpose: nothing was picked, so nothing is being published
    and none of the gate's three reasons is about to be true. Move the check below the
    gate and cancelling from a box reports a branch error for a run that did nothing."""
    monkeypatch.setattr(plug_projects, "edit_verdict", lambda **kwargs: "a box may not publish")
    assert main(["--picks", "${input:plugSelection}", "--yes"]) == 0
    assert "nothing was picked" in capsys.readouterr().out
