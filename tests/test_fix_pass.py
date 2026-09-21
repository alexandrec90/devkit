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
import fix_plan
import ship_intent

fix_pass = load_script("scripts/fix-pass.py")

NOW = _dt.datetime(2026, 9, 19, 9, 0, tzinfo=_dt.UTC)
VENDORED = ("scripts/hooks/tests/test_a.py::t",)


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
    }
    monkeypatch.setattr(
        fix_pass.devkit_project, "known_projects", lambda _t: ["devkit", "carameli"]
    )
    monkeypatch.setattr(
        fix_pass.ship_intent, "find_intents", lambda root, projects: table["intents"]
    )
    monkeypatch.setattr(fix_pass.push_gate, "interpreter", lambda tree: "py")
    monkeypatch.setattr(fix_pass.tb, "detect_default_branch", lambda git, fallback="main": "main")
    monkeypatch.setattr(
        fix_pass.ship_intent,
        "ship_one",
        lambda intent, python, base: (
            table["shipped"].append(intent.branch)
            or ship_intent.Outcome(intent, ship_intent.SHIPPED, "u")
        ),
    )
    monkeypatch.setattr(fix_pass.menu, "scan", lambda _ws: {"devkit": [], "carameli": []})
    monkeypatch.setattr(
        fix_pass.gate_evidence, "collect", lambda _ws, _found: list(table["failures"])
    )
    monkeypatch.setattr(fix_pass.gate_evidence, "newest_release", lambda _d: "v0.11.22")
    monkeypatch.setattr(
        fix_pass.gate_evidence,
        "collect_default_branches",
        lambda _ws, _projects: dict(table["branches"]),
    )
    monkeypatch.setattr(fix_pass, "pending_adoptions", lambda root, projects, tag: table["pending"])
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
    monkeypatch.setattr(fix_pass, "merge_green_adoptions", lambda root, projects: table["merged"])
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
    ledger = fix_plan.read_ledger(
        fix_pass.worktree.boxes_root(world["workspace"].parent) / fix_plan.LEDGER_NAME
    )
    assert list(ledger) == [
        fix_plan.decision_key(fix_plan.Decision(fix_plan.DISPATCH, "n", (failure(),)))
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
    is harness red like a vendored test is, and goes to the one devkit session."""
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
    assert world["dispatched"] == [(fix_plan.UPSTREAM, "claude")]
    text = artifact(world)
    assert "upstream devkit ledger -- the harness is red" in text
    assert "held     carameli #2" in text


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
        fix_plan.read_ledger(
            fix_pass.worktree.boxes_root(world["workspace"].parent) / fix_plan.LEDGER_NAME
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
    assert len(fix_plan.read_ledger(ledger_path)) == 2
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
        lambda f, root, agent, runner, options=None: seen.append(("pr", runner)) or 0,
    )
    monkeypatch.setattr(
        fix_pass.fix_prs,
        "dispatch_fresh",
        lambda d, root, agent, runner, options=None: seen.append(("fresh", runner)) or 0,
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


def test_pending_adoptions_names_the_consumers_still_adopting(monkeypatch, tmp_path):
    for name in ("devkit", "carameli", "roguelike"):
        (tmp_path / name).mkdir()
    monkeypatch.setattr(
        fix_pass.adoption_prs,
        "open_adoption_pr",
        lambda d, tag: "#1 u" if d.name == "roguelike" else "",
    )
    assert fix_pass.pending_adoptions(
        tmp_path, ["devkit", "carameli", "roguelike"], "v0.11.22"
    ) == ["roguelike"]
    assert fix_pass.pending_adoptions(tmp_path, ["carameli"], "") == []


def test_green_adoptions_are_merged_through_the_reconcile_merge(monkeypatch, tmp_path):
    (tmp_path / "carameli").mkdir()
    rows = [
        {
            "number": 5,
            "headRefName": "agent/auto/devkit-upgrade-v0-11-22-0919",
            "labels": [{"name": "automerge"}],
            "statusCheckRollup": [{"conclusion": "SUCCESS"}],
            "mergeable": "MERGEABLE",
        }
    ]
    monkeypatch.setattr(
        fix_pass.sweep,
        "gh_for",
        lambda d: lambda *a: type("R", (), {"returncode": 0, "stdout": json.dumps(rows)})(),
    )
    merged = []
    monkeypatch.setattr(
        fix_pass.worktree, "merge_pr", lambda gh, n: merged.append(n) or (True, f"merged PR #{n}")
    )
    lines = fix_pass.merge_green_adoptions(tmp_path, ["devkit", "carameli"])
    assert merged == [5] and lines == ["carameli #5 -- merged PR #5"]


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
    lines, refused = fix_pass.ship_intents(tmp_path, ["carameli"], fix_cycle.DISPATCH)
    assert refused == [] and len(lines) == 1
    assert lines[0].startswith("carameli master -- NOT shipped: master is the default branch")
    assert "agent-worktree.py new" in lines[0]


def test_ship_intents_in_plan_mode_only_says_what_it_would_do(monkeypatch, tmp_path):
    one = ship_intent.Intent("carameli", tmp_path, "agent/i", "S", "B")
    monkeypatch.setattr(fix_pass.ship_intent, "find_intents", lambda root, projects: [one])
    monkeypatch.setattr(
        fix_pass.ship_intent, "ship_one", lambda *a: pytest.fail("plan mode ships nothing")
    )
    lines, refused = fix_pass.ship_intents(tmp_path, ["carameli"], fix_cycle.PLAN)
    assert lines == ["carameli agent/i -- would ship: S"] and refused == []
