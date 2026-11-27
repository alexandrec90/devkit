"""`scripts/fix_elevated.py`: what the pass says about the worker, and what it never asks."""

from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import elevated_request
import fix_cycle
import fix_elevated
import fix_findings
import fix_loop

NOW = _dt.datetime(2026, 11, 6, 23, 0, tzinfo=_dt.UTC)
VERB = "restart-wsl-service"
triage = fix_findings.triage
MISSING = (elevated_request.MISSING, "not installed")
OUTDATED = (elevated_request.OUTDATED, "its copy differs from scripts/elevated_worker.py")
CURRENT = (elevated_request.CURRENT, "installed and current")
EXPOSED = (elevated_request.EXPOSED, "the devkit-elevated task is not read-only to the user")
UNKNOWN = (elevated_request.UNKNOWN, "the scheduler did not answer")


@pytest.fixture(autouse=True)
def on_windows(monkeypatch):
    monkeypatch.setattr(elevated_request, "WINDOWS", True)
    monkeypatch.setenv(triage.harness_events.HOST_ENV, "pc-one")


def _ctx(tmp_path: Path, mode: str = fix_cycle.DISPATCH) -> fix_loop.Context:
    devkit = tmp_path / "devkit"
    devkit.mkdir(exist_ok=True)
    return fix_loop.Context(tmp_path, [], devkit, tmp_path / "l.json", tmp_path / "h", mode, NOW)


def asked(ctx, age_seconds: float = 60) -> None:
    """A real request, made `age_seconds` ago, on a PC with no worker."""
    elevated_request.note_waiting(ctx.devkit_dir, VERB, NOW.timestamp() - age_seconds)


def run_pass(ctx, answer, outcomes=None) -> list[str]:
    journal = fix_findings.Journal(ctx.devkit_dir)
    lines = fix_elevated.check(ctx, journal, lambda _checkout: answer, lambda: outcomes or {})
    fix_loop.record(ctx, journal)
    return lines


def open_rows(ctx):
    return triage.open_items(triage.load(ctx.devkit_dir))


# --- nothing prompts for an install or an upgrade ------------------------------------


def test_a_pc_with_no_worker_and_no_request_owes_nobody_anything(tmp_path):
    """The standing per-machine step this replaced was the harness asking for an admin
    paste after unreviewed merges, which is what the design refused."""
    ctx = _ctx(tmp_path)
    assert run_pass(ctx, MISSING) == []
    assert open_rows(ctx) == []
    assert fix_loop.person_steps(ctx) == []


def test_an_outdated_worker_says_nothing_at_all(tmp_path):
    ctx = _ctx(tmp_path)
    assert run_pass(ctx, OUTDATED) == []
    assert open_rows(ctx) == []
    assert fix_loop.person_steps(ctx) == []


def test_an_upgrade_is_never_a_persons_step_even_with_a_request_about(tmp_path):
    """A working installed worker keeps working, so there is nothing to wait on."""
    ctx = _ctx(tmp_path)
    asked(ctx)
    run_pass(ctx, OUTDATED)
    assert open_rows(ctx) == []


def test_a_current_worker_says_nothing_at_all(tmp_path):
    ctx = _ctx(tmp_path)
    assert run_pass(ctx, CURRENT) == []
    assert open_rows(ctx) == []


# --- the one person's step -----------------------------------------------------------


def test_a_request_waiting_on_a_pc_with_no_worker_is_a_specific_step(tmp_path):
    ctx = _ctx(tmp_path)
    asked(ctx)
    assert len(run_pass(ctx, MISSING)) == 1
    [row] = open_rows(ctx)
    assert row.project == "devkit"
    assert VERB in row.fields["detail"]
    step = triage.person_step(row)
    assert step.startswith("on pc-one, from an elevated terminal, run `python -I -S ")
    assert str(ctx.devkit_dir / "scripts" / "install-elevated-worker.py") in step
    assert "--yes" in step
    assert "one administrator action per PC" in step
    assert "no job can take for you" in step


def test_the_step_reaches_the_morning_list_and_no_fixer_is_owed(tmp_path):
    ctx = _ctx(tmp_path)
    asked(ctx)
    run_pass(ctx, MISSING)
    [line] = fix_loop.person_steps(ctx)
    assert line.startswith("on pc-one, from an elevated terminal")
    assert fix_loop.owed(ctx) == 0, "a fixer holds no administrator token"


