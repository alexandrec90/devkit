"""Tests for the shared-task dispatcher.

The behaviour worth pinning is the failure text: a task that runs in the wrong
directory, or against a project that does not implement the action, must say so by
name. A traceback from a missing file in an unexpected cwd is the outcome this
script exists to prevent.
"""

import json
import re
from pathlib import Path

import project_registry
import pytest
import support
from support import (
    LIVE_WORKSPACE,
    REPO_ROOT,
    devkit_jsonc,
    devkit_project,
    in_an_ephemeral_box,
    needs_live_workspace,
    worktree,
    worktree_tiers,
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

WORKSPACE = json.dumps({"folders": [{"path": "alpha"}, {"path": "beta"}]})

# The same registry plus a *reference* checkout — one registered to be read but never
# shipped from. `NOT_PROJECTS` is empty today, so every test below that exercises the
# exclusion has to put a name in it; that is the only way the mechanism gets covered at
# all, and the alternative is deleting the checks that guard it.
REFERENCE = "reference-checkout"
REFERENCE_WORKSPACE = json.dumps(
    {"folders": [{"path": "alpha"}, {"path": "beta"}, {"path": REFERENCE}]}
)


@pytest.fixture
def checkouts(tmp_path):
    """Two conforming-ish checkouts: alpha has both scripts, beta has neither."""
    alpha = tmp_path / "alpha"
    (alpha / "scripts").mkdir(parents=True)
    (alpha / "scripts" / "lint-all.py").write_text("")
    (alpha / "scripts" / "run-tests.py").write_text("")
    (tmp_path / "beta").mkdir()
    return tmp_path


@pytest.fixture(autouse=True)
def ledger_dir(tmp_path_factory, monkeypatch):
    """Point the harness-events ledger, and the dispatcher's kept failure copies, at scratch.

    A dispatch that cannot start files a ledger event and keeps its reason under `logs/`
    (`record_task_failure`), and a dozen tests here drive exactly that path -- so without
    this every run of the suite would append rows to this machine's real ledger, which
    `harness_triage.py` reads as defects an agent hit. Autouse for the reason
    `scripts/hooks/tests/conftest.py` gives its own copy: a diagnostic a test can quietly
    poison is one nobody can trust, and knowing to opt in is exactly what gets forgotten.
    """
    scratch = tmp_path_factory.mktemp("ledger")
    monkeypatch.setenv("DEVKIT_DIR", str(scratch))
    monkeypatch.setattr(devkit_project, "FAILURE_ROOT", scratch)
    return scratch


def registry_lists(monkeypatch, *names: str) -> None:
    """Make the project registry list `names`, as `projects.toml` would.

    The dispatcher reads its projects off `project_registry`, not the workspace file a
    test hands it with `--workspace` -- that flag now only says where the checkouts sit.
    """
    monkeypatch.setattr(project_registry, "all_projects", lambda *_args, **_kw: list(names))


def ledger_rows(root: Path) -> list[dict[str, str]]:
    """Every event under `root/logs/`, as `{field: value}` with `event` among them."""
    rows = []
    for shard in sorted((root / "logs").glob("harness-events*.log")):
        for line in shard.read_text(encoding="utf-8").splitlines():
            _stamp, *pairs = line.split("\t")
            rows.append(dict(pair.split("=", 1) for pair in pairs))
    return rows


# --- the registry -----------------------------------------------------------


def test_projects_come_from_the_project_registry(monkeypatch):
    registry_lists(monkeypatch, "alpha", "beta")
    assert known_projects() == ["alpha", "beta"]


def test_the_reference_checkout_is_not_a_project(monkeypatch):
    # A NOT_PROJECTS entry ships no harness; nothing in ACTIONS applies to it.
    monkeypatch.setattr(devkit_project, "NOT_PROJECTS", frozenset({REFERENCE}))
    registry_lists(monkeypatch, "alpha", "beta", REFERENCE)
    assert known_projects() == ["alpha", "beta"]


def test_workspace_text_still_names_the_projects_it_lists(monkeypatch):
    """The deprecated argument: the workspace writers verify their own edit with it, so
    it reads the text it is given, never the registry."""
    registry_lists(monkeypatch, "elsewhere")
    monkeypatch.setattr(devkit_project, "NOT_PROJECTS", frozenset({REFERENCE}))
    assert known_projects(WORKSPACE) == ["alpha", "beta"]
    assert known_projects(REFERENCE_WORKSPACE) == ["alpha", "beta"]


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
    registry_lists(monkeypatch, "alpha", "beta")
    for name in ("alpha", "beta"):
        scripts = tmp_path / name / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "run-tests.py").write_text("")

    calls = []

    def fake_run(command, *, cwd, check, env):
        calls.append((command, cwd, env))
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(devkit_project.subprocess, "run", fake_run)
    result = devkit_project.main(["--workspace", str(workspace), "--project", "beta,alpha", "test"])

    assert result == 0
    assert [cwd.name for _, cwd, _ in calls] == ["beta", "alpha"]
    assert all(env["PYTHONUTF8"] == "1" for _, _, env in calls)


def test_a_dispatched_task_runs_in_utf8_mode_down_to_its_grandchildren(tmp_path, monkeypatch):
    """Clicking *Agent: Fix What Is Red* printed `UnicodeDecodeError: 'charmap' codec
    can't decode byte 0x9d` from a reader thread: the pass's runners use `text=True` with
    no encoding, and only the watchdog's path ran it in UTF-8 mode. Asserted through the
    real wrappers, since the variable has to survive log-wrap to reach the script."""
    workspace = tmp_path / "projects.code-workspace"
    registry_lists(monkeypatch, "alpha")
    (tmp_path / "alpha" / "scripts").mkdir(parents=True)
    (tmp_path / "alpha" / "scripts" / "run-tests.py").write_text(
        "import pathlib, subprocess, sys\n"
        "out = subprocess.run([sys.executable, '-c', \"import sys; sys.stdout.buffer.write("
        "'\\u201d'.encode())\"], capture_output=True, text=True).stdout\n"
        "pathlib.Path('seen.txt').write_text(f'{sys.flags.utf8_mode} {ascii(out)}')\n",
        encoding="utf-8",
    )

    assert devkit_project.main(["--workspace", str(workspace), "--project", "alpha", "test"]) == 0
    assert (tmp_path / "alpha" / "seen.txt").read_text() == "1 '\\u201d'"


def test_the_task_env_keeps_the_callers_and_adds_utf8_mode():
    env = devkit_project.task_env({"PATH": "/bin", "PYTHONUTF8": "0"})
    assert env == {"PATH": "/bin", "PYTHONUTF8": "1"}
    assert devkit_project.task_env()["PYTHONUTF8"] == "1"


def test_registered_but_missing_directory_is_distinguished(checkouts):
    with pytest.raises(ProjectError, match="is registered but"):
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
    """An action's own arguments are part of what it is; the picker's come after.

    `lint-changed` used to pin this with a real entry. Lint's scope is a picker now, so
    the ordering is asserted against a stand-in rather than deleted along with the twin
    — `docker-up`'s `--build` and `backtest`'s subcommand both still depend on it.
    """
    scoped = devkit_project.Action("scripts/lint-all.py", "Lint: Run", ("--changed",))
    command = plan_command(scoped, checkouts / "alpha", ["--verbose"])
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
        "Lint: Run",
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
    assert command[:3] == ["python", "scripts/notify-wrap.py", "Lint: Run"]
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
    (devkit_root / "scripts" / "hook-tests.py").write_text("")
    command = plan_command(ACTIONS["test-hooks"], checkouts / "beta", [], devkit_root)
    assert inner(command) == ["python", str(devkit_root / "scripts" / "hook-tests.py")]


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
        plan_command(ACTIONS["test-hooks"], checkouts / "alpha", [], tmp_path / "empty")


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


def test_the_template_names_the_cli_contract_the_dispatcher_demands():
    """d02a3ddd: the template still owed `docker-up.py` and `docker-down.py`, which are
    DEVKIT-owned now and which photolab, correctly, has neither of. The comment is the
    only place a generated project reads its contract, so it is held to `ACTIONS`."""
    template = support.REPO_ROOT / "templates" / "core" / "dot-vscode" / "tasks.json.tmpl"
    text = template.read_text(encoding="utf-8")
    contract = text[text.index("the CLI CONTRACT") : text.index("`devkit_project.py --check`")]
    owed = {
        action.script
        for action in ACTIONS.values()
        if action.owner == devkit_project.PROJECT and not action.projects
    }
    assert set(re.findall(r"`(scripts/[\w-]+\.py)`", contract)) == owed


