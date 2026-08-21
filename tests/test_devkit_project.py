"""Tests for the shared-task dispatcher.

The behaviour worth pinning is the failure text: a task that runs in the wrong
directory, or against a project that does not implement the action, must say so by
name. A traceback from a missing file in an unexpected cwd is the outcome this
script exists to prevent.
"""

import json
import re

import pytest
from support import (
    LIVE_WORKSPACE,
    REPO_ROOT,
    devkit_jsonc,
    devkit_project,
    needs_live_workspace,
)

devkit_jsonc_loads = devkit_jsonc.loads

ACTIONS = devkit_project.ACTIONS
Action = devkit_project.Action
ProjectError = devkit_project.ProjectError
conformance = devkit_project.conformance
known_projects = devkit_project.known_projects
insert_picker_option = devkit_project.insert_picker_option
plan_command = devkit_project.plan_command
project_selection = devkit_project.project_selection
resolve_project = devkit_project.resolve_project

WORKSPACE = json.dumps({"folders": [{"path": "alpha"}, {"path": "beta"}, {"path": "VanillaLand"}]})


@pytest.fixture
def checkouts(tmp_path):
    """Two conforming-ish checkouts: alpha has both scripts, beta has neither."""
    alpha = tmp_path / "alpha"
    (alpha / "scripts").mkdir(parents=True)
    (alpha / "scripts" / "lint-all.py").write_text("")
    (alpha / "scripts" / "run-tests.py").write_text("")
    (tmp_path / "beta").mkdir()
    return tmp_path


# --- the registry -----------------------------------------------------------


def test_projects_come_from_the_workspace_registry():
    assert known_projects(WORKSPACE) == ["alpha", "beta"]


def test_the_reference_checkout_is_not_a_project():
    # VanillaLand ships no harness; nothing in ACTIONS applies to it.
    assert "VanillaLand" not in known_projects(WORKSPACE)


# --- resolution -------------------------------------------------------------


def test_a_registered_project_resolves_to_its_directory(checkouts):
    assert resolve_project("alpha", ["alpha", "beta"], checkouts) == checkouts / "alpha"


def test_an_unknown_project_names_the_real_ones(checkouts):
    with pytest.raises(ProjectError, match=r"unknown project 'gamma'.*alpha, beta"):
        resolve_project("gamma", ["alpha", "beta"], checkouts)


def test_an_empty_project_is_rejected_rather_than_defaulting(checkouts):
    # A picker that supplies "" must not silently run somewhere plausible.
    with pytest.raises(ProjectError, match="no project given"):
        resolve_project("", ["alpha", "beta"], checkouts)


def test_a_multi_pick_project_value_is_split_in_selection_order():
    assert project_selection("beta, alpha") == ["beta", "alpha"]


def test_duplicate_and_empty_multi_pick_values_are_ignored():
    assert project_selection("alpha,,alpha,beta,") == ["alpha", "beta"]


def test_main_runs_every_selected_project_in_order(tmp_path, monkeypatch):
    workspace = tmp_path / "projects.code-workspace"
    workspace.write_text(json.dumps({"folders": [{"path": "alpha"}, {"path": "beta"}]}))
    for name in ("alpha", "beta"):
        scripts = tmp_path / name / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "run-tests.py").write_text("")

    calls = []

    def fake_run(command, *, cwd, check):
        calls.append((command, cwd, check))
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(devkit_project.subprocess, "run", fake_run)
    result = devkit_project.main(["--workspace", str(workspace), "--project", "beta,alpha", "test"])

    assert result == 0
    assert [cwd.name for _, cwd, _ in calls] == ["beta", "alpha"]


def test_registered_but_missing_directory_is_distinguished(checkouts):
    with pytest.raises(ProjectError, match="registered in the workspace but"):
        resolve_project("ghost", ["alpha", "beta", "ghost"], checkouts)


# --- command planning -------------------------------------------------------


def inner(command: list[str]) -> list[str]:
    """The dispatched script's own argv, with the wrapper prologues peeled off.

    Every plan is now `[notify-wrap] -> log-wrap -> the script`, and the assertions
    below are about the script: which one, in which order, with which arguments.
    Peeling keeps them saying that instead of re-baselining a longer literal every
    time a wrapper is added. `test_the_plan_is_wrapped_for_logging` is what holds the
    wrapping itself in place.
    """
    while "--" in command:
        command = command[command.index("--") + 1 :]
    return command


def test_command_runs_the_projects_own_script(checkouts):
    assert inner(plan_command(ACTIONS["lint"], checkouts / "alpha", [])) == [
        "python",
        "scripts/lint-all.py",
    ]


def test_fixed_action_args_come_before_caller_args(checkouts):
    command = plan_command(ACTIONS["lint-changed"], checkouts / "alpha", ["--verbose"])
    assert inner(command) == ["python", "scripts/lint-all.py", "--changed", "--verbose"]


def test_empty_picker_tokens_are_dropped(checkouts):
    """VS Code pickers can yield "", which argparse would read as a stray positional.

    devkit's tasks.json carries redundant-looking flags precisely to avoid this; the
    dispatcher drops empties too so a task cannot fail on an invisible argument.
    """
    assert inner(plan_command(ACTIONS["test"], checkouts / "alpha", ["", "-k", ""])) == [
        "python",
        "scripts/run-tests.py",
        "-k",
    ]


def test_the_plan_is_wrapped_for_logging(checkouts, tmp_path):
    """Twenty of the workspace's tasks are a dispatch, so this is what gives the task
    list a failure artifact at all -- and the only place that can, because the task
    names a picker and nothing knows the checkout until `resolve_project` has run."""
    devkit_root = tmp_path / "dk"
    (devkit_root / "scripts").mkdir(parents=True)
    command = plan_command(ACTIONS["lint"], checkouts / "alpha", [], devkit_root)
    assert command[:4] == [
        "python",
        str(devkit_root / "scripts" / "log-wrap.py"),
        "Lint: Everything",
        "--",
    ]


