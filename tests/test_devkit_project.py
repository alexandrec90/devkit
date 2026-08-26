"""Tests for the shared-task dispatcher.

The behaviour worth pinning is the failure text: a task that runs in the wrong
directory, or against a project that does not implement the action, must say so by
name. A traceback from a missing file in an unexpected cwd is the outcome this
script exists to prevent.
"""

import json
import re
from pathlib import Path

import pytest
from support import (
    LIVE_WORKSPACE,
    REPO_ROOT,
    devkit_jsonc,
    devkit_project,
    in_an_ephemeral_box,
    needs_live_workspace,
    needs_the_static_checkout,
    worktree,
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
        # Never hoisted — born scoped, and scoped by content rather than by capability:
        # the script encodes the comic-book skin's art, which only carameli has.
        "encode-art",
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
        # Born scoped, for `reclaim`'s reason rather than its own: the menu it picks from
        # is assembled from the box registry and the port registry, and there is exactly
        # one of each on this machine. So the question "which checkout" has no answer to
        # give -- every checkout is already a column in the menu -- and the task pins
        # `--project devkit` and asks the one question worth asking instead.
        #
        # There were two of these until 2026-08-25. A `preview` action ran
        # `preview-task.py`, held the `Preview: Open a UI Branch` label, and answered a
        # click with a worktree, an image build and a compose stack; this one runs `npm
        # run dev` on the frontend and was reachable only under a name nobody went
        # looking for. One label now, and the cheap script has it.
        # `test_no_task_dispatches_this_script` is what stops the other coming back.
        "preview-ui-host",
        # The teardown half of the pair above, scoped for the same reason and one more:
        # there is a single registry of running host preview servers on this machine, so
        # "stop them" is a machine-wide verb with no checkout to ask about. Per-checkout
        # it would stop the same servers N times.
        "preview-ui-stop",
        # Born scoped, and the third of the no-project-dimension kind -- but for a
        # sharper reason than `reclaim`'s: repeating a release is not merely a no-op on
        # runs 2..N, it is a failure. The second run would find the tag it just pushed
        # and refuse, so a two-checkout pick would report a red task for a release that
        # actually succeeded. Scoped, the task pins `--project devkit`.
        "release",
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


def test_the_host_preview_pair_is_one_script_told_apart_by_a_flag():
    """Serving and stopping share a script because they share a registry: `--stop` reads
    the file the serving run wrote. Split them and the stopper would be guessing which
    ports and pids to end, which is the state this pair exists to get out of."""
    assert ACTIONS["preview-ui-stop"].script == ACTIONS["preview-ui-host"].script
    assert ACTIONS["preview-ui-stop"].args == ("--stop",)
    assert ACTIONS["preview-ui-host"].args == ()


def test_stopping_the_host_previews_asks_nothing(canonical):
    """A teardown task is clicked when servers are already unwanted, often after the
    terminal that owned them is gone. A picker there would be a question about which
    checkout, and the answer -- all of them -- is the only one the registry can give."""
    task = next(t for t in canonical["tasks"] if t["label"] == "Preview: Stop Host UI Servers")
    args = [str(a) for a in task["args"]]
    assert not [a for a in args if "${input:" in a]
    assert args[args.index("--project") + 1] == "devkit"
    assert args[-1] == "preview-ui-stop"


# --- the test menu ----------------------------------------------------------
#
# One task replaced five: `Test: Run Suite`, `Test: Run Carameli Target`, both browser
# E2E tasks and the free hook-test run. `TEST_KINDS` is the table it spends -- a row is
# an (action, argument) pair, so eleven rows reach only actions `ACTIONS` already
# defines, scopes and wraps for logging.

TEST_KINDS = devkit_project.TEST_KINDS
TESTS_VERB = devkit_project.TESTS_VERB
SuiteKind = devkit_project.SuiteKind
kind_selection = devkit_project.kind_selection
plan_test_runs = devkit_project.plan_test_runs


def test_a_kind_is_an_action_and_the_argument_that_shapes_it():
    """`SuiteKind` carries no script, no label and no scope -- it borrows all three from
    the action it names, which is what keeps the menu from becoming a second registry.

    The empty-argument default is the common case (a row that runs an action plainly),
    and it is what lets `hooks` and `suite` be rows at all: neither adds anything to the
    command the deleted task ran.
    """
    assert SuiteKind("test").args == ()
    assert TEST_KINDS["suite"] == SuiteKind("test")
    assert TEST_KINDS["suite-changed"] == SuiteKind("test", ("--changed",))


def test_the_kind_class_is_not_named_for_pytest_to_collect():
    """Named `SuiteKind` rather than `TestKind` on purpose: pytest collects a
    `Test`-prefixed class out of any test module that imports it, and this module
    imports it by name. Collected, it would be reported as a test with a constructor
    warning on every run -- noise nobody would trace back to a dataclass."""
    assert not SuiteKind.__name__.startswith("Test")


def test_every_test_kind_dispatches_an_action_the_dispatcher_defines():
    """The menu cannot reach a script `ACTIONS` does not name.

    That is why a kind is spelled as (action, argument) rather than as a command line.
    The five tasks it replaced each carried their own script path and their own scope;
    a menu that carried script paths would be a second registry to keep in step with
    this one, and the first thing to drift would be the scope.
    """
    unknown = {kind: k.action for kind, k in TEST_KINDS.items() if k.action not in ACTIONS}
    assert not unknown, f"test kinds naming actions the dispatcher does not define: {unknown}"


def test_the_menu_verb_is_not_an_action():
    """`tests` fans out to several scripts, so it cannot be an `ACTIONS` entry: an action
    is one script, one label and one `logs/` artifact. It is a sibling of the action
    choices in the parser, and a collision would make one of the two unreachable behind
    whichever the parser resolved first -- silently, since both spellings are valid."""
    assert TESTS_VERB not in ACTIONS


def test_kinds_run_in_menu_order_however_they_were_ticked():
    """A checkbox list hands back the clicking order. A run whose sequence nobody can
    reproduce from the artifact is a run nobody can compare against the last one."""
    assert kind_selection("e2e,suite") == ["suite", "e2e"]
    assert kind_selection("suite,e2e") == ["suite", "e2e"]


def test_a_kind_ticked_twice_runs_once():
    assert kind_selection("suite,suite") == ["suite"]


def test_empty_entries_from_the_picker_are_dropped():
    """`separator: ","` on an empty tick, and the dispatcher's own empty-argument strip,
    both leave bare commas in the token."""
    assert kind_selection("suite,,") == ["suite"]
    assert kind_selection("") == []


def test_an_unknown_kind_is_refused_before_any_checkout_is_resolved():
    """The pairs are a cross-product, so a typo caught late is worse than late: the
    fourth of eight runs failing would leave three suites already run and five never
    attempted, with a toast reporting the failure of a suite nobody chose to skip."""
    with pytest.raises(ProjectError, match=r"unknown test kind.*sweet.*menu offers"):
        kind_selection("suite,sweet")


def test_the_cross_product_runs_one_checkout_at_a_time():
    """Project-major, so a checkout's runs happen in one stretch and its `logs/`
    artifacts are not interleaved with another checkout's."""
    runs, skipped = plan_test_runs(["suite", "hooks"], ["devkit", "carameli"])
    assert runs == [
        ("devkit", "suite"),
        ("devkit", "hooks"),
        ("carameli", "suite"),
        ("carameli", "hooks"),
    ]
    assert skipped == []


def test_a_pair_the_checkout_cannot_run_is_skipped_by_name_not_refused():
    """One menu is offered for every checkout, so `devkit` beside a Playwright row is the
    ordinary case rather than a mistake -- ticking widely has to stay safe. `check_scope`
    still refuses the single-action path, where an out-of-scope ask has nothing else to
    run and is simply wrong."""
    runs, skipped = plan_test_runs(["suite", "e2e"], ["devkit", "carameli"])
    assert runs == [("devkit", "suite"), ("carameli", "suite"), ("carameli", "e2e")]
    assert skipped == ["devkit: e2e is defined for carameli"]


def menu_run(tmp_path, monkeypatch, kinds, projects=("alpha",), scripts=("run-tests.py",)):
    """Dispatch the test menu over a throwaway workspace, recording what it ran."""
    workspace = tmp_path / "projects.code-workspace"
    workspace.write_text(json.dumps({"folders": [{"path": name} for name in projects]}))
    for name in projects:
        (tmp_path / name / "scripts").mkdir(parents=True)
        for script in scripts:
            (tmp_path / name / "scripts" / script).write_text("")

    calls = []

    def fake_run(command, *, cwd, check):
        calls.append((inner(command), cwd.name))
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(devkit_project.subprocess, "run", fake_run)
    code = devkit_project.main(
        ["--workspace", str(workspace), "--project", ",".join(projects), TESTS_VERB, kinds]
    )
    return code, calls


def test_the_menu_fans_out_to_one_run_per_checkout_and_kind(tmp_path, monkeypatch):
    code, calls = menu_run(tmp_path, monkeypatch, "suite,suite-changed", projects=("alpha", "beta"))

    assert code == 0
    assert [cwd for _, cwd in calls] == ["alpha", "alpha", "beta", "beta"]
    assert [command[1:] for command, _ in calls] == [
        ["scripts/run-tests.py"],
        ["scripts/run-tests.py", "--changed"],
        ["scripts/run-tests.py"],
        ["scripts/run-tests.py", "--changed"],
    ]


def test_two_kinds_of_one_action_are_told_apart_by_the_argument(tmp_path, monkeypatch):
    """Four of the eleven rows share `run-tests.py` and three share `run-e2e.py`. The
    argument is the whole difference, so it has to reach the script -- the deleted tasks
    spelled theirs in the task block, where nothing checked them against the action."""
    _, calls = menu_run(tmp_path, monkeypatch, "suite-changed")
    assert calls[0][0][1:] == ["scripts/run-tests.py", "--changed"]


def test_a_skipped_pair_is_printed_in_the_run_it_was_skipped_from(tmp_path, monkeypatch, capsys):
    """On stdout, beside the runs that did happen. A pair silently dropped reads as a
    suite that passed."""
    code, calls = menu_run(tmp_path, monkeypatch, "suite,e2e")
    assert code == 0
    assert len(calls) == 1, "an out-of-scope kind ran anyway"
    assert "[skipped] alpha: e2e is defined for carameli" in capsys.readouterr().out


def test_a_selection_with_nothing_in_scope_is_a_refusal_not_a_green_run(
    tmp_path, monkeypatch, capsys
):
    """Exiting 0 having run nothing would hand `notify-wrap.py` a pass and leave
    `log-wrap.py` an empty artifact -- the exact shape of a suite that passed."""

    def boom(*_args, **_kwargs):
        raise AssertionError("something ran although every pair was out of scope")

    monkeypatch.setattr(devkit_project.subprocess, "run", boom)
    workspace = tmp_path / "projects.code-workspace"
    workspace.write_text(json.dumps({"folders": [{"path": "alpha"}]}))
    (tmp_path / "alpha" / "scripts").mkdir(parents=True)

    code = devkit_project.main(
        ["--workspace", str(workspace), "--project", "alpha", TESTS_VERB, "e2e"]
    )

    assert code == 2
    assert "nothing to run" in capsys.readouterr().err


def test_the_menu_with_no_kind_ticked_names_the_kinds(tmp_path, monkeypatch, capsys):
    """`minCount: 1` stops this at the picker; the CLI is public and has no such gate."""
    code, calls = menu_run(tmp_path, monkeypatch, "")
    assert (code, calls) == (2, [])
    assert "no test kind given" in capsys.readouterr().err


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
        "mergeCheckout",
    ):
        assert _input_options(inputs[picker_id])[-2:] == ["probe", "probe-b"]
    # VanillaLand is a reference checkout and must not drift into the middle.
    assert [f["path"] for f in devkit_jsonc_loads(updated)["folders"]][-1] == "VanillaLand"