def test_the_generated_instructions_say_how_a_task_runs_a_script(checkouts, tmp_path):
    """f33fd4ed: photolab's `scripts/album.py` was written importing `dotenv` and relying
    on Ctrl+C, then rewritten once a task was asked for, since its `CLAUDE.md` said neither
    where tasks live nor what a dispatched script starts as. This holds those words to
    the argv that is their whole basis, and `log-wrap.py`'s windowless spawn."""
    devkit_root = tmp_path / "devkit"
    (devkit_root / "scripts").mkdir(parents=True)
    command = plan_command(ACTIONS["test"], checkouts / "alpha", [], devkit_root)
    assert command[-2:] == ["python", "scripts/run-tests.py"], "not the machine's `python`"
    log_wrap = (support.REPO_ROOT / "scripts" / "log-wrap.py").read_text(encoding="utf-8")
    assert "creationflags=NO_WINDOW" in log_wrap

    core = support.REPO_ROOT / "templates" / "core"
    for name in ("CLAUDE.md.tmpl", "dot-vscode/tasks.json.tmpl"):
        said = " ".join((core / name).read_text(encoding="utf-8").replace("//", " ").split())
        assert "under the machine's `python`, not" in said, name
        assert "no console" in said and "Ctrl+C" in said, name
    claude = (core / "CLAUDE.md.tmpl").read_text(encoding="utf-8")
    assert "A task lives in the workspace file" in claude


def test_the_generated_instructions_carry_no_vs_code_ui_conventions():
    """A generated `CLAUDE.md` is loaded into every session in that project, and its
    sessions drive scripts from a terminal, never VS Code's quick-pick. Task-UI settings
    (`"type": "process"`, `presentation.close`, the toast wrapper, label and `detail`
    conventions) were always-loaded context no such session could act on."""
    claude = (support.REPO_ROOT / "templates" / "core" / "CLAUDE.md.tmpl").read_text(
        encoding="utf-8"
    )
    for phrase in (
        "### VS Code tasks",
        '"type": "process"',
        '"close": false',
        "notify-wrap.py",
        "quick-pick",
    ):
        assert phrase not in claude, phrase


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
        # Born scoped, for `reclaim`'s reason exactly: the scheduled jobs are machine
        # state with no project dimension, so an unscoped action would run the same pass
        # once per ticked checkout and report nothing on runs 2..N. It is deliberately not
        # the thing that keeps the jobs current -- `devkit-installers` does that at logon
        # -- so what the click is for is the two decisions a schedule cannot make: seeing
        # the set, and taking it back off the machine.
        "installers",
        # Born scoped, for `reclaim`'s reason: which collectors run is this machine's
        # assignment, so once per ticked checkout would assign -- and start -- the same
        # collectors N times. The picker names the collector instead.
        "collectors",
        # The read-only page beside `reclaim`, scoped for exactly its reason: one process
        # table on the machine, so once per checkout would print the same page N times.
        "memory-inventory",
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
        # Born scoped, and the same kind as `preview-ui-host` above: one scan across
        # every checkout in the registry, so the checkout is a column in the menu rather
        # than a scope for the task. Run per selected checkout it would draw the same
        # cross-checkout dropdown two or three times over. The dispatch pins
        # `--project devkit` and asks which PR instead.
        "fix-pass",
        # Born scoped, and the same kind again: one scan across every checkout, so the
        # checkout is the first of the nested pickers rather than a scope for the task.
        # The pair exists because `claude --worktree` has no Codex counterpart -- see
        # `test_the_box_tier_keeps_one_task_and_it_is_read_only`.
        "worktree-new",
        "worktree-remove",
        # Born scoped, and the third of the no-project-dimension kind -- but for a
        # sharper reason than `reclaim`'s: repeating a release is not merely a no-op on
        # runs 2..N, it is a failure. The second run would find the tag it just pushed
        # and refuse, so a two-checkout pick would report a red task for a release that
        # actually succeeded. Scoped, the task pins `--project devkit`.
        "release",
        # Born scoped, by capability: only social-scraper has a browser profile to sign
        # in to, and its `scripts/login.py` is the CLI contract.
        "scraper-login",
        # Born scoped, by capability: only ibkr_trader has backtest runs, an MLflow store
        # and a strategy line-up to report on. `scripts/report-task.py` is the contract.
        "report",
        "strategy-lab",
        # Born scoped, by capability: only ibkr_trader holds Questrade accounts and the
        # forward snapshots a plan reads. `scripts/questrade-task.py` is the contract.
        "questrade-plan",
        # Born scoped, by capability: only ibkr_trader ships the web UI.
        # `scripts/ui-task.py` is the contract.
        "trader-ui",
        # Born scoped, by capability: only photolab ships the album.
        # `scripts/album.py` is the contract.
        "album",
    }


def test_the_scraper_login_runs_the_projects_script_with_the_platform(tmp_path):
    """The platform is the task's trailing argument, so the dispatch must carry it through
    to `scripts/login.py` untouched -- `x` is the only thing telling the script which
    profile under `data/browser-profiles/` to sign in to."""
    action = ACTIONS["scraper-login"]
    assert action.projects == devkit_project.SOCIAL_SCRAPER == ("social-scraper",)
    assert action.owner == devkit_project.PROJECT
    assert "scraper-login" not in expected_actions("devkit")

    checkout = tmp_path / "social-scraper"
    (checkout / "scripts").mkdir(parents=True)
    (checkout / "scripts" / "login.py").write_text("", encoding="utf-8")
    argv = plan_command(action, checkout, ["x"])
    assert argv[-3:] == ["python", "scripts/login.py", "x"]


def test_the_scraper_login_names_its_missing_script(tmp_path):
    """A checkout without the script is a conformance failure by name, not a traceback."""
    with pytest.raises(ProjectError, match=r"scripts/login\.py"):
        plan_command(ACTIONS["scraper-login"], tmp_path, ["x"])


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


def test_the_report_pair_shares_a_script_and_only_the_lab_fixes_a_subcommand():
    """The views are one action because the picked view rides in as the trailing argument;
    the lab is a second action because its cost and its two extra questions are what a
    picker cannot carry back."""
    assert ACTIONS["report"].script == ACTIONS["strategy-lab"].script == "scripts/report-task.py"
    assert ACTIONS["report"].args == ()
    assert ACTIONS["strategy-lab"].args == ("lab",)
    for key in ("report", "strategy-lab"):
        assert ACTIONS[key].projects == IBKR
        assert ACTIONS[key].owner == devkit_project.PROJECT
        assert key not in expected_actions("devkit")


@pytest.mark.parametrize("view", ["results", "factor", "models", "summary"])
def test_every_report_view_reaches_the_script_as_its_subcommand(tmp_path, canonical, view):
    """Each picker value is a subcommand of `report-task.py`, passed through untouched."""
    inputs = {spec["id"]: spec for spec in canonical["inputs"]}
    assert view in _picker_values(inputs["reportView"])

    checkout = tmp_path / "ibkr_trader"
    (checkout / "scripts").mkdir(parents=True)
    (checkout / "scripts" / "report-task.py").write_text("", encoding="utf-8")
    argv = plan_command(ACTIONS["report"], checkout, [view])
    assert argv[-3:] == ["python", "scripts/report-task.py", view]


def test_the_report_view_picker_offers_exactly_the_scripts_views(canonical):
    """A row the script has no subcommand for fails in a terminal after the checkout
    question; a view the script grew and the picker lacks is unreachable by click."""
    inputs = {spec["id"]: spec for spec in canonical["inputs"]}
    assert _picker_values(inputs["reportView"]) == {"results", "factor", "models", "summary"}


def test_the_strategy_lab_carries_both_of_its_required_flags(tmp_path, canonical):
    """`report-task.py lab` requires `--universe` and `--account`, so the task must spell
    both, each from a picker that always yields a real token."""
    task = next(t for t in canonical["tasks"] if t["label"] == "Report: Run Strategy Lab")
    args = [str(a) for a in task["args"]]
    assert args[args.index("strategy-lab") + 1 :] == [
        "--universe",
        "${input:labUniverse}",
        "--account",
        "${input:btAccount}",
    ]
    inputs = {spec["id"]: spec for spec in canonical["inputs"]}
    universes = _picker_values(inputs["labUniverse"])
    assert universes == {"sp500", "tickers-etfs.txt", "tickers.txt"}
    assert "" not in _picker_values(inputs["btAccount"])

    checkout = tmp_path / "ibkr_trader"
    (checkout / "scripts").mkdir(parents=True)
    (checkout / "scripts" / "report-task.py").write_text("", encoding="utf-8")
    tail = ["--universe", "sp500", "--account", "tfsa"]
    argv = plan_command(ACTIONS["strategy-lab"], checkout, tail)
    assert argv[-7:] == ["python", "scripts/report-task.py", "lab", *tail]


def test_the_report_actions_name_their_missing_script(tmp_path):
    """An ibkr_trader checkout that predates the script is a conformance failure by name."""
    for key, extra in (("report", ["results"]), ("strategy-lab", [])):
        with pytest.raises(ProjectError, match=r"scripts/report-task\.py"):
            plan_command(ACTIONS[key], tmp_path, extra)


def test_the_questrade_plan_is_ibkr_scoped_and_fixes_the_plan_subcommand():
    """The row is the `plan` subcommand alone; `check` is reachable from the CLI, and a
    menu row for it would be a click standing in for a schedule."""
    action = ACTIONS["questrade-plan"]
    assert action.script == "scripts/questrade-task.py"
    assert action.args == ("plan",)
    assert action.projects == IBKR
    assert action.owner == devkit_project.PROJECT
    assert "questrade-plan" not in expected_actions("devkit")
    assert not any(a.script == action.script and a.args == ("check",) for a in ACTIONS.values())


