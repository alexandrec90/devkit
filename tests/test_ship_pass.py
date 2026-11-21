"""`scripts/ship-pass.py`: which intents a fire takes, and what it leaves behind.

Every intent, spawn and network call is replaced: how one ships is `ship_intent`'s, and is
tested in `tests/test_ship_intent.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from support import REPO_ROOT, load_script

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import fix_cycle
import ship_intent

ship_pass = load_script("scripts/ship-pass.py")
ship_step = ship_pass.ship_step


def intent(tree: Path, project: str = "roguelike", direct: bool = True) -> ship_intent.Intent:
    return ship_intent.Intent(project, tree, f"agent/{tree.name}", "S", "B", direct=direct)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A workspace on one switch setting, with what would ship recorded."""
    (tmp_path / "devkit").mkdir()
    workspace = tmp_path / "alex.code-workspace"
    table: dict = {"intents": [], "shipped": [], "workspace": workspace, "root": tmp_path}

    def fix_pass_mode(mode: str) -> None:
        workspace.write_text(
            '{"folders": [{"path": "devkit"}, {"path": "roguelike"}], '
            f'"settings": {{"devkit.fixPass": "{mode}"}}}}',
            encoding="utf-8",
        )

    table["mode"] = fix_pass_mode
    fix_pass_mode("dispatch")
    monkeypatch.setattr(ship_pass, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        ship_pass.ship_intent, "find_intents", lambda root, projects: list(table["intents"])
    )
    monkeypatch.setattr(ship_pass.ship_intent, "stored_refusal", lambda i: None)
    monkeypatch.setattr(ship_step.fix_loop, "fixers_working", frozenset)
    monkeypatch.setattr(ship_step, "provision_for_ship", lambda tree: "")
    monkeypatch.setattr(ship_step.push_gate, "interpreter", lambda tree: "py")
    monkeypatch.setattr(ship_step.fix_loop, "why_held", lambda busy, tree, now: "")

    def ship_one(one, python, base):
        table["shipped"].append(one.branch)
        return ship_intent.Outcome(one, ship_intent.SHIPPED, "u")

    monkeypatch.setattr(ship_pass.ship_intent, "ship_one", ship_one)
    return table


def record(world) -> str:
    return (world["root"] / ship_pass.ARTIFACT).read_text(encoding="utf-8")


def test_a_direct_intent_is_taken_whatever_the_switch_says():
    direct = intent(Path("d"))
    assert all(ship_pass.takes(direct, mode) for mode in fix_cycle.MODES)


def test_a_gated_intent_is_taken_only_where_the_switch_is_off():
    """A second PC set to `off` held four intents nobody would ever ship, because shipping
    and sending fixers were one switch. `plan` is a dry run: it must do nothing."""
    gated = intent(Path("g"), direct=False)
    assert ship_pass.takes(gated, fix_cycle.OFF)
    assert not ship_pass.takes(gated, fix_cycle.PLAN)
    assert not ship_pass.takes(gated, fix_cycle.DISPATCH)


def test_a_plan_machine_ships_no_gated_intent(world, tmp_path):
    world["mode"]("plan")
    world["intents"] = [intent(tmp_path / "g", "carameli", False), intent(tmp_path / "d")]
    assert ship_pass.run(world["workspace"]) == ship_pass.EXIT_OK
    assert world["shipped"] == ["agent/d"]


def test_the_docs_say_what_off_means_and_what_the_stop_is():
    """`off` used to be a full stop, and a person relying on that needs the sentence."""
    readme = " ".join((REPO_ROOT / "README.md").read_text(encoding="utf-8").split())
    assert 'means "this PC sends no fixers"' in readme
    assert "To stop shipping, use `harness-switch.py" in readme
    assert "this PC sends no fixers" in " ".join(load_script("scripts/fix-pass.py").__doc__.split())
    assert "`harness-switch.py` is the stop" in " ".join(ship_pass.__doc__.split())