def test_the_wrapper_is_devkits_copy_not_the_targets(checkouts, tmp_path):
    """A checkout that has not pulled the release adding `scripts/log-wrap.py` still
    gets an artifact, and the wrapper that runs is the one this dispatcher was tested
    against rather than whatever vintage the target vendored."""
    devkit_root = tmp_path / "dk"
    (devkit_root / "scripts").mkdir(parents=True)
    (checkouts / "alpha" / "scripts" / "log-wrap.py").write_text("stale copy")
    command = plan_command(ACTIONS["lint"], checkouts / "alpha", [], devkit_root)
    assert str(devkit_root / "scripts" / "log-wrap.py") in command
    assert "scripts/log-wrap.py" not in command


def test_notify_wrap_is_used_when_the_project_ships_it(checkouts):
    (checkouts / "alpha" / "scripts" / "notify-wrap.py").write_text("")
    command = plan_command(ACTIONS["lint"], checkouts / "alpha", [])
    assert command[:3] == ["python", "scripts/notify-wrap.py", "Lint: Everything"]
    assert command[3] == "--"
    assert inner(command) == ["python", "scripts/lint-all.py"]


def test_the_toast_wraps_the_log_and_not_the_other_way_round(checkouts):
    """Order is the contract: notify-wrap needs only an exit code, so it goes outside;
    log-wrap needs the output, so it goes between the toast and the script. Inverted,
    the artifact would capture the wrapper's own chatter instead of the run's."""
    (checkouts / "alpha" / "scripts" / "notify-wrap.py").write_text("")
    command = plan_command(ACTIONS["lint"], checkouts / "alpha", [])
    notify = command.index("scripts/notify-wrap.py")
    logged = next(i for i, part in enumerate(command) if part.endswith("log-wrap.py"))
    assert notify < logged


def test_a_project_missing_the_script_is_named(checkouts):
    with pytest.raises(ProjectError, match="beta does not implement this action"):
        plan_command(ACTIONS["lint"], checkouts / "beta", [])


# --- devkit-owned actions ---------------------------------------------------


def test_devkit_owned_action_uses_an_absolute_path(checkouts, tmp_path):
    """It runs with cwd set to the checkout, so a relative path would miss."""
    devkit_root = tmp_path / "dk"
    (devkit_root / "scripts").mkdir(parents=True)
    (devkit_root / "scripts" / "git-sync-keep.py").write_text("")
    command = plan_command(ACTIONS["sync-branch"], checkouts / "beta", [], devkit_root)
    assert inner(command) == ["python", str(devkit_root / "scripts" / "git-sync-keep.py")]


def test_devkit_owned_action_works_in_a_non_conforming_checkout(checkouts, tmp_path):
    # beta ships no scripts/ at all — the ibkr_trader case. A devkit-owned action
    # must still run there; that is the whole point of the DEVKIT owner.
    devkit_root = tmp_path / "dk"
    (devkit_root / "scripts").mkdir(parents=True)
    (devkit_root / "scripts" / "docker-maint.py").write_text("")
    command = plan_command(ACTIONS["docker-prune"], checkouts / "beta", [], devkit_root)
    assert command[-1] == "prune"


def test_a_broken_devkit_checkout_is_distinguished_from_a_project_gap(checkouts, tmp_path):
    with pytest.raises(ProjectError, match="devkit is missing"):
        plan_command(ACTIONS["sync-branch"], checkouts / "alpha", [], tmp_path / "empty")


def test_docker_up_forces_a_rebuild(checkouts, tmp_path):
    """Keep the `--build` the hoisted carameli task carried.

    Dropping it would make "Docker: Start Stack" quietly start a stale image after a
    requirements or Dockerfile change — a stack that comes up healthy running last
    week's code, which nothing downstream reports.
    """
    devkit_root = tmp_path / "devkit"
    (devkit_root / "scripts").mkdir(parents=True)
    (devkit_root / "scripts" / "docker-maint.py").write_text("")
    command = plan_command(ACTIONS["docker-up"], checkouts / "beta", [], devkit_root)
    assert command[-2:] == ["up", "--build"]


def test_stack_actions_are_devkit_owned_with_a_project_override(checkouts, tmp_path):
    """PROJECT-owned would make the shared contract unsatisfiable where it should be.

    devkit and a `bare` preset have no compose stack at all, so demanding a
    `docker-up.py` from every checkout would report them as non-conforming for
    correctly lacking one. DEVKIT-owned + `docker-maint.py`'s `DELEGATES` gives both
    halves: a generic `compose up -d` that works in a freshly generated project, and
    carameli's health-polling script when the repo ships one.
    """
    devkit_root = tmp_path / "devkit"
    (devkit_root / "scripts").mkdir(parents=True)
    (devkit_root / "scripts" / "docker-maint.py").write_text("")
    for key in ("docker-up", "docker-down"):
        assert ACTIONS[key].owner == devkit_project.DEVKIT
        # Runs in a checkout that ships no scripts/ of its own.
        plan_command(ACTIONS[key], checkouts / "beta", [], devkit_root)


def test_hook_tests_is_devkit_owned_so_every_checkout_can_run_it(checkouts, tmp_path):
    """`pytest scripts/hooks/tests/ -q` is byte-identical in every consumer.

    The vendored tier is at the same path everywhere (it is in the MANIFEST) and
    pytest's `testpaths` excludes it everywhere, so a PROJECT-owned version would be
    four identical scripts. DEVKIT-owned means it runs in a checkout that ships no
    `scripts/` of its own at all.
    """
    devkit_root = tmp_path / "devkit"
    (devkit_root / "scripts").mkdir(parents=True)
    (devkit_root / "scripts" / "hook-tests.py").write_text("")
    command = plan_command(ACTIONS["test-hooks"], checkouts / "beta", [], devkit_root)
    assert inner(command)[1] == str(devkit_root / "scripts" / "hook-tests.py")