def test_it_is_filed_once_however_many_passes_see_it(tmp_path):
    ctx = _ctx(tmp_path)
    asked(ctx)
    run_pass(ctx, MISSING)
    run_pass(ctx, MISSING)
    assert len(open_rows(ctx)) == 1


def test_a_request_nobody_repeated_ages_out_and_the_step_goes(tmp_path):
    ctx = _ctx(tmp_path)
    asked(ctx, age_seconds=elevated_request.WAITING_FOR - 60)
    run_pass(ctx, MISSING)
    assert len(open_rows(ctx)) == 1
    asked(ctx, age_seconds=elevated_request.WAITING_FOR + 60)
    run_pass(ctx, MISSING)
    assert open_rows(ctx) == []


def test_installing_retires_the_step_and_the_note(tmp_path):
    ctx = _ctx(tmp_path)
    asked(ctx)
    run_pass(ctx, MISSING)
    [row] = open_rows(ctx)
    assert run_pass(ctx, CURRENT) == [
        f"retired [{row.id}] -- no longer true of this PC, as of this pass"
    ]
    assert open_rows(ctx) == []
    assert fix_loop.person_steps(ctx) == []
    assert not (ctx.devkit_dir / elevated_request.WAITING).exists()


def test_each_machine_gets_its_own_group(tmp_path, monkeypatch):
    """`host=` is not in a signature, so the name in the detail is all that keeps two PCs
    from collapsing into one group and one line."""
    ctx = _ctx(tmp_path)
    asked(ctx)
    run_pass(ctx, MISSING)
    monkeypatch.setenv(triage.harness_events.HOST_ENV, "pc-two")
    run_pass(ctx, MISSING)
    steps = sorted(triage.person_step(row)[:9] for row in open_rows(ctx))
    assert steps == ["on pc-one", "on pc-two"]
    assert len(fix_loop.person_steps(ctx)) == 2


def test_a_machine_retires_only_its_own_row(tmp_path, monkeypatch):
    """A pooled ledger holds other PCs' rows, and this PC's pass knows nothing about them."""
    ctx = _ctx(tmp_path)
    asked(ctx)
    run_pass(ctx, MISSING)
    monkeypatch.setenv(triage.harness_events.HOST_ENV, "pc-two")
    assert run_pass(ctx, CURRENT) == []
    [row] = open_rows(ctx)
    assert row.host == "pc-one"


def test_a_machine_that_could_not_say_files_and_retires_nothing(tmp_path):
    ctx = _ctx(tmp_path)
    asked(ctx)
    run_pass(ctx, MISSING)
    assert run_pass(ctx, UNKNOWN) == []
    assert len(open_rows(ctx)) == 1


def test_a_rehearsal_retires_nothing_and_keeps_the_note(tmp_path):
    ctx = _ctx(tmp_path)
    asked(ctx)
    run_pass(ctx, MISSING)
    [row] = open_rows(ctx)
    rehearsal = _ctx(tmp_path, fix_cycle.PLAN)
    journal = fix_findings.Journal(ctx.devkit_dir)
    lines = fix_elevated.check(rehearsal, journal, lambda _c: CURRENT, dict)
    assert lines[0].startswith(f"would retire [{row.id}]")
    assert open_rows(ctx)
    assert (ctx.devkit_dir / elevated_request.WAITING).exists()


def test_off_windows_there_is_nothing_to_owe(tmp_path, monkeypatch):
    monkeypatch.setattr(elevated_request, "WINDOWS", False)
    ctx = _ctx(tmp_path)
    asked(ctx)
    assert run_pass(ctx, MISSING) == []
    assert open_rows(ctx) == []


# --- what is filed for a fixer ------------------------------------------------------

FAILED = {VERB: (elevated_request.FAILED, "WSLService is stuck, and a restart did not help")}
REFUSED = {VERB: (elevated_request.REFUSED, "WSLService is stuck, inside its 6-hour rate limit")}


@pytest.mark.parametrize("outcomes", [FAILED, REFUSED], ids=["failed", "refused"])
def test_a_failed_or_refused_run_reaches_the_ledger_as_a_fixers(tmp_path, outcomes):
    """Not a person's step: those are for a request waiting on a PC with no worker."""
    ctx = _ctx(tmp_path)
    run_pass(ctx, CURRENT, outcomes)
    [row] = open_rows(ctx)
    result, detail = outcomes[VERB]
    assert f"{VERB} on pc-one {result}: {detail}" in row.fields["detail"]
    assert triage.person_step(row) == ""
    assert fix_loop.owed(ctx) == 1