def test_a_dispatching_machine_ships_direct_intents_and_leaves_gated_ones(world, tmp_path):
    world["intents"] = [intent(tmp_path / "d"), intent(tmp_path / "g", "carameli", False)]
    assert ship_pass.run(world["workspace"]) == ship_pass.EXIT_OK
    assert world["shipped"] == ["agent/d"]
    assert "roguelike agent/d -- shipped: u" in record(world)


def test_a_machine_that_does_not_dispatch_ships_gated_intents_through_the_gated_path(
    world, tmp_path
):
    world["mode"]("off")
    gated = intent(tmp_path / "g", "carameli", False)
    world["intents"] = [gated]
    assert ship_pass.run(world["workspace"]) == ship_pass.EXIT_OK
    assert world["shipped"] == ["agent/g"]
    assert ship_pass.takes(gated, fix_cycle.OFF) and not gated.direct


def test_a_fire_with_nothing_to_ship_leaves_the_last_record_alone(world, monkeypatch):
    """One fire every two minutes would otherwise erase a failure before anyone read it."""
    (world["root"] / "logs").mkdir()
    (world["root"] / ship_pass.ARTIFACT).write_text("the last ship\n", encoding="utf-8")
    monkeypatch.setattr(
        ship_step.fix_loop, "fixers_working", lambda: pytest.fail("nothing to ask about")
    )
    assert ship_pass.run(world["workspace"]) == ship_pass.EXIT_OK
    assert record(world) == "the last ship\n"


def test_a_first_fire_with_nothing_to_ship_still_leaves_a_record(world):
    assert ship_pass.run(world["workspace"]) == ship_pass.EXIT_OK
    assert "nothing to ship" in record(world)


def test_a_fire_another_holds_the_lock_for_exits_quietly(world, tmp_path):
    world["intents"] = [intent(tmp_path / "d")]
    with ship_pass.worktree.named_lock(tmp_path, ship_step.SHIP_LOCK_NAME, 0.1, 60.0):
        assert ship_pass.run(world["workspace"]) == ship_pass.EXIT_OK
    assert world["shipped"] == []


def test_the_pass_step_and_a_fire_exclude_each_other(world, tmp_path, monkeypatch):
    """Both directions through the one lock: a pass's ship step in progress stops a fire on
    a machine set to `off` and shipped by hand, and a fire under way stops that step."""
    fix_pass = load_script("scripts/fix-pass.py")
    world["mode"]("off")
    world["intents"] = [intent(tmp_path / "g", "carameli", False)]
    during: list = []

    def fires_during_the_step(gated, root, mode, journal, budget, clock):
        during.append(ship_pass.run(world["workspace"]))
        return [], [], False

    monkeypatch.setattr(fix_pass.ship_step, "ship_each", fires_during_the_step)
    monkeypatch.setattr(fix_pass.ship_step, "left_to_ship_task", lambda *a: ([], []))
    fix_pass.ship_intents(tmp_path, [], fix_cycle.DISPATCH)
    assert during == [ship_pass.EXIT_OK] and world["shipped"] == [], "the fire yielded"


def test_a_pass_step_the_ship_task_holds_the_lock_against_ships_nothing(
    world, tmp_path, monkeypatch
):
    fix_pass = load_script("scripts/fix-pass.py")
    monkeypatch.setattr(fix_pass, "SHIP_LOCK_WAIT", 0.1)
    monkeypatch.setattr(
        fix_pass.ship_step, "ship_each", lambda *a: pytest.fail("two shippers at once")
    )
    with ship_pass.worktree.named_lock(tmp_path, ship_step.SHIP_LOCK_NAME, 0.1, 60.0):
        [line], refused, failed = fix_pass.ship_intents(tmp_path, [], fix_cycle.DISPATCH)
    assert "the ship task holds" in line and not refused and not failed


def test_a_plan_pass_takes_no_lock(world, tmp_path, monkeypatch):
    """It ships nothing, so it waits on nobody."""
    fix_pass = load_script("scripts/fix-pass.py")
    monkeypatch.setattr(
        fix_pass.worktree, "named_lock", lambda *a: pytest.fail("a plan needs no lock")
    )
    assert fix_pass.ship_intents(tmp_path, [], fix_cycle.PLAN) == ([], [], False)


