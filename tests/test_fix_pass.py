"""`scripts/fix-pass.py`: the wiring of one pass, with every network and spawn replaced.

The decisions are tested where they live -- `test_fix_plan.py`, `test_fix_cycle.py`,
`test_ship_intent.py`, `test_gate_evidence.py`. What is here is that the pass calls them
in the right order, respects the switch, records what it sent, and writes its artifact
on every run including the ones where it did nothing.
"""

from __future__ import annotations

import datetime as _dt
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from support import REPO_ROOT, load_script

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import fix_cycle
import fix_ledger
import fix_plan
import ship_intent

fix_pass = load_script("scripts/fix-pass.py")
MISSING_TOOLS = fix_pass.missing_tools  # the real preflight, before `tools_on_path` stubs it

NOW = _dt.datetime(2026, 9, 19, 9, 0, tzinfo=_dt.UTC)
VENDORED = ("scripts/hooks/tests/test_untested_symbols.py::t",)


@pytest.fixture(autouse=True)
def tools_on_path(monkeypatch):
    """Every CLI the preflight asks for is present unless a test says otherwise."""
    monkeypatch.setattr(fix_pass, "missing_tools", lambda: [])


def failure(**fields) -> fix_plan.Failure:
    base: dict[str, Any] = {
        "kind": fix_plan.PR,
        "project": "carameli",
        "number": 412,
        "title": "T",
        "url": "u",
        "head": "agent/x-0919",
        "base": "main",
        "sha": "abc",
        "reason": "1 check failing",
        "signature": ("tests/test_x.py::t",),
    }
    base.update(fields)
    return fix_plan.Failure(**base)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A workspace with devkit and carameli, every outside call stubbed, and a log of
    what the pass did. Returns the mutable stub table."""
    for name in ("devkit", "carameli"):
        (tmp_path / name).mkdir()
    workspace = tmp_path / "alex.code-workspace"
    workspace.write_text(
        '{"folders": [{"path": "devkit"}, {"path": "carameli"}], "settings": {}}', encoding="utf-8"
    )
    table = {
        "workspace": workspace,
        "intents": [],
        "failures": [],
        "branches": {"devkit": (True, None), "carameli": (True, None)},
        "backlog": None,
        "pending": [],
        "dispatched": [],
        "merged": [],
        "shipped": [],
        "blocked": [],
        "order": [],
        "release": "",
        "releases": [],
    }
    monkeypatch.setattr(
        fix_pass.devkit_project, "known_projects", lambda _t: ["devkit", "carameli"]
    )
    monkeypatch.setattr(
        fix_pass.ship_intent, "find_intents", lambda root, projects: table["intents"]
    )
    monkeypatch.setattr(fix_pass.push_gate, "interpreter", lambda tree: "py")
    monkeypatch.setattr(
        fix_pass.ship_intent,
        "ship_one",
        lambda intent, python, base: (
            table["shipped"].append(intent.branch)
            or ship_intent.Outcome(intent, ship_intent.SHIPPED, "u")
        ),
    )
    monkeypatch.setattr(
        fix_pass.menu, "scan", lambda _ws, projects=None: {name: [] for name in projects or []}
    )
    monkeypatch.setattr(
        fix_pass.gate_evidence,
        "collect",
        lambda _ws, found: (
            table["order"].append(("collect", sorted(found))) or list(table["failures"])
        ),
    )
    monkeypatch.setattr(
        fix_pass.fix_reports, "find_blocked", lambda root, projects: list(table["blocked"])
    )
    monkeypatch.setattr(fix_pass.gate_evidence, "newest_release", lambda _d: "v0.11.22")
    monkeypatch.setattr(
        fix_pass.gate_evidence,
        "collect_default_branches",
        lambda _ws, _projects: dict(table["branches"]),
    )
    monkeypatch.setattr(
        fix_pass.fix_release, "pending_adoptions", lambda root, projects, tag: table["pending"]
    )
    monkeypatch.setattr(
        fix_pass.fix_release,
        "cut_release",
        lambda ws, tag, green, dispatching, now: (
            table["releases"].append((tag, green, dispatching)) or table["release"]
        ),
    )
    monkeypatch.setattr(
        fix_pass.fix_backlog, "ledger_failure", lambda devkit_dir, root: table["backlog"]
    )
    monkeypatch.setattr(
        fix_pass,
        "dispatch",
        lambda decision, root, agent, options=None: (
            table["dispatched"].append((decision.action, agent))
            or table.update(opened=options)
            or 0
        ),
    )
    monkeypatch.setattr(
        fix_pass.fix_release,
        "merge_green_adoptions",
        lambda root, projects: (
            table["order"].append(("merge", sorted(projects))) or table["merged"]
        ),
    )
    monkeypatch.setattr(fix_pass, "REPO_ROOT", tmp_path / "devkit")
    return table


def artifact(world) -> str:
    return (world["workspace"].parent / "devkit" / fix_pass.ARTIFACT).read_text(encoding="utf-8")


def test_off_writes_one_line_and_touches_nothing(world):
    world["failures"] = [failure()]
    assert fix_pass.run(world["workspace"], fix_cycle.OFF, "claude-bg", NOW) == 0
    assert "mode=off" in artifact(world)
    assert world["dispatched"] == [] and world["shipped"] == []


def test_a_switched_off_fire_leaves_a_manual_passs_record_alone(world):
    """The scheduled job fires every half hour; during the manual week the record of a
    hand-run pass is what is being read, and an `off` fire has nothing to say over it."""
    fix_pass.run(world["workspace"], fix_cycle.PLAN, "claude-bg", NOW)
    before = artifact(world)
    assert "mode=plan" in before
    assert fix_pass.run(world["workspace"], fix_cycle.OFF, "claude-bg", NOW) == 0
    assert artifact(world) == before


def test_plan_writes_the_whole_plan_and_sends_nothing(world):
    world["failures"] = [failure()]
    world["intents"] = [ship_intent.Intent("carameli", Path("t"), "agent/i-0919", "S", "B")]
    assert fix_pass.run(world["workspace"], fix_cycle.PLAN, "claude-bg", NOW) == 0
    text = artifact(world)
    assert "would ship: S" in text
    assert "would send (dispatch)" in text
    assert world["dispatched"] == [] and world["shipped"] == []


def test_dispatch_ships_intents_sends_fixers_records_them_and_merges_adoptions(world):
    world["failures"] = [failure()]
    world["intents"] = [ship_intent.Intent("carameli", Path("t"), "agent/i-0919", "S", "B")]
    world["merged"] = ["carameli #9 -- merged"]
    assert fix_pass.run(world["workspace"], fix_cycle.DISPATCH, "codex", NOW) == 0
    assert world["shipped"] == ["agent/i-0919"]
    assert world["dispatched"] == [(fix_plan.DISPATCH, "codex")]
    ledger = fix_ledger.read_ledger(
        fix_pass.worktree.boxes_root(world["workspace"].parent) / fix_ledger.LEDGER_NAME
    )
    assert list(ledger) == [
        fix_ledger.decision_key(fix_plan.Decision(fix_plan.DISPATCH, "n", (failure(),)))
    ]
    text = artifact(world)
    assert "sent     carameli #412 -- dispatch" in text
    assert "merged   carameli #9" in text


def test_a_second_pass_sends_nothing_at_the_same_failure(world):
    world["failures"] = [failure()]
    fix_pass.run(world["workspace"], fix_cycle.DISPATCH, "claude-bg", NOW)
    fix_pass.run(world["workspace"], fix_cycle.DISPATCH, "claude-bg", NOW)
    assert len(world["dispatched"]) == 1
    assert "already dispatched" in artifact(world)


def test_while_the_harness_is_red_only_one_devkit_session_goes(world):
    world["failures"] = [
        failure(project="carameli", number=1, signature=VENDORED),
        failure(project="carameli", number=2),
    ]
    world["branches"]["devkit"] = (False, red_main())
    assert fix_pass.run(world["workspace"], fix_cycle.DISPATCH, "claude-bg", NOW) == 0
    assert world["dispatched"] == [(fix_plan.UPSTREAM, "claude-bg")]
    text = artifact(world)
    assert "harness  RED" in text and "held     carameli #2" in text


def red_main(**fields) -> fix_plan.Failure:
    base: dict[str, Any] = {
        "kind": fix_plan.BRANCH,
        "project": "devkit",
        "number": 0,
        "head": "",
        "base": "main",
        "sha": "fb17a310",
        "run_id": "55",
        "workflow": "PR Gate",
        "url": "u/55",
        "reason": "",
        # Not the PR helper's id: the same id in two projects is a shared signature,
        # which is harness by classification and a different test's subject.
        "signature": ("tests/test_devkit.py::t",),
    }
    base.update(fields)
    return failure(**base)


def test_a_red_devkit_main_alone_sends_one_devkit_session_and_holds_the_projects(world):
    """What the first manual pass did not do: it held four carameli PRs behind a red
    devkit main and sent nobody at the main, because the red was a reason and not a
    failure. Now it is both."""
    world["failures"] = [failure(number=2)]
    world["branches"]["devkit"] = (False, red_main())
    assert fix_pass.run(world["workspace"], fix_cycle.DISPATCH, "claude", NOW) == 0
    assert world["dispatched"] == [(fix_plan.UPSTREAM, "claude")]
    text = artifact(world)
    assert "harness  RED -- 1 harness failure(s) open; devkit's default-branch gate is red" in text
    assert "upstream devkit origin/main" in text
    assert "held     carameli #2" in text and "sent     devkit origin/main -- upstream" in text


def test_an_untagged_release_commits_red_main_holds_everything_and_sends_nobody(world):
    """Between the release merge and its tag, main is red by construction: the pass says
    which red it is holding behind, and sends no session at a test the tag will fix."""
    world["failures"] = [failure(number=2)]
    world["branches"]["devkit"] = (
        False,
        red_main(signature=("tests/test_new_project.py::" + fix_plan.RELEASE_TEST,)),
    )
    assert fix_pass.run(world["workspace"], fix_cycle.DISPATCH, "claude", NOW) == 0
    assert world["dispatched"] == []
    text = artifact(world)
    assert "harness  RED" in text and "held     carameli #2" in text
    assert "skip     devkit origin/main -- red by construction" in text


def test_collect_red_gathers_prs_default_branches_and_the_backlog_with_devkits_verdict(world):
    backlog = failure(kind=fix_plan.LEDGER, project="devkit", number=0, head="")
    world["failures"] = [failure(number=2)]
    world["branches"] = {"devkit": (False, red_main()), "carameli": (True, None)}
    world["backlog"] = backlog
    failures, green = fix_pass.collect_red(world["workspace"], ["devkit", "carameli"], [])
    assert [f.kind for f in failures] == [fix_plan.PR, fix_plan.BRANCH, fix_plan.LEDGER]
    assert green is False


def test_the_ledgers_open_backlog_rides_in_the_devkit_session(world):
    """Every entry on the harness-defect ledger is a devkit defect, so an open backlog
    goes to the one devkit session -- alone, when nothing else is harness-shaped. It
    is not a reason to hold the projects: one unresolved hook event anywhere was
    holding every project fixer."""
    world["failures"] = [failure(number=2)]
    world["backlog"] = failure(
        kind=fix_plan.LEDGER,
        project="devkit",
        number=0,
        head="",
        workflow="harness ledger",
        signature=("scheduled-job-failed devkit [84ada64c] x1",),
    )
    assert fix_pass.run(world["workspace"], fix_cycle.DISPATCH, "claude", NOW) == 0
    assert world["dispatched"] == [(fix_plan.UPSTREAM, "claude"), (fix_plan.DISPATCH, "claude")]
    text = artifact(world)
    assert "harness  clean" in text
    assert "upstream devkit ledger -- harness-shaped in 1 checkout(s) (devkit)" in text
    assert "sent     carameli #2 -- dispatch" in text and "held" not in text


def test_a_projects_red_main_is_a_project_failure_sent_once_the_harness_is_clean(world):
    world["branches"]["carameli"] = (False, red_main(project="carameli"))
    assert fix_pass.run(world["workspace"], fix_cycle.DISPATCH, "claude", NOW) == 0
    assert world["dispatched"] == [(fix_plan.DISPATCH, "claude")]
    assert "harness  clean" in artifact(world)
    assert "sent     carameli origin/main -- dispatch" in artifact(world)


def test_a_refused_commit_is_a_failure_the_pass_sends_at_the_same_worktree(
    world, monkeypatch, tmp_path
):
    tree = tmp_path / "carameli" / ".claude" / "worktrees" / "i"
    tree.mkdir(parents=True)
    one = ship_intent.Intent("carameli", tree, "agent/i-0919", "S", "B")
    world["intents"] = [one]
    monkeypatch.setattr(
        fix_pass.ship_intent,
        "ship_one",
        lambda intent, python, base: ship_intent.Outcome(intent, ship_intent.REFUSED, "commit: no"),
    )
    ship_intent.write_state(
        tree, {"stage": ship_intent.REFUSED, "step": "commit", "output": "detect secrets Failed"}
    )
    assert fix_pass.run(world["workspace"], fix_cycle.DISPATCH, "claude-bg", NOW) == 0
    assert world["dispatched"] == [(fix_plan.DISPATCH, "claude-bg")]
    assert "carameli agent/i-0919 -- dispatch" in artifact(world)


def test_a_session_that_failed_to_open_is_the_exit_code_and_not_recorded(world, monkeypatch):
    world["failures"] = [failure()]
    monkeypatch.setattr(fix_pass, "dispatch", lambda *a, **k: 1)
    assert fix_pass.run(world["workspace"], fix_cycle.DISPATCH, "claude-bg", NOW) == 1
    assert (
        fix_ledger.read_ledger(
            fix_pass.worktree.boxes_root(world["workspace"].parent) / fix_ledger.LEDGER_NAME
        )
        == {}
    )
    assert "FAILED to open" in artifact(world)


def test_an_update_is_one_gh_call_and_no_session(monkeypatch, tmp_path):
    calls = []

    def gh_for(project_dir):
        def gh(*args):
            calls.append((project_dir.name, args))
            code = 0 if args[2] == "379" else 1
            return subprocess.CompletedProcess(args, code, "", "GraphQL: merge conflict")

        return gh

    monkeypatch.setattr(fix_pass.sweep, "gh_for", gh_for)
    monkeypatch.setattr(fix_pass.fix_prs, "dispatch_pr", lambda *a: pytest.fail("no session"))
    monkeypatch.setattr(fix_pass.fix_prs, "dispatch_fresh", lambda *a: pytest.fail("no session"))
    behind = failure(number=379, behind=True)
    assert (
        fix_pass.dispatch(fix_plan.Decision(fix_plan.UPDATE, "n", (behind,)), tmp_path, "claude")
        == 0
    )
    assert calls == [("carameli", ("pr", "update-branch", "379"))]
    stuck = failure(number=381, behind=True)
    assert fix_pass.update_branch(stuck, tmp_path) == fix_pass.EXIT_FAILED, (
        "GitHub refuses to update a conflicted branch; the next pass reads it as a conflict"
    )


def test_plan_mode_says_an_update_would_be_an_update(world):
    world["failures"] = [failure(behind=True)]
    fix_pass.run(world["workspace"], fix_cycle.PLAN, "claude-bg", NOW)
    assert "carameli #412 -- would update the branch" in artifact(world)


def test_send_all_records_only_what_opened_and_caps_the_rest(world, tmp_path):
    ledger_path = tmp_path / "dispatch.json"
    go = [
        fix_plan.Decision(fix_plan.DISPATCH, "n", (failure(number=1),)),
        fix_plan.Decision(fix_plan.UPDATE, "n", (failure(number=2, behind=True),)),
    ]
    sent, capped, worst = fix_pass.send_all(
        go, ledger_path, tmp_path, fix_cycle.DISPATCH, "claude", NOW
    )
    assert worst == 0 and capped == []
    assert sent == ["carameli #1 -- dispatch", "carameli #2 -- update"]
    assert len(fix_ledger.read_ledger(ledger_path)) == 2
    sent, capped, worst = fix_pass.send_all(
        go, ledger_path, tmp_path, fix_cycle.DISPATCH, "claude", NOW
    )
    assert (
        sent == []
        and [why for _, why in capped]
        == ["already dispatched at " + NOW.isoformat(timespec="seconds")] * 2
    )


def test_dispatch_routes_a_branch_to_the_pr_path_and_the_rest_to_a_fresh_one(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(
        fix_pass.fix_prs,
        "dispatch_pr",
        lambda f, root, agent, runner, key: seen.append(("pr", runner)) or 0,
    )
    monkeypatch.setattr(
        fix_pass.fix_prs,
        "dispatch_fresh",
        lambda d, root, agent, runner, key: seen.append(("fresh", runner)) or 0,
    )
    fix_pass.dispatch(fix_plan.Decision(fix_plan.RESOLVE, "n", (failure(),)), tmp_path, "claude-bg")
    fix_pass.dispatch(
        fix_plan.Decision(fix_plan.DISPATCH, "n", (failure(kind=fix_plan.COMMIT),)),
        tmp_path,
        "claude-bg",
    )
    fix_pass.dispatch(
        fix_plan.Decision(fix_plan.UPSTREAM, "n", (failure(),)), tmp_path, "claude-bg"
    )
    fix_pass.dispatch(
        fix_plan.Decision(fix_plan.DISPATCH, "n", (failure(kind=fix_plan.NIGHTLY),)),
        tmp_path,
        "claude-bg",
    )
    assert [kind for kind, _ in seen] == ["pr", "pr", "fresh", "fresh"]
    assert all(runner is fix_pass.ship_intent.run_quiet for _, runner in seen), (
        "a scheduled pass spawns window-less"
    )


def test_the_release_step_reads_devkits_verdict_and_lands_on_the_record(world):
    """Handed devkit's own default-branch verdict, which decides whether a start can
    succeed, and whether this pass dispatches -- `plan` only says it would."""
    world["branches"]["devkit"] = ("running", None)
    world["release"] = "started -- 1 change(s) v0.11.22 cannot deliver: x"
    assert fix_pass.run(world["workspace"], fix_cycle.DISPATCH, "claude-bg", NOW) == 0
    fix_pass.run(world["workspace"], fix_cycle.PLAN, "claude-bg", NOW)
    assert world["releases"] == [("v0.11.22", "running", True), ("v0.11.22", "running", False)]
    assert "release  started -- 1 change(s)" in artifact(world)


def test_the_cli_reads_the_switch_from_the_workspace_and_forces_the_background_agent_when_scheduled(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "alex.code-workspace"
    workspace.write_text('{"settings": {"devkit.fixPass": "plan"}}', encoding="utf-8")
    seen = []
    monkeypatch.setattr(
        fix_pass, "run", lambda ws, mode, launch, **k: seen.append((mode, launch.agent)) or 0
    )
    assert fix_pass.main(["--scheduled", "--agent", "codex", "--workspace", str(workspace)]) == 0
    assert (
        fix_pass.main(["--mode", "dispatch", "--agent", "codex", "--workspace", str(workspace)])
        == 0
    )
    assert fix_pass.main(["--workspace", str(workspace)]) == 0
    assert seen == [
        (fix_cycle.PLAN, "claude-bg"),
        (fix_cycle.DISPATCH, "codex"),
        (fix_cycle.PLAN, "claude-bg"),
    ]


def test_the_parser_defaults_to_the_background_agent_and_no_mode():
    """No mode means the workspace file decides, which is the scheduled job's whole
    contract; the agent default is the one a scheduler can open."""
    args = fix_pass.build_parser().parse_args([])
    assert (args.mode, args.agent, args.scheduled) == (None, "claude-bg", False)


def test_a_missing_workspace_is_a_usage_error(tmp_path, capsys):
    assert fix_pass.main(["--workspace", str(tmp_path / "nope")]) == fix_pass.EXIT_USAGE
    assert "no workspace file" in capsys.readouterr().err


def test_a_crash_is_written_to_the_record_before_the_traceback(world, monkeypatch, tmp_path):
    """The first real dispatch died on a TypeError and the artifact still described the
    previous pass; a crash is the one outcome the record must not miss."""

    def boom(ws, mode, launch, **_kwargs):
        raise TypeError("run_quiet() got an unexpected keyword argument 'check'")

    monkeypatch.setattr(fix_pass, "run", boom)
    with pytest.raises(TypeError):
        fix_pass.main(["--mode", "plan", "--workspace", str(world["workspace"])])
    assert artifact(world).startswith("fix-pass: CRASHED -- TypeError: run_quiet()")


def test_a_cli_missing_from_path_is_named_before_the_pass_starts(world, monkeypatch, capsys):
    """`gh` installed after VS Code started is absent from every task's PATH; the pass
    died on a `FileNotFoundError` naming no program. It now names it and never starts."""
    monkeypatch.setattr(fix_pass, "missing_tools", lambda: ["gh"])
    monkeypatch.setattr(fix_pass, "run", lambda *a, **k: pytest.fail("the pass started"))
    code = fix_pass.main(["--mode", "dispatch", "--workspace", str(world["workspace"])])
    assert code == fix_pass.EXIT_USAGE
    assert "not usable from this PATH: gh" in capsys.readouterr().err
    assert artifact(world).startswith("fix-pass: FAILED -- not usable from this PATH: gh")
    assert "restart VS Code" in artifact(world)


def test_missing_tools_counts_a_store_alias_and_a_missing_binary_alike(monkeypatch):
    """`python3` found on PATH but exiting 9009 is the Store alias: the ruff hooks refused
    the ship and the post-checkout hook failed `git worktree add`, naming no program."""
    spawned = []

    def fake(argv, **_kwargs):
        spawned.append(argv)
        if argv[0] == "gh":
            raise FileNotFoundError("gh")
        return subprocess.CompletedProcess(argv, 9009 if argv[0] == "python3" else 0)

    monkeypatch.setattr(fix_pass.subprocess, "run", fake)
    assert MISSING_TOOLS() == ["gh", "python3"]
    assert ["python3", "-c", ""] in spawned and ["git", "--version"] in spawned


def test_runs_is_the_exit_code_and_an_unspawnable_binary_is_false(monkeypatch):
    for code, expected in ((0, True), (9009, False)):
        monkeypatch.setattr(
            fix_pass.subprocess,
            "run",
            lambda argv, _c=code, **_k: subprocess.CompletedProcess(argv, _c),
        )
        assert fix_pass.runs(["python3", "-c", ""]) is expected

    def missing(*_a, **_k):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(fix_pass.subprocess, "run", missing)
    assert fix_pass.runs(["gh", "--version"]) is False


def test_missing_tools_is_empty_when_every_tool_runs(monkeypatch):
    monkeypatch.setattr(
        fix_pass.subprocess, "run", lambda argv, **_k: subprocess.CompletedProcess(argv, 0)
    )
    assert MISSING_TOOLS() == []


def test_the_bootstrap_provides_every_cli_the_pass_requires():
    """A fresh machine got no `gh` from `bootstrap-machine.ps1`, and the first pass died."""
    script = (REPO_ROOT / "scripts" / "bootstrap-machine.ps1").read_text(encoding="utf-8")
    for tool in fix_pass.REQUIRED_TOOLS:
        wanted = "Test-Runs 'python3'" if tool == "python3" else f"Command = '{tool}'"
        assert wanted in script, tool


def test_a_switched_off_pass_needs_no_cli(world, monkeypatch):
    monkeypatch.setattr(fix_pass, "missing_tools", lambda: pytest.fail("probed while off"))
    assert fix_pass.main(["--mode", "off", "--workspace", str(world["workspace"])]) == 0


def test_the_artifact_is_written_under_logs(tmp_path):
    path = fix_pass.write_artifact("hello", tmp_path)
    assert path == tmp_path / "logs" / "fix-pass.log"
    assert path.read_text(encoding="utf-8") == "hello\n"


def test_a_blocked_intent_is_said_and_never_shipped_in_any_mode(monkeypatch, tmp_path):
    stuck = ship_intent.Intent(
        "carameli", tmp_path, "master", "S", "B", blocked="master is the default branch"
    )
    monkeypatch.setattr(fix_pass.ship_intent, "find_intents", lambda root, projects: [stuck])
    monkeypatch.setattr(
        fix_pass.ship_intent, "ship_one", lambda *a: pytest.fail("a blocked intent never ships")
    )
    lines, refused, failed = fix_pass.ship_intents(tmp_path, ["carameli"], fix_cycle.DISPATCH)
    assert refused == [] and len(lines) == 1 and not failed
    assert lines[0].startswith("carameli master -- NOT shipped: master is the default branch")
    assert "agent-worktree.py new" in lines[0]


def test_ship_intents_in_plan_mode_only_says_what_it_would_do(monkeypatch, tmp_path):
    one = ship_intent.Intent("carameli", tmp_path, "agent/i", "S", "B")
    monkeypatch.setattr(fix_pass.ship_intent, "find_intents", lambda root, projects: [one])
    monkeypatch.setattr(
        fix_pass.ship_intent, "ship_one", lambda *a: pytest.fail("plan mode ships nothing")
    )
    lines, refused, failed = fix_pass.ship_intents(tmp_path, ["carameli"], fix_cycle.PLAN)
    assert lines == ["carameli agent/i -- would ship: S"] and refused == [] and not failed


# --- what the last review found ---------------------------------------------------------


def test_green_adoptions_are_merged_before_the_red_is_read(world):
    """Read first, the release whose adoptions just went green held the projects for
    one more pass; merged first, the hold lifts on this one."""
    fix_pass.run(world["workspace"], fix_cycle.DISPATCH, "claude", NOW)
    assert world["order"] == [
        ("merge", ["carameli", "devkit"]),
        ("collect", ["carameli", "devkit"]),
    ]


def test_a_project_on_hold_still_ships_its_intent_but_is_not_read(world, monkeypatch):
    """`devkit.onHold` was honoured by the upgrade sweep only; the pass scanned and sent
    sessions at paused checkouts."""
    world["workspace"].write_text(
        '{"folders": [{"path": "devkit"}, {"path": "carameli"}, {"path": "paused"}], '
        '"settings": {"devkit.onHold": ["paused"]}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        fix_pass.devkit_project, "known_projects", lambda _t: ["devkit", "carameli", "paused"]
    )
    seen = []
    monkeypatch.setattr(
        fix_pass.ship_intent, "find_intents", lambda root, projects: seen.append(projects) or []
    )
    fix_pass.run(world["workspace"], fix_cycle.DISPATCH, "claude", NOW)
    assert seen == [["devkit", "carameli", "paused"]]
    assert world["order"] == [
        ("merge", ["carameli", "devkit"]),
        ("collect", ["carameli", "devkit"]),
    ]
    assert "on hold  paused -- nothing red is read there (devkit.onHold)" in artifact(world)


def test_a_blocked_report_marks_the_ledger_and_no_second_session_goes(world):
    """The one channel back from a fixer: what it could not do is on the record as
    "needs a human", and the pass stops spending sessions on it -- for good, not for a
    day, because the session did report."""
    world["failures"] = [failure()]
    fix_pass.run(world["workspace"], fix_cycle.DISPATCH, "claude", NOW)
    key = fix_ledger.decision_key(fix_plan.Decision(fix_plan.DISPATCH, "n", (failure(),)))
    world["blocked"] = [
        fix_pass.fix_reports.Blocked("carameli", Path("t"), "agent/x-0919", key, "needs a database")
    ]
    fix_pass.run(world["workspace"], fix_cycle.DISPATCH, "claude", NOW + _dt.timedelta(days=30))
    assert len(world["dispatched"]) == 1
    text = artifact(world)
    assert "blocked  carameli agent/x-0919 -- needs a database" in text
    assert "capped   carameli #412 -- needs a human: needs a database" in text
    world["blocked"] = [
        fix_pass.fix_reports.Blocked("carameli", Path("t"), "agent/y", "", "hand-picked tree")
    ]
    fix_pass.run(world["workspace"], fix_cycle.PLAN, "claude", NOW)
    assert "blocked  carameli agent/y -- hand-picked tree (no dispatch on the ledger" in artifact(
        world
    )


def test_a_dispatch_past_the_resend_window_is_sent_again(world):
    """A session that died leaves only its ledger entry; after the window the pass looks
    again rather than saying "already dispatched" for a week."""
    world["failures"] = [failure()]
    fix_pass.run(world["workspace"], fix_cycle.DISPATCH, "claude", NOW)
    fix_pass.run(world["workspace"], fix_cycle.DISPATCH, "claude", NOW + _dt.timedelta(hours=5))
    assert len(world["dispatched"]) == 1
    fix_pass.run(world["workspace"], fix_cycle.DISPATCH, "claude", NOW + _dt.timedelta(hours=7))
    assert len(world["dispatched"]) == 2


def test_fixers_go_while_the_failure_moves_and_stop_when_it_does_not(world):
    """The retry policy end to end: a fixer pushes (new sha) and the same test is still
    red -- one more; again -- a person. A fixer that changed what fails made progress."""
    for sha in ("a1", "b2", "c3"):
        world["failures"] = [failure(sha=sha)]
        fix_pass.run(world["workspace"], fix_cycle.DISPATCH, "claude", NOW)
    assert len(world["dispatched"]) == 2
    assert "2 session(s) sent and it is still red unchanged -- needs a human" in artifact(world)
    world["failures"] = [failure(sha="d4", signature=("tests/test_other.py::t",))]
    fix_pass.run(world["workspace"], fix_cycle.DISPATCH, "claude", NOW)
    assert len(world["dispatched"]) == 3


def test_every_pass_leaves_a_line_in_the_history(world):
    """The record is overwritten per pass, so a run of passes that sent nothing left no
    trace of having happened; the history is what shows it."""
    world["failures"] = [failure()]
    for _ in range(2):
        fix_pass.run(world["workspace"], fix_cycle.DISPATCH, "claude", NOW)
    path = world["workspace"].parent / "devkit" / fix_pass.HISTORY
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [len(line["sent"]) for line in lines] == [1, 0]
    assert "already dispatched" in lines[1]["capped"][0]
    assert lines[1]["harness"] == "clean"


def test_the_history_keeps_only_its_last_lines(world, monkeypatch):
    monkeypatch.setattr(fix_pass, "HISTORY_KEEP", 3)
    for _ in range(5):
        fix_pass.run(world["workspace"], fix_cycle.PLAN, "claude", NOW)
    path = world["workspace"].parent / "devkit" / fix_pass.HISTORY
    assert len(path.read_text(encoding="utf-8").splitlines()) == 3


def test_append_history_starts_the_file_and_appends_to_it(tmp_path):
    account = fix_cycle.Account(fix_cycle.PLAN, fix_cycle.harness_state({}, True, []))
    path = fix_pass.append_history(account, NOW, tmp_path)
    assert path == tmp_path / fix_pass.HISTORY
    fix_pass.append_history(account, NOW, tmp_path)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["mode"] for line in lines] == [fix_cycle.PLAN] * 2


def test_a_pr_behind_a_red_base_waits_for_the_bases_fixer(world):
    """The 09-19 pass: carameli's master, #379 and #381, three sessions in one second."""
    world["failures"] = [failure(number=379), failure(number=381, signature=())]
    world["branches"]["carameli"] = (False, red_main(project="carameli"))
    fix_pass.run(world["workspace"], fix_cycle.DISPATCH, "claude", NOW)
    assert world["dispatched"] == [(fix_plan.DISPATCH, "claude")]
    text = artifact(world)
    assert "sent     carameli origin/main -- dispatch" in text
    assert "held     carameli #379 -- held: origin/main is red in carameli" in text
    assert "held     carameli #381 -- held: origin/main is red in carameli" in text


