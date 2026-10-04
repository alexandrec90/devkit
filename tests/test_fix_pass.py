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
# Kept before `no_session_busy` stubs it, for the one test that drives it.
FIXERS_WORKING = fix_pass.fix_loop.fixers_working
# Kept before `nothing_to_provision` stubs it, for the tests that drive it.
PROVISION_FOR_SHIP = fix_pass.provision_for_ship


@pytest.fixture(autouse=True)
def tools_on_path(monkeypatch):
    """Every CLI the preflight asks for is present unless a test says otherwise."""
    monkeypatch.setattr(fix_pass, "missing_tools", lambda: [])


@pytest.fixture(autouse=True)
def unelevated(monkeypatch):
    """This process holds an ordinary token unless a test says otherwise: a suite run
    from an elevated shell would hand every dispatch here to the real scheduled task."""
    monkeypatch.setattr(fix_pass.agent_tabs, "is_elevated", lambda: False)


@pytest.fixture(autouse=True)
def git_trusted(monkeypatch):
    """Every `git_trust.adopt` the pass makes, recorded and never run: a dispatching
    pass writes git's global config, which is this machine's, not the suite's."""
    adopted: list[tuple[Path, bool]] = []
    monkeypatch.setattr(
        fix_pass.git_trust, "adopt", lambda root, write: adopted.append((root, write)) or ""
    )
    return adopted


@pytest.fixture(autouse=True)
def no_session_busy(monkeypatch):
    """No session on this machine is working in a tree unless a test says one is."""
    monkeypatch.setattr(fix_pass.fix_loop, "fixers_working", frozenset)


@pytest.fixture(autouse=True)
def nothing_to_provision(monkeypatch):
    """Every tree has its toolchain unless a test says otherwise: the real check would
    run `uv sync` in whatever path a test's intent names."""
    monkeypatch.setattr(fix_pass, "provision_for_ship", lambda tree: "")


def test_an_intent_whose_fixer_is_still_busy_waits_for_it(monkeypatch, tmp_path):
    """The pass shipped 0926-19's intent at 04:09 while that session was still fixing a
    group filed after it wrote the intent, and #422's while its sweep kept editing --
    whose later edits the resolver then found unstaged."""
    busy = tmp_path / "busy"
    trees = [ship_intent.Intent("devkit", busy, "agent/b", "S", "B")]
    trees.append(ship_intent.Intent("devkit", tmp_path / "idle", "agent/i", "S", "B"))
    monkeypatch.setattr(fix_pass.ship_intent, "find_intents", lambda root, projects: trees)
    shipped = []
    monkeypatch.setattr(
        fix_pass.ship_intent,
        "ship_one",
        lambda i, p, b: shipped.append(i.branch) or ship_intent.Outcome(i, "shipped", "u"),
    )
    listed = [{"kind": "background", "status": "busy", "cwd": str(busy)}]
    listed.append({"kind": "interactive", "status": "busy", "cwd": str(tmp_path / "idle")})
    monkeypatch.setattr(fix_pass.fix_loop.bg_sessions, "listed", lambda runner: listed)
    monkeypatch.setattr(fix_pass.fix_loop, "fixers_working", FIXERS_WORKING)
    lines, _, _ = fix_pass.ship_intents(tmp_path, ["devkit"], fix_cycle.DISPATCH)
    assert shipped == ["agent/i"], "an interactive supervisor's own tree still ships"
    assert lines[0] == "devkit agent/b -- held: its session is still working in the tree"