def test_the_questrade_plan_carries_its_strategy_flag(tmp_path, canonical):
    """`questrade-task.py plan` requires `--strategy`, so the task spells it from a picker
    that always yields a real token."""
    task = next(t for t in canonical["tasks"] if t["label"] == "Questrade: Recommend Orders")
    args = [str(a) for a in task["args"]]
    assert args[args.index("questrade-plan") + 1 :] == ["--strategy", "${input:questradeStrategy}"]
    assert args[args.index("--project") + 1] == "${input:ibkrCheckout}"

    checkout = tmp_path / "ibkr_trader"
    (checkout / "scripts").mkdir(parents=True)
    (checkout / "scripts" / "questrade-task.py").write_text("", encoding="utf-8")
    tail = ["--strategy", "core_satellite"]
    argv = plan_command(ACTIONS["questrade-plan"], checkout, tail)
    assert argv[-5:] == ["python", "scripts/questrade-task.py", "plan", *tail]


def test_the_questrade_strategy_picker_offers_the_whole_registry(canonical):
    """ibkr_trader's `signals.portfolio.available()`, defaulting to `core_satellite`. A row
    the registry lacks fails after the checkout question; a strategy the registry grew and
    the picker lacks is unreachable by click."""
    spec = next(i for i in canonical["inputs"] if i["id"] == "questradeStrategy")
    assert _picker_values(spec) == {
        "core_satellite",
        "couch_potato",
        "recent_momentum",
        "steady_compounders",
        "ml_lt_ridge",
        "ml_lt",
        "momentum_lt",
        "equal_weight",
        "buy_and_hold",
    }
    assert spec["default"] == "core_satellite"


def test_the_questrade_plan_names_its_missing_script(tmp_path):
    """An ibkr_trader checkout that predates the script is a conformance failure by name."""
    with pytest.raises(ProjectError, match=r"scripts/questrade-task\.py"):
        plan_command(ACTIONS["questrade-plan"], tmp_path, ["--strategy", "core_satellite"])


def test_the_trader_ui_is_ibkr_scoped_and_runs_the_script_bare(tmp_path):
    """`ui-task.py` takes no subcommand: its port defaults inside the checkout's .env, so
    the dispatch adds nothing between the interpreter and the script."""
    action = ACTIONS["trader-ui"]
    assert action.script == "scripts/ui-task.py"
    assert action.args == ()
    assert action.projects == IBKR
    assert action.owner == devkit_project.PROJECT
    assert "trader-ui" not in expected_actions("devkit")

    checkout = tmp_path / "ibkr_trader"
    (checkout / "scripts").mkdir(parents=True)
    (checkout / "scripts" / "ui-task.py").write_text("", encoding="utf-8")
    argv = plan_command(action, checkout, [])
    assert argv[-2:] == ["python", "scripts/ui-task.py"]


def test_the_trader_ui_task_names_its_checkout_rather_than_asking(canonical):
    """The server binds one port, so a multi-pick would start the second checkout's UI
    only after the first's terminal closed -- or onto a port already taken. A literal
    `--project` asks nothing; the literal-scope test above holds it to the action."""
    task = next(t for t in canonical["tasks"] if t["label"] == "IBKR: Open Trader UI")
    args = [str(a) for a in task["args"]]
    assert args[args.index("--project") + 1 :] == ["ibkr_trader", "trader-ui"]


def test_the_trader_ui_names_its_missing_script(tmp_path):
    """A checkout without the UI branch merged is a conformance failure by name."""
    with pytest.raises(ProjectError, match=r"scripts/ui-task\.py"):
        plan_command(ACTIONS["trader-ui"], tmp_path, [])


def test_the_album_is_photolab_scoped_and_runs_the_script_bare(tmp_path):
    """`album.py` reads its ports from the checkout's .env, so the dispatch adds nothing
    between the interpreter and the script, and only photolab owes the script."""
    action = ACTIONS["album"]
    assert action.script == "scripts/album.py"
    assert action.label == "Photolab: Open Album"
    assert action.args == ()
    assert action.projects == devkit_project.PHOTOLAB == ("photolab",)
    assert action.owner == devkit_project.PROJECT
    assert "album" in expected_actions("photolab")
    assert "album" not in expected_actions("devkit")

    checkout = tmp_path / "photolab"
    (checkout / "scripts").mkdir(parents=True)
    (checkout / "scripts" / "album.py").write_text("", encoding="utf-8")
    argv = plan_command(action, checkout, [])
    assert argv[-2:] == ["python", "scripts/album.py"]


def test_the_album_task_names_its_checkout_rather_than_asking(canonical):
    """The album binds fixed ports, so a second checkout could only collide with the first.
    A literal `--project` asks nothing; the literal-scope test holds it to the action."""
    task = next(t for t in canonical["tasks"] if t["label"] == "Photolab: Open Album")
    args = [str(a) for a in task["args"]]
    assert args[args.index("--project") + 1 :] == ["photolab", "album"]


def test_the_album_names_its_missing_script(tmp_path):
    """A checkout without `album.py` is a conformance failure by name."""
    with pytest.raises(ProjectError, match=r"scripts/album\.py"):
        plan_command(ACTIONS["album"], tmp_path, [])


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
# One task replaced six: `Test: Run Suite`, `Test: Run Carameli Target`, both browser
# E2E tasks, the free hook-test run, and the paid live-CLI smoke. `TEST_KINDS` is the
# table it spends -- a row is an (action, argument) pair, so every row reaches only
# actions `ACTIONS` already defines, scopes and wraps for logging.

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
    registry_lists(monkeypatch, *projects)
    for name in projects:
        (tmp_path / name / "scripts").mkdir(parents=True)
        for script in scripts:
            (tmp_path / name / "scripts" / script).write_text("")

    calls = []

    def fake_run(command, *, cwd, check, env):
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
    registry_lists(monkeypatch, "alpha")
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
    assert {"test", "lint"} <= expected_actions("devkit")


