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

A fourth since the checkboxes moved into VS Code: `selection_from_ticks` reads a pick
made against a file some *earlier* pass wrote, so the difference between "unticked" and
"never offered" is the only thing standing between a stale menu and a silent unplug.
"""

import datetime as dt
import json
import subprocess

import pytest
from support import REPO_ROOT, devkit_project, load_script, worktree

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
menu_detail = plug_projects.menu_detail
menu_payload = plug_projects.menu_payload
parse_command = plug_projects.parse_command
parse_ticks = plug_projects.parse_ticks
picked_nothing = plug_projects.picked_nothing
read_menu = plug_projects.read_menu
refresh_menu = plug_projects.refresh_menu
selection_from_ticks = plug_projects.selection_from_ticks
write_menu = plug_projects.write_menu
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
        (Candidate("alpha", plugged=True, on_disk=True, on_github=True), "untick to retire it"),
        (Candidate("gamma", plugged=False, on_disk=False, on_github=True), "clones acme/gamma"),
        (Candidate("delta", plugged=False, on_disk=True, on_github=False), "CREATES the private"),
        (
            Candidate("eps", plugged=False, on_disk=True, on_github=True),
            "registers it, and nothing",
        ),
    ],
)
def test_each_row_says_what_its_own_tick_costs(candidate, expected):
    """The task runs `--ticked ... --yes`, so the quick-pick *is* the confirmation and
    this line is the last thing anyone reads before a private GitHub repo is created."""
    assert expected in menu_detail(candidate, "acme")


def test_the_group_label_carries_the_timestamp():
    """The extension can only read a *file*, so the list is stale by construction and
    the reader has to be told how stale. A group label draws as a separator row -- the
    one line in a quick-pick that cannot be ticked, which is why it holds this."""
    when = dt.datetime(2026, 8, 26, 17, 15, tzinfo=dt.UTC)
    groups = menu_payload(inventory(REGISTRY, ["alpha"], ["alpha"]), "acme", now=when)
    assert len(groups) == 1
    assert "as of " in groups[0]["label"] and "2026-08-26" in groups[0]["label"]
    assert "ticked = in the workspace registry" in groups[0]["label"]


def test_the_boxes_open_ticked_exactly_as_the_registry_stands():
    """What makes this a checklist rather than a menu: the pick is an *edit* of the live
    state, so an unchanged pick has to be a no-op. Revert `picked` and every click
    becomes "retire everything that was already registered"."""
    candidates = inventory(REGISTRY, ["alpha", "beta"], ["alpha", "beta", "gamma"])
    options = menu_payload(candidates, "acme")[0]["options"]
    assert {o["value"]: o["picked"] for o in options} == {
        "alpha": True,
        "beta": True,
        "gamma": False,
    }


def test_every_row_carries_every_field_the_quick_pick_draws():
    candidates = inventory(REGISTRY, ["alpha", "beta"], ["alpha", "beta", "gamma"])
    for option in menu_payload(candidates, "acme")[0]["options"]:
        assert set(option) == {"value", "label", "description", "detail", "picked"}
        assert all(isinstance(option[key], str) for key in ("value", "label", "description"))


def test_a_folder_with_no_harness_says_so_in_the_row():
    """The same flag the terminal listing carries, in the only column a quick-pick has
    room for: plugging an unharnessed folder registers a checkout no task can run."""
    candidates = inventory('{"folders": []}', ["delta"], [])
    (option,) = menu_payload(candidates, "acme")[0]["options"]
    assert option["description"] == "folder only  (no .devkit.toml)"


def test_the_menu_survives_a_round_trip_as_the_names_it_offered(tmp_path):
    """`read_menu` answers the **offered** set rather than the ticked one, which is what
    lets `--ticked` tell an untick from a row that was never drawn."""
    path = tmp_path / "plug-menu.json"
    candidates = inventory(REGISTRY, ["alpha", "beta"], ["alpha", "beta", "gamma"])
    assert write_menu(menu_payload(candidates, "acme"), path) == path
    assert read_menu(path) == ["alpha", "beta", "gamma"]


def test_a_missing_or_corrupt_menu_reads_as_no_menu(tmp_path):
    """None rather than [], because an empty offered-set would make every registered
    project look like a row the reader deliberately left unticked."""
    assert read_menu(tmp_path / "nothing.json") is None
    corrupt = tmp_path / "plug-menu.json"
    corrupt.write_text("[{", encoding="utf-8")
    assert read_menu(corrupt) is None
    corrupt.write_text('[{"label": "x"}]', encoding="utf-8")
    assert read_menu(corrupt) is None


def test_a_failed_repo_listing_leaves_the_previous_menu_alone(monkeypatch, tmp_path):
    """The one refusal worth spelling out. `gather` degrades to the folder half alone
    when `gh` is unreachable, and a project that is on GitHub then reads as `folder
    only` -- a row whose detail offers to *create* the repo it already has. A stale menu
    is a wrong list; that one would be a wrong act."""
    candidates = inventory(REGISTRY, ["alpha", "beta"], [])
    monkeypatch.setattr(plug_projects, "gather", lambda: (candidates, ["gh could not list repos"]))
    path = tmp_path / "plug-menu.json"
    path.write_text("[]", encoding="utf-8")

    assert refresh_menu(path) is None
    assert path.read_text(encoding="utf-8") == "[]"


def test_the_refresh_never_raises_whatever_the_scan_did(monkeypatch, tmp_path):
    """`worktree.py reconcile` runs this as a rider every fifteen minutes: a menu that
    could not be built must never redden a pass that reaped boxes correctly."""

    def explode():
        raise RuntimeError("the workspace file is a directory today")

    monkeypatch.setattr(plug_projects, "gather", explode)
    assert refresh_menu(tmp_path / "plug-menu.json") is None


def test_a_menu_that_cannot_be_written_is_reported_rather_than_raised(tmp_path):
    """Same containment one layer down -- `write_menu` is on the rider's path too."""
    blocked = tmp_path / "plug-menu.json"
    blocked.mkdir()
    assert write_menu([], blocked) is None


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