def test_a_session_still_working_holds_the_intent_as_the_fix_pass_would(
    world, tmp_path, monkeypatch
):
    monkeypatch.setattr(ship_step.fix_loop, "why_held", lambda busy, tree, now: "still working")
    world["intents"] = [intent(tmp_path / "d")]
    assert ship_pass.run(world["workspace"]) == ship_pass.EXIT_OK
    assert world["shipped"] == []
    assert "roguelike agent/d -- held: still working" in record(world)


def _refused(tree: Path) -> ship_intent.Outcome:
    one = intent(tree)
    return ship_intent.Outcome(one, ship_intent.REFUSED, "commit: no")


def test_a_stored_refusal_is_not_tried_again(world, tmp_path, monkeypatch):
    """Nothing about it has moved, and a fetch every two minutes would not change that."""
    world["intents"] = [intent(tmp_path / "d")]
    monkeypatch.setattr(ship_pass.ship_intent, "stored_refusal", lambda i: _refused(tmp_path / "d"))
    assert ship_pass.run(world["workspace"]) == ship_pass.EXIT_OK
    assert world["shipped"] == []
    assert "roguelike agent/d -- refused" in record(world)


def _ledger(monkeypatch) -> list:
    written: list = []

    def record_all(found, _items, _devkit):
        written.extend(found)
        return list(found)

    monkeypatch.setattr(ship_pass.triage, "load", lambda devkit: [])
    monkeypatch.setattr(ship_pass.fix_findings, "record_all", record_all)
    return written


def test_the_steps_of_a_fire_each_read_on_their_own(tmp_path, monkeypatch):
    """`set_refused_aside` sets aside, `ship` ships; only the last spawns."""
    keep, parked = intent(tmp_path / "keep"), intent(tmp_path / "parked")
    monkeypatch.setattr(
        ship_pass.ship_intent,
        "stored_refusal",
        lambda one: _refused(one.tree) if one is parked else None,
    )
    journal = ship_step.Journal(tmp_path)
    todo, lines = ship_pass.set_refused_aside([keep, parked], fix_cycle.DISPATCH, journal)
    assert todo == [keep] and lines == ["roguelike agent/parked -- refused: commit: no"]
    assert journal.findings == []


def test_a_refusal_is_filed_by_what_the_state_file_says(tmp_path):
    one = intent(tmp_path / "d")
    ship_intent.write_state(
        one.tree, {"stage": ship_intent.REFUSED, "step": "commit", "output": "ok\nerror: no\n"}
    )
    journal = ship_step.Journal(tmp_path)
    ship_pass.file_refusal(one, journal)
    [finding] = journal.findings
    assert (finding.kind, finding.project) == ("ship-refused", "roguelike")
    assert finding.detail == "roguelike agent/d: commit: error: no"


def test_ship_records_nothing_and_reports_clean_with_nothing_to_do(tmp_path, monkeypatch):
    written = _ledger(monkeypatch)
    monkeypatch.setattr(ship_step.fix_loop, "fixers_working", frozenset)
    assert ship_pass.ship(tmp_path, [], fix_cycle.DISPATCH) == ([], False)
    assert written == []


def test_write_artifact_makes_its_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(ship_pass, "REPO_ROOT", tmp_path)
    assert ship_pass.write_artifact("said\n") == tmp_path / ship_pass.ARTIFACT
    assert (tmp_path / ship_pass.ARTIFACT).read_text(encoding="utf-8") == "said\n"


def test_the_workspace_defaults_to_the_machines_own():
    assert ship_pass.build_parser().parse_args([]).workspace == ship_pass.worktree.DEFAULT_WORKSPACE