# --- retirement, the inverse ------------------------------------------------

unregister = devkit_project.unregister
remove_folder = devkit_project.remove_folder
remove_picker_option = devkit_project.remove_picker_option


def test_retirement_drops_the_project_from_the_registry():
    updated = unregister(register(COMMENTED, ["newproj"]), ["newproj"])
    assert "newproj" not in devkit_project.known_projects(updated)


def test_retirement_drops_every_picker_option():
    text = """{
        "folders": [{"path": "alpha"}, {"path": "beta"}],
        "tasks": {
            "inputs": [
                {"id": "project", "options": ["alpha", "beta"], "default": "alpha"},
                {"id": "daemonProject", "options": ["alpha", "beta"], "default": "alpha"},
                {"id": "worktreeProject", "options": ["alpha", "beta"], "default": "alpha"},
                {"id": "mergeCheckout", "options": ["alpha", "beta"], "default": "alpha"}
            ]
        }
    }"""
    updated = devkit_jsonc_loads(unregister(text, ["alpha"]))
    for picker in updated["tasks"]["inputs"]:
        assert picker["options"] == ["beta"]


def test_retiring_the_default_repoints_it():
    """A `pickString` whose default names a retired checkout offers it as the pre-filled
    answer, so the one option that resolves to nothing is the one already selected."""
    text = """{
        "folders": [{"path": "alpha"}, {"path": "beta"}],
        "tasks": {"inputs": [{"id": "project", "options": ["alpha", "beta"], "default": "alpha"}]}
    }"""
    picker = devkit_jsonc_loads(unregister(text, ["alpha"]))["tasks"]["inputs"][0]
    assert picker["default"] == "beta"


def test_a_default_that_survives_is_left_alone():
    text = """{
        "folders": [{"path": "alpha"}, {"path": "beta"}],
        "tasks": {"inputs": [{"id": "project", "options": ["alpha", "beta"], "default": "beta"}]}
    }"""
    picker = devkit_jsonc_loads(unregister(text, ["alpha"]))["tasks"]["inputs"][0]
    assert picker["default"] == "beta"


def test_retirement_preserves_comments():
    """The reason the whole tier is text surgery rather than a load/dump round trip."""
    updated = unregister(register(COMMENTED, ["newproj"]), ["newproj"])
    assert "sweep.py reads this as the project registry" in updated
    assert "MAINTAINED BY new-project.py" in updated