def test_unticking_an_offered_row_retires_it():
    assert selection_from_ticks(("alpha",), ["alpha", "beta"], {"alpha", "beta"}) == {"alpha"}


def test_ticking_a_row_that_was_not_registered_adds_it():
    assert selection_from_ticks(("alpha", "gamma"), ["alpha", "gamma"], {"alpha"}) == {
        "alpha",
        "gamma",
    }


def test_a_project_registered_since_the_menu_was_written_is_not_retired():
    """The regression the offered-set exists for. `beta` was registered after the last
    refresh, so it is absent from the file and therefore absent from the answer --
    reading the answer as the whole intended registry would unplug it on a click that
    never mentioned it, and unregistered is invisible to every sweep and to the guard."""
    assert selection_from_ticks(("alpha",), ["alpha", "gamma"], {"alpha", "beta"}) == {
        "alpha",
        "beta",
    }


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
def menu_file(monkeypatch, tmp_path):
    """Point the cached options file somewhere disposable. Both `read_menu` and
    `write_menu` default at call time, so every caller that passes no path follows."""
    path = tmp_path / "logs" / "plug-menu.json"
    monkeypatch.setattr(plug_projects, "MENU_CACHE", path)
    return path


@pytest.fixture
def ticking(listed, menu_file, monkeypatch):
    """`--ticked` with the gate open and the registry edit stubbed.

    What these tests read is the selection `main` computed from the answer, which is
    the half that decides an unplug -- not the git tree it would otherwise have to
    build to get past `edit_verdict`.
    """
    write_menu(menu_payload(listed, plug_projects.DEFAULT_OWNER), menu_file)
    monkeypatch.setattr(plug_projects, "edit_verdict", lambda **kwargs: "")
    monkeypatch.setattr(plug_projects, "live_carries_a_hand_edit", lambda path: False)
    monkeypatch.setattr(plug_projects.sweep, "git_for", lambda root: lambda *a, **k: done("main\n"))
    monkeypatch.setattr(plug_projects.task_branch, "detect_default_branch", lambda git: "main")
    monkeypatch.setattr(plug_projects, "unplug_hazards", lambda *a, **kwargs: ())
    applied: list = []
    monkeypatch.setattr(plug_projects, "_apply", lambda steps, owner, out: applied.extend(steps))
    return applied


def test_refreshing_the_menu_writes_a_row_for_every_candidate(listed, menu_file, capsys):
    assert main(["--refresh-menu"]) == 0
    assert read_menu(menu_file) == ["alpha", "beta", "gamma"]
    assert "3 row(s)" in capsys.readouterr().out


