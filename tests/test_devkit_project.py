"""Tests for the `--project X <action>` dispatcher.

The behaviour worth pinning is the failure text: a run in the wrong directory, or
against a project that does not implement the action, must say so by name. A traceback
from a missing file in an unexpected cwd is the outcome this script exists to prevent.
"""

import re
from pathlib import Path

import project_registry
import pytest
import support
from support import (
    REPO_ROOT,
    devkit_project,
    in_an_ephemeral_box,
    worktree,
    worktree_tiers,
)

ACTIONS = devkit_project.ACTIONS
Action = devkit_project.Action
ProjectError = devkit_project.ProjectError
conformance = devkit_project.conformance
known_projects = devkit_project.known_projects
plan_command = devkit_project.plan_command
project_selection = devkit_project.project_selection
resolve_project = devkit_project.resolve_project

# A *reference* checkout — one registered to be read but never shipped from.
# `NOT_PROJECTS` is empty today, so every test below that exercises the exclusion has to
# put a name in it; that is the only way the mechanism gets covered at all.
REFERENCE = "reference-checkout"


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

    The dispatcher reads its projects off `project_registry`; the deprecated
    `--workspace` a test hands it only says where the checkouts sit.
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


def test_without_workspace_the_checkouts_sit_under_the_registrys_root(tmp_path, monkeypatch):
    """`--workspace` is optional now: absent, the root is `project_registry.projects_root`."""
    registry_lists(monkeypatch, "alpha")
    (tmp_path / "alpha" / "scripts").mkdir(parents=True)
    (tmp_path / "alpha" / "scripts" / "run-tests.py").write_text("")
    monkeypatch.setattr(project_registry, "projects_root", lambda *_a, **_kw: tmp_path)
    calls = []

    def fake_run(command, *, cwd, check, env):
        calls.append(cwd)
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(devkit_project.subprocess, "run", fake_run)
    assert devkit_project.main(["--project", "alpha", "test"]) == 0
    assert calls == [tmp_path / "alpha"]


def test_the_deprecated_workspace_flag_names_the_root_by_its_directory(tmp_path, monkeypatch):
    """Scheduled-task XML on a PC may still pass `--workspace`: only its directory counts,
    and the file it names need not exist."""
    monkeypatch.setattr(project_registry, "projects_root", lambda *_a, **_kw: Path("/nowhere"))
    assert devkit_project.dispatch_root(tmp_path / "gone.code-workspace") == tmp_path
    assert devkit_project.dispatch_root(None) == Path("/nowhere")