def test_an_intent_whose_fixer_waits_on_a_background_task_is_held(monkeypatch, tmp_path):
    """8d2f56f5: 0929-7 ended its turn to wait on the suite it had started, so `claude
    agents` listed it idle and the pass shipped the tree mid-test. The stamped session's
    transcript still had the task out."""
    trees = [ship_intent.Intent("devkit", tmp_path / "waiting", "agent/w", "S", "B")]
    trees.append(ship_intent.Intent("devkit", tmp_path / "done", "agent/d", "S", "B"))
    monkeypatch.setattr(fix_pass.ship_intent, "find_intents", lambda root, projects: trees)
    waiting = {str(tmp_path / "waiting")}
    monkeypatch.setattr(
        fix_pass.fix_loop.fix_reports, "awaiting_task", lambda tree, now: str(tree) in waiting
    )
    shipped = []
    monkeypatch.setattr(
        fix_pass.ship_intent,
        "ship_one",
        lambda i, p, b: shipped.append(i.branch) or ship_intent.Outcome(i, "shipped", "u"),
    )
    lines, _, _ = fix_pass.ship_intents(tmp_path, ["devkit"], fix_cycle.DISPATCH)
    assert shipped == ["agent/d"]
    assert lines[0] == "devkit agent/w -- held: its session is still working in the tree"


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
        "trees": [],
        "spent": [],
        "friction": [],
        "jobs": [],
        "reopen": [],
        "order": [],
        "release": "",
        "releases": [],
        "memory": None,
        "moved": "",
        "installers": [],
        "installers_code": 0,
        "devkit_fixes": [],
        "dependabot": [],
        "dependabot_notes": [],
        "drift": [],
        "issues": [],
        "upkeep": [],
    }
    # GitHub's Dependabot, the checkouts' default branches and their tracker issues are
    # the machine's own: each step is a table entry, and what it was asked is recorded.
    monkeypatch.setattr(
        fix_pass.fix_dependabot,
        "collect",
        lambda ws, projects, now: (list(table["dependabot"]), list(table["dependabot_notes"])),
    )
    monkeypatch.setattr(
        fix_pass.fix_drift,
        "tend",
        lambda ws, projects, mode: table["upkeep"].append(("drift", mode)) or list(table["drift"]),
    )
    monkeypatch.setattr(
        fix_pass.fix_issues,
        "sweep_green",
        lambda ws, projects, mode: (
            table["upkeep"].append(("issues", mode)) or list(table["issues"])
        ),
    )
    # The machine's real scheduler is never touched: `maintain` re-registers tasks.
    monkeypatch.setattr(
        fix_pass.installers,
        "main",
        lambda argv: table["installers"].append(list(argv)) or table["installers_code"],
    )
    monkeypatch.setattr(fix_pass.fix_send.host_memory, "available_mb", lambda: table["memory"])
    # The machine's real boot: a runner started minutes ago would read every stamped
    # session before it as stopped by a restart.
    monkeypatch.setattr(fix_pass.fix_loop.fix_reports, "booted_at", lambda now=None: None)
    monkeypatch.setattr(fix_pass.fix_send, "code_moved", lambda root: table["moved"])
    monkeypatch.setattr(
        fix_pass.devkit_project, "known_projects", lambda _t: ["devkit", "carameli"]
    )
    monkeypatch.setattr(
        fix_pass.ship_intent, "find_intents", lambda root, projects: table["intents"]
    )
    monkeypatch.setattr(fix_pass.push_gate, "interpreter", lambda tree: "py")
    monkeypatch.setattr(
        fix_pass.ship_intent, "is_spent", lambda intent: intent.branch in table["spent"]
    )
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
    # The read-back: the trees, the transcripts and the resolutions are all the machine's
    # own, so each is a table entry here. The harness-defect ledger is real, under tmp.
    loop = fix_pass.fix_loop
    monkeypatch.setattr(loop.fix_reports, "read_trees", lambda root, projects: list(table["trees"]))
    monkeypatch.setattr(loop.session_friction, "harvest", lambda *a, **k: list(table["friction"]))
    monkeypatch.setattr(loop.schedule_health, "query", lambda *a, **k: list(table["jobs"]))
    monkeypatch.setattr(loop.collectors, "scheduled_tasks", lambda *a, **k: {})
    monkeypatch.setattr(loop.collectors, "tray_rows", lambda *a, **k: [])
    monkeypatch.setattr(
        loop.fix_verify,
        "verify",
        lambda *a, **k: loop.fix_verify.Outcome(reopen=list(table["reopen"])),
    )
    monkeypatch.setattr(loop.bg_sessions, "stop_finished", lambda trees, runner: [])
    monkeypatch.setattr(loop, "working_dirs", frozenset)
    monkeypatch.setattr(loop.friction_pending, "detector_fixes", lambda gh, git: [])
    monkeypatch.setattr(loop, "devkit_fixes", lambda ctx: tuple(table["devkit_fixes"]))
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
        fix_pass.fix_red.fix_backlog,
        "ledger_failure",
        lambda devkit_dir, root, in_flight=None: table["backlog"],
    )
    monkeypatch.setattr(
        fix_pass.fix_send,
        "dispatch",
        lambda decision, root, agent, problem="": (
            table["dispatched"].append((decision.action, agent)) or 0
        ),
    )
    monkeypatch.setattr(
        fix_pass.fix_release,
        "merge_green_adoptions",
        lambda root, projects, journal=None: (
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


def test_the_workspace_is_trusted_before_any_git_call_and_written_only_by_a_dispatch(
    world, git_trusted, monkeypatch
):
    """5025d284, e1463857: trees an elevated session made are refused to this unelevated
    pass as dubious ownership. Trusted before the ship step, in every mode that runs;
    git's global config is written only by a pass that acts."""
    order = []
    monkeypatch.setattr(fix_pass, "run", lambda *a, **k: order.append(len(git_trusted)) or 0)
    workspace = str(world["workspace"])
    root = world["workspace"].parent.resolve()
    assert fix_pass.main(["--mode", "off", "--workspace", workspace]) == 0
    assert git_trusted == [], "a switched-off pass runs no git"
    for mode in ("plan", "dispatch"):
        assert fix_pass.main(["--mode", mode, "--workspace", workspace]) == 0
    assert git_trusted == [(root, False), (root, True)]
    assert order == [0, 1, 2], "trusted before the pass runs"


def test_plan_writes_the_whole_plan_and_sends_nothing(world):
    world["failures"] = [failure()]
    world["intents"] = [
        ship_intent.Intent("carameli", Path("t"), "agent/i-0919", "S", "B"),
        ship_intent.Intent("carameli", Path("u"), "agent/old-0919", "Old", "B"),
    ]
    world["spent"] = ["agent/old-0919"]
    assert fix_pass.run(world["workspace"], fix_cycle.PLAN, "claude-bg", NOW) == 0
    text = artifact(world)
    assert "agent/i-0919 -- would ship: S" in text
    assert "agent/old-0919 -- would set aside, already shipped: Old" in text, (
        "a plan says what a dispatch would do, not 'would ship' over merged work"
    )
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


def test_a_dispatching_pass_brings_every_installer_current_and_a_plan_does_not(world):
    """990856e5: #404 moved the scheduled pass behind its watchdog, and the task went on
    running the pass bare until `installers.py maintain`'s next daily fire. The pass
    merges such changes and fires half-hourly, so a dispatching one applies them; a plan
    writes nothing, the scheduler included."""
    assert fix_pass.run(world["workspace"], fix_cycle.PLAN, "claude-bg", NOW) == 0
    assert world["installers"] == []
    assert fix_pass.run(world["workspace"], fix_cycle.DISPATCH, "claude-bg", NOW) == 0
    assert world["installers"] == [["maintain", "--workspace", str(world["workspace"])]]


def dependabot_failure(project: str = "carameli") -> fix_plan.Failure:
    return failure(
        kind=fix_plan.DEPENDABOT,
        project=project,
        number=0,
        head="",
        sha="",
        title=f"Dependabot in {project}: 1 package(s) with alerts no PR answers",
        signature=(f"{fix_plan.ALERT_ENTRY}urllib3 >= 2.8.0",),
    )


def test_what_dependabot_cannot_do_is_sent_recorded_under_its_source_and_capped(world):
    """ibkr_trader's Dependabot failed 11 of 15 runs and nothing read it. Now it is a
    failure like any other -- and a new kind of dispatch, so it has a daily cap."""
    world["dependabot"] = [dependabot_failure("carameli"), dependabot_failure("devkit")]
    world["dependabot_notes"] = ["data-lake -- Dependabot Updates fails only because ..."]
    assert fix_pass.run(world["workspace"], fix_cycle.DISPATCH, "claude-bg", NOW) == 0
    assert len(world["dispatched"]) == 2
    path = fix_pass.worktree.boxes_root(world["workspace"].parent) / fix_ledger.LEDGER_NAME
    ledger = fix_ledger.read_ledger(path)
    assert {entry["source"] for entry in ledger.values()} == {fix_plan.DEPENDABOT}
    text = artifact(world)
    assert "dependabot data-lake -- Dependabot Updates fails only because" in text
    world["dependabot"] = [dependabot_failure("sports_betting")]
    fix_pass.run(world["workspace"], fix_cycle.DISPATCH, "claude-bg", NOW)
    assert len(world["dispatched"]) == 2, "the third of the day waits"
    assert "dependabot daily cap: 2 of 2" in artifact(world)


def test_upkeep_runs_in_every_mode_and_lands_on_the_record(world):
    world["drift"] = [
        "ibkr_trader main -- uv.lock is origin/main's already; main's uv.lock restored"
    ]
    world["issues"] = ["carameli #12 (Nightly) -- would close: green on origin/main at the tip"]
    assert fix_pass.run(world["workspace"], fix_cycle.PLAN, "claude-bg", NOW) == 0
    assert world["upkeep"] == [("drift", fix_cycle.PLAN), ("issues", fix_cycle.PLAN)]
    text = artifact(world)
    assert "drift    ibkr_trader main -- uv.lock is origin/main's already" in text
    assert "issue    carameli #12 (Nightly) -- would close" in text


def test_an_upkeep_line_that_failed_is_filed_against_its_checkout(world, tmp_path):
    world["drift"] = ["ibkr_trader main -- uv.lock relocked: FAILED to cut agent/auto/relock-x"]
    world["issues"] = ["carameli #12 (Nightly) -- FAILED to close: HTTP 403"]
    journal = fix_pass.Journal(tmp_path)
    ctx = fix_pass.context(world["workspace"], fix_cycle.DISPATCH, NOW)
    drift, issues = fix_pass.tend(world["workspace"], ctx, journal)
    assert (drift, issues) == (world["drift"], world["issues"])
    assert [(f.kind, f.project) for f in journal.findings] == [
        ("drift-failed", "ibkr_trader"),
        ("issue-close-failed", "carameli"),
    ]


def test_a_failed_installer_is_filed_for_the_devkit_session(world, monkeypatch, tmp_path):
    """55655d1a: the finding cites a copy of `installers.log`, not the file itself --
    every later `installers.py` run rewrites that, and the evidence went with it."""
    monkeypatch.setattr(fix_pass.installers.sweep, "source_checkout", lambda root: tmp_path)
    artifact = tmp_path / "logs" / "installers.log"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("install-x.py: failed -- Access is denied\n", encoding="utf-8")
    world["installers_code"] = 2
    journal = fix_pass.Journal(tmp_path)
    assert fix_pass.refresh_installers(world["workspace"], journal) == 2
    (finding,) = journal.findings
    assert finding.kind == "installer-failed" and finding.project == "devkit"
    kept = Path(finding.evidence)
    assert kept != artifact and kept.parent == tmp_path / "logs" / "findings"
    artifact.write_text("rewritten by the next run\n", encoding="utf-8")
    assert "Access is denied" in kept.read_text(encoding="utf-8")
    world["installers_code"] = 1  # stale, and repaired: nothing to file
    assert fix_pass.refresh_installers(world["workspace"], journal) == 1
    assert len(journal.findings) == 1


def test_a_pass_whose_code_moved_under_it_sends_no_one_and_asks_to_be_rerun(world):
    """carameli #395 went to a devkit session because #407, which reroutes it, merged
    21s after the watchdog fast-forwarded the checkout: the pass routed with old code."""
    world["failures"] = [failure()]
    world["moved"] = "be69035aa..6276e81bb"
    assert fix_pass.run(world["workspace"], fix_cycle.DISPATCH, "claude-bg", NOW) == 75
    assert world["dispatched"] == []
    assert "held     carameli #412 -- devkit's scripts/ moved be69035aa..6276e81bb" in artifact(
        world
    )


def test_plan_mode_never_asks_whether_the_code_moved(world, monkeypatch):
    monkeypatch.setattr(fix_pass.fix_send, "code_moved", lambda root: pytest.fail("fetched"))
    world["failures"] = [failure()]
    assert fix_pass.run(world["workspace"], fix_cycle.PLAN, "claude-bg", NOW) == 0


def test_hold_if_moved_holds_every_decision_behind_the_range(monkeypatch, tmp_path):
    decision = fix_plan.Decision(fix_plan.DISPATCH, "n", (failure(),))
    earlier = (decision, "already held")
    monkeypatch.setattr(fix_pass.fix_send, "code_moved", lambda root: "")
    ctx = _ctx(tmp_path)
    assert fix_pass.fix_send.hold_if_moved([decision], [earlier], ctx) == (
        [decision],
        [earlier],
        "",
    )
    monkeypatch.setattr(fix_pass.fix_send, "code_moved", lambda root: "aaa..bbb")
    go, held, moved = fix_pass.fix_send.hold_if_moved([decision], [earlier], ctx)
    assert go == [] and moved == "aaa..bbb"
    assert held == [earlier, (decision, "devkit's scripts/ moved aaa..bbb mid-pass; rerun")]


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return done.stdout.strip()


@pytest.fixture
def static(tmp_path):
    """A static checkout on `main`, its origin, and an author who pushes to it."""
    origin, author, static = tmp_path / "origin.git", tmp_path / "author", tmp_path / "devkit"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    _git(tmp_path, "clone", str(origin), str(author))
    for key, value in (("user.email", "t@t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(author, "config", key, value)
    _git(author, "config", "core.hooksPath", str(tmp_path / "no-hooks"))
    (author / "scripts").mkdir()
    (author / "scripts" / "route.py").write_text("OLD = 1\n", encoding="utf-8")
    _git(author, "add", ".")
    _git(author, "commit", "-m", "one")
    _git(author, "push", "origin", "main")
    _git(tmp_path, "clone", str(origin), str(static))
    _git(static, "remote", "set-head", "origin", "main")
    return author, static


def _push(author: Path, path: str) -> None:
    (author / path).parent.mkdir(parents=True, exist_ok=True)
    (author / path).write_text("NEW = 2\n", encoding="utf-8")
    _git(author, "add", ".")
    _git(author, "commit", "-m", f"change {path}")
    _git(author, "push", "origin", "main")


def test_code_moved_names_the_range_only_when_scripts_changed_upstream(static):
    author, checkout = static
    assert fix_pass.fix_send.code_moved(checkout) == "", "current"
    _push(author, "README.md")
    assert fix_pass.fix_send.code_moved(checkout) == "", "a docs-only merge routes nothing"
    _push(author, "scripts/route.py")
    old = _git(checkout, "rev-parse", "HEAD")[:9]
    new = _git(author, "rev-parse", "HEAD")[:9]
    assert fix_pass.fix_send.code_moved(checkout) == f"{old}..{new}"


def test_code_moved_sees_a_fast_forward_made_under_a_running_pass(static, monkeypatch):
    """0b9c6b88: the reconcile job fast-forwarded the static checkout 39s before `send`,
    so HEAD equalled origin while the modules in memory were the pre-#416 ones, and the
    pass crashed on the bug the fast-forward had just brought the fix for."""
    monkeypatch.setattr(fix_pass.fix_send, "LOADED_FROM", {})
    author, checkout = static
    fix_pass.fix_send.pin_loaded(checkout)
    old = _git(checkout, "rev-parse", "HEAD")[:9]
    _push(author, "scripts/route.py")
    _git(checkout, "pull", "--ff-only", "--quiet")  # what reconcile does every 15 minutes
    new = _git(checkout, "rev-parse", "HEAD")[:9]
    assert fix_pass.fix_send.code_moved(checkout) == f"{old}..{new}"


def test_code_moved_leaves_a_branch_of_its_own_and_a_linked_worktree_alone(static, tmp_path):
    author, checkout = static
    _push(author, "scripts/route.py")
    for key, value in (("user.email", "t@t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(checkout, "config", key, value)
    _git(checkout, "config", "core.hooksPath", str(tmp_path / "no-hooks"))
    (checkout / "local.txt").write_text("x\n", encoding="utf-8")
    _git(checkout, "add", ".")
    _git(checkout, "commit", "-m", "local work")
    assert fix_pass.fix_send.code_moved(checkout) == "", "not an ancestor: someone's branch"
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    assert fix_pass.fix_send.code_moved(linked) == ""
    assert fix_pass.fix_send.code_moved(tmp_path / "missing") == "", "a git failure is no move"


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


def test_collect_red_gathers_prs_and_default_branches_and_the_backlog_is_read_apart(world):
    """The backlog is read on its own: a collect step that raises must still leave the
    devkit session something to be sent at, since the crash is on that backlog."""
    backlog = failure(kind=fix_plan.LEDGER, project="devkit", number=0, head="")
    world["failures"] = [failure(number=2)]
    world["branches"] = {"devkit": (False, red_main()), "carameli": (True, None)}
    world["backlog"] = backlog
    failures, green, unread = fix_pass.fix_red.collect_red(
        world["workspace"], ["devkit", "carameli"], []
    )
    assert [f.kind for f in failures] == [fix_plan.PR, fix_plan.BRANCH]
    assert green is False and unread == []
    assert fix_pass.fix_red.backlog_failure(world["workspace"]) == backlog


def test_an_unreadable_devkit_main_is_re_gated_and_holds_nothing_meanwhile(world, monkeypatch):
    """What left six PRs stale: every merge to devkit main was the auto-merge workflow's,
    whose push raises no event, so no gate ever ran at the tip. The harness read RED on
    "could not be read" on every pass, forever, and held every project PR behind it."""
    regated = []
    monkeypatch.setattr(
        fix_pass.fix_red,
        "regate",
        lambda project_dir: regated.append(project_dir.name) or (True, "main -- gate re-run"),
    )
    world["failures"] = [failure(number=2)]
    world["branches"] = {"devkit": (None, None), "carameli": (None, None)}
    assert fix_pass.run(world["workspace"], fix_cycle.DISPATCH, "claude", NOW) == 0
    assert regated == ["devkit", "carameli"]
    text = artifact(world)
    assert "regate   devkit main -- gate re-run" in text
    assert "harness  clean" in text and "could not be read" not in text
    assert world["dispatched"] == [(fix_plan.DISPATCH, "claude")], "carameli #2 is not held"


def test_a_regate_that_failed_leaves_the_harness_unreadable(world, monkeypatch):
    monkeypatch.setattr(
        fix_pass.fix_red, "regate", lambda _d: (False, "main -- FAILED to re-run the gate: x")
    )
    world["failures"] = [failure(number=2)]
    world["branches"]["devkit"] = (None, None)
    assert fix_pass.run(world["workspace"], fix_cycle.DISPATCH, "claude", NOW) == 0
    text = artifact(world)
    assert "regate   devkit main -- FAILED" in text and "could not be read" in text
    assert "held     carameli #2" in text


def test_plan_mode_only_says_it_would_re_gate(world, monkeypatch):
    monkeypatch.setattr(fix_pass.fix_red, "regate", lambda _d: pytest.fail("plan re-gated"))
    world["branches"]["devkit"] = (None, None)
    fix_pass.run(world["workspace"], fix_cycle.PLAN, "claude", NOW)
    assert "regate   devkit -- would re-run the gate" in artifact(world)


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


def test_a_session_that_failed_to_open_is_the_exit_code_not_recorded_and_filed(world, monkeypatch):
    world["failures"] = [failure()]
    monkeypatch.setattr(fix_pass.fix_send, "dispatch", lambda *a, **k: 1)
    assert fix_pass.run(world["workspace"], fix_cycle.DISPATCH, "claude-bg", NOW) == 1
    assert (
        fix_ledger.read_ledger(
            fix_pass.worktree.boxes_root(world["workspace"].parent) / fix_ledger.LEDGER_NAME
        )
        == {}
    )
    text = artifact(world)
    assert "FAILED to dispatch" in text
    assert "filed    carameli dispatch-failed: carameli #412" in text


def test_an_update_is_one_gh_call_and_no_session(monkeypatch, tmp_path):
    calls = []

    def gh_for(project_dir):
        def gh(*args):
            calls.append((project_dir.name, args))
            code = 0 if args[2] == "379" else 1
            return subprocess.CompletedProcess(args, code, "", "GraphQL: merge conflict")

        return gh

    monkeypatch.setattr(fix_pass.fix_send.sweep, "gh_for", gh_for)
    monkeypatch.setattr(fix_pass.fix_prs, "dispatch_pr", lambda *a: pytest.fail("no session"))
    monkeypatch.setattr(fix_pass.fix_prs, "dispatch_fresh", lambda *a: pytest.fail("no session"))
    behind = failure(number=379, behind=True)
    assert (
        fix_pass.fix_send.dispatch(
            fix_plan.Decision(fix_plan.UPDATE, "n", (behind,)), tmp_path, "claude"
        )
        == 0
    )
    assert calls == [("carameli", ("pr", "update-branch", "379"))]
    stuck = failure(number=381, behind=True)
    assert fix_pass.fix_send.update_branch(stuck, tmp_path) == fix_pass.EXIT_FAILED, (
        "GitHub refuses to update a conflicted branch; the next pass reads it as a conflict"
    )


def test_an_update_that_fails_because_the_pr_just_closed_is_not_a_failure(monkeypatch, tmp_path):
    """The pass filed "update-failed #390" 27 seconds after #390 closed, and a sweep spent
    2 calls finding that out. A failed update re-reads the PR before anything is filed."""

    def gh_for(_project_dir):
        def gh(*args):
            if args[:2] == ("pr", "view"):
                return subprocess.CompletedProcess(args, 0, '{"state": "MERGED"}', "")
            return subprocess.CompletedProcess(args, 1, "", "GraphQL: not open")

        return gh

    monkeypatch.setattr(fix_pass.fix_send.sweep, "gh_for", gh_for)
    assert fix_pass.fix_send.update_branch(failure(number=390, behind=True), tmp_path) == 0


def test_a_rerun_is_one_workflow_run_on_the_tip_and_no_session(monkeypatch, tmp_path, capsys):
    """85219e18: a nightly red before main's tip is run again there, and whatever that
    run says is the next pass's to read -- no fixer is spent finding the fix merged."""
    calls = []

    def gh_for(project_dir):
        def gh(*args):
            calls.append((project_dir.name, args))
            code = 0 if args[2] == "nightly.yml" else 1
            return subprocess.CompletedProcess(args, code, "", "HTTP 422: no dispatch trigger")

        return gh

    monkeypatch.setattr(fix_pass.fix_prs.sweep, "gh_for", gh_for)
    monkeypatch.setattr(fix_pass.fix_prs, "dispatch_fresh", lambda *a: pytest.fail("no session"))
    fields = {"kind": fix_plan.NIGHTLY, "project": "ibkr_trader", "number": 69}
    nightly = failure(
        **fields, workflow="Nightly", sha="59a4ef5", tip="b31b60c", rerun_file="nightly.yml"
    )
    decision = fix_plan.Decision(fix_plan.RERUN, "n", (nightly,))
    assert fix_pass.fix_send.dispatch(decision, tmp_path, "claude") == 0
    assert calls == [("ibkr_trader", ("workflow", "run", "nightly.yml", "--ref", "main"))]
    assert "Nightly re-run on origin/main at b31b60c" in capsys.readouterr().out
    refused = failure(**fields, rerun_file="other.yml")
    assert fix_pass.fix_prs.rerun_workflow(refused, tmp_path) == fix_pass.EXIT_FAILED
    assert "workflow run failed: HTTP 422: no dispatch trigger" in capsys.readouterr().err


def test_plan_mode_says_a_rerun_would_be_a_rerun(world, tmp_path):
    nightly = failure(kind=fix_plan.NIGHTLY, number=69, sha="a", tip="b", rerun_file="n.yml")
    go = [fix_plan.Decision(fix_plan.RERUN, "n", (nightly,))]
    sent, _, _ = fix_pass.fix_send.send_all(go, _ctx(tmp_path, fix_cycle.PLAN), "claude")
    assert sent == ["carameli #69 -- would re-run the workflow"]


def test_plan_mode_says_an_update_would_be_an_update(world):
    world["failures"] = [failure(behind=True)]
    fix_pass.run(world["workspace"], fix_cycle.PLAN, "claude-bg", NOW)
    assert "carameli #412 -- would update the branch" in artifact(world)


def test_the_context_carries_when_the_machine_last_started(world, monkeypatch):
    """What lets read-back tell a fixer a restart killed (`fix_reports.INTERRUPTED`) from
    one that died, which the 2026-09-30 power-off filed three of as harness defects."""
    booted = NOW - _dt.timedelta(minutes=3)
    monkeypatch.setattr(fix_pass.fix_loop.fix_reports, "booted_at", lambda now=None: booted)
    ctx = fix_pass.context(world["workspace"], fix_cycle.DISPATCH, NOW)
    assert (ctx.booted, ctx.now, ctx.projects) == (booted, NOW, ["devkit", "carameli"])
    assert ctx.root == world["workspace"].parent and ctx.devkit_dir == ctx.root / "devkit"


def _ctx(tmp_path, mode=fix_cycle.DISPATCH, now=NOW):
    return fix_pass.fix_loop.Context(
        tmp_path,
        ["devkit", "carameli"],
        tmp_path / "devkit",
        tmp_path / "dispatch.json",
        tmp_path / "history.jsonl",
        mode,
        now,
    )


def test_send_all_records_only_what_opened_and_caps_the_rest(world, tmp_path):
    ctx = _ctx(tmp_path)
    go = [
        fix_plan.Decision(fix_plan.DISPATCH, "n", (failure(number=1),)),
        fix_plan.Decision(fix_plan.UPDATE, "n", (failure(number=2, behind=True),)),
    ]
    sent, capped, worst = fix_pass.fix_send.send_all(go, ctx, "claude")
    assert worst == 0 and capped == []
    assert sent == ["carameli #1 -- dispatch", "carameli #2 -- update"]
    assert len(fix_ledger.read_ledger(ctx.ledger_path)) == 2
    sent, capped, worst = fix_pass.fix_send.send_all(go, ctx, "claude")
    assert (
        sent == []
        and [why for _, why in capped]
        == ["already dispatched at " + NOW.isoformat(timespec="seconds")] * 2
    )


def test_a_session_the_machine_has_no_memory_for_is_held_for_the_next_pass(world, tmp_path):
    """Round four sent nine fixers at once and Claude Code killed the supervisor for low
    memory. The probe is read once, so each session sent this pass is charged against
    it -- a just-started session has not grown into its memory yet. An update opens no
    session and is never held; the held ones wait, unrecorded, for the next pass."""
    send = fix_pass.fix_send
    world["memory"] = send.MEMORY_FLOOR_MB + send.SESSION_MB * 3 // 2
    go = [
        fix_plan.Decision(fix_plan.DISPATCH, "n", (failure(number=1),)),
        fix_plan.Decision(fix_plan.DISPATCH, "n", (failure(number=2),)),
        fix_plan.Decision(fix_plan.UPDATE, "n", (failure(number=3, behind=True),)),
    ]
    ctx = _ctx(tmp_path)
    sent, capped, _ = send.send_all(go, ctx, "claude")
    assert sent == ["carameli #1 -- dispatch", "carameli #3 -- update"]
    [(held, why)] = capped
    assert held.failures[0].number == 2 and why.startswith(send.HELD_FOR_MEMORY)
    assert len(fix_ledger.read_ledger(ctx.ledger_path)) == 2, "#2 is free to go next pass"
    world["memory"] = None
    assert send.send_all(go[1:2], ctx, "claude")[0] == ["carameli #2 -- dispatch"], (
        "a probe that cannot read the machine holds nothing"
    )


def test_a_second_devkit_session_waits_for_the_one_still_working(world, tmp_path):
    """Two sessions at one backlog was a waste the dispatch ledger could not see: its
    key changes the moment a new finding lands, while the first session is mid-sweep."""
    upstream = fix_plan.Decision(fix_plan.UPSTREAM, "n", (failure(project="devkit"),))
    busy = fix_pass.fix_loop.Closed(harness_busy="C:/ws/devkit/.claude/worktrees/fix-x")
    sent, capped, _ = fix_pass.fix_send.send_all([upstream], _ctx(tmp_path), "claude", closed=busy)
    assert sent == [] and world["dispatched"] == []
    assert (
        capped[0][1]
        == "held until the devkit session in C:/ws/devkit/.claude/worktrees/fix-x finishes"
    )


SCRAPER_23 = "https://github.com/alexandrec90/social-scraper/pull/23"
ROGUELIKE_52 = "https://github.com/alexandrec90/roguelike/pull/52"


def test_a_consumer_failure_a_devkit_fix_names_waits_for_it(world, tmp_path):
    """f9ebcfd4: a devkit session was sent at social-scraper #23 while #483, whose body
    named it as what it unblocks, had merged and was waiting on adoption. A failure no
    such PR names, or one PR naming only part of the decision, still goes."""
    consumers = (
        failure(project="social-scraper", number=23, url=SCRAPER_23),
        failure(project="roguelike", number=52, url=ROGUELIKE_52),
    )
    upstream = fix_plan.Decision(fix_plan.UPSTREAM, "n", consumers)
    named = fix_pass.fix_loop.Closed(
        devkit_fixes=((483, f"unblocks {ROGUELIKE_52} and\n{SCRAPER_23}, once released"),)
    )
    sent, capped, _ = fix_pass.fix_send.send_all([upstream], _ctx(tmp_path), "claude", closed=named)
    assert sent == [] and world["dispatched"] == []
    assert capped[0][1] == "pending devkit #483, which names every failure it is for"
    part = fix_pass.fix_loop.Closed(devkit_fixes=((483, f"unblocks {SCRAPER_23}"),))
    sent, capped, _ = fix_pass.fix_send.send_all([upstream], _ctx(tmp_path), "claude", closed=part)
    assert len(sent) == 1 and capped == []


def test_named_by_matches_a_whole_url_across_prs():
    decision = fix_plan.Decision(
        fix_plan.UPSTREAM,
        "n",
        (failure(url=SCRAPER_23), failure(kind=fix_plan.LEDGER, url="")),
    )
    named_by = fix_pass.fix_send.named_by
    assert (
        named_by(decision, ((9, f"({SCRAPER_23})"), (7, f"see {SCRAPER_23}."))) == "devkit #7, #9"
    )
    assert named_by(decision, ((9, f"{SCRAPER_23}4"), (8, f"{SCRAPER_23}/files"))) == ""
    ledger_only = fix_plan.Decision(fix_plan.UPSTREAM, "n", (failure(kind=fix_plan.LEDGER),))
    assert named_by(ledger_only, ((9, "anything"),)) == "", "the backlog is not held"


def test_dispatch_routes_a_branch_to_the_pr_path_and_the_rest_to_a_fresh_one(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(
        fix_pass.fix_prs,
        "dispatch_pr",
        lambda f, root, agent, runner, key, problem: seen.append(("pr", runner)) or 0,
    )
    monkeypatch.setattr(
        fix_pass.fix_prs,
        "dispatch_fresh",
        lambda d, root, agent, runner, key, problem: seen.append(("fresh", runner)) or 0,
    )
    fix_pass.fix_send.dispatch(
        fix_plan.Decision(fix_plan.RESOLVE, "n", (failure(),)), tmp_path, "claude-bg"
    )
    fix_pass.fix_send.dispatch(
        fix_plan.Decision(fix_plan.DISPATCH, "n", (failure(kind=fix_plan.COMMIT),)),
        tmp_path,
        "claude-bg",
    )
    fix_pass.fix_send.dispatch(
        fix_plan.Decision(fix_plan.UPSTREAM, "n", (failure(),)), tmp_path, "claude-bg"
    )
    fix_pass.fix_send.dispatch(
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


def _launch():
    return fix_pass.agent_models.Launch.parse("claude-bg", None, None)


def test_a_dispatching_pass_beside_a_running_one_ships_and_sends_nothing(
    tmp_path, monkeypatch, capsys
):
    """babfee68: a supervised pass and the scheduled one started 21 seconds apart and
    shipped the same intents; the loser's push was refused ("cannot lock ref") and filed
    as a ship failure of a branch that had just shipped."""
    workspace = tmp_path / "alex.code-workspace"
    monkeypatch.setattr(fix_pass, "REPO_ROOT", tmp_path / "devkit")
    fix_pass.write_artifact("fix-pass: mode=dispatch\nharness  clean")
    monkeypatch.setattr(fix_pass, "run", lambda *a, **k: pytest.fail("a second pass ran"))
    with fix_pass.worktree.named_lock(tmp_path, fix_pass.RUN_LOCK_NAME, 0.1, 60.0) as held:
        assert held
        code = fix_pass.run_alone(workspace, fix_cycle.DISPATCH, _launch(), wait=0.2, stale=60.0)
    assert code == fix_pass.EXIT_OK
    assert "another dispatching pass holds" in capsys.readouterr().out
    # The running pass is usually the scheduled one, in another checkout: left alone,
    # this checkout's record was the previous pass's, read back as this one's.
    record = (tmp_path / "devkit" / fix_pass.ARTIFACT).read_text(encoding="utf-8")
    assert record.startswith("fix-pass: yielded -- another dispatching pass holds")


def test_a_dispatching_pass_holds_the_lock_while_it_runs_and_releases_it(tmp_path, monkeypatch):
    workspace = tmp_path / "alex.code-workspace"
    lock = fix_pass.worktree.boxes_root(tmp_path) / fix_pass.RUN_LOCK_NAME
    monkeypatch.setattr(fix_pass, "run", lambda *a, **k: 7 if lock.is_dir() else 0)
    assert fix_pass.run_alone(workspace, fix_cycle.DISPATCH, _launch()) == 7
    assert not lock.exists()


def test_a_plan_pass_and_an_unmakeable_lock_run_regardless(tmp_path, monkeypatch):
    """A plan ships and sends nothing, so it never waits; and a lock that could not be made
    at all is no evidence of another pass, so the pass runs as it did before the lock."""
    workspace = tmp_path / "alex.code-workspace"
    monkeypatch.setattr(fix_pass, "run", lambda ws, mode, launch: 5)
    with fix_pass.worktree.named_lock(tmp_path, fix_pass.RUN_LOCK_NAME, 0.1, 60.0):
        assert fix_pass.run_alone(workspace, fix_cycle.PLAN, _launch(), wait=0.1) == 5
    unmakeable = tmp_path / "other"
    unmakeable.mkdir()
    fix_pass.worktree.boxes_root(unmakeable).write_text("a file, not a directory", encoding="utf-8")
    elsewhere = unmakeable / "alex.code-workspace"
    assert fix_pass.run_alone(elsewhere, fix_cycle.DISPATCH, _launch(), wait=0.1) == 5


def test_the_pass_lock_outlives_no_pass_the_watchdog_lets_run():
    """A lock broken while its pass still runs is two passes again; the watchdog stops a
    pass at `TIMEOUT`, so only a lock older than that can be a dead pass's."""
    watchdog = load_script("scripts/fix-pass-watchdog.py")
    assert fix_pass.RUN_LOCK_STALE > watchdog.TIMEOUT.total_seconds()
    assert fix_pass.RUN_LOCK_STALE + fix_pass.RUN_LOCK_WAIT < 2 * watchdog.TIMEOUT.total_seconds()
    # The fire after one the watchdog killed breaks the lock rather than waiting it out.
    assert fix_pass.RUN_LOCK_STALE < watchdog.TIMEOUT.total_seconds() + 5 * 60


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

    monkeypatch.setattr(fix_pass.ship_intent.subprocess, "run", fake)
    assert MISSING_TOOLS() == ["gh", "python3"]
    assert ["python3", "-c", ""] in spawned and ["git", "--version"] in spawned


def test_runs_is_the_exit_code_and_an_unspawnable_binary_is_false(monkeypatch):
    for code, expected in ((0, True), (9009, False)):
        monkeypatch.setattr(
            fix_pass.ship_intent.subprocess,
            "run",
            lambda argv, _c=code, **_k: subprocess.CompletedProcess(argv, _c),
        )
        assert fix_pass.runs(["python3", "-c", ""]) is expected

    def missing(*_a, **_k):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(fix_pass.ship_intent.subprocess, "run", missing)
    assert fix_pass.runs(["gh", "--version"]) is False


def test_missing_tools_is_empty_when_every_tool_runs(monkeypatch):
    monkeypatch.setattr(
        fix_pass.ship_intent.subprocess,
        "run",
        lambda argv, **_k: subprocess.CompletedProcess(argv, 0),
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
    journal = fix_pass.Journal(tmp_path)
    lines, refused, failed = fix_pass.ship_intents(
        tmp_path, ["carameli"], fix_cycle.DISPATCH, journal
    )
    assert refused == [] and len(lines) == 1 and not failed
    assert lines[0].startswith("carameli master -- NOT shipped: master is the default branch")
    # Filed, so the devkit session moves the work to a branch -- not left for a person.
    [found] = journal.findings
    assert (found.kind, found.project, found.evidence) == (
        "intent-unshippable",
        "carameli",
        str(tmp_path),
    )


def test_an_elevated_dispatch_hands_the_pass_to_the_scheduled_task(world, monkeypatch, capsys):
    """VS Code ran elevated, so its "Fix What Is Red" pass refused every background
    launch (an elevated `claude --bg` service locks the scheduled pass out for hours) and
    left each for a pass up to 30 minutes away. It now starts that pass at once instead:
    the scheduled task runs with the user's ordinary token."""
    ran = []
    monkeypatch.setattr(fix_pass.agent_tabs, "is_elevated", lambda: True)
    monkeypatch.setattr(fix_pass, "run", lambda *a: pytest.fail("an elevated pass ran in place"))
    monkeypatch.setattr(
        fix_pass.subprocess,
        "run",
        lambda argv, **k: ran.append(argv) or subprocess.CompletedProcess(argv, 0, "SUCCESS", ""),
    )
    workspace = str(world["workspace"])
    fix_pass.write_artifact("fix-pass: mode=plan\nharness  clean")
    assert fix_pass.main(["--mode", "dispatch", "--workspace", workspace]) == 0
    assert [argv for argv in ran if argv[0] == "schtasks"] == [
        ["schtasks", "/Run", "/TN", fix_pass.SCHEDULED_TASK]
    ]
    assert "handed to the scheduled task" in capsys.readouterr().out
    # The record is rewritten to say so: the supervisor read the previous plan pass's
    # record back three times, as three clean dispatches, while the task ran elsewhere.
    assert artifact(world).startswith(f"fix-pass: handed to {fix_pass.SCHEDULED_TASK} --")
    installer = load_script("scripts/install-fix-pass-task.py")
    assert fix_pass.SCHEDULED_TASK == installer.TASK_NAME


def test_a_scheduled_task_that_will_not_start_is_said_not_swallowed(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(fix_pass, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        fix_pass.subprocess,
        "run",
        lambda argv, **k: subprocess.CompletedProcess(argv, 1, "", "ERROR: Access is denied."),
    )
    assert fix_pass.hand_to_scheduled_task() == fix_pass.EXIT_USAGE
    assert "could not start devkit-fix-pass: ERROR: Access is denied." in capsys.readouterr().err
    record = (tmp_path / fix_pass.ARTIFACT).read_text(encoding="utf-8")
    assert record.startswith("fix-pass: FAILED -- elevated, and could not start devkit-fix-pass")


def test_an_elevated_plan_or_scheduled_pass_still_runs_in_place(world, monkeypatch):
    monkeypatch.setattr(fix_pass.agent_tabs, "is_elevated", lambda: True)
    monkeypatch.setattr(fix_pass, "missing_tools", lambda: [])
    ran = []
    monkeypatch.setattr(fix_pass, "run", lambda ws, mode, launch: ran.append(mode) or 0)
    workspace = str(world["workspace"])
    assert fix_pass.main(["--mode", "plan", "--workspace", workspace]) == 0
    assert ran == [fix_cycle.PLAN]


def test_a_carried_intent_is_recorded_under_the_branch_it_went_out_on(monkeypatch, tmp_path):
    """`ship_one` moves an intent off a retired branch; the record named the retired one,
    so "shipped devkit agent/fix-harness-ledger-0927" read as a merged branch going out
    again -- the one thing the supervisor's rehearsal is there to catch."""
    found = ship_intent.Intent("devkit", tmp_path, "agent/x-0927", "S", "B")
    moved = ship_intent.Intent("devkit", tmp_path, "agent/x-0927-2", "S", "B")
    monkeypatch.setattr(fix_pass.ship_intent, "find_intents", lambda root, projects: [found])
    monkeypatch.setattr(fix_pass.fix_loop, "fixers_working", frozenset)
    monkeypatch.setattr(
        fix_pass.ship_intent,
        "ship_one",
        lambda *a: ship_intent.Outcome(moved, ship_intent.SHIPPED, "u/pull/9", "u/pull/9"),
    )
    monkeypatch.setattr(
        fix_pass.ship_intent, "retired_at", lambda tree, branch: "2026-09-27T00:00:00Z"
    )
    pointed = []
    monkeypatch.setattr(
        fix_pass.fix_loop.triage,
        "repoint",
        lambda old, new, since, root: pointed.append((old, new, since, root)) or ["r1", "r2"],
    )
    [line], _, _ = fix_pass.ship_intents(tmp_path, ["devkit"], fix_cycle.DISPATCH)
    assert line.startswith(
        "devkit agent/x-0927-2 (carried off agent/x-0927; 2 resolution(s) re-pointed) -- shipped"
    )
    # Its session's resolutions named the retired branch, whose PR merged before they were
    # written: `fix_verify` could never settle them, and reopened them two days on.
    assert pointed == [
        ("agent/x-0927", "agent/x-0927-2", "2026-09-27T00:00:00Z", tmp_path / "devkit")
    ]


def test_a_refused_intent_is_recorded_by_the_line_that_says_why(monkeypatch, tmp_path):
    """2026-10-02: the record kept the output's tail -- "refused: mment If a secret has
    already been committed, visit https://help.github.com/..." -- and the line naming the
    failed hook, further up, never reached it."""
    one = ship_intent.Intent("sports_betting", tmp_path, "agent/i", "S", "B")
    output = (
        "Detect secrets...........................Failed\n- hook id: detect-secrets\n"
        + "boilerplate a reader needs nothing from\n" * 20
        + "If a secret has already been committed, visit https://help.github.com/x\n"
    )
    monkeypatch.setattr(fix_pass.ship_intent, "find_intents", lambda root, projects: [one])
    monkeypatch.setattr(fix_pass.fix_loop, "fixers_working", frozenset)
    monkeypatch.setattr(fix_pass, "provision_for_ship", lambda tree: "")

    def refuse(*_a):
        ship_intent.write_state(tmp_path, {"stage": "refused", "step": "fixers", "output": output})
        return ship_intent.Outcome(one, ship_intent.REFUSED, f"fixers: {output.strip()[-400:]}")

    monkeypatch.setattr(fix_pass.ship_intent, "ship_one", refuse)
    [line], [failure], _ = fix_pass.ship_intents(tmp_path, ["sports_betting"], fix_cycle.DISPATCH)
    assert line == (
        "sports_betting agent/i -- refused: fixers: Detect secrets...........................Failed"
    )
    assert failure.tree == str(tmp_path)


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


def test_a_project_on_hold_is_read_and_fixed_like_any_other(world, monkeypatch):
    """The pass once skipped `devkit.onHold` checkouts, and three "unwire the agent
    hooks" PRs sat red in them indefinitely: a PR that exists is work in flight, and
    nothing but the pass would ever move it."""
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
        ("merge", ["carameli", "devkit", "paused"]),
        ("collect", ["carameli", "devkit", "paused"]),
    ]
    assert "on hold" not in artifact(world)


def _blocked_tree(world, tmp_path, reason: str) -> Path:
    """A fixer's tree, stamped the way `fix-prs` stamps it, holding a blocked report."""
    decision = fix_plan.Decision(fix_plan.DISPATCH, "n", (failure(),))
    tree = tmp_path / "carameli" / ".claude" / "worktrees" / "x"
    (tree / "logs").mkdir(parents=True, exist_ok=True)
    reports = fix_pass.fix_loop.fix_reports
    reports.stamp(
        tree, fix_ledger.decision_key(decision), "n", NOW, problem=fix_ledger.problem_key(decision)
    )
    (tree / reports.BLOCKED_FILE).write_text(reason, encoding="utf-8")
    world["trees"] = [reports.Tree("carameli", tree, "agent/x-0919", reports.read_stamp(tree), ())]
    return tree


def _open_findings(tmp_path):
    triage = fix_pass.fix_loop.triage
    return triage.open_items(triage.load(tmp_path / "devkit"))


def test_a_blocked_report_is_escalated_and_fresh_fixers_follow_its_resolution(world, tmp_path):
    """A blocked fixer used to park its problem as "needs a human" forever. Now it is a
    finding the devkit session takes over, and once that is resolved the problem gets
    fresh fixers -- no person anywhere in the loop."""
    world["failures"] = [failure()]
    fix_pass.run(world["workspace"], fix_cycle.DISPATCH, "claude", NOW)
    tree = _blocked_tree(world, tmp_path, "needs a database")
    later = NOW + _dt.timedelta(days=30)
    fix_pass.run(world["workspace"], fix_cycle.DISPATCH, "claude", later)
    assert len(world["dispatched"]) == 1
    text = artifact(world)
    assert "blocked  carameli agent/x-0919 -- needs a database" in text
    assert "filed    carameli fixer-blocked: carameli agent/x-0919: needs a database" in text
    assert "capped   carameli #412 -- escalated: the devkit session has it" in text
    assert "needs a human" not in text
    assert not (tree / "logs" / "fix-blocked.md").exists(), "filed away, so read once"

    [finding] = _open_findings(tmp_path)
    triage = fix_pass.fix_loop.triage
    triage.resolve([finding.id], "the fixture now makes its database", root=tmp_path / "devkit")
    world["trees"] = []
    fix_pass.run(world["workspace"], fix_cycle.DISPATCH, "claude", later + _dt.timedelta(hours=1))
    assert len(world["dispatched"]) == 2, "resolved, the problem starts over"


def test_a_dispatch_past_the_resend_window_is_sent_again(world):
    """A session that died leaves only its ledger entry; after the window the pass looks
    again rather than saying "already dispatched" for a week."""
    world["failures"] = [failure()]
    fix_pass.run(world["workspace"], fix_cycle.DISPATCH, "claude", NOW)
    fix_pass.run(world["workspace"], fix_cycle.DISPATCH, "claude", NOW + _dt.timedelta(hours=5))
    assert len(world["dispatched"]) == 1
    fix_pass.run(world["workspace"], fix_cycle.DISPATCH, "claude", NOW + _dt.timedelta(hours=7))
    assert len(world["dispatched"]) == 2


def test_fixers_go_while_the_failure_moves_and_escalate_when_it_does_not(world, tmp_path):
    """The retry policy end to end: a fixer pushes (new sha) and the same test is still
    red -- one more; again -- the devkit session, filed with the problem as its key. A
    fixer that changed what fails made progress, and goes."""
    for sha in ("a1", "b2", "c3"):
        world["failures"] = [failure(sha=sha)]
        fix_pass.run(world["workspace"], fix_cycle.DISPATCH, "claude", NOW)
    assert len(world["dispatched"]) == 2
    text = artifact(world)
    assert "escalated to the devkit session: 2 fixer session(s) left it red and unchanged" in text
    assert "filed    carameli fixers-exhausted: carameli #412" in text
    problem = fix_ledger.problem_key(fix_plan.Decision(fix_plan.DISPATCH, "n", (failure(),)))
    assert [i.fields.get("key") for i in _open_findings(tmp_path)] == [problem]
    fix_pass.run(world["workspace"], fix_cycle.DISPATCH, "claude", NOW)
    assert "escalated: the devkit session has it" in artifact(world)
    assert len(_open_findings(tmp_path)) == 1, "filed once, not once per pass"
    world["failures"] = [failure(sha="d4", signature=("tests/test_other.py::t",))]
    fix_pass.run(world["workspace"], fix_cycle.DISPATCH, "claude", NOW)
    assert len(world["dispatched"]) == 3


def test_a_step_that_raises_is_filed_and_the_devkit_session_still_goes(
    world, monkeypatch, tmp_path
):
    """The crash that stops every later step is the one a self-correcting loop cannot
    report. Collecting the red raises here, and the devkit session still goes -- at the
    backlog that now carries the crash."""
    world["backlog"] = failure(
        kind=fix_plan.LEDGER, project="devkit", number=0, head="", signature=("x",)
    )

    def boom(*_a, **_k):
        raise RuntimeError("gh answered garbage")

    monkeypatch.setattr(fix_pass.fix_red, "collect_red", boom)
    assert (
        fix_pass.run(world["workspace"], fix_cycle.DISPATCH, "claude", NOW) == fix_pass.EXIT_FAILED
    )
    assert world["dispatched"] == [(fix_plan.UPSTREAM, "claude")]
    text = artifact(world)
    assert "filed    devkit pass-step-crashed: fix-pass step 'collect' raised RuntimeError" in text
    [found] = _open_findings(tmp_path)
    assert Path(found.fields["evidence"]).read_text(encoding="utf-8").startswith("Traceback")


def test_a_plan_that_raises_sends_only_the_devkit_session(world, monkeypatch):
    world["failures"] = [failure(number=2)]
    world["backlog"] = failure(
        kind=fix_plan.LEDGER, project="devkit", number=0, head="", signature=("x",)
    )
    monkeypatch.setattr(fix_pass.fix_plan, "plan", lambda *a: [][1])
    fix_pass.run(world["workspace"], fix_cycle.DISPATCH, "claude", NOW)
    assert world["dispatched"] == [(fix_plan.UPSTREAM, "claude")]
    assert "the plan step raised; only the devkit session goes" in artifact(world)


def test_plan_mode_says_what_it_would_file_and_files_nothing(world, tmp_path):
    world["friction"] = [
        fix_pass.Finding(
            "poll", "carameli", "sleep N", event=fix_pass.fix_loop.fix_findings.FRICTION
        )
    ]
    fix_pass.run(world["workspace"], fix_cycle.PLAN, "claude", NOW)
    text = artifact(world)
    assert "filed    would file: carameli poll: sleep N" in text
    assert "ledger   0 open on the harness-defect ledger" in text
    assert _open_findings(tmp_path) == []


def test_friction_is_filed_once_while_it_stays_open(world, tmp_path):
    world["friction"] = [
        fix_pass.Finding(
            "poll", "carameli", "sleep N", event=fix_pass.fix_loop.fix_findings.FRICTION
        )
    ]
    fix_pass.run(world["workspace"], fix_cycle.DISPATCH, "claude", NOW)
    fix_pass.run(world["workspace"], fix_cycle.DISPATCH, "claude", NOW)
    assert len(_open_findings(tmp_path)) == 1
    assert "ledger   1 open on the harness-defect ledger" in artifact(world)


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


def test_publish_prints_writes_the_record_and_adds_a_history_line(tmp_path, capsys):
    account = fix_cycle.Account(fix_cycle.PLAN, fix_cycle.harness_state({}, True, []))
    path = fix_pass.publish(account, NOW, tmp_path)
    assert path == tmp_path / fix_pass.ARTIFACT
    assert path.read_text(encoding="utf-8").strip() == fix_cycle.render(account).strip()
    assert f"fix-pass: record at {path}" in capsys.readouterr().out
    assert len((tmp_path / fix_pass.HISTORY).read_text(encoding="utf-8").splitlines()) == 1


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
    assert "filed    carameli ship-failed: carameli agent/i-0919: push: no" in artifact(world)


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
        fix_pass.fix_prs,
        "dispatch_pr",
        lambda f, root, agent, runner, key, problem: keys.append(key) or 0,
    )
    decision = fix_plan.Decision(fix_plan.DISPATCH, "n", (refused,))
    assert fix_pass.fix_send.dispatch(decision, tmp_path, "claude") == 0
    assert keys == [fix_ledger.decision_key(decision)]
    assert not (tree / ship_intent.INTENT_FILE).exists()
    assert (tree / ship_intent.REFUSED_FILE).read_text(encoding="utf-8") == "S\n\nB\n"
    (tree / ship_intent.INTENT_FILE).write_text("S\n", encoding="utf-8")
    monkeypatch.setattr(fix_pass.fix_prs, "dispatch_pr", lambda *a: 1)
    assert fix_pass.fix_send.dispatch(decision, tmp_path, "claude") == 1
    assert (tree / ship_intent.INTENT_FILE).exists(), "a session that did not open leaves it"


def test_decide_is_the_plan_the_classes_and_the_phase_over_what_was_read():
    harness, go, held, skipped = fix_pass.decide([failure(number=2)], True, "v0.11.22", [], ())
    assert harness.clean and [d.action for d in go] == [fix_plan.DISPATCH]
    assert held == [] and skipped == []


def test_decide_hands_the_adoption_prefixes_to_the_classes():
    """A ratchet red on an adoption may be the ratchet itself moving, which is devkit's
    (the v0.11.21 fan-out). Classed without the prefixes it read as the PR's own ratchet,
    and the harness stayed clean while the adoption waited on a fixer that cannot help."""
    ratchet = (
        "scripts/hooks/tests/test_structure_check.py::test_nothing_is_new_or_worse_than_the_baseline",
    )
    red = failure(head="agent/auto/devkit-upgrade-v0-11-25-0926", signature=ratchet)
    prefixes = ("agent/auto/devkit-upgrade-", "agent/devkit-upgrade-")
    harness, _, _, _ = fix_pass.decide([red], True, "v0.11.25", [], prefixes)
    assert not harness.clean and harness.reasons == ("1 harness failure(s) open",)


def test_a_session_working_in_a_branch_tree_holds_a_fixer_for_that_branch(world, tmp_path):
    """The first supervised run sent a fixer into the worktree an interactive session was
    in. A live session there is a wait, and the record says whose."""
    busy = fix_pass.fix_loop.Closed(busy={("carameli", "agent/x-0919"): "C:/t/x"})
    sent, capped, _ = fix_pass.fix_send.send_all(
        [fix_plan.Decision(fix_plan.DISPATCH, "n", (failure(),))],
        _ctx(tmp_path),
        "claude",
        closed=busy,
    )
    assert sent == [] and capped[0][1] == "a session is working in C:/t/x"
    other = fix_plan.Decision(fix_plan.DISPATCH, "n", (failure(head="agent/y"),))
    assert fix_pass.fix_send.send_all([other], _ctx(tmp_path), "claude", closed=busy)[0]


def test_a_session_in_a_consumers_branch_tree_never_holds_the_devkit_session(world, tmp_path):
    """2026-10-02 supervision: sports_betting's refused intent was folded with the ledger
    into the one upstream decision, and the idle interactive session in that intent's
    tree capped it on every pass -- the backlog grew 0 -> 1 -> 2 with no devkit session
    sent. That session opens a fresh devkit tree (`dispatch_fresh`), never the branch's."""
    refused = failure(project="sports_betting", kind=fix_plan.COMMIT, number=0, head="worktree-h")
    upstream = fix_plan.Decision(fix_plan.UPSTREAM, "n", (refused,))
    busy = fix_pass.fix_loop.Closed(busy={("sports_betting", "worktree-h"): "C:/t/h"})
    sent, capped, _ = fix_pass.fix_send.send_all([upstream], _ctx(tmp_path), "claude", closed=busy)
    assert capped == [] and len(sent) == 1
    assert world["dispatched"] == [(fix_plan.UPSTREAM, "claude")]


def test_a_tree_without_the_projects_pre_commit_is_provisioned_before_it_ships(
    monkeypatch, tmp_path
):
    """2026-10-02 supervision: sports_betting's worktree-harmonic-humming-kay had no
    `.venv`, nor had its checkout, so `ship.py --fix` refused ("provision first") on every
    pass and the refusal went to a devkit session. Provisioning is one command, and
    anything a script can do, no session does. The interpreter is read after it, so the
    commit stage runs with the `.venv` just made."""
    tree = tmp_path / "tree"
    one = ship_intent.Intent("sports_betting", tree, "worktree-h", "S", "B")
    monkeypatch.setattr(fix_pass.ship_intent, "find_intents", lambda root, projects: [one])
    order: list[str] = []
    monkeypatch.setattr(fix_pass, "provision_for_ship", PROVISION_FOR_SHIP)
    monkeypatch.setattr(fix_pass.sweep, "source_checkout", lambda root: tmp_path / "checkout")
    step = fix_pass.worktree.ProvisionStep("uv sync", ("uv", "sync"))
    monkeypatch.setattr(fix_pass.worktree, "plan_provision", lambda path, quiet: (step,))
    monkeypatch.setattr(
        fix_pass.worktree,
        "run_provision",
        lambda path, steps: order.append(f"provision {path.name}") or (True, ["provisioned"]),
    )
    monkeypatch.setattr(fix_pass.push_gate, "interpreter", lambda root: order.append("ask") or "py")
    monkeypatch.setattr(
        fix_pass.ship_intent,
        "ship_one",
        lambda i, p, b: order.append(f"ship {p}") or ship_intent.Outcome(i, "shipped", "u"),
    )
    lines, _, _ = fix_pass.ship_intents(tmp_path, ["sports_betting"], fix_cycle.DISPATCH)
    assert order == ["provision tree", "ask", "ship py"]
    assert lines[0] == "sports_betting worktree-h -- provisioned its toolchain: uv sync"

    # A tree whose own pre-commit is there is left as it is.
    order.clear()
    (tree / ".venv" / "Scripts").mkdir(parents=True)
    (tree / ".venv" / "Scripts" / "pre-commit.exe").write_text("", encoding="utf-8")
    (tree / ".venv" / "bin").mkdir(parents=True)
    (tree / ".venv" / "bin" / "pre-commit").write_text("", encoding="utf-8")
    fix_pass.ship_intents(tmp_path, ["sports_betting"], fix_cycle.DISPATCH)
    assert order == ["ask", "ship py"]


def test_a_failed_provision_is_said_and_the_ship_still_tries(monkeypatch, tmp_path):
    """The commit stage's refusal is still the failure the plan places; the record says
    why the tree had no toolchain to refuse with."""
    one = ship_intent.Intent("sports_betting", tmp_path / "t", "worktree-h", "S", "B")
    monkeypatch.setattr(fix_pass.ship_intent, "find_intents", lambda root, projects: [one])
    monkeypatch.setattr(fix_pass, "provision_for_ship", PROVISION_FOR_SHIP)
    monkeypatch.setattr(fix_pass.sweep, "source_checkout", lambda root: root)
    step = fix_pass.worktree.ProvisionStep("uv sync", ("uv", "sync"))
    monkeypatch.setattr(fix_pass.worktree, "plan_provision", lambda path, quiet: (step,))
    failed = ["FAILED provision: uv sync failed: no Python 3.12"]
    monkeypatch.setattr(fix_pass.worktree, "run_provision", lambda path, steps: (False, failed))
    monkeypatch.setattr(fix_pass.push_gate, "interpreter", lambda root: "py")
    monkeypatch.setattr(
        fix_pass.ship_intent, "ship_one", lambda i, p, b: ship_intent.Outcome(i, "shipped", "u")
    )
    lines, _, _ = fix_pass.ship_intents(tmp_path, ["sports_betting"], fix_cycle.DISPATCH)
    assert lines[0] == (
        "sports_betting worktree-h -- FAILED to provision: "
        "FAILED provision: uv sync failed: no Python 3.12"
    )
    assert lines[1].startswith("sports_betting worktree-h -- shipped")


def test_a_refused_ships_detail_is_one_record_line(world, monkeypatch):
    one = ship_intent.Intent("carameli", Path("t"), "agent/i-0919", "S", "B")
    world["intents"] = [one]
    detail = "fixers: check yaml...Passed\nDetect secrets....Failed\n\n- hook id: detect-secrets\n"
    monkeypatch.setattr(
        fix_pass.ship_intent,
        "ship_one",
        lambda i, p, b: ship_intent.Outcome(i, ship_intent.REFUSED, detail),
    )
    monkeypatch.setattr(
        fix_pass.ship_intent, "refusal_failure", lambda o, b: failure(kind=fix_plan.COMMIT)
    )
    lines, _, _ = fix_pass.ship_intents(Path("r"), ["carameli"], fix_cycle.DISPATCH)
    assert lines == [
        "carameli agent/i-0919 -- refused: fixers: check yaml...Passed Detect secrets....Failed - hook id: detect-secrets"
    ]


def test_a_pass_started_outside_utf8_mode_reruns_itself_in_it(monkeypatch):
    """A launcher that is neither the watchdog nor the task dispatcher got the
    `'charmap' codec can't decode byte 0x9d` traceback from a reader thread; the pass
    now puts itself in UTF-8 mode, and hands back the rerun's exit code."""
    ran = []
    handles = []

    def fake_run(argv, *, check, creationflags, stdin=None):
        ran.append(argv)
        handles.append(stdin)
        return subprocess.CompletedProcess(argv, 4)

    monkeypatch.setattr(fix_pass.subprocess, "run", fake_run)
    monkeypatch.setattr(fix_pass.sweep, "console_python", lambda: "python.exe")
    assert fix_pass.in_utf8_mode(["--mode", "plan"], utf8_mode=0) == 4
    assert ran == [
        ["python.exe", "-X", "utf8", str(REPO_ROOT / "scripts" / "fix-pass.py"), "--mode", "plan"]
    ]
    # Handed no std handle at all, a `CREATE_NO_WINDOW` child writes to a hidden console
    # of its own: `fix-pass.py --mode plan` from a terminal printed nothing, not even the
    # record's path (2026-10-03). Naming any one makes Windows pass the other two through.
    assert handles == [subprocess.DEVNULL], "the rerun's output never reached the caller"
    assert fix_pass.in_utf8_mode(["--mode", "plan"], utf8_mode=1) is None
    assert len(ran) == 1, "a pass already in UTF-8 mode started a second one"