# --- project-scoped actions -------------------------------------------------
#
# The mechanism that let the last two `.vscode/tasks.json` files be deleted. A task
# defined inside a repo is rendered once per WORKTREE folder, so carameli's Playwright
# run appeared twice in the quick-pick with nothing to tell the copies apart. Scoping
# moves it up without claiming every checkout can run it.

check_scope = devkit_project.check_scope
expected_actions = devkit_project.expected_actions
in_scope = devkit_project.in_scope
CARAMELI = devkit_project.CARAMELI
IBKR = devkit_project.IBKR


def test_an_unscoped_action_applies_everywhere():
    assert in_scope(ACTIONS["lint"], "anything-at-all")


def test_a_scoped_action_applies_to_the_checkouts_that_can_run_it():
    """One checkout per repo now. This asserted "both halves of its worktree pair" back
    when every repo was checked out twice; the `-b` tier is gone (ephemeral boxes give
    unbounded concurrency instead of two), so the scope and the picker are both single
    and `test_a_scoped_task_offers_exactly_the_checkouts_its_action_allows` is what keeps
    them agreeing."""
    assert in_scope(ACTIONS["e2e"], "carameli")
    assert in_scope(ACTIONS["backtest"], "ibkr_trader")
    assert not in_scope(ACTIONS["e2e"], "carameli-b")


def test_an_out_of_scope_checkout_is_refused_by_name():
    """Not left to the missing-script error, which reads like "devkit has not implemented
    backtesting yet" and invites someone to go and implement it."""
    with pytest.raises(ProjectError, match=r"devkit is out of scope.*ibkr_trader"):
        check_scope(ACTIONS["backtest"], "devkit")


def test_scoping_crosses_neither_direction_between_the_two_repos():
    assert not in_scope(ACTIONS["e2e"], "ibkr_trader")
    assert not in_scope(ACTIONS["ingest"], "carameli")


def test_an_in_scope_checkout_passes_the_check():
    check_scope(ACTIONS["e2e"], "carameli")  # must not raise


def test_the_scoped_actions_cover_every_hoisted_project_task():
    """The eight that came out of the two deleted files, by action key.

    Listed rather than counted: a missing entry here means a task the user used to be
    able to click is now unreachable from anywhere, which nothing else in the suite
    notices — `test_every_action_is_reachable_from_a_task` only checks the actions that
    still exist.

    Equality rather than a subset, so a *new* scoped action has to be added here
    deliberately and say where it came from. The list is no longer only the hoisted
    ones, which is why each non-hoisted entry carries its origin in a comment.
    """
    scoped = {key for key, action in ACTIONS.items() if action.projects}
    assert scoped == {
        "test-target",
        "e2e",
        "ngrok",
        "vnc",
        "ingest",
        "snapshot-monthly",
        "backtest",
        "backtest-oos",
        # From the GENERATOR template rather than a live repo — the last task anywhere
        # to leave a `.vscode/tasks.json`.
        "db-revision",
        # Never hoisted — born scoped. The integration suite spans carameli and the
        # VanillaLand checkout, and VanillaLand is in NOT_PROJECTS, so carameli fronts
        # for the pair and no other checkout can run it.
        "local-e2e",
        # Also born scoped, and the only entry whose scope is devkit itself: the
        # live-CLI smokes are in `tests/`, the one tree `sync-devkit.py` never vendors,
        # so no consumer has the file. Every other action here is scoped by capability;
        # this one is scoped by where the code physically is.
        "test-hooks-live",
        # Born scoped, and the only entry scoped for a reason that is neither capability
        # nor code location: `reclaim` has no project dimension at all. It sweeps the
        # machine's `%TEMP%`, stops every container on the daemon and reconciles one box
        # registry, so running it once per selected checkout would repeat a machine-wide
        # job N times and report nothing on runs 2..N. Scoping it to devkit is what lets
        # the task pin `--project devkit` and offer no picker.
        "reclaim",
        # Born scoped, for `reclaim`'s reason rather than its own: the menu
        # `preview-task.py` prints is assembled from the box registry and the port
        # registry, and there is exactly one of each on this machine. So the question
        # "which checkout" has no answer to give -- every checkout is already a column
        # in the menu -- and the task pins `--project devkit` and asks the one question
        # worth asking instead.
        "preview",
    }


def test_db_revision_spans_both_repos_but_excludes_devkit():
    """The one scoped action covering two repos: both have an Alembic tree.

    devkit is excluded because it has no database at all (`[db] enabled = false`), and an
    unscoped action would report it — plus every future `bare` preset — as permanently
    non-conforming, which is how `--check` becomes a report nobody reads.
    """
    assert set(ACTIONS["db-revision"].projects) == set(devkit_project.DB_PROJECTS)
    assert set(devkit_project.DB_PROJECTS) == {*CARAMELI, *IBKR}
    assert "devkit" not in devkit_project.DB_PROJECTS
    assert "db-revision" not in expected_actions("devkit")


def test_db_revision_is_project_owned_because_the_two_bodies_differ():
    """Not DEVKIT-owned with an override, which is the shape the Docker actions use.

    There is no sensible generic `alembic revision` to fall back to: carameli must run it
    inside the app container to bypass PgBouncer for DDL, ibkr_trader runs it on the host
    because its `app` profile is a scheduler rather than a dev shell. Neither is a
    degenerate case of the other, so the shared thing is the CLI and nothing else.
    """
    assert ACTIONS["db-revision"].owner == devkit_project.PROJECT
    assert ACTIONS["db-revision"].script == "scripts/db-revision.py"