def test_a_dispatched_task_runs_in_utf8_mode_down_to_its_grandchildren(tmp_path, monkeypatch):
    """Running *Agent: Fix What Is Red* printed `UnicodeDecodeError: 'charmap' codec
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

    Every plan is `log-wrap -> the script`, and the assertions
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
    """An action's own arguments are part of what it is; the caller's come after.

    Asserted against a stand-in — `docker-up`'s `--build` and `backtest`'s subcommand
    both depend on it.
    """
    scoped = devkit_project.Action("scripts/lint-all.py", "Lint: Run", ("--changed",))
    command = plan_command(scoped, checkouts / "alpha", ["--verbose"])
    assert inner(command) == ["python", "scripts/lint-all.py", "--changed", "--verbose"]


def test_empty_tokens_are_dropped(checkouts):
    """An empty argument would reach the script's argparse as a stray positional, so the
    dispatcher drops it and a run cannot fail on an invisible argument."""
    assert inner(plan_command(ACTIONS["test"], checkouts / "alpha", ["", "-k", ""])) == [
        "python",
        "scripts/run-tests.py",
        "-k",
    ]


def test_the_plan_is_wrapped_for_logging(checkouts, tmp_path):
    """This is what gives every dispatched action a failure artifact -- and the only
    place that can, because nothing knows the checkout until `resolve_project` has run."""
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


def test_a_projects_notify_wrap_no_longer_wraps_the_plan(checkouts, tmp_path):
    """The completion toast was for a clicked VS Code task; nothing is clicked now, so a
    checkout still shipping `scripts/notify-wrap.py` gets the log-wrapped argv alone."""
    devkit_root = tmp_path / "dk"
    (devkit_root / "scripts").mkdir(parents=True)
    (checkouts / "alpha" / "scripts" / "notify-wrap.py").write_text("")
    command = plan_command(ACTIONS["lint"], checkouts / "alpha", [], devkit_root)
    assert "scripts/notify-wrap.py" not in command
    assert command == [
        "python",
        str(devkit_root / "scripts" / "log-wrap.py"),
        "Lint: Run",
        "--",
        "python",
        "scripts/lint-all.py",
    ]


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
# Scoping defines a one-repo action here without claiming every checkout can run it.

check_scope = devkit_project.check_scope
expected_actions = devkit_project.expected_actions
in_scope = devkit_project.in_scope
CARAMELI = devkit_project.CARAMELI
IBKR = devkit_project.IBKR


def test_an_unscoped_action_applies_everywhere():
    assert in_scope(ACTIONS["lint"], "anything-at-all")


def test_a_scoped_action_applies_to_the_checkouts_that_can_run_it():
    """One checkout per repo now: the `-b` tier is gone (ephemeral boxes give unbounded
    concurrency instead of two), so the scope is a single name."""
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

    Listed rather than counted: a missing entry here means an action the user used to be
    able to dispatch is now unreachable from anywhere, which nothing else in the suite
    notices.

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
        # checkout is an argument of the script rather than a scope for the action.
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
def test_every_report_view_reaches_the_script_as_its_subcommand(tmp_path, view):
    """Each view is a subcommand of `report-task.py`, passed through untouched."""
    checkout = tmp_path / "ibkr_trader"
    (checkout / "scripts").mkdir(parents=True)
    (checkout / "scripts" / "report-task.py").write_text("", encoding="utf-8")
    argv = plan_command(ACTIONS["report"], checkout, [view])
    assert argv[-3:] == ["python", "scripts/report-task.py", view]


def test_the_strategy_lab_carries_both_of_its_required_flags(tmp_path):
    """`report-task.py lab` requires `--universe` and `--account`; the caller's pair rides
    through after the fixed subcommand."""
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


def test_the_questrade_plan_carries_its_strategy_flag(tmp_path):
    """`questrade-task.py plan` requires `--strategy`; it rides through after `plan`."""
    checkout = tmp_path / "ibkr_trader"
    (checkout / "scripts").mkdir(parents=True)
    (checkout / "scripts" / "questrade-task.py").write_text("", encoding="utf-8")
    tail = ["--strategy", "core_satellite"]
    argv = plan_command(ACTIONS["questrade-plan"], checkout, tail)
    assert argv[-5:] == ["python", "scripts/questrade-task.py", "plan", *tail]


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


def test_list_answers_on_a_machine_with_no_workspace_file(tmp_path, monkeypatch, capsys):
    """The dispatcher's verbs read the project registry, never the workspace file, so
    `--list` answers where there is none at all."""
    registry_lists(monkeypatch, "alpha", "beta")
    missing = tmp_path / "absent.code-workspace"
    assert devkit_project.main(["--workspace", str(missing), "--list"]) == 0
    assert capsys.readouterr().out.split() == ["alpha", "beta"]
    assert devkit_project.main(["--list"]) == 0
    assert capsys.readouterr().out.split() == ["alpha", "beta"]


def test_an_unsubstituted_task_input_is_an_unknown_project_not_a_cancel(
    tmp_path, monkeypatch, capsys
):
    """The dismissed-picker strip went with VS Code: a literal `${input:...}` is now an
    argument like any other, refused by name rather than read as Escape and exited 0."""
    registry_lists(monkeypatch, "alpha")

    def refuse(*args, **kwargs):
        raise AssertionError("a refused dispatch must not spawn anything")

    monkeypatch.setattr(devkit_project.subprocess, "run", refuse)
    argv = ["--workspace", str(tmp_path / "x"), "--project", "${input:project}", "test"]
    assert devkit_project.main(argv) == 2
    assert "unknown project" in capsys.readouterr().err


def test_the_workspace_file_verbs_are_gone(capsys):
    """VS Code is retired: nothing renders, adopts or checks the workspace file now, so
    the flags are a usage error rather than a silent no-op someone mistakes for a run."""
    for flag in ("--render-workspace", "--check-workspace", "--adopt-workspace"):
        with pytest.raises(SystemExit) as exited:
            devkit_project.main([flag])
        assert exited.value.code == 2, flag
    capsys.readouterr()


@pytest.mark.parametrize("scope, expected", [("", []), ("--changed", ["--changed"])])
def test_both_lint_scopes_reach_the_one_action(checkouts, scope, expected):
    """The two scopes are one action plus a token — the empty one being dropped by
    `plan_command` is what lets the wide scope pass nothing at all."""
    command = plan_command(ACTIONS["lint"], checkouts / "alpha", [scope])
    assert inner(command) == ["python", "scripts/lint-all.py", *expected]


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

PROJECTS_ROOT = Path("/ws")


def plan(before=(), after=("app/main.py",), branch="master", lint_ok=True):
    return devkit_project.autofix_ship_plan(
        "alpha",
        branch,
        before,
        after,
        lint_ok=lint_ok,
        root=PROJECTS_ROOT,
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
        assert step[step.index("--root") + 1] == str(PROJECTS_ROOT)
        assert "--workspace" not in step


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
    for step in calls[1:]:
        assert step[step.index("--root") + 1] == str(tmp_path), "not the dispatch's root"


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
    """No action is no task to file it under: `--list` and `--check` only report, and
    the refusal on stderr is the whole report."""
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