@pytest.mark.parametrize("result", ["recovered", "healthy"])
def test_a_later_good_run_retires_it(tmp_path, result):
    ctx = _ctx(tmp_path)
    run_pass(ctx, CURRENT, FAILED)
    run_pass(ctx, CURRENT, {VERB: (result, "answers again")})
    assert open_rows(ctx) == []


def test_a_good_result_files_nothing(tmp_path):
    ctx = _ctx(tmp_path)
    assert run_pass(ctx, CURRENT, {VERB: ("recovered", "answers again")}) == []
    assert open_rows(ctx) == []


def test_a_task_the_user_can_reach_is_filed_and_is_no_persons_step(tmp_path):
    """Item 4's scheduled check: a defect however it happened, and not an admin paste."""
    ctx = _ctx(tmp_path)
    run_pass(ctx, EXPOSED)
    [row] = open_rows(ctx)
    assert "exposed: the devkit-elevated task is not read-only" in row.fields["detail"]
    assert "pc-one" in row.fields["detail"]
    assert triage.person_step(row) == ""
    run_pass(ctx, CURRENT)
    assert open_rows(ctx) == []


# --- the pieces the pass is built from -------------------------------------------------


def test_the_step_names_the_machine_and_the_exact_command(tmp_path):
    said = fix_elevated.step("pc-one", tmp_path)
    assert said.startswith("on pc-one, from an elevated terminal, run `")
    assert f"`{elevated_request.install_command(tmp_path)}`" in said
    assert said.endswith("which no job can take for you")


def test_the_waiting_detail_is_stable_and_carries_the_step(tmp_path):
    """No count and no date: a finding is filed only when no open one shares it."""
    said = fix_elevated.waiting_detail("pc-one", tmp_path, VERB)
    assert said.startswith(f"{VERB} was requested on pc-one, which has no elevated worker -- ")
    assert said.endswith(fix_elevated.step("pc-one", tmp_path))
    assert triage.PERSON_STEP in said
    assert said == fix_elevated.waiting_detail("pc-one", tmp_path, VERB)


def test_what_is_owed_is_a_line_per_wait_exposure_and_bad_run(tmp_path):
    outcomes = {
        "zz-verb": (elevated_request.FAILED, "did not answer"),
        VERB: (elevated_request.REFUSED, "inside its window"),
        "fine": ("recovered", "answers again"),
    }
    said = fix_elevated.owed("pc-one", tmp_path, EXPOSED, [VERB], outcomes)
    assert said == [
        fix_elevated.waiting_detail("pc-one", tmp_path, VERB),
        f"the elevated worker on pc-one is exposed: {EXPOSED[1]}",
        f"{VERB} on pc-one refused: inside its window",
        "zz-verb on pc-one failed: did not answer",
    ]


def test_nothing_is_owed_by_a_current_worker_with_good_runs(tmp_path):
    assert fix_elevated.owed("pc-one", tmp_path, CURRENT, [], {VERB: ("healthy", "x")}) == []


def test_retiring_resolves_only_this_machines_rows_not_kept(tmp_path):
    ctx = _ctx(tmp_path)
    asked(ctx)
    run_pass(ctx, MISSING)
    [row] = open_rows(ctx)
    detail = row.fields["detail"]
    assert fix_elevated.retire(ctx, "pc-one", {detail}) == []
    assert fix_elevated.retire(ctx, "pc-two", set()) == []
    assert open_rows(ctx) == [row]
    note = "no longer true of this PC, as of this pass"
    assert fix_elevated.retire(ctx, "pc-one", set()) == [f"retired [{row.id}] -- {note}"]
    assert open_rows(ctx) == []


def test_a_rehearsal_only_says_what_it_would_retire(tmp_path):
    ctx = _ctx(tmp_path)
    asked(ctx)
    run_pass(ctx, MISSING)
    [row] = open_rows(ctx)
    rehearsal = _ctx(tmp_path, fix_cycle.PLAN)
    [line] = fix_elevated.retire(rehearsal, "pc-one", set())
    assert line.startswith(f"would retire [{row.id}]")
    assert open_rows(ctx) == [row]