def test_the_two_backtest_actions_share_a_script_and_differ_by_subcommand():
    """The OOS run fixes its own warm-up and simulation starts, so it cannot be `backtest`
    with different picker answers — it is a separate subcommand of one script."""
    assert ACTIONS["backtest"].script == ACTIONS["backtest-oos"].script
    assert ACTIONS["backtest"].args == ("run",)
    assert ACTIONS["backtest-oos"].args == ("oos",)


# --- conformance ------------------------------------------------------------


def test_a_scoped_action_is_not_expected_of_other_projects():
    """Without this, hoisting carameli's Playwright task would report ibkr_trader and
    devkit as missing `scripts/run-e2e.py` — a gap neither should ever close, and the
    kind of noise that teaches everyone to stop reading `--check`."""
    assert "e2e" in expected_actions("carameli")
    assert "e2e" not in expected_actions("ibkr_trader")
    assert "e2e" not in expected_actions("devkit")


def test_unscoped_actions_are_expected_of_everyone():
    assert {"test", "lint", "lint-changed"} <= expected_actions("devkit")


def test_conformance_reports_per_project_support(checkouts):
    report = conformance(["alpha", "beta"], checkouts)
    assert set(report["alpha"]) == {"lint", "lint-changed", "test"}
    assert report["beta"] == []


def test_conformance_ignores_devkit_owned_actions(checkouts):
    """Otherwise every checkout looks conformant and the real gap is hidden."""
    devkit_owned = {k for k, a in ACTIONS.items() if a.owner == devkit_project.DEVKIT}
    assert devkit_owned, "expected at least one devkit-owned action"
    reported = set(conformance(["alpha", "beta"], checkouts)["alpha"])
    assert not (reported & devkit_owned)


# --- registration -----------------------------------------------------------

RegistryEditError = devkit_project.RegistryEditError
register = devkit_project.register

COMMENTED = """{
\t// The one workspace. sweep.py reads this as the project registry.
\t"folders": [
\t\t{
\t\t\t"path": "carameli"
\t\t},
\t\t{
\t\t\t"name": "VanillaLand (reference)",
\t\t\t"path": "VanillaLand"
\t\t}
\t],
\t"tasks": {
\t\t"version": "2.0.0",
\t\t"tasks": [],
\t\t"inputs": [
\t\t\t{
\t\t\t\t// MAINTAINED BY new-project.py
\t\t\t\t"id": "project",
\t\t\t\t"type": "pickString",
\t\t\t\t"description": "Which checkout to run this in",
\t\t\t\t"options": [
\t\t\t\t\t"carameli"
\t\t\t\t],
\t\t\t\t"default": "carameli"
\t\t\t}
\t\t]
\t}
}
"""


def test_registration_adds_the_project_to_the_registry():
    updated = register(COMMENTED, ["newproj"])
    assert "newproj" in devkit_project.known_projects(updated)


def test_registration_adds_the_picker_option():
    updated = register(COMMENTED, ["newproj"])
    options = devkit_jsonc_loads(updated)["tasks"]["inputs"][0]["options"]
    assert options == ["carameli", "newproj"]


def test_picker_registration_updates_the_multi_test_picker_too():
    text = """{
        "tasks": {
            "inputs": [
                {"id": "project", "options": ["alpha"]},
                {"id": "daemonProject", "options": ["alpha"]},
                {"id": "worktreeProject", "options": ["alpha"]},
                {"id": "sweepScope", "options": ["alpha"]},
                {"id": "upgradeScope", "options": ["alpha"]},
                {"id": "mergeCheckout", "options": ["alpha"]}
            ]
        }
    }"""
    updated = devkit_jsonc_loads(insert_picker_option(text, "beta"))
    for picker in updated["tasks"]["inputs"]:
        assert picker["options"] == ["alpha", "beta"]


def test_registration_preserves_comments():
    """A json.dumps round-trip would delete these; the folder list would lose the only
    place it explains what VanillaLand is and why sweep depends on it."""
    updated = register(COMMENTED, ["newproj"])
    assert "sweep.py reads this as the project registry" in updated
    assert "MAINTAINED BY new-project.py" in updated


def test_reference_checkouts_stay_last():
    updated = register(COMMENTED, ["newproj"])
    paths = [f["path"] for f in devkit_jsonc_loads(updated)["folders"]]
    assert paths == ["carameli", "newproj", "VanillaLand"]


def test_registering_a_project_and_its_worktree():
    updated = register(COMMENTED, ["newproj", "newproj-b"])
    assert devkit_project.known_projects(updated) == ["carameli", "newproj", "newproj-b"]


def test_registration_is_idempotent():
    """new-project.py can be re-run over an existing name; that must not double-add."""
    once = register(COMMENTED, ["newproj"])
    twice = register(once, ["newproj"])
    assert once == twice


def test_the_result_is_still_valid_jsonc():
    updated = register(COMMENTED, ["newproj", "newproj-b"])
    assert devkit_jsonc_loads(updated)["tasks"]["version"] == "2.0.0"


def test_a_workspace_without_a_folders_array_is_refused():
    with pytest.raises(RegistryEditError, match=r"no .folders. array"):
        register('{"tasks": {}}', ["x"])


@needs_live_workspace
def test_registering_against_the_real_workspace_file():
    """The shape assertions above are on a fixture; this proves them on the live file."""
    text = LIVE_WORKSPACE.read_text(encoding="utf-8")
    updated = register(text, ["probe", "probe-b"])
    assert "probe" in devkit_project.known_projects(updated)
    assert "probe-b" in devkit_project.known_projects(updated)
    picker = next(i for i in devkit_jsonc_loads(updated)["tasks"]["inputs"] if i["id"] == "project")
    options = _input_options(picker)
    assert options[-2:] == ["probe", "probe-b"]
    inputs = {i["id"]: i for i in devkit_jsonc_loads(updated)["tasks"]["inputs"]}
    for picker_id in (
        "daemonProject",
        "worktreeProject",
        "sweepScope",
        "upgradeScope",
        "mergeCheckout",
    ):
        assert _input_options(inputs[picker_id])[-2:] == ["probe", "probe-b"]
    # VanillaLand is a reference checkout and must not drift into the middle.
    assert [f["path"] for f in devkit_jsonc_loads(updated)["folders"]][-1] == "VanillaLand"