def test_registration_and_retirement_round_trip_byte_for_byte():
    """The strongest statement of `_drop_element`'s comma handling there is: a stray or
    missing separator is a trailing comma the workspace file's own parser rejects, and
    `sweep.parse_workspace` reads an unparseable registry as "no checkouts" rather than
    as a failure."""
    assert unregister(register(COMMENTED, ["newproj"]), ["newproj"]) == COMMENTED


def test_retiring_a_project_that_is_not_registered_is_a_no_op():
    """The picker re-runs over its own result: a name already gone must not fail."""
    assert unregister(COMMENTED, ["never-was-here"]) == COMMENTED


def test_retiring_the_last_folder_entry_takes_the_comma_before_it():
    """An element with nothing after it owns the *preceding* comma, not a following one.
    Taking the wrong side leaves `[..., ]`, which is the trailing comma above."""
    text = '{"folders": [{"path": "alpha"}, {"path": "beta"}]}'
    assert devkit_jsonc_loads(remove_folder(text, "beta"))["folders"] == [{"path": "alpha"}]


def test_a_folder_entry_is_matched_by_path_not_by_label():
    """VS Code rewrites this file whenever a workspace setting is changed through its UI,
    so its spacing is not ours to predict, and a folder may carry a display `name` that
    is not its path."""
    text = '{"folders": [{ "name" : "Alpha (reference)" , "path" :  "alpha" }, {"path": "beta"}]}'
    assert devkit_jsonc_loads(remove_folder(text, "alpha"))["folders"] == [{"path": "beta"}]


def test_removing_the_only_folder_entry_is_refused():
    with pytest.raises(RegistryEditError, match="only element"):
        remove_folder('{"folders": [{"path": "alpha"}]}', "alpha")


def test_removing_a_folder_that_is_not_there_is_refused():
    with pytest.raises(RegistryEditError, match="not in the workspace folders list"):
        remove_folder('{"folders": [{"path": "alpha"}, {"path": "beta"}]}', "gamma")


def test_a_picker_that_never_listed_the_name_is_left_alone():
    """`mergeCheckout` lists more than the registry, and an older workspace file may
    carry fewer pickers, so "not there" is the same outcome as "removed"."""
    text = '{"tasks": {"inputs": [{"id": "project", "options": ["alpha", "beta"]}]}}'
    assert remove_picker_option(text, "gamma") == text


def test_a_workspace_without_a_project_picker_is_refused():
    with pytest.raises(RegistryEditError, match=r"no .project. input"):
        remove_picker_option('{"tasks": {"inputs": []}}', "alpha")


def test_a_half_applied_retirement_is_refused_rather_than_written():
    """`unregister` verifies its own result for the reason `register` does: the failure
    it guards against is silent everywhere it matters."""
    text = '{"folders": [{"path": "alpha"}, {"path": "beta"}]}'
    with pytest.raises(RegistryEditError, match=r"no .project. input"):
        unregister(text, ["alpha"])


@needs_live_workspace
def test_retiring_against_the_real_workspace_file():
    """The fixtures above are three inputs deep; the live file's `project` picker nests
    its options two levels inside `args`, and every picker carries comments."""
    text = LIVE_WORKSPACE.read_text(encoding="utf-8")
    victim = devkit_project.known_projects(text)[0]
    updated = unregister(text, [victim])
    assert victim not in devkit_project.known_projects(updated)
    inputs = {i["id"]: i for i in devkit_jsonc_loads(updated)["tasks"]["inputs"]}
    for picker_id in ("project", "daemonProject", "worktreeProject", "mergeCheckout"):
        options = _input_options(inputs[picker_id])
        assert victim not in options
        default = inputs[picker_id].get("default")
        assert default is None or default in options
    # VanillaLand is not a project, so no checkbox can retire it out of the registry.
    assert [f["path"] for f in devkit_jsonc_loads(updated)["folders"]][-1] == "VanillaLand"


@needs_live_workspace
def test_the_real_workspace_file_round_trips():
    text = LIVE_WORKSPACE.read_text(encoding="utf-8")
    assert unregister(register(text, ["probe"]), ["probe"]) == text


# --- the canonical task block ------------------------------------------------

tasks_drift = devkit_project.tasks_drift
workspace_tasks = devkit_project.workspace_tasks


@pytest.fixture
def canonical():
    return devkit_project.canonical_tasks()


def test_the_canonical_block_exists_and_parses(canonical):
    assert canonical["version"] == "2.0.0"
    assert canonical["tasks"], "the canonical task block defines no tasks"


def test_no_drift_against_itself(canonical):
    assert tasks_drift(canonical, canonical) == []


def test_the_rule_sends_an_editor_to_the_canonical_copy_first():
    """The rule must name `workspace.jsonc` as the thing to edit before it names a render.

    The old ordering assertion guarded a preflight `--check-tasks` before an adopt,
    which was the best available advice while the LIVE file was the source. It is the
    wrong shape now: the point is not to sequence two commands but to send the edit to
    the copy that has a branch. A rule that mentioned rendering first would read as
    "publish, then edit", which is the failure again.
    """
    rule = (REPO_ROOT / ".claude" / "rules" / "vscode-tasks.md").read_text(encoding="utf-8")
    section = rule[rule.index("## Changing a task") :]
    assert section.index("workspace.jsonc") < section.index("--render-workspace")
    assert "Never hand-edit the live file" in section

    header = devkit_project.canonical_text()[:4000]
    assert "--render-workspace" in header, "the canonical copy must say how it is published"
    assert header.index("--check-workspace") < header.index("--adopt-workspace")


# --- the whole file: drift, render, adopt ------------------------------------