def test_a_refresh_with_no_repo_listing_refuses_rather_than_writing(monkeypatch, menu_file, capsys):
    """The CLI half of `refresh_menu`'s refusal, and it exits 2 rather than pretending:
    the rider swallows the same case silently because it must, but a person who typed
    the command is owed the reason the menu they are about to pick from is unchanged."""
    monkeypatch.setattr(plug_projects, "ARTIFACT", menu_file.parent / "plug-projects.log")
    candidates = inventory(REGISTRY, ["alpha", "beta"], [])
    monkeypatch.setattr(plug_projects, "gather", lambda: (candidates, ["gh could not list repos"]))
    menu_file.parent.mkdir(parents=True, exist_ok=True)
    menu_file.write_text("[]", encoding="utf-8")

    assert main(["--refresh-menu"]) == 2
    assert "offer to create repos that exist" in capsys.readouterr().out
    assert menu_file.read_text(encoding="utf-8") == "[]"


def test_the_ticks_are_read_as_an_edit_of_the_registry(ticking, capsys):
    """`beta` was offered and left unticked, so it is retired -- and nothing else is."""
    assert main(["--ticked", "alpha", "--yes"]) == 0
    assert ticking == [Step(plug_projects.UNPLUG, "beta")]
    assert "unplug  beta" in capsys.readouterr().out


def test_ticking_a_row_that_was_not_registered_plugs_it(ticking):
    assert main(["--ticked", "alpha,beta,gamma", "--yes"]) == 0
    assert ticking == [Step(plug_projects.PLUG, "gamma", clone=True)]


def test_a_project_registered_since_the_menu_was_written_survives_a_click(
    ticking, menu_file, listed, capsys
):
    """The regression, end to end. The file offers alpha and gamma; `beta` was
    registered afterwards, so no tick could possibly mention it -- and reading the
    answer as the whole intended registry would retire it without saying so."""
    stale = [c for c in listed if c.name != "beta"]
    write_menu(menu_payload(stale, plug_projects.DEFAULT_OWNER), menu_file)

    assert main(["--ticked", "alpha", "--yes"]) == 0
    assert ticking == []
    assert "nothing to change" in capsys.readouterr().out


def test_a_row_that_has_since_vanished_is_dropped_with_a_note(ticking, menu_file, listed, capsys):
    """The other direction of the same staleness, and it is only a note: the world moved
    on after the file was written, but the *other* ticks are still true."""
    ghost = Candidate("ghost", plugged=False, on_disk=True, on_github=False)
    write_menu(menu_payload([*listed, ghost], plug_projects.DEFAULT_OWNER), menu_file)

    assert main(["--ticked", "alpha,beta,ghost", "--yes"]) == 0
    assert ticking == []
    assert "no longer on disk, on GitHub or in the registry: ghost" in capsys.readouterr().out


def test_ticking_nothing_is_refused_rather_than_retiring_everything(ticking, capsys):
    """A quick-pick with every box cleared resolves to the empty string, which reads as
    a plan to unplug the whole workspace. Nobody means that, and `--unplug NAME` is
    there for anyone who does."""
    assert main(["--ticked", "", "--yes"]) == 1
    assert ticking == []
    assert "retire the whole registry" in capsys.readouterr().err


def test_a_checklist_that_was_never_built_names_the_command_that_builds_it(
    ticking, menu_file, capsys
):
    """`--ticked` cannot be interpreted without the offered set, so a missing file is an
    error rather than a guess -- guessing here means unplugging."""
    menu_file.unlink()
    assert main(["--ticked", "alpha", "--yes"]) == 2
    assert "--refresh-menu" in capsys.readouterr().err


def test_escaping_the_quick_pick_costs_a_line_even_where_the_gate_would_refuse(
    listed, menu_file, monkeypatch, capsys
):
    """Escape leaves the literal `${input:...}` in the argument, and that is resolved
    before `edit_verdict` on purpose: nothing was picked, so nothing is being published
    and none of the gate's three reasons is about to be true. Move the check below the
    gate and cancelling from a box reports a branch error for a run that did nothing."""
    monkeypatch.setattr(plug_projects, "edit_verdict", lambda **kwargs: "a box may not publish")
    assert main(["--ticked", "${input:plugSelection}", "--yes"]) == 0
    assert "nothing was picked" in capsys.readouterr().out


def test_the_menu_is_rebuilt_after_the_registry_moves(ticking, menu_file, listed):
    """`reconcile` would fix it within the quarter hour, and a second click inside that
    window is exactly when someone is most likely to look at rows still pre-ticked from
    the state this run replaced. The menu starts here as a one-row file, so a run that
    skipped the rebuild would leave it that way."""
    one_row = [c for c in listed if c.name == "alpha"]
    write_menu(menu_payload(one_row, plug_projects.DEFAULT_OWNER), menu_file)

    assert main(["--ticked", "alpha,beta,gamma", "--yes"]) == 0
    assert read_menu(menu_file) == ["alpha", "beta", "gamma"]