# --- the canonical task block ------------------------------------------------

tasks_drift = devkit_project.tasks_drift
workspace_tasks = devkit_project.workspace_tasks


@pytest.fixture
def canonical():
    return devkit_jsonc_loads(devkit_project.CANONICAL_TASKS.read_text(encoding="utf-8"))


def test_the_canonical_block_exists_and_parses(canonical):
    assert canonical["version"] == "2.0.0"
    assert canonical["tasks"], "the canonical task block defines no tasks"


def test_no_drift_against_itself(canonical):
    assert tasks_drift(canonical, canonical) == []


def test_drift_reports_a_missing_task(canonical):
    trimmed = {**canonical, "tasks": canonical["tasks"][1:]}
    problems = tasks_drift(trimmed, canonical)
    assert any(p.startswith("missing from the workspace:") for p in problems)


def test_drift_reports_a_changed_definition(canonical):
    changed = {
        **canonical,
        "tasks": [{**canonical["tasks"][0], "command": "nope"}, *canonical["tasks"][1:]],
    }
    assert any(p.startswith("definition differs:") for p in tasks_drift(changed, canonical))


def test_drift_reports_an_extra_input(canonical):
    extra = {**canonical, "inputs": [*canonical["inputs"], {"id": "stray"}]}
    assert "input not in devkit: stray" in tasks_drift(extra, canonical)


def test_drift_reports_a_changed_input_definition(canonical):
    """An input whose OPTIONS changed is drift, even though its id did not.

    This gate compared the id set alone, so a picker could gain or lose a checkout in the
    live workspace and devkit's canonical copy would keep reporting a match. That is not
    hypothetical: it is how the `project` picker came to list data-lake on the workstation
    and not in this repo, which in turn hid that the scope pickers had never been extended
    at all.
    """
    first, *rest = canonical["inputs"]
    changed = {**canonical, "inputs": [{**first, "options": ["nope"]}, *rest]}
    assert f"input definition differs: {first['id']}" in tasks_drift(changed, canonical)


def test_the_preview_row_dropdown_asks_for_the_project_exactly_once(canonical):
    """`${pickStringRemember:previewProject}` may appear in ONE `jsonOption` field only.

    The extension substitutes each template string of `jsonOption` in turn, and every
    occurrence of a `${pickStringRemember:...}` variable opens its own quick-pick.
    Spelled in all four fields, the project dropdown appeared four times per run before
    the branch list showed. `value` must resolve first — insertion order is evaluation
    order — storing the answer under the nested pick's `key`, and the other fields read
    it back with `${remember:previewProject}`, which never prompts.
    """
    row = next(i for i in canonical["inputs"] if i["id"] == "previewRow")
    template = row["args"]["jsonOption"]
    assert next(iter(template)) == "value", "the prompting field must be evaluated first"
    prompting = [f for f, expr in template.items() if "${pickStringRemember:" in expr]
    assert prompting == ["value"], f"fields that would each open a project pick: {prompting}"
    reading = [f for f, expr in template.items() if "${remember:previewProject}" in expr]
    assert reading == ["label", "description", "detail"]


def _dispatched_actions(canonical) -> dict[str, str]:
    """{action key: task label} for every task routed through `devkit_project.py`."""
    found: dict[str, str] = {}
    for task in canonical["tasks"]:
        args = [str(a) for a in task.get("args", [])]
        if not args or not args[0].endswith("devkit_project.py"):
            continue
        # The dispatcher's CLI is `--project <name> <action> [extra…]`.
        index = args.index("--project")
        found[args[index + 2]] = task["label"]
    return found


def test_every_dispatched_task_names_a_real_action(canonical):
    """The workspace block passes the action key through VERBATIM.

    That is the whole seam between the two files, and it is stringly-typed: a task
    naming `docker-upp` is not a parse error anywhere — VS Code renders it, the click
    succeeds, and argparse rejects the choice several layers down, in a terminal, with
    the project picker already answered. This is the only place that mismatch can be
    caught before someone clicks it.
    """
    unknown = {
        action: label
        for action, label in _dispatched_actions(canonical).items()
        if action not in ACTIONS
    }
    assert not unknown, f"tasks naming actions the dispatcher does not define: {unknown}"


def test_every_action_is_reachable_from_a_task(canonical):
    """The other direction: an action nobody can click is dead weight in ACTIONS.

    Adding an entry to `ACTIONS` is documented as the only step a new generic task
    needs — which is true only if the task block is actually extended to call it.
    Without this, a half-finished hoist leaves an action that exists, is tested, and
    is reachable only by typing the CLI nobody uses.
    """
    unreachable = set(ACTIONS) - set(_dispatched_actions(canonical))
    assert not unreachable, f"actions with no task to invoke them: {sorted(unreachable)}"


# Tasks that deliberately do not persist a failure artifact. Both launch a window and
# exit — a Windows Terminal set, a VNC viewer — so their "output" is the thing they
# opened, and there is no run text for anyone to read afterwards. Named here rather than
# passed over, so a task that stops writing one has to say why in this list.
UNLOGGED_TASKS = {
    "Agents: Open Tabs (External Terminal)": "spawns terminal tabs; the window is the output",
    "Agents: Resume Recent Sessions": "same — reopens sessions in tabs, then exits",
    "IBKR: Open Gateway VNC Viewer": "launches a GUI viewer; nothing to parse when it closes",
}