@pytest.fixture
def workspace_pair(tmp_path, monkeypatch):
    """A canonical copy and a live file, both writable, wired into the module.

    The real pair cannot be used: rendering WRITES the live workspace file, which every
    VS Code window on this machine is reading.
    """
    canonical = tmp_path / "workspace.jsonc"
    canonical.write_text(devkit_project.canonical_text(), encoding="utf-8", newline="\n")
    live = tmp_path / "alex-projects.code-workspace"
    live.write_text(canonical.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    monkeypatch.setattr(devkit_project, "CANONICAL_WORKSPACE", canonical)
    return canonical, live


def _run(live, *flags):
    return devkit_project.main(["--workspace", str(live), *flags])


def test_drift_names_a_checkout_that_appeared_only_in_the_live_file():
    """`folders` is the registry every sweep reads, so "differs" is not an answer."""
    canonical = {"folders": [{"path": "carameli"}]}
    live = {"folders": [{"path": "carameli"}, {"path": "sports_betting"}]}
    problems = devkit_project.workspace_drift(live, canonical)
    assert "folder in the workspace but not in devkit: sports_betting" in problems


def test_drift_names_a_checkout_missing_from_the_live_file():
    canonical = {"folders": [{"path": "carameli"}, {"path": "devkit"}]}
    problems = devkit_project.workspace_drift({"folders": [{"path": "carameli"}]}, canonical)
    assert "folder missing from the workspace: devkit" in problems


def test_drift_reports_a_changed_setting():
    problems = devkit_project.workspace_drift(
        {"settings": {"powershell.cwd": "devkit"}}, {"settings": {"powershell.cwd": "carameli"}}
    )
    assert "settings differs" in problems


def test_drift_ignores_layout_and_comments(workspace_pair):
    """VS Code rewrites this file itself; a byte comparison would cry wolf every time."""
    canonical, live = workspace_pair
    payload = devkit_jsonc_loads(canonical.read_text(encoding="utf-8"))
    live.write_text(json.dumps(payload, indent=8), encoding="utf-8", newline="\n")
    assert _run(live, "--check-workspace") == 0


def test_render_publishes_the_canonical_copy(workspace_pair):
    canonical, live = workspace_pair
    live.write_text(
        live.read_text(encoding="utf-8").replace('"powershell.cwd": "carameli"', '"x": "y"'),
        encoding="utf-8",
        newline="\n",
    )
    assert _run(live, "--render-workspace", "--force") == 0
    assert live.read_text(encoding="utf-8") == canonical.read_text(encoding="utf-8")
    assert _run(live, "--check-workspace") == 0


def test_render_refuses_to_overwrite_an_unadopted_live_edit(workspace_pair):
    """The whole point of the stamp. A render that silently discarded a hand edit would
    be the old last-writer-wins failure wearing devkit's name."""
    canonical, live = workspace_pair
    canonical.write_text(
        canonical.read_text(encoding="utf-8").replace('"powershell.cwd": "carameli"', '"a": "b"'),
        encoding="utf-8",
        newline="\n",
    )
    live.write_text(
        live.read_text(encoding="utf-8").replace('"powershell.cwd": "carameli"', '"c": "d"'),
        encoding="utf-8",
        newline="\n",
    )
    before = live.read_text(encoding="utf-8")
    assert _run(live, "--render-workspace") == 1
    assert live.read_text(encoding="utf-8") == before, "the live edit was overwritten anyway"


def test_render_proceeds_once_the_live_edit_is_adopted(workspace_pair):
    canonical, live = workspace_pair
    live.write_text(
        live.read_text(encoding="utf-8").replace('"powershell.cwd": "carameli"', '"c": "d"'),
        encoding="utf-8",
        newline="\n",
    )
    assert _run(live, "--adopt-workspace") == 0
    assert devkit_jsonc_loads(canonical.read_text(encoding="utf-8"))["settings"] == {"c": "d"}
    assert _run(live, "--check-workspace") == 0


def test_render_stamps_so_the_next_one_is_not_mistaken_for_a_hand_edit(workspace_pair):
    """A rendered file differs from what was there before; without the stamp the very
    next render would read its own output as somebody else's edit."""
    canonical, live = workspace_pair
    canonical.write_text(
        canonical.read_text(encoding="utf-8").replace('"powershell.cwd": "carameli"', '"a": "b"'),
        encoding="utf-8",
        newline="\n",
    )
    assert _run(live, "--render-workspace", "--force") == 0
    canonical.write_text(
        canonical.read_text(encoding="utf-8").replace('"a": "b"', '"e": "f"'),
        encoding="utf-8",
        newline="\n",
    )
    assert _run(live, "--render-workspace") == 0, "the second render refused its own output"
    assert devkit_jsonc_loads(live.read_text(encoding="utf-8"))["settings"] == {"e": "f"}


def test_a_current_live_file_is_stamped_without_being_rewritten(workspace_pair):
    """Adoption leaves the pair equal but unstamped, which must not arm the refusal."""
    _canonical, live = workspace_pair
    assert devkit_project.read_stamp(live) is None
    assert _run(live, "--render-workspace") == 0
    assert devkit_project.read_stamp(live) == devkit_project.semantic_digest(
        live.read_text(encoding="utf-8")
    )


def _add_a_folder(canonical):
    """One unambiguous difference, in the key the drift report names rather than diffs."""
    payload = devkit_jsonc_loads(canonical.read_text(encoding="utf-8"))
    payload["folders"] = [*payload.get("folders", []), {"path": "invented"}]
    canonical.write_text(json.dumps(payload), encoding="utf-8", newline="\n")


def test_publish_workspace_reports_which_of_the_three_things_it_did(workspace_pair):
    """The CLI and the session-start hook publish through this one function, so the
    verdict has to be readable rather than inferred from an exit code. It is the whole
    reason the render logic left `main()`: a second answer to "is it safe to overwrite
    the file every window on this machine reads" is the last thing this should grow."""
    canonical, live = workspace_pair
    assert devkit_project.publish_workspace(live) == (devkit_project.RENDER_CURRENT, [])

    _add_a_folder(canonical)
    outcome, problems = devkit_project.publish_workspace(live)
    assert outcome == devkit_project.RENDER_PUBLISHED and problems
    assert live.read_text(encoding="utf-8") == canonical.read_text(encoding="utf-8")


def test_publish_workspace_refuses_a_live_file_it_did_not_stamp(workspace_pair):
    """The refusal belongs here and not in each caller -- the hook publishes unattended,
    so a caller that forgot the check would discard a hand edit with nobody watching."""
    canonical, live = workspace_pair
    devkit_project.stamp_path(live).unlink(missing_ok=True)
    _add_a_folder(canonical)
    before = live.read_text(encoding="utf-8")

    outcome, problems = devkit_project.publish_workspace(live)

    assert outcome == devkit_project.RENDER_REFUSED and problems
    assert live.read_text(encoding="utf-8") == before
    assert devkit_project.publish_workspace(live, force=True)[0] == devkit_project.RENDER_PUBLISHED


def test_a_written_stamp_reads_back_and_a_missing_one_is_not_an_error(tmp_path):
    """`write_stamp`/`read_stamp` are what make a render refusable, so the round trip is
    pinned directly rather than only through `--render-workspace`. A live file devkit
    has never written has no stamp, and that has to read as "unknown", not as a crash --
    it is the state every machine is in before the first render."""
    live = tmp_path / devkit_project.DEFAULT_WORKSPACE.name
    assert devkit_project.read_stamp(live) is None
    devkit_project.write_stamp(live, "cafef00d")
    assert devkit_project.read_stamp(live) == "cafef00d"


def test_the_stamp_sits_beside_the_live_file_not_under_devkit_logs(tmp_path):
    """An ephemeral box has its own empty `logs/`; a stamp there would make every box's
    first render believe the live file had been hand-edited."""
    live = tmp_path / devkit_project.DEFAULT_WORKSPACE.name
    assert devkit_project.stamp_path(live).parent == live.parent


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


def _picker_args(spec: dict) -> list[dict]:
    """A picker's `args`, and every nested `pickStringRemember` picker's, flattened."""
    args = spec.get("args")
    found = [args] if isinstance(args, dict) else []
    for nested in (args or {}).get("pickStringRemember", {}).values():
        if isinstance(nested, dict):
            found.append(nested)
    return found


def test_no_picker_opts_into_the_extensions_escaped_ui_flag(canonical):
    """`checkEscapedUI` makes a dropdown usable ONCE per window, and it looks like a fix.

    It reads as "abort the launch when the user escapes", which is what a cancelled pick
    should do. It is implemented as a sticky bit: an Escape stores `__escapedUI` in the
    extension's `rememberStore`, every later command that opts in returns `undefined`
    BEFORE opening its quick-pick, and nothing clears the bit except a successful pick
    from a command that opted in -- which can no longer happen. So `Preview: Open a UI
    Branch` could be cancelled exactly once, and every click after that went straight to
    "Nothing picked -- the dropdown was cancelled" with no list ever drawn.

    A ratchet over the whole block rather than over `previewRow`, because the flag is
    per-command and the next picker to copy one of these will copy whatever is here.
    Without it a cancelled run costs one terminal saying it picked nothing, which every
    dispatched script already treats as a graceful no-op.
    """
    opted_in = [
        spec["id"]
        for spec in canonical["inputs"]
        if any(args.get("checkEscapedUI") for args in _picker_args(spec))
    ]
    assert opted_in == []


def _test_kind_picker(canonical) -> str:
    """The input id the test-menu task hands its ticked kinds through."""
    for task in canonical["tasks"]:
        args = [str(a) for a in task.get("args", [])]
        if not args or not args[0].endswith("devkit_project.py"):
            continue
        index = args.index("--project")
        if args[index + 2] == TESTS_VERB:
            picker = re.fullmatch(r"\$\{input:(\w+)\}", args[index + 3])
            assert picker, f"the menu's kinds argument is not a picker: {args[index + 3]}"
            return picker.group(1)
    raise AssertionError(f"no task dispatches the '{TESTS_VERB}' verb")


def _test_kind_options(canonical) -> list[dict]:
    inputs = {spec["id"]: spec for spec in canonical["inputs"]}
    return _input_options(inputs[_test_kind_picker(canonical)])


def _dispatched_actions(canonical) -> dict[str, str]:
    """{action key: task label} for every task routed through `devkit_project.py`.

    The `tests` verb is not an action but a menu, so what it reaches is whatever its
    kind picker offers -- expanded here because both reachability tests below depend on
    it. Four actions stopped being tasks of their own when the five test tasks became
    rows; without the expansion `test_every_action_is_reachable_from_a_task` would read
    them as dead weight in `ACTIONS` and the consolidation could not have landed.
    """
    found: dict[str, str] = {}
    for task in canonical["tasks"]:
        args = [str(a) for a in task.get("args", [])]
        if not args or not args[0].endswith("devkit_project.py"):
            continue
        # The dispatcher's CLI is `--project <name> <action> [extra…]`.
        index = args.index("--project")
        verb = args[index + 2]
        if verb != TESTS_VERB:
            found[verb] = task["label"]
            continue
        for option in _test_kind_options(canonical):
            # `.get`, not `[]`: a menu row naming no kind is a readable failure in
            # `test_the_menu_offers_exactly_the_kinds_the_dispatcher_knows`, rather than
            # a KeyError raised out of a helper three other tests share.
            kind = TEST_KINDS.get(option["value"])
            if kind:
                found[kind.action] = task["label"]
    return found


def test_the_menu_offers_exactly_the_kinds_the_dispatcher_knows(canonical):
    """Both directions across the seam, which is stringly-typed like every other one
    here: a row's `value` travels to the CLI verbatim.

    A row naming no kind is rejected in a terminal after both dropdowns are answered. A
    kind with no row is the quieter half -- it exists, it is tested, and it is reachable
    only by typing a CLI nobody uses, which is what the whole task block exists to
    replace.
    """
    offered = {option["value"] for option in _test_kind_options(canonical)}
    assert offered == set(TEST_KINDS)


def test_a_kind_only_some_checkouts_can_run_says_so_in_its_row(canonical):
    """One menu is offered whatever was ticked first, so a row states its own scope.

    A kind list narrowed by the ticked checkouts would be two dependent pickers, and VS
    Code resolves sibling `${input:...}` in no defined order with neither given sight of
    the other (`.claude/rules/vscode-tasks.md`). The extension's own `dependsOn` filters
    the *result* rather than the list, so an out-of-scope tick would be dropped with
    nothing said -- and the dispatcher prints `[skipped]` for exactly that reason. The
    sentence in the row is what makes the skip expected rather than surprising.
    """
    unstated = []
    for option in _test_kind_options(canonical):
        action = ACTIONS[TEST_KINDS[option["value"]].action]
        description = option.get("description", "")
        expected = action.projects or ("every checkout",)
        if any(name not in description for name in expected):
            unstated.append((option["value"], expected, description))
    assert not unstated, f"menu rows whose scope is not written where it is read: {unstated}"


def test_the_test_task_asks_which_checkouts_before_which_kinds(canonical):
    """The order the user asked for, and the order the args are read in: VS Code prompts
    `${input:...}` in the order they appear in the command line it is building."""
    task = next(t for t in canonical["tasks"] if t["label"] == "Test: Run Suite")
    args = [str(a) for a in task["args"]]
    assert args[args.index("--project") + 1] == "${input:project}"
    assert args.index("${input:project}") < args.index(f"${{input:{_test_kind_picker(canonical)}}}")


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
    "Agents: Import Limited Claude Sessions": "same — opens imported sessions in tabs",
    "IBKR: Open Gateway VNC Viewer": "launches a GUI viewer; nothing to parse when it closes",
    "Workspace: Plug / Unplug Projects": (
        "the script writes logs/plug-projects.log itself, naming the registry it ended "
        "with rather than transcribing the run; the checkboxes are VS Code's quick-pick, "
        "so a bare `python scripts/plug-projects.py` is the only entry point that prompts"
    ),
}