def test_an_open_adoption_goes_and_holds_only_its_own_projects_other_prs(world):
    """The release was still being adopted because this PR was red, and this PR was held
    because the release was still being adopted -- and so was every other project's."""
    world["pending"] = ["carameli"]
    adoption = failure(
        number=7, head="agent/auto/devkit-upgrade-v0-11-22-0921", signature=("lint src/a.py",)
    )
    world["failures"] = [
        adoption,
        failure(number=8),
        failure(project="devkit", number=9, signature=("tests/test_d.py::t",)),
    ]
    fix_pass.run(world["workspace"], fix_cycle.DISPATCH, "claude", NOW)
    assert world["dispatched"] == [(fix_plan.DISPATCH, "claude")] * 2
    text = artifact(world)
    assert "harness  clean" in text
    assert "adopting carameli" in text
    assert "sent     carameli #7 -- dispatch" in text
    assert "sent     devkit #9 -- dispatch" in text
    assert "held     carameli #8 -- held until the newest release is adopted in carameli" in text


def test_record_blocked_marks_only_a_stamped_report(monkeypatch, tmp_path):
    ledger_path = tmp_path / "dispatch.json"
    fix_ledger.record(ledger_path, "pr:carameli:412:abc:d:dispatch", "n", NOW)
    reports = [
        fix_pass.fix_reports.Blocked(
            "carameli", Path("t"), "agent/x", "pr:carameli:412:abc:d:dispatch", "no db"
        ),
        fix_pass.fix_reports.Blocked("carameli", Path("u"), "agent/y", "", "by hand"),
    ]
    monkeypatch.setattr(fix_pass.fix_reports, "find_blocked", lambda root, projects: reports)
    lines = fix_pass.record_blocked(tmp_path, ["carameli"], ledger_path)
    assert lines[0] == "carameli agent/x -- no db"
    assert lines[1].startswith("carameli agent/y -- by hand (no dispatch on the ledger")
    ledger = fix_ledger.read_ledger(ledger_path)
    assert ledger["pr:carameli:412:abc:d:dispatch"]["blocked"] == "no db" and len(ledger) == 1