def test_a_refusal_is_the_fix_passs_on_a_machine_that_dispatches(world, tmp_path, monkeypatch):
    written = _ledger(monkeypatch)
    world["intents"] = [intent(tmp_path / "d")]
    monkeypatch.setattr(ship_pass.ship_intent, "stored_refusal", lambda i: _refused(tmp_path / "d"))
    ship_pass.run(world["workspace"])
    assert written == [], "the pass turns it into a failure, and a fixer goes"


def test_a_refusal_with_no_fixer_coming_is_filed(world, tmp_path, monkeypatch):
    written = _ledger(monkeypatch)
    world["mode"]("off")
    world["intents"] = [intent(tmp_path / "d")]
    monkeypatch.setattr(ship_pass.ship_intent, "stored_refusal", lambda i: _refused(tmp_path / "d"))
    ship_pass.run(world["workspace"])
    assert [f.kind for f in written] == ["ship-refused"]


def test_a_refusal_this_fire_made_is_filed_too_where_no_fixer_is_coming(
    world, tmp_path, monkeypatch
):
    written = _ledger(monkeypatch)
    world["mode"]("off")
    world["intents"] = [intent(tmp_path / "d")]

    def refuse(one, python, base):
        ship_intent.write_state(
            one.tree, {"stage": ship_intent.REFUSED, "step": "commit", "output": "error: no"}
        )
        return ship_intent.Outcome(one, ship_intent.REFUSED, "commit: no")

    monkeypatch.setattr(ship_pass.ship_intent, "ship_one", refuse)
    ship_pass.run(world["workspace"])
    assert [f.kind for f in written] == ["ship-refused"]
    assert "error: no" in written[0].detail


def test_a_failed_ship_is_filed_and_leaves_the_exit_clean(world, tmp_path, monkeypatch):
    """Filed by the step; a non-zero exit would have `log-wrap` file the same thing."""
    written = _ledger(monkeypatch)
    world["intents"] = [intent(tmp_path / "d")]
    monkeypatch.setattr(
        ship_pass.ship_intent,
        "ship_one",
        lambda one, python, base: ship_intent.Outcome(one, ship_intent.FAILED, "push: no"),
    )
    assert ship_pass.run(world["workspace"]) == ship_pass.EXIT_OK
    assert [f.kind for f in written] == ["ship-failed"]


def test_a_step_that_crashed_is_this_script_breaking(world, tmp_path, monkeypatch):
    written = _ledger(monkeypatch)
    world["intents"] = [intent(tmp_path / "d")]

    def boom(one, python, base):
        raise ValueError("boom")

    monkeypatch.setattr(ship_pass.ship_intent, "ship_one", boom)
    assert ship_pass.run(world["workspace"]) == ship_pass.EXIT_FAILED
    assert [f.kind for f in written] == ["pass-step-crashed"]


def test_no_workspace_file_is_a_usage_error_with_a_record(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ship_pass, "REPO_ROOT", tmp_path)
    assert ship_pass.main(["--workspace", str(tmp_path / "absent")]) == ship_pass.EXIT_USAGE
    assert "no workspace file" in (tmp_path / ship_pass.ARTIFACT).read_text(encoding="utf-8")


def test_main_trusts_the_workspace_in_this_process_and_writes_no_global_config(world, monkeypatch):
    adopted = []
    monkeypatch.setattr(
        ship_pass.git_trust, "adopt", lambda root, write: adopted.append((root, write)) or ""
    )
    assert ship_pass.main(["--workspace", str(world["workspace"])]) == ship_pass.EXIT_OK
    assert adopted == [(world["workspace"].parent.resolve(), False)]


def test_a_crash_says_so_in_the_record_and_is_raised(world, monkeypatch):
    monkeypatch.setattr(ship_pass.git_trust, "adopt", lambda root, write: "")

    def boom(_workspace):
        raise RuntimeError("boom")

    monkeypatch.setattr(ship_pass, "run", boom)
    with pytest.raises(RuntimeError):
        ship_pass.main(["--workspace", str(world["workspace"])])
    assert "CRASHED -- RuntimeError: boom" in record(world)