def test_every_workspace_scoped_task_writes_a_failure_artifact(canonical):
    """The task-file half of the artifact rule.

    Dispatched tasks get their artifact inside the dispatcher, where the chosen checkout
    is finally known — see `plan_command`. These
    run against devkit itself, so the wrapping has to be written here, and a new task
    added without it would fail silently into a terminal nobody kept.
    """
    missing = []
    for task in canonical["tasks"]:
        args = [str(a) for a in task.get("args", ())]
        label = task["label"]
        if args and args[0].endswith("devkit_project.py"):
            continue  # a dispatch: wrapped inside plan_command, where the checkout is known
        if any(reason in label for reason in UNLOGGED_TASKS):
            continue
        if not any("log-wrap.py" in a for a in args):
            missing.append(label)
    assert not missing, f"tasks with no failure artifact: {missing}"


def test_every_workspace_file_command_is_reachable_from_a_task(canonical):
    """The three directions between the live file and `workspace.jsonc` are one click.

    A flag nobody can click is how the old arrangement stayed broken: `--check-tasks`
    existed for years, was correct, and was never wired to anything -- so the only
    thing that ever ran it was a test that CI skips. Reachability is the difference
    between a gate and a documented intention.
    """
    spelled = {arg for task in canonical["tasks"] for arg in map(str, task.get("args", ()))}
    for flag in ("--check-workspace", "--render-workspace", "--adopt-workspace"):
        assert flag in spelled, f"{flag} is reachable only by typing it"


def test_the_workspace_file_tasks_are_not_dispatches(canonical):
    """There is exactly one workspace file, so there is no checkout to pick -- and a
    task that named the `project` picker would ask a question with no bearing on what
    it does. They call the script directly, which is why they carry their own
    `log-wrap.py` -- asserted by the artifact test above, and only there: this test used
    to assert it a second time with no exemption path, so the first `Workspace:` task
    that legitimately could not be piped (the interactive one) failed a rule the table
    it was listed in had already excused it from.
    """
    for task in canonical["tasks"]:
        if not task["label"].startswith("Workspace: "):
            continue
        args = [str(a) for a in task.get("args", ())]
        assert not args[0].endswith("devkit_project.py"), task["label"]
        assert "${input:project}" not in args, task["label"]


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