def test_conformance_reports_per_project_support(checkouts):
    report = conformance(["alpha", "beta"], checkouts)
    assert set(report["alpha"]) == {"lint", "test"}
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
\t\t\t"name": "reference-checkout (reference)",
\t\t\t"path": "reference-checkout"
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
                {"id": "mergeCheckout", "options": ["alpha"]},
                {"id": "adoptProjects", "options": ["alpha"]}
            ]
        }
    }"""
    updated = devkit_jsonc_loads(insert_picker_option(text, "beta"))
    for picker in updated["tasks"]["inputs"]:
        assert picker["options"] == ["alpha", "beta"]


def test_registration_preserves_comments():
    """A json.dumps round-trip would delete these; the folder list would lose the only
    place it explains what a reference checkout is and why sweep depends on the list."""
    updated = register(COMMENTED, ["newproj"])
    assert "sweep.py reads this as the project registry" in updated
    assert "MAINTAINED BY new-project.py" in updated


def test_reference_checkouts_stay_last(monkeypatch):
    """`NOT_PROJECTS` is empty, so `insert_folder` appends and the rule never fires on
    the live registry. It is still the rule the moment a reference checkout comes back,
    which is exactly when nobody will re-derive it — so cover it with one in the set."""
    monkeypatch.setattr(devkit_project, "NOT_PROJECTS", frozenset({REFERENCE}))
    updated = register(COMMENTED, ["newproj"])
    paths = [f["path"] for f in devkit_jsonc_loads(updated)["folders"]]
    assert paths == ["carameli", "newproj", REFERENCE]


def test_registering_a_project_and_its_worktree():
    updated = register(COMMENTED, ["newproj", "newproj-b"])
    assert devkit_project.known_projects(updated) == [
        "carameli",
        REFERENCE,
        "newproj",
        "newproj-b",
    ]


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
        "mergeCheckout",
        "adoptProjects",
    ):
        assert _input_options(inputs[picker_id])[-2:] == ["probe", "probe-b"]
    # No reference checkout is registered today, so the new pair simply lands last.
    assert [f["path"] for f in devkit_jsonc_loads(updated)["folders"]][-2:] == [
        "probe",
        "probe-b",
    ]


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
                {"id": "mergeCheckout", "options": ["alpha", "beta"], "default": "alpha"},
                {"id": "adoptProjects", "options": ["alpha", "beta"], "default": "alpha"}
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
    """`mergeCheckout` lists more than the registry, `adoptProjects` lists less, and an
    older workspace file may carry fewer pickers, so "not there" is the same outcome as
    "removed"."""
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
    for picker_id in (
        "project",
        "daemonProject",
        "mergeCheckout",
        "adoptProjects",
    ):
        options = _input_options(inputs[picker_id])
        assert victim not in options
        default = inputs[picker_id].get("default")
        assert default is None or default in options
    # Only the picked project leaves; every other folder survives the edit.
    assert (
        len(devkit_jsonc_loads(updated)["folders"]) == len(devkit_jsonc_loads(text)["folders"]) - 1
    )


@needs_live_workspace
def test_the_real_workspace_file_round_trips():
    text = LIVE_WORKSPACE.read_text(encoding="utf-8")
    assert unregister(register(text, ["probe"]), ["probe"]) == text


# --- the canonical task block ------------------------------------------------

workspace_tasks = devkit_project.workspace_tasks


@pytest.fixture
def canonical():
    return devkit_project.canonical_tasks()


def test_the_canonical_block_exists_and_parses(canonical):
    assert canonical["version"] == "2.0.0"
    assert canonical["tasks"], "the canonical task block defines no tasks"


def test_list_answers_on_a_machine_with_no_workspace_file(tmp_path, monkeypatch, capsys):
    """The dispatcher's verbs read the project registry, never the workspace file, so
    `--list` answers where there is none at all."""
    registry_lists(monkeypatch, "alpha", "beta")
    missing = tmp_path / "alex-projects.code-workspace"
    assert devkit_project.main(["--workspace", str(missing), "--list"]) == 0
    assert capsys.readouterr().out.split() == ["alpha", "beta"]


def test_the_workspace_file_verbs_are_gone(capsys):
    """VS Code is retired: nothing renders, adopts or checks the workspace file now, so
    the flags are a usage error rather than a silent no-op someone mistakes for a run."""
    for flag in ("--render-workspace", "--check-workspace", "--adopt-workspace"):
        with pytest.raises(SystemExit) as exited:
            devkit_project.main([flag])
        assert exited.value.code == 2, flag
    capsys.readouterr()


def test_a_picker_may_be_emptied_but_the_folders_list_may_not():
    """A picker with no options is honest -- nothing on this PC to adopt a release into --
    while a workspace with no folder is not a workspace at all."""
    text = '{"tasks": {"inputs": [{"id": "project", "options": ["alpha"]}]}}'
    assert (
        devkit_jsonc_loads(remove_picker_option(text, "alpha"))["tasks"]["inputs"][0]["options"]
        == []
    )
    with pytest.raises(RegistryEditError, match="only element"):
        remove_folder('{"folders": [{"path": "alpha"}]}', "alpha")


def test_a_nested_picker_is_asked_for_in_one_json_option_field_only(canonical):
    """`${pickStringRemember:...}` may appear in ONE `jsonOption` field per input.

    The extension substitutes each template string of `jsonOption` in turn, and every
    occurrence of such a variable opens its own quick-pick. Spelled in all four fields,
    the nested project dropdown appeared four times per run before the list it narrowed
    showed. `value` must resolve first — insertion order is evaluation order — storing
    the answer under the nested pick's `key`, and the other fields read it back with
    `${remember:<key>}`, which never prompts.

    Written over every input rather than over the one that had the bug: the inputs that
    had it are live `shellCommand.execute` pickers now and nest nothing, so naming one
    would leave the trap covered only while somebody remembered to re-aim this. It
    passes vacuously today, which is the correct state for a ratchet on a shape nothing
    currently uses.
    """
    for spec in canonical["inputs"]:
        template = (spec.get("args") or {}).get("jsonOption")
        if not isinstance(template, dict):
            continue
        prompting = [f for f, expr in template.items() if "${pickStringRemember:" in str(expr)]
        if not prompting:
            continue
        assert next(iter(template)) == "value", (
            f"{spec['id']}: the prompting field must be evaluated first"
        )
        assert prompting == ["value"], (
            f"{spec['id']}: fields that would each open their own pick: {prompting}"
        )


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


def test_the_lint_task_asks_for_its_scope_rather_than_splitting_in_two(canonical):
    """One task and a dropdown — the shape `Test: Run Suite` has always had.

    Lint was two adjacent entries whose labels differed only in scope, each needing its
    own icon, its own detail and — in the dispatcher — its own `lint-changed` action for
    one flag. A scope that fits in a picker is a picker: the list gets shorter, and the
    second question is asked only after the first has been answered.
    """
    lint = [task for task in canonical["tasks"] if task["label"].startswith("Lint:")]
    assert [task["label"] for task in lint] == ["Lint: Run"]
    assert [str(a) for a in lint[0]["args"]][-2:] == ["lint", "${input:lintScope}"]

    scope = next(i for i in canonical["inputs"] if i["id"] == "lintScope")
    assert [option["value"] for option in scope["options"]] == ["", "--changed"]
    # The wide scope is the default: lint rewrites what it reads, so the click that most
    # often precedes a commit must not be the one that leaves half the tree unformatted.
    assert scope["default"] == ""


@pytest.mark.parametrize("picked, expected", [("", []), ("--changed", ["--changed"])])
def test_both_lint_scopes_reach_the_one_action(checkouts, picked, expected):
    """The picker's two branches are one action plus a token — the empty one being
    dropped by `plan_command` is what lets the wide branch pass nothing at all."""
    command = plan_command(ACTIONS["lint"], checkouts / "alpha", [picked])
    assert inner(command) == ["python", "scripts/lint-all.py", *expected]


# Tasks that deliberately run outside `log-wrap.py`, each with its reason -- and empty,
# because the wrapper is no longer only the artifact: it is what files a failed task on
# the harness-events ledger, which the owner's rule says every routine task must reach.
# So "its output is a window, there is nothing to read" stopped being a reason: the run
# can still fail (no `wt.exe`, a refused registry edit), and that failure is what is
# filed. Three tasks were listed and none is now. `Agents: Resume Recent Sessions` and
# `Workspace: Plug / Unplug Projects` are wrapped -- neither reads stdin on its task
# path, which is the one thing a pipe cannot carry -- and `IBKR: Open Gateway VNC
# Viewer` had been a dispatch, which `plan_command` wraps, all along.
#
# A task that genuinely cannot be piped (one that must prompt mid-run in a way the
# whole-line rule in `.claude/rules/vscode-tasks.md` cannot serve) goes here, and the
# reason has to say how its failure reaches the ledger instead.
UNLOGGED_TASKS: dict[str, str] = {}


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


def test_every_unlogged_exception_still_runs_unwrapped(canonical):
    """The other way an exemption goes stale: the task is brought back into line and the
    entry stays, licensing a future deviation nobody argued for. `IBKR: Open Gateway VNC
    Viewer` was exempt while being a dispatch, which `plan_command` wraps -- the list
    said a task wrote no artifact when it did. Same ratchet `CONTRACT_EXCEPTIONS` holds."""
    for exempt, reason in UNLOGGED_TASKS.items():
        assert reason, f"{exempt} is exempt with no reason"
        for task in canonical["tasks"]:
            if exempt not in task["label"]:
                continue
            args = [str(a) for a in task.get("args", ())]
            assert not (args and args[0].endswith("devkit_project.py")), (
                f"{task['label']} is a dispatch, which plan_command wraps; drop its exemption"
            )
            assert not any("log-wrap.py" in a for a in args), (
                f"{task['label']} runs inside log-wrap.py now; drop its exemption"
            )


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
#
# `runOptions` is the one that destroyed work. VS Code's defaults are `instanceLimit: 1`
# and `instancePolicy: "prompt"`, so clicking a task that is still running offers to
# terminate it, and a second *Project: New from devkit* killed the first mid-push: an
# empty GitHub repo and a scaffold with no `.git`. `panel: "new"` does not prevent it.
# The limit lets a second run start beside the first, and `warn` means that past the
# limit the new run is refused with a warning rather than the old one being killed.
TASK_CONTRACT = {
    "type": "process",  # VS Code watches the process, so the exit-code icon is real
    "presentation.panel": "new",  # one run, one terminal; nothing is overwritten
    "presentation.close": False,  # the terminal stays open for review
    "presentation.reveal": "always",  # a task you clicked shows you what it did
    "runOptions.instanceLimit": 4,  # a second click runs beside the first
    "runOptions.instancePolicy": "warn",  # past the limit, refuse the new run, kill nothing
}

# Tasks that deliberately finish without a toast, for the same reason `UNLOGGED_TASKS`
# exists: a toast reports that something you were not watching has ended, and these
# either end instantly or hand you a window that is itself the notification.
UNTOASTED_TASKS = {
    "Agents: Resume Recent Sessions": "the tabs it reopens are the notification",
    "Workspace: List Tasks as a Table": "same — the table is the output, in front of you",
}

# Deviations, each with the reason it is one. A new task does not belong here: this is
# for the handful whose *output is not in their terminal at all*.
CONTRACT_EXCEPTIONS = {
    ("Agent: Sync Codex Context", "presentation.reveal"): (
        "silent: a context sync that prints nothing worth stealing focus for"
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


def test_a_literal_checkout_dispatch_agrees_with_its_actions_scope(canonical):
    """The test above skips a task whose `--project` is a literal rather than a picker,
    which is what a task does when its action is defined for one checkout: a picker of
    length one asks a question with no second answer. That trade is only safe while the
    literal and the scope agree — so this is the same assertion, made against the
    constant instead of against an option list.

    "Test: Harness Hook Tests — paid, live CLI" used to be the case this was written
    for, and it was named in the test's own name; it is two rows on the test menu now,
    and "Machine: Reclaim Resources" is what still spells a checkout out. That the
    rename was the only work the fold cost here is the paragraph below being true.

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
# They gated a picker that chooses WHICH CHECKOUTS a workspace-scoped batch task should
# act on -- `sweepScope`, then `upgradeScope`, both deleted, and for a while there was
# no such picker left to gate. The reason they went is worth keeping and is narrower
# than the sentence that used to stand here: each one's option list was a HAND-KEPT
# second copy of the project registry, `register()` never wrote to it, and so a newly
# generated project could run every generic task while `--all` was the only way to
# sweep or upgrade it. Every copy of that list has drifted at least once.
#
# The comment also used to argue the question itself was worthless -- that adopting a
# release in a subset of consumers is not an operation anyone wants. That was wrong,
# and `adoptProjects` is the correction: choosing four consumers of five is precisely
# what a human is at the dropdown for, and `--all` made it a terminal command instead.
# What that picker does NOT do is keep a second copy -- `insert_picker_option` names it
# alongside `project`, so registration reaches it -- which is why it is allowed where
# its two ancestors were not.
#
# The ratchet's guarantee is spread across three checks now rather than one:
# `test_picker_registration_updates_the_multi_test_picker_too` covers the registration
# side, the tail of `test_project_scope_inputs_are_real_multi_picks` requires
# `daemonProject` to reach every checkout `project` knows, and
# `test_the_adoption_picker_is_every_consumer_of_a_release` pins `adoptProjects` to the
# registry minus devkit in both directions. Add a scope picker of your own and it needs
# the third of those, written for its own option list: the failure they were all
# written for is a newly generated project that every generic task can reach and no
# batch task can.


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
        "adoptProjects",
    ):
        spec = inputs[picker_id]
        assert spec["type"] == "command"
        assert spec["args"]["multiPick"] is True
        assert spec["args"]["optionGroups"][0]["minCount"] == 1

    # The single-pick one is single-pick on purpose -- there is one Docker daemon -- but
    # it still has to reach every checkout the registry knows. That is the half a
    # per-picker option list keeps losing: `project` gains the new project because
    # `register()` writes it, and a hand-maintained sibling does not. `worktreeProject`
    # was the second of these until the `Worktree: New Box` task it fed was retired --
    # `test_the_box_tier_keeps_one_task_and_it_is_read_only` says why.
    for picker_id in ("daemonProject",):
        assert inputs[picker_id]["type"] == "pickString"
        assert _picker_values(inputs[picker_id]) == _picker_values(inputs["project"])