def test_every_workspace_scoped_task_writes_a_failure_artifact(canonical):
    """The task-file half of the artifact rule.

    The other 20 tasks are a `devkit_project.py` dispatch and get theirs inside the
    dispatcher, where the chosen checkout is finally known — see `plan_command`. These
    run against devkit itself, so the wrapping has to be written here, and a new task
    added without it would fail silently into a terminal nobody kept.
    """
    missing = []
    for task in canonical["tasks"]:
        args = [str(a) for a in task.get("args", ())]
        label = task["label"]
        if any("devkit_project.py" in a for a in args):
            continue  # wrapped by the dispatcher
        if any(reason in label for reason in UNLOGGED_TASKS):
            continue
        if not any("log-wrap.py" in a for a in args):
            missing.append(label)
    assert not missing, f"tasks with no failure artifact: {missing}"


def test_the_unlogged_exceptions_are_all_real_tasks(canonical):
    """A stale exemption is how a task quietly loses its artifact: the label is renamed,
    the entry here stops matching anything, and nothing reports that it now exempts
    nobody. Same ratchet the scope pickers carry."""
    labels = {task["label"] for task in canonical["tasks"]}
    for exempt in UNLOGGED_TASKS:
        assert any(exempt in label for label in labels), f"{exempt} names no task"


def test_every_task_has_a_label_and_a_detail(canonical):
    """CLAUDE.md's convention: `detail` is the only place a one-click action can
    state its cost or blast radius."""
    for task in canonical["tasks"]:
        assert task.get("label"), task
        assert task.get("detail"), f"{task['label']} has no detail"


def test_every_task_has_an_icon(canonical):
    """With every task consolidated into one list, the icon is what makes it navigable.

    A task with no icon renders as a bare label in a list of twenty-nine, which is the
    state this consolidation would otherwise have created.
    """
    for task in canonical["tasks"]:
        icon = task.get("icon", {})
        assert icon.get("id"), f"{task['label']} has no icon id"
        assert icon.get("color"), f"{task['label']} has no icon colour"


def test_no_two_tasks_share_an_icon_and_colour(canonical):
    """An icon repeated under two labels is worse than no icon: it reads as "same kind of
    thing" to the eye and then is not.

    Two pairs were exactly that before the consolidation — `beaker`/green under both test
    tasks, `checklist`/yellow under both lint tasks.
    """
    seen: dict[tuple[str, str], str] = {}
    clashes = []
    for task in canonical["tasks"]:
        icon = task.get("icon", {})
        key = (icon.get("id", ""), icon.get("color", ""))
        if key in seen:
            clashes.append(f"{seen[key]} and {task['label']} both use {key[0]}/{key[1]}")
        seen[key] = task["label"]
    assert not clashes, "; ".join(clashes)


def test_a_scoped_task_offers_exactly_the_checkouts_its_action_allows(canonical):
    """The seam between this file and `Action.projects`, asserted from both ends.

    These have to agree or the picker is a trap: an option the dispatcher refuses looks
    like a supported choice right up to the point it fails in a terminal, with the rest of
    the inputs already answered. Offering FEWER than the action allows is the quieter
    failure — the `-b` worktree silently stops being reachable from the editor.
    """
    inputs = {spec["id"]: spec for spec in canonical["inputs"]}
    checked = 0
    for task in canonical["tasks"]:
        args = [str(a) for a in task.get("args", [])]
        if not args or not args[0].endswith("devkit_project.py") or "--project" not in args:
            continue
        index = args.index("--project")
        picker = re.fullmatch(r"\$\{input:([A-Za-z_][A-Za-z0-9_]*)\}", args[index + 1])
        action = ACTIONS.get(args[index + 2])
        if picker is None or action is None or not action.projects:
            continue
        offered = [
            option if isinstance(option, str) else option["value"]
            for option in _input_options(inputs[picker.group(1)])
        ]
        assert set(offered) == set(action.projects), (
            f"{task['label']}: picker offers {sorted(offered)} but the action is defined "
            f"for {sorted(action.projects)}"
        )
        checked += 1
    assert checked, "no scoped task found — the wiring this test guards is gone"


def test_the_live_smoke_task_names_the_only_checkout_that_can_run_it(canonical):
    """The test above skips a task whose `--project` is a literal rather than a picker,
    which is exactly what "Test: Harness Hook Tests — paid, live CLI" is: its action is
    defined for one checkout, and a picker of length one asks a question with no second
    answer. That trade is only safe while the literal and the scope agree — so this is
    the same assertion, made against the constant instead of against an option list.

    Written for the general case rather than for one label: a second single-scope task
    spelled the same way is covered the day it is added, which is when it would
    otherwise be silently ungated.
    """
    checked = 0
    for task in canonical["tasks"]:
        args = [str(a) for a in task.get("args", [])]
        if not args or not args[0].endswith("devkit_project.py") or "--project" not in args:
            continue
        index = args.index("--project")
        name, key = args[index + 1], args[index + 2]
        if name.startswith("${input:"):
            continue
        action = ACTIONS.get(key)
        assert action is not None, f"{task['label']}: {key} is not an action"
        assert action.projects == (name,), (
            f"{task['label']}: hard-codes --project {name} but {key} is defined for "
            f"{list(action.projects) or 'every checkout'}"
        )
        checked += 1
    assert checked, "no literal-checkout dispatch found — this test now guards nothing"


# The pickers that choose a CHECKOUT for a workspace-scoped task, and who each one is
# allowed to leave out. `register()` now extends these with the project registry; this
# comparison is the backstop for hand edits and for intentional exclusions such as
# devkit being the source rather than an upgrade target.
#
# An exclusion needs its reason written here. That is the same bargain
# `tests/test_dispatch_coherence.py` strikes for an unvendored dispatch target: leaving a
# checkout out stays possible, but as a decision someone recorded rather than a list
# nobody updated.
SCOPE_PICKERS: dict[str, dict[str, str]] = {
    "sweepScope": {},
    "upgradeScope": {
        "devkit": (
            "is the source a release is pulled FROM, not a consumer of it; "
            "upgrade-project.py refuses it by name"
        ),
    },
}