# The settings every task carries, and what each one is for. A task is a one-click
# action with no review step, so what makes the set navigable is that they all behave
# the same way — and the way drift arrives is a new task written by copying whichever
# neighbour happened to be nearest.
#
# `panel: "new"` is the one with a history. The VS Code default, `shared`, puts every
# task in one terminal, so starting any task erases what the last one printed, with no
# warning and nothing to scroll back to; `dedicated` is half a fix, separating task from
# task while still overwriting the previous run of the *same* task, which is the pair a
# reader most often wants side by side. The `logs/` artifacts do not cover the gap —
# `log-wrap.py` empties a task's log when it passes, so a successful run exists only in
# its terminal. Terminals accumulate instead, and that is the accepted trade.
TASK_CONTRACT = {
    "type": "process",  # VS Code watches the process, so the exit-code icon is real
    "presentation.panel": "new",  # one run, one terminal; nothing is overwritten
    "presentation.close": False,  # the terminal stays open for review
    "presentation.reveal": "always",  # a task you clicked shows you what it did
}

# Tasks that deliberately finish without a toast, for the same reason `UNLOGGED_TASKS`
# exists: a toast reports that something you were not watching has ended, and these
# either end instantly or hand you a window that is itself the notification.
UNTOASTED_TASKS = {
    "Agents: Open Tabs (External Terminal)": "the tabs it opens are the notification",
    "Agents: Resume Recent Sessions": "same — reopens sessions in tabs, then exits",
    "Agents: Import Limited Claude Sessions": "same — opens imported sessions in tabs",
    "Ports: Show Checkout Allocations": "prints a table and exits; you are already looking",
    "Workspace: List Tasks as a Table": "same — the table is the output, in front of you",
}

# Deviations, each with the reason it is one. A new task does not belong here: this is
# for the handful whose *output is not in their terminal at all*.
CONTRACT_EXCEPTIONS = {
    ("Agent: Sync Codex Context", "presentation.reveal"): (
        "silent: a context sync that prints nothing worth stealing focus for"
    ),
    ("Agents: Open Tabs (External Terminal)", "presentation.reveal"): (
        "silent: the tabs it opens are the output; its own terminal holds one line"
    ),
    ("Agents: Open Tabs (External Terminal)", "presentation.close"): (
        "closes: same — nothing is left in this terminal to review"
    ),
}


def _setting(task: dict, dotted: str):
    value = task
    for key in dotted.split("."):
        value = value.get(key, {}) if isinstance(value, dict) else {}
    return value if value != {} else None


def test_every_task_matches_the_presentation_contract(canonical):
    """One table for the whole task block, so a new task cannot pick up half of it.

    Before this test the block had drifted exactly the way it drifts: 33 tasks pinned
    `close: false` and 8 left it to the default, and `panel` was `shared` on most and
    `dedicated` on five — a distinction nobody had decided, arrived at by each task being
    copied from a different neighbour.
    """
    wrong = []
    for task in canonical["tasks"]:
        for dotted, expected in TASK_CONTRACT.items():
            if (task["label"], dotted) in CONTRACT_EXCEPTIONS:
                continue
            actual = _setting(task, dotted)
            if actual != expected:
                wrong.append(f"{task['label']}: {dotted} is {actual!r}, want {expected!r}")
    assert not wrong, "\n".join(wrong)


def test_every_contract_exception_names_a_real_task_and_still_deviates(canonical):
    """The same ratchet `UNLOGGED_TASKS` and the scope exclusions carry.

    An exemption outlives what it exempted twice over: the label is renamed and it
    matches nothing, or the task is brought back into line and the entry now licenses a
    future deviation nobody argued for.
    """
    tasks = {task["label"]: task for task in canonical["tasks"]}
    for (label, dotted), reason in CONTRACT_EXCEPTIONS.items():
        assert reason, f"{label}/{dotted} is exempt with no reason"
        assert label in tasks, f"{label} names no task"
        assert _setting(tasks[label], dotted) != TASK_CONTRACT[dotted], (
            f"{label} now matches the contract on {dotted}; drop its exception"
        )


def test_every_direct_task_toasts_when_it_finishes(canonical):
    """`notify-wrap.py` outermost on every task that is not a dispatch.

    The dispatched ones get it from `plan_command`; these are written by hand and are
    where it goes missing. Outermost matters: the toast needs only an exit code, so it
    wraps `log-wrap.py`, which needs the output.
    """
    missing = []
    for task in canonical["tasks"]:
        args = [str(a) for a in task.get("args", ())]
        if any("devkit_project.py" in a for a in args):
            continue  # the dispatcher wraps it
        if any(exempt in task["label"] for exempt in UNTOASTED_TASKS):
            continue
        if not args or "notify-wrap.py" not in args[0]:
            missing.append(task["label"])
    assert not missing, f"tasks that finish without a toast: {missing}"


def test_the_untoasted_exceptions_are_all_real_tasks(canonical):
    labels = {task["label"] for task in canonical["tasks"]}
    for exempt in UNTOASTED_TASKS:
        assert any(exempt in label for label in labels), f"{exempt} names no task"


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


# `SCOPE_PICKERS` and `test_every_scope_picker_can_aim_at_every_checkout` lived here.
# They gated a class with no members left: a picker that chooses WHICH CHECKOUTS a
# workspace-scoped batch task should act on. `sweepScope` went first, `upgradeScope`
# with the change that added this comment -- both for the same reason, which is worth
# keeping rather than the check that guarded them. A release is one upstream revision,
# so adopting it in a subset of consumers is not an operation anyone wants (see
# `upgrade-project.upgrade_one` -- a consumer already current costs a fetch, so `--all`
# is both cheaper to reason about and cheaper to run than the question was). The list
# of options such a picker needs is a second copy of the project registry, and every
# copy of it has drifted at least once.
#
# So: a batch task that acts on the workspace takes `--all`, and a task that acts on
# ONE checkout uses `project`, which `register()` maintains. What the ratchet actually
# guaranteed is not lost with it -- `test_picker_registration_updates_the_multi_test_
# picker_too` covers the registration side, and the tail of
# `test_project_scope_inputs_are_real_multi_picks` still requires `daemonProject` and
# `worktreeProject` to reach every checkout `project` knows. Only the *scope* dimension
# is gone. Reintroduce a scope picker and this is the check it needs back: the failure
# it was written for is a newly generated project that every generic task can reach and
# no batch task can.


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


def test_project_scope_inputs_are_real_multi_picks(canonical):
    """Every batch scope uses checkboxes and requires at least one selection."""
    inputs = {spec["id"]: spec for spec in canonical["inputs"]}
    for picker_id in (
        "project",
        "carameliCheckout",
        "ibkrCheckout",
        "dbCheckout",
    ):
        spec = inputs[picker_id]
        assert spec["type"] == "command"
        assert spec["args"]["multiPick"] is True
        assert spec["args"]["optionGroups"][0]["minCount"] == 1

    # The single-pick ones are single-pick on purpose -- one Docker daemon, one repo a
    # box is cut from -- but they still have to reach every checkout the registry knows.
    # That is the half a per-picker option list keeps losing: `project` gains the new
    # project because `register()` writes it, and a hand-maintained sibling does not.
    for picker_id in ("daemonProject", "worktreeProject"):
        assert inputs[picker_id]["type"] == "pickString"
        assert _picker_values(inputs[picker_id]) == _picker_values(inputs["project"])