# What a task spells to receive a live checkout stage's answer. The flag IS the
# contract between the two: a live stage's rows are `<checkout>@<scan token>` values
# with no option list to read, so there is nothing else about the input that says what
# it asks. `scripts/picker_scan.py` owns the format behind it.
CHECKOUT_FLAG = "--checkouts="


def _checkout_pickers(canonical: dict) -> set[str]:
    """Input ids that ask which checkout, derived rather than listed.

    Two spellings, both derived rather than rostered — a hand-kept list is the second
    copy this file has already retired twice (`sweepScope`, `upgradeScope`).

    An input whose whole OPTION list is checkout names is one, whichever extension
    draws it, so that half reads the registry. A live `shellCommand.execute` stage has
    no options to read, so that half reads the `--checkouts=` flag a task spells to
    receive it: the flag is the only thing that distinguishes a checkout stage from any
    other live picker, and it is exactly what the receiving script parses.
    """
    projects = set(known_projects(devkit_project.canonical_text()))
    found = set()
    for spec in canonical["inputs"]:
        try:
            values = _picker_values(spec)
        except (KeyError, IndexError, TypeError):
            continue
        if values and values <= projects:
            found.add(spec["id"])
    for task in canonical["tasks"]:
        for arg in (str(a) for a in task.get("args", ())):
            if arg.startswith(CHECKOUT_FLAG):
                found.update(re.findall(r"\$\{input:([^}]+)\}", arg))
    return found


def test_no_task_detail_promises_a_checkout_question_it_does_not_ask(canonical):
    """A `detail` may not say it asks which checkout unless a checkout picker feeds it.

    Four tasks said it while asking nothing, for two releases. `Preview: Open a UI
    Branch`, `Agent: Fix What Is Red`, `Agent: New Worktree` and `Agent: Delete
    Worktrees` each dropped their checkout stage when their list went live — on the
    since-corrected belief that a live picker could not have one — and every one of them
    kept the "Asks which checkout, then which of its ..." opening the two-stage version
    had. The quick-pick is the one surface with no README, so a second line promising a
    question the click does not ask is the whole cost, and it was reported as a
    regression by the person clicking it.

    The stage is back, so this now passes the other way round; it stays because the
    claim and the picker are still two separate edits, and either can move alone.

    One direction only. Plenty of tasks ask and phrase it some other way; what is not
    allowed is claiming the question when there is nothing to answer it.
    """
    pickers = _checkout_pickers(canonical)
    assert pickers, "no checkout picker was derived; the heuristic has stopped matching"
    for task in canonical["tasks"]:
        args = " ".join(str(a) for a in task.get("args", ()))
        if any(f"${{input:{picker}}}" in args for picker in pickers):
            continue
        assert "which checkout" not in task["detail"].lower(), (
            f"{task['label']} says it asks which checkout, but no checkout picker feeds it"
        )


def test_a_dependent_picker_resolves_after_the_stage_it_depends_on(canonical):
    """A task's checkout stage must appear before the input whose command reads it.

    The one documented limitation of `augustocdias.tasks-shell-input`: it substitutes
    `${input:<id>}` inside a later input's command from the value it recorded when that
    input LAST resolved, and the resolution order is the left-to-right order the inputs
    appear in the task's own arguments. Put the row stage first and it does not fail —
    it filters by the checkouts of some EARLIER click, which is a wrong list drawn
    confidently, and on `Agent: Delete Worktrees` a wrong list of things to destroy.

    So the ordering is asserted here rather than left to whoever next reorders an
    argument list for tidiness. The receiving scripts refuse a pick from a checkout the
    first stage did not return, which is the runtime half of the same guard; this is
    the half that fails before anybody clicks.
    """
    dependents = {
        spec["id"]: re.findall(r"\$\{input:([^}]+)\}", json.dumps(spec.get("args", {})))
        for spec in canonical["inputs"]
    }
    checked = 0
    for task in canonical["tasks"]:
        order = [
            found
            for arg in (str(a) for a in task.get("args", ()))
            for found in re.findall(r"\$\{input:([^}]+)\}", arg)
        ]
        for position, input_id in enumerate(order):
            for needed in dependents.get(input_id, ()):
                assert needed in order, (
                    f"{task['label']}: `{input_id}` reads `{needed}`, which the task never "
                    "resolves — the extension would substitute an earlier click's value"
                )
                assert order.index(needed) < position, (
                    f"{task['label']}: `{needed}` must appear before `{input_id}` in the "
                    "arguments; the extension resolves them left to right"
                )
                checked += 1
    assert checked, "no dependent picker was found; this test has stopped covering anything"


def test_every_dependent_picker_is_a_shell_command_one(canonical):
    """The extension's other documented limitation, and it is not the ordering.

    `${input:...}` inside an input's command resolves only against inputs
    `shellCommand.execute` itself drew — that is the only place it records an answer.
    A `pickString` or a `pickStringRemember` in that position substitutes empty, which
    reaches the script as an unfiltered list rather than as an error.
    """
    inputs = {spec["id"]: spec for spec in canonical["inputs"]}
    for spec in canonical["inputs"]:
        for needed in re.findall(r"\$\{input:([^}]+)\}", json.dumps(spec.get("args", {}))):
            assert inputs[needed]["command"] == "shellCommand.execute", (
                f"`{spec['id']}` reads `{needed}`, which is not a shellCommand.execute "
                "input — the extension records no answer for it and would substitute empty"
            )


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


# The one task that can reach a checkout `NOT_PROJECTS` excludes. Named once, because two
# tests below assert about it and a renamed label must break them rather than quietly
# exempt itself.
MERGE_TASK = "Git: Merge Origin Default into Current Branch"


def test_the_merge_picker_reaches_the_reference_checkouts_too(canonical):
    """The one picker whose option list is not the registry, and why.

    Every other picker offers the registry, or the registry minus a documented
    exclusion. This one offers the registry PLUS `NOT_PROJECTS`: merging origin's
    default branch in is pure
    git — no `.devkit.toml`, no virtualenv, no vendored tier — so it is the one action a
    reference checkout can take. The set is empty today, which makes the two sides equal
    rather than making the rule wrong; the equality below is what keeps them agreeing
    when a reference checkout is registered again.

    Asserted as an equality in both directions so the picker cannot drift either way: a
    newly generated project has to reach it (`register()` maintains it alongside
    `project`), and a checkout that stops being a non-project has to leave it.
    """
    inputs = {spec["id"]: spec for spec in canonical["inputs"]}
    registry = _picker_values(inputs["project"])
    expected = registry | set(devkit_project.NOT_PROJECTS)
    assert _picker_values(inputs["mergeCheckout"]) == expected