# Option values that mean "every checkout" rather than naming one.
SCOPE_ALL = {"--all"}


def _input_options(spec: dict) -> list:
    """Options from a native pickString or Command Variable multi-pick."""
    if "options" in spec:
        return spec["options"]
    return spec["args"]["optionGroups"][0]["options"]


def _picker_values(spec: dict) -> set[str]:
    """Every picker value, whether written bare or as a label/value pair."""
    return {
        option if isinstance(option, str) else option["value"] for option in _input_options(spec)
    }


def _scope_names(spec: dict) -> set[str]:
    """The checkouts a multi-select scope picker can aim at."""
    return _picker_values(spec) - SCOPE_ALL


def test_every_scope_picker_can_aim_at_every_checkout(canonical):
    """A workspace-scoped task must be able to name any checkout the registry knows.

    Measured against the `project` picker rather than the live `folders` list on purpose:
    `project` is the one list `register()` maintains, it ships inside this repo, and this
    assertion therefore runs in CI. The consequence is the useful one — generating a
    project now fails devkit's own suite until these lists are extended too, instead of
    being discovered the first time someone tries to sweep just that repo.
    """
    inputs = {spec["id"]: spec for spec in canonical["inputs"]}
    registry = _picker_values(inputs["project"])
    for picker_id, exclusions in SCOPE_PICKERS.items():
        expected = registry - set(exclusions)
        offered = _scope_names(inputs[picker_id])
        assert offered == expected, (
            f"{picker_id} cannot aim at {sorted(expected - offered)} "
            f"and offers unknown {sorted(offered - expected)}"
        )


def test_project_scope_inputs_are_real_multi_picks(canonical):
    """Every batch scope uses checkboxes and requires at least one selection."""
    inputs = {spec["id"]: spec for spec in canonical["inputs"]}
    for picker_id in (
        "project",
        "carameliCheckout",
        "ibkrCheckout",
        "dbCheckout",
        "sweepScope",
        "upgradeScope",
    ):
        spec = inputs[picker_id]
        assert spec["type"] == "command"
        assert spec["args"]["multiPick"] is True
        assert spec["args"]["optionGroups"][0]["minCount"] == 1

    # The single-pick ones are single-pick on purpose -- one Docker daemon, one repo a
    # box is cut from -- but they still have to reach every checkout the registry knows,
    # which is the half that was silently skipped before `SCOPE_PICKERS` existed.
    for picker_id in ("daemonProject", "worktreeProject"):
        assert inputs[picker_id]["type"] == "pickString"
        assert _picker_values(inputs[picker_id]) == _picker_values(inputs["project"])


def test_every_scope_exclusion_names_a_real_checkout_and_a_reason(canonical):
    """A stale exclusion is the failure mode above, wearing the test's own uniform.

    Left unchecked, a checkout removed from the workspace would keep its entry here and
    keep one picker exempt from the rule for a project that no longer exists — and the
    next real omission could be papered over by adding a line to `SCOPE_PICKERS` rather
    than to the picker.
    """
    inputs = {spec["id"]: spec for spec in canonical["inputs"]}
    registry = _picker_values(inputs["project"])
    for picker_id, exclusions in SCOPE_PICKERS.items():
        for name, reason in exclusions.items():
            assert name in registry, f"{picker_id} excludes {name}, which is not a checkout"
            assert reason.strip(), f"{picker_id} excludes {name} with no reason given"


# The one task that reaches a checkout `NOT_PROJECTS` excludes. Named once, because two
# tests below assert about it and a renamed label must break them rather than quietly
# exempt itself.
MERGE_TASK = "Git: Merge Origin Default into Current Branch"


def test_the_merge_picker_reaches_the_reference_checkouts_too(canonical):
    """The exception to `SCOPE_PICKERS`, and the reason it is not simply listed there.

    Every other picker offers the registry minus its documented exclusions. This one
    offers the registry PLUS `NOT_PROJECTS`: merging origin's default branch in is pure
    git — no `.devkit.toml`, no virtualenv, no vendored tier — so it is the one action a
    reference checkout can take, and the checkout it was written for is one.

    Asserted as an equality in both directions so the picker cannot drift either way: a
    newly generated project has to reach it (`register()` maintains it alongside
    `project`), and a checkout that stops being a non-project has to leave it.
    """
    inputs = {spec["id"]: spec for spec in canonical["inputs"]}
    registry = _picker_values(inputs["project"])
    expected = registry | set(devkit_project.NOT_PROJECTS)
    assert _picker_values(inputs["mergeCheckout"]) == expected


def test_the_merge_task_bypasses_the_dispatcher_on_purpose(canonical):
    """A dispatch would resolve through `known_projects` and refuse the reference
    checkout by name — correctly, since every ACTION needs a harness. Routing this one
    around the dispatcher is what keeps that rule intact instead of punching a hole in
    it, so the bypass is the behaviour, not an oversight to be tidied up later."""
    task = next(t for t in canonical["tasks"] if t["label"] == MERGE_TASK)
    args = [str(a) for a in task["args"]]
    assert not any("devkit_project.py" in arg for arg in args)
    assert "scripts/git-merge-default.py" in args
    assert "${input:mergeCheckout}" in args


def test_every_input_referenced_is_defined(canonical):
    """An undefined ${input:…} fails at click time with an opaque error."""
    defined = {i["id"] for i in canonical["inputs"]}
    referenced = set(re.findall(r"\$\{input:([A-Za-z_][A-Za-z0-9_]*)\}", json.dumps(canonical)))
    assert referenced <= defined, f"undefined inputs: {referenced - defined}"
    assert defined <= referenced, f"unused inputs: {defined - referenced}"