def test_the_test_kinds_input_is_a_checkbox_list_the_dispatcher_can_split(canonical):
    """Both of the test task's questions are checkboxes, and the second is this one.

    The separator is the load-bearing part: an input resolves to ONE string, so a
    multi-pick joins the ticked *values* with it and `kind_selection` splits on the same
    character. A default separator is `,` too, but writing it keeps the two halves of
    one contract in sight of each other rather than agreeing by coincidence.

    `minCount` is what makes `main`'s empty-selection refusal a backstop rather than the
    first thing a user meets.
    """
    inputs = {spec["id"]: spec for spec in canonical["inputs"]}
    spec = inputs[_test_kind_picker(canonical)]
    assert spec["type"] == "command"
    assert spec["args"]["multiPick"] is True
    assert spec["args"]["separator"] == ","
    assert spec["args"]["optionGroups"][0]["minCount"] == 1
    # No value may contain the separator, or one tick would arrive as two kinds.
    assert not [
        option["value"] for option in _test_kind_options(canonical) if "," in option["value"]
    ]


# The one task that reaches a checkout `NOT_PROJECTS` excludes. Named once, because two
# tests below assert about it and a renamed label must break them rather than quietly
# exempt itself.
MERGE_TASK = "Git: Merge Origin Default into Current Branch"