# The two clicks that ask which consumers should adopt a devkit release. Named once for
# the reason `MERGE_TASK` is: two tests assert about them, and a renamed label must break
# those rather than quietly exempt itself.
ADOPTION_TASKS = ("Devkit: Cut Release", "Devkit: Upgrade Projects")


def test_the_adoption_picker_is_every_consumer_of_a_release(canonical):
    """The other picker whose option list is not the registry, and why.

    `mergeCheckout` offers the registry PLUS `NOT_PROJECTS`; this one offers it MINUS
    `devkit`, and the exclusion is a correctness requirement rather than tidiness. A
    release is pulled *from* that checkout, and `upgrade-project.py` treats a checkout
    that was named and cannot be a target as an operator error — one stray tick would
    stop a five-consumer run, where `--all` skips devkit silently.

    An equality in both directions, for the same reason the merge picker gets one: a
    newly generated project has to reach this list (`insert_picker_option` names it, and
    `register()` never names devkit, so insertion stays correct), and a retired one has
    to leave it. The hand-kept ancestors this replaces — `sweepScope`, `upgradeScope` —
    had no such check and drifted; see the block above `_input_options`.
    """
    inputs = {spec["id"]: spec for spec in canonical["inputs"]}
    expected = _picker_values(inputs["project"]) - {"devkit"}
    assert expected, "the registry is devkit alone — this test now guards nothing"
    assert _picker_values(inputs["adoptProjects"]) == expected


def test_the_adoption_tasks_ask_rather_than_assuming_every_consumer(canonical):
    """`--all` in either of these is the regression: it is the spelling that made
    adopting somewhere-but-not-everywhere a terminal command. The scheduled pass and the
    release pipeline still pass `--all` — they are unattended, and nobody is at the
    dropdown — but that is argv those callers build, never a task argument here."""
    for label in ADOPTION_TASKS:
        task = next(t for t in canonical["tasks"] if t["label"] == label)
        args = [str(a) for a in task["args"]]
        assert any("${input:adoptProjects}" in arg for arg in args), f"{label}: never asks"
        assert "--all" not in args, f"{label}: hard-codes --all over the checklist"


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


# The scripts that recognise an unresolved `${input:…}` and exit 0 having run nothing.
# Spelled out rather than derived, because the failure this guards against is a *new*
# task wired to a script that has no such check — deriving the list from the tasks would
# make every new task exempt by construction.
CANCEL_AWARE_RECEIVERS = {
    "devkit_project.py",
    "devkit_ports.py",
    "resume-sessions.py",
    "plug-projects.py",
    "upgrade-project.py",
    "harness-switch.py",
}


def _receiving_script(args: list[str]) -> str:
    """The script a task's arguments are actually handed to.

    Wrappers nest left to right with a bare `--` before the command they run, so the
    innermost one is what follows the last separator; a dispatched task has no separator
    and is its own receiver. The guard has to live in that script: `log-wrap.py` passes
    its tail through untouched, so a check in the outer layer would prove nothing about
    what the inner one does with the literal.
    """
    tail = args
    if "--" in args:
        tail = args[len(args) - args[::-1].index("--") :]
    return next((arg.split("/")[-1] for arg in tail if arg.endswith(".py")), "")


def test_every_task_with_a_command_picker_can_be_cancelled(canonical):
    """A dismissed `command` input must reach a script that treats it as a cancel.

    VS Code aborts a task run itself when one of *its own* inputs (`promptString`,
    `pickString`) is escaped. It cannot for a `command` input — the extension returns
    nothing, no substitution is recorded, and the task launches with the literal
    `${input:<id>}` still in its argument list. Every multi-select picker here is a
    command input, so "cancel means nothing runs" is a property of the receiving script
    and of nothing else; before this, escaping the project picker ran the dispatcher,
    which reported `unknown project '${input:project}'` as a task *failure*, complete
    with a red icon and a toast, for a run the user had just called off.
    """
    command_inputs = {spec["id"] for spec in canonical["inputs"] if spec.get("type") == "command"}
    assert command_inputs, "no command-typed inputs — the picker convention has changed"

    offenders = {}
    for task in canonical["tasks"]:
        args = [str(arg) for arg in task.get("args", [])]
        if not any(f"${{input:{name}}}" in arg for name in command_inputs for arg in args):
            continue
        receiver = _receiving_script(args)
        if receiver not in CANCEL_AWARE_RECEIVERS:
            offenders[task["label"]] = receiver
    assert not offenders, (
        f"tasks whose dismissed picker reaches a script that would run anyway: {offenders}"
    )


# Which extension supplies each `command` input's command, and so which
# `recommendations` entry every task using that picker depends on. Keyed by the prefix
# that identifies the provider, because `rioj7.command-variable` contributes a family.
PICKER_EXTENSIONS = {
    "extension.commandvariable.": "rioj7.command-variable",
    "shellCommand.execute": "augustocdias.tasks-shell-input",
}


def _providing_extension(command: str) -> str:
    """The extension a `command` input's command comes from, or "" for none known."""
    return next(
        (name for prefix, name in PICKER_EXTENSIONS.items() if command.startswith(prefix)), ""
    )


def test_every_extension_a_picker_needs_is_a_workspace_recommendation(canonical):
    """A `command` input is dead on a machine without the extension behind it, and the
    failure names a command rather than a package -- so the prerequisite has to be
    written down where something checks it.

    `extensions.recommendations` in the canonical workspace is that list, and it is the
    only one: `scripts/vscode_extensions.py` reads it to report what is missing at
    session start, and a recommendation is what VS Code offers to install. This is the
    half that cannot be forgotten -- adding a picker on a new extension turns red here
    until the entry exists.

    An unknown command id fails rather than passing. A picker whose provider nobody
    wrote down is exactly the one nobody will list as a prerequisite either, and
    treating it as "needs nothing" would make this test quietly stop covering it.
    """
    recommended = set(
        devkit_jsonc_loads(devkit_project.canonical_text())["extensions"]["recommendations"]
    )
    needed: dict[str, list[str]] = {}
    for spec in canonical["inputs"]:
        if spec.get("type") != "command":
            continue
        provider = _providing_extension(str(spec.get("command", "")))
        assert provider, (
            f"input {spec['id']!r} runs {spec.get('command')!r}, which no entry of "
            f"PICKER_EXTENSIONS claims -- name the extension that supplies it"
        )
        needed.setdefault(provider, []).append(spec["id"])

    assert needed, "no command-typed inputs -- the picker convention has changed"
    missing = {name: ids for name, ids in needed.items() if name not in recommended}
    assert not missing, (
        f"extensions these pickers need and the workspace does not recommend: {missing}"
    )


def test_a_dismissed_picker_dispatches_nothing(monkeypatch, capsys):
    """The dispatcher's half of the contract above, exercised rather than inferred.

    Asserted through `main` because the guard's placement is the point: it sits ahead of
    `argparse`, which would otherwise turn the literal into a usage error on the
    arguments that carry a `type=` or a `choices=`.
    """

    def refuse(*args, **kwargs):
        raise AssertionError("a cancelled task must not spawn anything")

    monkeypatch.setattr(devkit_project.subprocess, "run", refuse)
    assert devkit_project.main(["--project", "${input:project}", "test"]) == 0
    assert "cancelled" in capsys.readouterr().out


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