def test_a_failed_ship_is_the_exit_code(world, monkeypatch):
    one = ship_intent.Intent("carameli", Path("t"), "agent/i-0919", "S", "B")
    world["intents"] = [one]
    monkeypatch.setattr(
        fix_pass.ship_intent,
        "ship_one",
        lambda intent, python, base: ship_intent.Outcome(intent, ship_intent.FAILED, "push: no"),
    )
    assert (
        fix_pass.run(world["workspace"], fix_cycle.DISPATCH, "claude", NOW) == fix_pass.EXIT_FAILED
    )
    assert "shipped  carameli agent/i-0919 -- failed: push: no" in artifact(world)


def test_dispatching_a_refused_commit_sets_its_intent_aside(monkeypatch, tmp_path):
    """With the intent gone, the fixer's edits are a dirty tree with no intent -- a
    session still working -- until it ships; the next pass does not re-run the commit
    stage over half of them."""
    tree = tmp_path / "carameli" / ".claude" / "worktrees" / "i"
    (tree / "logs").mkdir(parents=True)
    (tree / ship_intent.INTENT_FILE).write_text("S\n\nB\n", encoding="utf-8")
    refused = failure(
        kind=fix_plan.COMMIT,
        number=0,
        head="agent/i-0919",
        signature=("commit refused",),
        tree=str(tree),
    )
    keys = []
    monkeypatch.setattr(
        fix_pass.fix_prs, "dispatch_pr", lambda f, root, agent, runner, key: keys.append(key) or 0
    )
    decision = fix_plan.Decision(fix_plan.DISPATCH, "n", (refused,))
    assert fix_pass.dispatch(decision, tmp_path, "claude") == 0
    assert keys == [fix_ledger.decision_key(decision)]
    assert not (tree / ship_intent.INTENT_FILE).exists()
    assert (tree / ship_intent.REFUSED_FILE).read_text(encoding="utf-8") == "S\n\nB\n"
    (tree / ship_intent.INTENT_FILE).write_text("S\n", encoding="utf-8")
    monkeypatch.setattr(fix_pass.fix_prs, "dispatch_pr", lambda *a: 1)
    assert fix_pass.dispatch(decision, tmp_path, "claude") == 1
    assert (tree / ship_intent.INTENT_FILE).exists(), "a session that did not open leaves it"