def test_the_merge_picker_reaches_the_reference_checkouts_too(canonical):
    """The one picker whose option list is not the registry, and why.

    Every other picker offers the registry, or the registry minus a documented
    exclusion. This one offers the registry PLUS `NOT_PROJECTS`: merging origin's
    default branch in is pure
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


# A release tag as this file could come to carry one: `v1.2.3` in a label, a detail or a
# task argument. Comments are already gone by the time `canonical` is parsed, so a
# version written as *reasoning* is out of scope and stays allowed -- what this catches
# is a version presented to whoever is deciding, or handed to a script.
_RELEASE_TAG = re.compile(r"\bv\d+\.\d+\.\d+\b")


def _strings(node, path: str = "") -> list[tuple[str, str]]:
    """Every string in the task block, paired with where it sits."""
    if isinstance(node, dict):
        return [
            pair
            for key, value in node.items()
            for pair in _strings(value, f"{path}.{key}" if path else str(key))
        ]
    if isinstance(node, list):
        return [
            pair for index, item in enumerate(node) for pair in _strings(item, f"{path}[{index}]")
        ]
    return [(path, node)] if isinstance(node, str) else []


def test_no_task_or_picker_states_a_release_version(canonical):
    """Nothing renders this file from the tag list, so a version written here cannot move.

    `releaseLevel` carried a worked example per option -- the newest tag on the day they
    were written, and what each bump would make of it -- which is genuinely the most
    useful thing a three-option dropdown could say and was wrong from the next release
    onwards, offering "the usual" patch as a version that had already shipped. A stale
    number in a quick-pick is worse than no number: it is read as the answer, by someone
    who clicked the task precisely because they did not want to work the version out.

    The remedy is not a fresher literal, which is the same defect with a later date on
    it. It is that the run says it -- `release-pipeline.py` resolves the version from
    `git tag` and prints it as its first line, and the task is a dry run by default, so
    the concrete number is one click away and cannot be stale. Anything else here that
    needs a version has the same option: read it, do not write it down.
    """
    pinned = [
        f"{where} = {text!r}" for where, text in _strings(canonical) if _RELEASE_TAG.search(text)
    ]
    assert not pinned, (
        "the workspace task block states a release version:\n  "
        + "\n  ".join(sorted(pinned))
        + "\nNothing bumps it when a release is cut. Let the script that reads `git tag` "
        "print it instead, or describe the move without the number."
    )


def test_the_sweep_has_no_workspace_task(canonical):
    """`sweep.py` is a CLI and an import, and nothing in the quick-pick calls it.

    There were five: two read-only reports and the three shipping steps. None had ever
    been run on the machine they were written for -- `log-wrap` writes `logs/<slug>.log`
    per run and nothing prunes that directory, and no `ship-*.log` was ever created --
    because every reader the sweep has is automatic now. `workspace-status.py` runs it
    at session start and prints the stranded-work line; `worktree.py reconcile` runs
    `--sync` every fifteen minutes and reports what it refused to park. A one-click
    duplicate of either is a second owner for one tier's lifecycle, and `--ship`'s
    sweep-shaped commit message lost to `/ship` per repo once an agent was a box away.

    So this is not "we removed some tasks" -- it is that the quick-pick is the wrong
    surface for this tool entirely. Re-adding one means naming which automatic reader
    it replaces, not just deleting this test. Nothing stops anyone typing
    `python scripts/sweep.py --branch --yes`, and the modes are covered by
    `tests/test_sweep.py` either way.
    """
    for task in canonical["tasks"]:
        args = [str(a) for a in task.get("args", [])]
        assert not any("sweep.py" in a for a in args), (
            f"{task['label']} puts sweep.py back in the quick-pick; the readers that "
            "replaced it are workspace-status.py and worktree.py reconcile"
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


def test_a_box_is_told_apart_from_a_checkout_by_the_directory_above_it():
    """What `needs_the_static_checkout` turns on. Both halves matter: a false positive
    turns this drift check off on the machine it is the only gate for, and a false
    negative is the Stop-gate dead end it exists to end."""
    boxes = Path("C:/ws") / worktree.BOXES_DIR_NAME
    assert in_an_ephemeral_box(boxes / "devkit--some-task-0824")
    assert not in_an_ephemeral_box(Path("C:/ws/devkit"))
    assert not in_an_ephemeral_box(boxes)  # the boxes directory is not itself a box


@needs_live_workspace
@needs_the_static_checkout
def test_the_live_workspace_matches_the_canonical_copy():
    """The check `--check-workspace` runs, as a test so devkit's own gate catches drift.

    Whole-file now, not the task block alone. `folders` had no gate of any kind before:
    `new-project.py` registers a checkout by editing the live file, so a generated
    project existed only in the copy with no history — and that list is what every
    sweep, status line and `--project` picker resolves against.

    Skipped from a box: the live file is rendered from the static checkout *after* a
    branch merges, so there the comparison asks whether an un-merged edit has already
    landed. See `support.needs_the_static_checkout` for why rendering early is not the
    way out of that.
    """
    text = LIVE_WORKSPACE.read_text(encoding="utf-8")
    problems = devkit_project.workspace_drift(
        devkit_jsonc_loads(text), devkit_jsonc_loads(devkit_project.canonical_text())
    )
    assert not problems, (
        "run `python scripts/devkit_project.py --render-workspace` (or --adopt-workspace "
        "to keep the live edits): " + "; ".join(problems)
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


# --- shipping what an autofix action rewrote ---------------------------------
#
# The reversion check for this block: revert `autofix_ship_plan` to "always ship" and
# three of these fail; revert it to "never ship" and the first one does.

WORKSPACE_FILE = "/ws/projects.code-workspace"


def plan(before=(), after=("app/main.py",), branch="master", lint_ok=True):
    return devkit_project.autofix_ship_plan(
        "alpha",
        branch,
        before,
        after,
        lint_ok=lint_ok,
        workspace=Path(WORKSPACE_FILE),
        slug="lint-autofix",
    )


def test_a_green_autofix_run_on_a_clean_home_branch_is_branched_then_shipped():
    outcome = plan()
    assert not outcome.note
    assert [step[-4:] for step in outcome.commands] == [
        ("--branch", "--slug", "lint-autofix", "--yes"),
        ("--only", "alpha", "--ship", "--yes"),
    ]
    for step in outcome.commands:
        assert step[1].endswith("sweep.py")
        assert "--only" in step and "alpha" in step
        assert str(Path(WORKSPACE_FILE)) in step


def test_the_branch_step_comes_first():
    """Order is load-bearing: `--ship` refuses a home branch, by its own design."""
    branch, ship = plan().commands
    assert "--branch" in branch and "--ship" not in branch
    assert "--ship" in ship and "--branch" not in ship


def test_a_run_that_rewrote_nothing_does_nothing_and_says_nothing():
    assert plan(before=("app/main.py",), after=("app/main.py",)) == devkit_project.AutofixOutcome()


def test_pre_existing_changes_are_not_swept_into_a_mechanical_pr():
    outcome = plan(before=("app/main.py",), after=("app/main.py", "app/util.py"))
    assert not outcome.commands
    assert "/ship" in outcome.note


def test_fixes_on_a_task_branch_ride_the_task_they_belong_to():
    outcome = plan(branch="agent/comic-book-ui-0819")
    assert not outcome.commands
    assert "agent/comic-book-ui-0819" in outcome.note


def test_a_run_that_still_reported_findings_is_not_shipped():
    outcome = plan(lint_ok=False)
    assert not outcome.commands
    assert "red gate" in outcome.note


def test_a_detached_head_is_declined_rather_than_branched():
    outcome = plan(branch="")
    assert not outcome.commands
    assert "detached" in outcome.note


@pytest.mark.parametrize(
    "kwargs",
    [
        {"before": ("app/main.py",), "after": ("app/main.py", "app/util.py")},
        {"branch": "agent/x-0819"},
        {"lint_ok": False},
        {"branch": ""},
    ],
)
def test_every_decline_names_the_churn_it_is_declining(kwargs):
    """A silent decline is how this churn went unnoticed in the first place."""
    outcome = plan(**kwargs)
    assert not outcome.commands
    assert "autofix rewrote" in outcome.note


def test_only_declared_generated_actions_rewrite_the_tree():
    """A new autofix action must be a deliberate entry here, not an inherited default."""
    assert {name for name, a in ACTIONS.items() if a.autofix} == {
        "lint",
        "lint-changed",
        "sync-codex",
    }


def test_generated_actions_get_distinct_branches_and_only_codex_opts_into_automerge():
    lint = ACTIONS["lint"]
    lint_changed = ACTIONS["lint-changed"]
    codex = ACTIONS["sync-codex"]

    assert lint.autofix_slug == "lint-autofix"
    assert codex.autofix_slug == "codex-context-sync"
    assert lint.autofix_labels == lint_changed.autofix_labels == ()
    assert codex.autofix_labels == (
        devkit_project.sweep.AUTOFIX_LABEL,
        devkit_project.sweep.AUTOMERGE_LABEL,
    )


def autofix_run(tmp_path, monkeypatch, argv, dirty=("app/main.py",), returncode=0):
    """Dispatch a lint action over one checkout, recording every command it runs."""
    workspace = tmp_path / "projects.code-workspace"
    workspace.write_text(json.dumps({"folders": [{"path": "alpha"}]}))
    scripts = tmp_path / "alpha" / "scripts"
    scripts.mkdir(parents=True)
    action_name = argv[-1]
    (tmp_path / "alpha" / ACTIONS[action_name].script).write_text("")

    calls = []
    snapshots = iter([("master", ()), ("master", dirty)])
    monkeypatch.setattr(devkit_project, "autofix_state", lambda _directory: next(snapshots))

    def fake_run(command, *, cwd, check):
        calls.append(command)
        return type("Result", (), {"returncode": returncode if len(calls) == 1 else 0})()

    monkeypatch.setattr(devkit_project.subprocess, "run", fake_run)
    code = devkit_project.main(["--workspace", str(workspace), *argv])
    return code, calls


def test_the_lint_task_hands_its_churn_to_the_sweep(tmp_path, monkeypatch):
    _, calls = autofix_run(tmp_path, monkeypatch, ["--project", "alpha", "lint"])
    assert len(calls) == 3, calls
    assert [c[-1] for c in calls[1:]] == ["--yes", "--yes"]
    assert all(c[1].endswith("sweep.py") for c in calls[1:])
    assert "--label" not in calls[-1], "lint's existing review policy changed"


def test_the_codex_sync_hands_its_churn_to_a_labelled_automerge_pr(tmp_path, monkeypatch):
    _, calls = autofix_run(
        tmp_path,
        monkeypatch,
        ["--project", "alpha", "sync-codex"],
        dirty=(".agents/skills/ship/SKILL.md",),
    )

    branch, ship = calls[1:]
    assert branch[-4:] == ("--branch", "--slug", "codex-context-sync", "--yes")
    assert ship[-6:] == (
        "--label",
        "autofix",
        "--label",
        "automerge",
        "--ship",
        "--yes",
    )


def test_no_ship_fixes_leaves_the_churn_in_the_working_tree(tmp_path, monkeypatch):
    _, calls = autofix_run(tmp_path, monkeypatch, ["--no-ship-fixes", "--project", "alpha", "lint"])
    assert len(calls) == 1, "the dispatcher shipped despite --no-ship-fixes"


def test_a_non_autofix_action_is_never_snapshotted(tmp_path, monkeypatch):
    """`autofix_state` shells out to git; a `test` run must not pay for that."""
    workspace = tmp_path / "projects.code-workspace"
    workspace.write_text(json.dumps({"folders": [{"path": "alpha"}]}))
    scripts = tmp_path / "alpha" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "run-tests.py").write_text("")

    def boom(_directory):
        raise AssertionError("a non-autofix action snapshotted the tree")

    monkeypatch.setattr(devkit_project, "autofix_state", boom)
    monkeypatch.setattr(
        devkit_project.subprocess,
        "run",
        lambda command, *, cwd, check: type("Result", (), {"returncode": 0})(),
    )
    assert devkit_project.main(["--workspace", str(workspace), "--project", "alpha", "test"]) == 0


def test_a_failed_sweep_step_stops_the_chain_and_reports(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "projects.code-workspace"
    workspace.write_text(json.dumps({"folders": [{"path": "alpha"}]}))
    scripts = tmp_path / "alpha" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "lint-all.py").write_text("")

    snapshots = iter([("master", ()), ("master", ("app/main.py",))])
    monkeypatch.setattr(devkit_project, "autofix_state", lambda _directory: next(snapshots))
    calls = []

    def fake_run(command, *, cwd, check):
        calls.append(command)
        # The lint run passes; the `--branch` step fails.
        return type("Result", (), {"returncode": 0 if len(calls) == 1 else 3})()

    monkeypatch.setattr(devkit_project.subprocess, "run", fake_run)
    code = devkit_project.main(["--workspace", str(workspace), "--project", "alpha", "lint"])

    assert code == 3
    assert len(calls) == 2, "the --ship step ran after --branch failed"
    assert "still in the working tree" in capsys.readouterr().err
