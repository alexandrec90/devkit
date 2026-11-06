"""`scripts/fix_elevated.py`: one person's step per PC, said only while it is true."""

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
triage = fix_findings.triage
MISSING = (elevated_request.MISSING, "not installed")
OUTDATED = (elevated_request.OUTDATED, "its copy differs from scripts/elevated_worker.py")
CURRENT = (elevated_request.CURRENT, "installed and current")
UNKNOWN = (elevated_request.UNKNOWN, "the scheduler did not answer")


@pytest.fixture(autouse=True)
def on_windows(monkeypatch):
    monkeypatch.setattr(elevated_request, "WINDOWS", True)
    monkeypatch.setenv(triage.harness_events.HOST_ENV, "pc-one")


def _ctx(tmp_path: Path, mode: str = fix_cycle.DISPATCH) -> fix_loop.Context:
    devkit = tmp_path / "devkit"
    devkit.mkdir(exist_ok=True)
    return fix_loop.Context(tmp_path, [], devkit, tmp_path / "l.json", tmp_path / "h", mode, NOW)


def run_pass(ctx, answer) -> list[str]:
    journal = fix_findings.Journal(ctx.devkit_dir)
    lines = fix_elevated.check(ctx, journal, lambda _checkout: answer)
    fix_loop.record(ctx, journal)
    return lines


def open_rows(ctx):
    return triage.open_items(triage.load(ctx.devkit_dir))


def test_a_pc_with_no_worker_owes_a_person_one_specific_step(tmp_path):
    ctx = _ctx(tmp_path)
    assert run_pass(ctx, MISSING) == ["pc-one -- elevated worker missing: filed for its owner"]
    [row] = open_rows(ctx)
    assert row.project == "devkit"
    step = triage.person_step(row)
    assert step.startswith("on pc-one, from an elevated terminal, run `python ")
    assert str(ctx.devkit_dir / "scripts" / "install-elevated-worker.py") in step
    assert "--yes" in step
    assert "one administrator action per PC" in step
    assert "no job can take for you" in step


def test_the_step_reaches_the_morning_list_and_no_fixer_is_owed(tmp_path):
    ctx = _ctx(tmp_path)
    run_pass(ctx, MISSING)
    [line] = fix_loop.person_steps(ctx)
    assert line.startswith("on pc-one, from an elevated terminal")
    assert fix_loop.owed(ctx) == 0, "a fixer holds no administrator token"


def test_it_is_filed_once_however_many_passes_see_it(tmp_path):
    ctx = _ctx(tmp_path)
    run_pass(ctx, MISSING)
    run_pass(ctx, MISSING)
    assert len(open_rows(ctx)) == 1


def test_installing_it_retires_the_step_with_nobody_retiring_it(tmp_path):
    ctx = _ctx(tmp_path)
    run_pass(ctx, MISSING)
    [row] = open_rows(ctx)
    assert run_pass(ctx, CURRENT) == [
        f"retired [{row.id}] -- the elevated worker on this PC is as this checkout ships it, "
        "as of this pass"
    ]
    assert open_rows(ctx) == []
    assert fix_loop.person_steps(ctx) == []


def test_a_current_worker_says_nothing_at_all(tmp_path):
    ctx = _ctx(tmp_path)
    assert run_pass(ctx, CURRENT) == []
    assert open_rows(ctx) == []


def test_an_outdated_worker_says_so_and_names_the_same_command(tmp_path):
    ctx = _ctx(tmp_path)
    run_pass(ctx, OUTDATED)
    [row] = open_rows(ctx)
    assert "outdated: its copy differs from scripts/elevated_worker.py" in row.fields["detail"]
    assert "--yes" in triage.person_step(row)


def test_going_from_missing_to_outdated_swaps_the_row(tmp_path):
    ctx = _ctx(tmp_path)
    run_pass(ctx, MISSING)
    run_pass(ctx, OUTDATED)
    [row] = open_rows(ctx)
    assert "outdated" in row.fields["detail"]


def test_each_machine_gets_its_own_group(tmp_path, monkeypatch):
    """`host=` is not in a signature, so the name in the detail is all that keeps two PCs
    from collapsing into one group and one line."""
    ctx = _ctx(tmp_path)
    run_pass(ctx, MISSING)
    monkeypatch.setenv(triage.harness_events.HOST_ENV, "pc-two")
    run_pass(ctx, MISSING)
    steps = sorted(triage.person_step(row)[:9] for row in open_rows(ctx))
    assert steps == ["on pc-one", "on pc-two"]
    assert len(fix_loop.person_steps(ctx)) == 2


def test_a_machine_retires_only_its_own_row(tmp_path, monkeypatch):
    """A pooled ledger holds other PCs' rows, and this PC's pass knows nothing about them."""
    ctx = _ctx(tmp_path)
    run_pass(ctx, MISSING)
    monkeypatch.setenv(triage.harness_events.HOST_ENV, "pc-two")
    assert run_pass(ctx, CURRENT) == []
    [row] = open_rows(ctx)
    assert row.host == "pc-one"


def test_a_machine_that_could_not_say_files_and_retires_nothing(tmp_path):
    ctx = _ctx(tmp_path)
    run_pass(ctx, MISSING)
    assert run_pass(ctx, UNKNOWN) == []
    assert len(open_rows(ctx)) == 1


def test_a_rehearsal_retires_nothing(tmp_path):
    ctx = _ctx(tmp_path)
    run_pass(ctx, MISSING)
    [row] = open_rows(ctx)
    rehearsal = _ctx(tmp_path, fix_cycle.PLAN)
    journal = fix_findings.Journal(ctx.devkit_dir)
    lines = fix_elevated.check(rehearsal, journal, lambda _checkout: CURRENT)
    assert lines[0].startswith(f"would retire [{row.id}]")
    assert open_rows(ctx)


def test_off_windows_there_is_nothing_to_owe(tmp_path, monkeypatch):
    monkeypatch.setattr(elevated_request, "WINDOWS", False)
    ctx = _ctx(tmp_path)
    assert run_pass(ctx, MISSING) == []
    assert open_rows(ctx) == []