def test_the_box_tier_keeps_one_task_and_it_is_read_only(canonical):
    """`worktree.py` reaches the quick-pick once, as `list`. The other three went.

    The box tier had four rows, and three of them had no caller who was not an agent --
    which is the test, because an agent reaches every subcommand through the CLI and
    never through VS Code:

    - **New Box** passed no `--session`, and `worktree-guard.py` finds a box by its
      lease (one per session+project). A hand-cut box is therefore adopted by nobody: it
      costs a cold toolchain install and a port slot out of a fixed ceiling, and
      `reconcile_action` reaps it as "never used" once it is past the newborn grace.
    - **Reconcile** is the `devkit-worktree-reconcile` scheduled task, which runs the
      same pass every fifteen minutes -- `install-reconcile-task.py` owns its arguments.
      The one-click copy was also the more dangerous of the two, because it passed no
      `--merge-label`: its "also merge every green PR" option meant every green PR,
      where the schedule merges only what carries the label.
    - **Reap Finished Boxes** could not reap anything reconcile would not, and usually
      less -- `reap_decision` sets `awaiting_pr` for a caller with no PR in hand, so it
      refuses a pushed box that reconcile reaps under disk pressure or past
      `max_age_days`.

    `list` stays because boxes are absent from `folders` by design, so it is the only
    thing that reports them to a human at all. Re-adding one of the three means naming
    the human it is for, not just deleting this test.

    The four `Agent:` lifecycle rows came back once and have gone again, and the reason
    they went is not the reason they arrived. They were added because `harness-switch.py`
    can stand the branch tier down, and with the guard off nothing cuts a box unless a
    person asks — so a hand spelling was needed. **Claude Code then grew its own**:
    `claude --worktree <topic>` cuts and enters a worktree at `.claude/worktrees/<topic>`,
    offers keep-or-remove on exit, and sweeps a stale one that is clean and pushed. That
    is `spawn`, `attach` and `delete` in a built-in nobody here maintains, and `/ship` is
    the delivery half — `ship.is_shippable` accepts the `worktree-<topic>` branch it cuts
    for exactly this reason. A row that duplicates a built-in is the same clutter as a row
    that duplicates a scheduled pass.

    **Two of them are back, and the built-ins are exactly why the argument does not cover
    them.** `--worktree` was a *Claude Code* flag alone when these rows were written; Codex
    has one now, and it cuts somewhere else entirely — `~/.codex/worktrees/<digest>/`,
    outside the checkout. So the machine has two conventions whatever this menu does, and
    what these rows add is what neither built-in does: `Agent: New Worktree` asks which
    checkout and which base branch before cutting, into `.claude/worktrees/` on purpose —
    it is also where a remote Claude session spawns — and `Agent: Delete Worktrees` reads
    EVERY tier, which is the half a built-in bounded to its own runtime and its own session
    structurally cannot. The human they are for is the one who typed this request:
    somebody driving Codex from the quick-pick.

    They are not the box tier either, and the same seam says so. No port lease, no
    `COMPOSE_PROJECT_NAME`, no toolchain provisioning and no reaper — `agent-box.py spawn`
    is still the answer for a session that runs a compose stack, and stays CLI-only, so
    the box tier now has no clicked entry point at all. What every one of these shares is
    `agent_tabs.open_agent`, because two copies of "which window does the agent open in"
    is the duplication that seam exists to prevent.

    **`Agent: Fix What Is Red` is a third row in that tier, and that is not a
    contradiction either.** Every part of what it does is a thing `claude --worktree` has
    no shape for: the worktree sits on the *PR's own head branch* with upstream set so a
    bare push lands on the PR, and the session opens already knowing which PR, what is
    failing on it now, and that the job ends merged. That is a job spec, not an isolation
    primitive. It cuts into `.claude/worktrees/` like the other two, so the delete row
    can see what it left behind -- it used to cut a box, which meant the one task most
    likely to leave a worktree behind was the one whose worktrees no dropdown listed.
    This test reads task **args**, so it passes because that row dispatches through
    `devkit_project.py`; `fix-prs.py` still reaches `agent_command` internally, which is
    the point of the seam.

    `agent-box.py` and `harness-switch.py` therefore stay, and keep their CLI verbs — the
    other thing the built-in cannot give a box is a port lease and a
    `COMPOSE_PROJECT_NAME`, so `agent-box.py spawn` remains the answer for a session that
    runs a compose stack. Typed, by the one person who would want it, which is the bar
    this test has always applied.

    Reconcile is still not a row, and that half stands unchanged: the schedule owns it,
    the one-click copy's missing `--merge-label` is still the reason, and
    `harness-switch.py --off --group jobs` is how it is stood down.
    """
    # Matched on the file NAME, not on a substring of the path: `agent-worktree.py`
    # ends in the same eleven characters and is a different tier entirely, so a
    # substring test would report the `.claude/worktrees/` rows as box-tier rows.
    callers = [
        task
        for task in canonical["tasks"]
        if any(Path(str(a)).name == "worktree.py" for a in task.get("args", []))
    ]
    assert [task["label"] for task in callers] == ["Worktree: List Boxes — read-only"]
    args = [str(a) for a in callers[0]["args"]]
    assert "list" in args, "the surviving box task must be the read-only one"

    # `agent-box.py` and `harness-switch.py` are CLI-only: neither gets a row back
    # without naming the human it is for, which is what the docstring above does.
    clickable = [
        task["label"]
        for task in canonical["tasks"]
        if any(
            script in str(a)
            for a in task.get("args", [])
            for script in ("agent-box.py", "harness-switch.py")
        )
    ]
    assert not clickable, (
        f"{clickable} duplicates a Claude Code built-in (`claude --worktree` plus "
        "`/ship`); a row that duplicates one is the same clutter as a row that "
        "duplicates a scheduled pass"
    )

    retired = {
        "worktreeProject",
        "worktreeSlug",
        "reconcileMerge",
        "agentProject",
        "agentBase",
        "agentSlug",
        "agentBranch",
        "agentCli",
        "agentCliOnly",
        "harnessVerb",
        "harnessGroups",
    }
    defined = {spec["id"] for spec in canonical["inputs"]}
    assert not (retired & defined), (
        f"{sorted(retired & defined)} feeds a task that no longer exists -- a picker "
        "with no caller is a question nobody asks"
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
    """`support.in_an_ephemeral_box`: every disposable tier counts, and Codex's reaches
    the predicate through the shape test rather than through a `.git` read, so it holds
    for a path that is already gone.
    """
    boxes = Path("C:/ws") / worktree.BOXES_DIR_NAME
    codex = Path(worktree_tiers.home_of(worktree_tiers.TIERS[1]))
    assert in_an_ephemeral_box(boxes / "devkit--some-task-0824")
    assert in_an_ephemeral_box(Path("C:/ws/devkit/.claude/worktrees/snoopy-sauteeing-gray"))
    assert in_an_ephemeral_box(codex / "worktrees" / "2e51" / "devkit")
    assert not in_an_ephemeral_box(Path("C:/ws/devkit"))
    assert not in_an_ephemeral_box(boxes)  # the boxes directory is not itself a box
    # ...nor is the directory either agent tier's worktrees sit in.
    assert not in_an_ephemeral_box(Path("C:/ws/devkit/.claude/worktrees"))
    assert not in_an_ephemeral_box(codex / "worktrees" / "2e51")


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
        "sync-codex",
    }


def test_generated_actions_get_distinct_branches_and_only_codex_opts_into_automerge():
    lint = ACTIONS["lint"]
    codex = ACTIONS["sync-codex"]

    assert lint.autofix_slug == "lint-autofix"
    assert codex.autofix_slug == "codex-context-sync"
    assert lint.autofix_labels == ()
    assert codex.autofix_labels == (
        devkit_project.sweep.AUTOFIX_LABEL,
        devkit_project.sweep.AUTOMERGE_LABEL,
    )


def autofix_run(tmp_path, monkeypatch, argv, dirty=("app/main.py",), returncode=0):
    """Dispatch a lint action over one checkout, recording every command it runs."""
    workspace = tmp_path / "projects.code-workspace"
    registry_lists(monkeypatch, "alpha")
    scripts = tmp_path / "alpha" / "scripts"
    scripts.mkdir(parents=True)
    action_name = argv[-1]
    (tmp_path / "alpha" / ACTIONS[action_name].script).write_text("")

    calls = []
    snapshots = iter([("master", ()), ("master", dirty)])
    monkeypatch.setattr(devkit_project, "autofix_state", lambda _directory: next(snapshots))

    def fake_run(command, *, cwd, check, env):
        calls.append(command)
        assert env["PYTHONUTF8"] == "1", "the ship steps run devkit's sweep.py, which decodes too"
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
    registry_lists(monkeypatch, "alpha")
    scripts = tmp_path / "alpha" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "run-tests.py").write_text("")

    def boom(_directory):
        raise AssertionError("a non-autofix action snapshotted the tree")

    monkeypatch.setattr(devkit_project, "autofix_state", boom)
    monkeypatch.setattr(
        devkit_project.subprocess,
        "run",
        lambda command, *, cwd, check, env: type("Result", (), {"returncode": 0})(),
    )
    assert devkit_project.main(["--workspace", str(workspace), "--project", "alpha", "test"]) == 0


def failed_sweep_step(tmp_path, monkeypatch):
    """Dispatch `lint` over one checkout whose run passes and whose `--branch` step fails."""
    workspace = tmp_path / "projects.code-workspace"
    registry_lists(monkeypatch, "alpha")
    scripts = tmp_path / "alpha" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "lint-all.py").write_text("")

    snapshots = iter([("master", ()), ("master", ("app/main.py",))])
    monkeypatch.setattr(devkit_project, "autofix_state", lambda _directory: next(snapshots))
    calls = []

    def fake_run(command, *, cwd, check, env):
        calls.append(command)
        # The lint run passes; the `--branch` step fails.
        return type("Result", (), {"returncode": 0 if len(calls) == 1 else 3})()

    monkeypatch.setattr(devkit_project.subprocess, "run", fake_run)
    code = devkit_project.main(["--workspace", str(workspace), "--project", "alpha", "lint"])
    return code, calls


def test_a_failed_sweep_step_stops_the_chain_and_reports(tmp_path, monkeypatch, capsys):
    code, calls = failed_sweep_step(tmp_path, monkeypatch)

    assert code == 3
    assert len(calls) == 2, "the --ship step ran after --branch failed"
    assert "still in the working tree" in capsys.readouterr().err


# --- a task that fails outside log-wrap still reaches the ledger ---------------
#
# `plan_command` wraps every dispatched script in `log-wrap.py`, which files each failure
# it sees as a `scheduled-job-failed` event. Two of the dispatcher's own failures happen
# where no wrapper is running: a refusal before anything was planned, and the autofix ship
# steps after the wrapped run has already exited.