def test_every_mutating_sweep_task_offers_the_scope_picker(canonical):
    """`--only` restricts every sweep mode, so every step that changes a checkout has
    to let you aim it at one.

    Step 3 shipped without the picker and so was all-or-nothing: when a sync failed in
    one repo, the only way to retry it was the CLI, and the fallback for a one-click
    workflow being unable to express "just this one" is re-running it over every
    checkout. The read-only modes are deliberately exempt — an unscoped sweep IS the
    report, and a scoped one answers a question nobody asked of it.
    """
    for task in canonical["tasks"]:
        args = [str(a) for a in task.get("args", [])]
        if not any("sweep.py" in a for a in args):
            continue
        if not {"--branch", "--ship", "--sync"} & set(args):
            continue
        assert any("${input:sweepScope}" in arg for arg in args), (
            f"{task['label']} changes checkouts but cannot be scoped to one"
        )


# The sweep's mutating modes, and who is allowed to be their one-click entry point.
# `--branch` rescues work a hook could not stop landing on a home branch, which no
# schedule can decide for you -- it keeps its task. The other two do not: `--sync` is
# run unattended by `worktree.py reconcile` every fifteen minutes, and `--ship` writes
# a commit message describing the sweep rather than the change, which `/ship` per repo
# always does better now that an agent is a box away.
SWEEP_MODES_WITHOUT_A_TASK = ("--ship", "--sync")


def test_the_scheduled_sweep_modes_have_no_workspace_task(canonical):
    """One owner per tier, asserted where the duplicate would be added.

    Both of these had a task -- `Ship: Publish Swept Work (step 2)` and `Ship: Sync
    Worktrees (step 3)` -- and neither was ever run on the machine they were written
    for: the box tier had already removed the stranding they existed to clean up, and
    reconcile had been syncing every checkout on a schedule for months. A hand-crank
    beside a scheduled job is a second owner for one tier's lifecycle, and the two
    disagree the first time someone clicks it mid-pass. Re-adding one means retiring
    the automatic caller first, not just deleting this test.
    """
    for task in canonical["tasks"]:
        args = [str(a) for a in task.get("args", [])]
        if not any("sweep.py" in a for a in args):
            continue
        clash = set(args) & set(SWEEP_MODES_WITHOUT_A_TASK)
        assert not clash, (
            f"{task['label']} runs sweep.py {' '.join(sorted(clash))}, which has an "
            "owner already -- reconcile's schedule for --sync, /ship per repo for --ship"
        )


def test_some_task_still_routes_through_the_dispatcher(canonical):
    """The wiring itself, asserted separately from what it points at.

    This is what survives of a second "is it a real action?" check that guessed the action
    from `args[-1]`, falling back to `args[-2]`. The guess held only while every dispatched
    task ended with the action key or a picker; the hoisted tasks end with real arguments
    (`--arg=${input:ingestArg}`, a TigerVNC path), so it started reporting those as unknown
    actions. `test_every_dispatched_task_names_a_real_action` above makes the same
    assertion off the dispatcher's actual CLI shape — `--project <name> <action>` — which
    is positional and does not need guessing. Only the emptiness check was unique to it.
    """
    dispatched = _dispatched_actions(canonical)
    assert dispatched, "no task routes through the dispatcher — the wiring is gone"


@needs_live_workspace
def test_the_live_workspace_matches_the_canonical_block(canonical):
    """The check `--check-tasks` runs, as a test so devkit's own gate catches drift."""
    text = LIVE_WORKSPACE.read_text(encoding="utf-8")
    problems = tasks_drift(workspace_tasks(text), canonical)
    assert not problems, "run `python scripts/devkit_project.py --adopt-tasks`: " + "; ".join(
        problems
    )


@needs_live_workspace
def test_the_project_picker_lists_only_real_checkouts():
    """A stale picker entry is caught by resolve_project, but it should not be there."""
    text = LIVE_WORKSPACE.read_text(encoding="utf-8")
    picker = next(i for i in workspace_tasks(text)["inputs"] if i["id"] == "project")
    assert set(_input_options(picker)) <= set(devkit_project.known_projects(text))


# --- the real repos ---------------------------------------------------------


def test_devkit_itself_implements_every_action_it_is_on_the_hook_for():
    """devkit is upstream: if it cannot satisfy its own contract, the contract is wrong.

    "Its own" is `expected_actions("devkit")`, not every PROJECT-owned action — the scoped
    ones belong to one repo's worktree pair and demanding a `run-e2e.py` or an IBKR
    backtest of devkit would be asking it to grow a frontend and a broker.
    """
    expected = expected_actions("devkit")
    # Resolved against THIS checkout, not `<root>/devkit`. The subject is the repo the
    # test lives in, and looking it up by directory name assumed that name is always
    # `devkit` — which stopped being true the moment devkit was worked on from an
    # ephemeral box (`.worktrees/devkit--<topic>-<mmdd>`). The name-based lookup found
    # no such directory, reported every action missing, and named the contract as the
    # fault rather than the lookup.
    missing = sorted(key for key in expected if not (REPO_ROOT / ACTIONS[key].script).is_file())
    assert not missing, f"devkit is missing: {missing}"
    assert not (expected & {"e2e", "backtest"}), "a scoped action leaked into devkit's contract"


def test_every_devkit_owned_script_exists():
    """The other half: a DEVKIT action pointing at a script devkit does not ship."""
    missing = [
        a.script
        for a in ACTIONS.values()
        if a.owner == devkit_project.DEVKIT and not (REPO_ROOT / a.script).is_file()
    ]
    assert not missing, f"devkit-owned scripts missing: {missing}"