def failing_dispatch(tmp_path, monkeypatch, *argv, registry_text='projects = ["alpha", "beta"]'):
    """`main` over a throwaway registry listing alpha and beta, neither of which exists.

    `registry_text` is parsed as `projects.toml` would be, so a malformed one raises
    exactly what the real file would.
    """
    monkeypatch.setattr(
        project_registry,
        "all_projects",
        lambda *_args, **_kw: project_registry.parse_registry(registry_text),
    )
    workspace = tmp_path / "projects.code-workspace"
    return devkit_project.main(["--workspace", str(workspace), *argv])


def test_a_dispatch_that_cannot_start_is_filed_on_the_ledger(
    tmp_path, monkeypatch, ledger_dir, capsys
):
    """The owner's rule: every routine task that fails reaches the ledger, because
    failures share causes and are triaged together. A stale picker entry, or a checkout
    that lacks the action's script, is refused before `plan_command` wraps anything --
    so it was a red icon in a terminal, and nothing anywhere said it kept happening."""
    assert failing_dispatch(tmp_path, monkeypatch, "--project", "gamma", "lint") == 2
    assert "unknown project 'gamma'" in capsys.readouterr().err, "the terminal line is unchanged"

    [row] = ledger_rows(ledger_dir)
    assert row["event"] == "scheduled-job-failed"
    assert row["message"] == "task 'Lint: Run' could not start"
    assert row["exit"] == "2"
    assert "unknown project 'gamma'" in row["cause"]
    kept = (ledger_dir / row["artifact"]).read_text(encoding="utf-8")
    assert "unknown project 'gamma'" in kept, "the event names a file holding the reason"


@pytest.mark.parametrize(
    "text, said", [("projects = [", "projects.toml"), ('projects = "x"', "a list of names")]
)
def test_an_unreadable_registry_is_filed_under_the_task_it_stopped(
    tmp_path, monkeypatch, ledger_dir, text, said
):
    argv = ("--project", "alpha", "tests", "suite")
    assert failing_dispatch(tmp_path, monkeypatch, *argv, registry_text=text) == 2
    [row] = ledger_rows(ledger_dir)
    assert row["message"] == "task 'Test: Run Suite' could not start"
    assert "cannot read the project registry" in row["cause"]
    assert said in row["cause"]


def test_a_dispatch_reads_no_workspace_file(tmp_path, monkeypatch):
    """`--workspace` only says where the checkouts sit: the file it names need not
    exist, which is what lets the dispatcher outlive the VS Code workspace."""
    (tmp_path / "alpha" / "scripts").mkdir(parents=True)
    (tmp_path / "alpha" / "scripts" / "run-tests.py").write_text("")
    monkeypatch.setattr(
        devkit_project.subprocess,
        "run",
        lambda command, *, cwd, check, env: type("Result", (), {"returncode": 0})(),
    )
    assert failing_dispatch(tmp_path, monkeypatch, "--project", "alpha", "test") == 0
    assert not (tmp_path / "projects.code-workspace").exists()


def test_the_message_names_the_task_and_nothing_that_varies(tmp_path, monkeypatch, ledger_dir):
    """`harness_triage.Item.signature` groups by the message, so a refusal that recurs
    has to read as one defect recurring. What differs between two runs rides in
    `cause`, where it splits a different reason into a different group."""
    failing_dispatch(tmp_path, monkeypatch, "--project", "gamma", "lint")
    failing_dispatch(tmp_path, monkeypatch, "--project", "delta", "lint")
    first, second = ledger_rows(ledger_dir)
    assert first["message"] == second["message"] == "task 'Lint: Run' could not start"
    assert first["cause"] != second["cause"]


def test_a_run_that_names_no_action_files_nothing(tmp_path, monkeypatch, ledger_dir):
    """No action is no task to file it under: `--list` is a picker's, and `--check` is
    typed by hand, where the refusal on stderr is the whole report."""
    assert failing_dispatch(tmp_path, monkeypatch, "--list", registry_text="projects = [") == 2
    assert ledger_rows(ledger_dir) == []


def test_a_ledger_that_cannot_be_reached_leaves_the_exit_code_alone(tmp_path, monkeypatch):
    """Best-effort, for `log-wrap.record_failure`'s reason: a task whose bookkeeping
    crashed would report the bookkeeping instead of its own failure."""

    def missing():
        raise ImportError("no log-wrap.py in this checkout")

    monkeypatch.setattr(devkit_project, "_log_wrap", missing)
    assert failing_dispatch(tmp_path, monkeypatch, "--project", "gamma", "lint") == 2


@pytest.mark.parametrize(
    "action, title",
    [("lint", "Lint: Run"), (TESTS_VERB, "Test: Run Suite"), ("", ""), ("no-such", "")],
)
def test_a_failure_is_filed_under_the_label_log_wrap_gets(action, title):
    """`Action.label` is what `plan_command` hands `log-wrap.py`, so one task's failures
    are filed under one name whichever side of the wrapper they happened on."""
    assert devkit_project.task_title(action) == title


def test_a_failed_autofix_ship_is_filed_against_its_checkout(tmp_path, monkeypatch, ledger_dir):
    """The ship steps run after `log-wrap.py` has exited -- the lint run passed, so it
    filed nothing -- and a failed `sweep.py --branch` was one stderr line. The reason is
    kept in the checkout's own `logs/`, beside the churn it failed to ship."""
    code, _calls = failed_sweep_step(tmp_path, monkeypatch)

    assert code == 3
    [row] = ledger_rows(ledger_dir)
    assert row["message"] == "task 'Lint: Run' could not ship its autofix"
    assert (row["project"], row["exit"]) == ("alpha", "3")
    assert row["command"].endswith("--branch --slug lint-autofix --yes")
    assert "still in the working tree" in (tmp_path / "alpha" / row["artifact"]).read_text(
        encoding="utf-8"
    )


def test_refuse_dispatch_prints_files_and_exits_2(ledger_dir, capsys):
    error = ProjectError("alpha does not implement this action")
    assert devkit_project.refuse_dispatch(error, "e2e", ["--project", "alpha", "e2e"]) == 2
    assert capsys.readouterr().err == "devkit_project: alpha does not implement this action\n"
    [row] = ledger_rows(ledger_dir)
    assert row["message"] == "task 'Test: Run Browser E2E' could not start"
    assert row["command"].endswith("--project alpha e2e")


def test_report_ship_failure_names_the_step_and_where_the_fixes_are(tmp_path, capsys):
    step = ("python", "sweep.py", "--ship", "--yes")
    devkit_project.report_ship_failure(tmp_path / "alpha", ACTIONS["sync-codex"], step, 4)
    assert "[alpha] shipping the autofix churn failed (exit 4)" in capsys.readouterr().err
    kept = (
        tmp_path
        / "alpha"
        / "logs"
        / "agent-sync-codex-context-could-not-ship-its-autofix.failed.log"
    )
    assert "sweep.py --ship --yes" in kept.read_text(encoding="utf-8")


def test_a_kept_failure_never_overwrites_the_one_log_wrap_keeps(tmp_path, ledger_dir):
    """`log-wrap.py` keeps `logs/<slug>.failed.log` for the run it wrapped, and a ledger
    row points there. A dispatcher failure of the same task writing that path would
    leave the older row naming a file that now holds a different failure."""
    artifact = devkit_project.record_task_failure(
        "Lint: Run", "could not start", ["python", "x.py"], "E: x", tmp_path, 2
    )
    assert artifact is not None
    assert artifact != "logs/lint-run.failed.log"
    assert (tmp_path / artifact).is_file()


def test_every_project_is_told_what_a_task_script_starts_as(checkouts, tmp_path):
    """983bc6e6: "instruction budget: hot 5547/5550 tok" left `engineering.md` no room for
    this, so the fact reached only projects generated after the fix. A path-scoped rule in
    the `MANIFEST` reaches every project at its next pull and costs the hot tier nothing.
    The words are held to the argv that is their whole basis, and to the windowless spawn."""
    devkit_root = tmp_path / "devkit"
    (devkit_root / "scripts").mkdir(parents=True)
    command = plan_command(ACTIONS["test"], checkouts / "alpha", [], devkit_root)
    assert command[-2:] == ["python", "scripts/run-tests.py"], "not the machine's `python`"
    log_wrap = (support.REPO_ROOT / "scripts" / "log-wrap.py").read_text(encoding="utf-8")
    assert "creationflags=NO_WINDOW" in log_wrap

    rel = ".claude/rules/task-scripts.md"
    manifest = (support.REPO_ROOT / "scripts" / "devkit_manifest.py").read_text(encoding="utf-8")
    assert f'"{rel}"' in manifest, "not vendored, so no existing project ever reads it"
    text = (support.REPO_ROOT / rel).read_text(encoding="utf-8")
    assert re.search(r"^paths:$", text, re.M), "unscoped, so it is back on the hot budget"
    said = " ".join(text.split())
    assert "under the machine's `python`, not this project's `.venv`" in said
    assert "no console" in said and "Ctrl+C" in said
