"""`scripts/e2e_results.py`: a local end-to-end run, as a file and as the fix pass's red."""

from __future__ import annotations

import datetime as _dt
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import e2e_results
import fix_cycle
import fix_plan
import project_python

OUTPUT = """\
tests/test_tasks_live.py F.                                         [100%]
=========================== short test summary info ============================
FAILED tests/test_tasks_live.py::test_every_picker_draws - AssertionError: no rows
FAILED tests/test_tasks_live.py::test_escape_cancels[agent] - assert 2 == 0
"""


def red(**fields: Any) -> e2e_results.Result:
    base: dict[str, Any] = {
        "project": "carameli",
        "status": e2e_results.FAILED,
        "sha": "59a4ef5aaaaa",
        "base": "master",
        "exit_code": 1,
        "signature": e2e_results.signature_of(OUTPUT, 1, "carameli"),
    }
    base.update(fields)
    return e2e_results.Result(**base)


def test_the_signature_is_the_failing_ids_pytest_printed():
    assert e2e_results.signature_of(OUTPUT, 1, "carameli") == (
        "tests/test_tasks_live.py::test_escape_cancels[agent]",
        "tests/test_tasks_live.py::test_every_picker_draws",
    )


def test_a_red_run_naming_no_test_is_signed_by_its_exit_code_never_empty():
    """An empty signature matches every other artifact-less failure, and the plan holds
    a failure that names nothing as unread -- forever, for a file nothing re-reads."""
    sig = e2e_results.signature_of("Traceback ...\nImportError: no module x\n", 2, "carameli")
    assert sig == ("carameli: scripts/e2e.py exited 2 naming no failing test",)
    assert not fix_plan.names_nothing(sig)


def test_a_missing_tier_is_its_own_signature_and_one_the_plan_reads(tmp_path):
    """Stable, so the ledger dedupes it per commit; named, so the plan does not hold it as
    unread; and told apart from a red run's, so the prompt asks for a tier."""
    assert e2e_results.is_missing((e2e_results.MISSING,))
    assert not e2e_results.is_missing(("tests/t.py::test_a",))
    assert not e2e_results.is_missing((e2e_results.MISSING, "tests/t.py::test_a"))
    assert not fix_plan.names_nothing((e2e_results.MISSING,))
    assert not fix_plan.is_vendored((e2e_results.MISSING,))
    red = e2e_results.Result(
        "roguelike", e2e_results.FAILED, sha="abc", signature=(e2e_results.MISSING,)
    )
    failure = e2e_results.to_failure(red, "abc", tmp_path)
    assert failure is not None and failure.signature == (e2e_results.MISSING,)


def test_a_result_round_trips_through_its_file(tmp_path):
    path = e2e_results.write(red(), tmp_path)
    assert path == tmp_path / "logs" / "e2e" / "carameli" / "result.json"
    assert e2e_results.read("carameli", tmp_path) == red()


def test_a_missing_or_unreadable_or_unknown_result_reads_as_none(tmp_path):
    assert e2e_results.read("carameli", tmp_path) is None
    where = e2e_results.result_dir("carameli", tmp_path)
    where.mkdir(parents=True)
    (where / "result.json").write_text("{not json", encoding="utf-8")
    assert e2e_results.read("carameli", tmp_path) is None
    (where / "result.json").write_text(json.dumps({"project": "carameli", "status": "?"}))
    assert e2e_results.read("carameli", tmp_path) is None


def test_a_newer_runners_extra_keys_do_not_break_an_older_reader(tmp_path):
    e2e_results.write(red(), tmp_path)
    path = e2e_results.result_dir("carameli", tmp_path) / "result.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps(record | {"duration": 12.5}), encoding="utf-8")
    assert e2e_results.read("carameli", tmp_path) == red()


def test_only_a_red_result_is_a_failure_and_it_is_a_local_red_default_branch(tmp_path):
    assert e2e_results.to_failure(red(status=e2e_results.PASSED), "t", tmp_path) is None
    assert e2e_results.to_failure(red(status=e2e_results.SKIPPED), "t", tmp_path) is None
    e2e_results.write(red(), tmp_path)
    failure = e2e_results.to_failure(red(), "b31b60cbbbbb", tmp_path)
    assert failure is not None
    assert (failure.kind, failure.project, failure.base) == (fix_plan.LOCAL, "carameli", "master")
    assert (failure.sha, failure.tip) == ("59a4ef5aaaaa", "b31b60cbbbbb")
    assert failure.signature == red().signature
    assert failure.workflow == e2e_results.WORKFLOW
    assert failure.evidence == str(e2e_results.result_dir("carameli", tmp_path))


def test_a_red_result_with_no_evidence_directory_says_so_rather_than_naming_one(tmp_path):
    failure = e2e_results.to_failure(red(signature=()), "", tmp_path)
    assert failure is not None and failure.evidence == ""
    assert failure.signature == ("carameli: scripts/e2e.py exited 1 naming no failing test",)


def test_the_venv_spellings_are_project_pythons(tmp_path):
    """A copy, because that module spawns where a scheduled job may not; pinned here."""
    owned = {Path(project_python.VENV_DIR, *parts) for parts in project_python.VENV_SUBPATHS}
    assert set(e2e_results.VENV_INTERPRETERS) == owned


def test_a_checkout_without_its_own_venv_runs_under_the_fallback(tmp_path):
    assert e2e_results.venv_interpreter(tmp_path, "py") == "py"
    posix = tmp_path / ".venv" / "bin" / "python"
    posix.parent.mkdir(parents=True)
    posix.write_text("", encoding="utf-8")
    assert e2e_results.venv_interpreter(tmp_path, "py") == str(posix)


def _git(tip: str, code: int = 0):
    def git(*args):
        return subprocess.CompletedProcess(args, code, f"{tip}\n", "")

    return git


def test_collect_reads_every_red_project_at_its_bases_current_tip(tmp_path):
    workspace = tmp_path / "w.code-workspace"
    devkit = tmp_path / "devkit"
    for name in ("carameli", "roguelike", "devkit"):
        (tmp_path / name).mkdir(exist_ok=True)
    e2e_results.write(red(), devkit)
    e2e_results.write(red(project="roguelike", status=e2e_results.PASSED, signature=()), devkit)
    found = e2e_results.collect(
        workspace, ["carameli", "roguelike", "devkit"], lambda _p: _git("b31b60c")
    )
    assert [(f.project, f.tip) for f in found] == [("carameli", "b31b60c")]


def test_origin_tip_is_the_remote_branch_or_nothing():
    assert e2e_results.origin_tip(_git("b31b60c"), "main") == "b31b60c"
    assert e2e_results.origin_tip(_git("x", code=1), "main") == ""


def test_collect_leaves_the_tip_unknown_when_git_cannot_read_it(tmp_path):
    (tmp_path / "carameli").mkdir()
    e2e_results.write(red(), tmp_path / "devkit")
    found = e2e_results.collect(tmp_path / "w", ["carameli"], lambda _p: _git("x", code=1))
    assert [f.tip for f in found] == [""]


# --- the shared halves ----------------------------------------------------------------------


def test_a_spawn_is_read_the_same_way_by_every_tier():
    done = subprocess.CompletedProcess([], 0, " out \n", "")
    assert e2e_results.ok(done)
    assert not e2e_results.ok(None)
    assert e2e_results.out(done) == "out"
    failed = subprocess.CompletedProcess([], 1, "first\n", "last\nfatal: no\n")
    assert e2e_results.said(failed) == "first"
    assert e2e_results.said(subprocess.CompletedProcess([], 1, "", "")) == "no output"


def test_a_green_verdict_carries_no_reason_and_a_red_one_its_signature():
    ended = e2e_results.Result("carameli", e2e_results.SKIPPED, sha="abc", base="main")
    green = e2e_results.verdict(ended, 0, OUTPUT, reason="timed out")
    assert (green.status, green.sha, green.reason, green.signature) == (
        e2e_results.PASSED,
        "abc",
        "",
        (),
    )
    red_run = e2e_results.verdict(ended, 2, OUTPUT, reason="timed out")
    assert (red_run.status, red_run.exit_code, red_run.reason) == (
        e2e_results.FAILED,
        2,
        "timed out",
    )
    assert red_run.signature == e2e_results.signature_of(OUTPUT, 2, "carameli")


def test_record_writes_the_output_and_then_the_result(tmp_path):
    path = e2e_results.record(red(), "said\n", tmp_path)
    assert path.name == e2e_results.RESULT_FILE
    output = path.parent / e2e_results.OUTPUT_FILE
    assert output.read_text(encoding="utf-8") == "said\n"


def test_a_line_names_the_verdict():
    assert e2e_results.line_for(red(status=e2e_results.PASSED)).startswith("carameli: passed at ")
    skipped = red(status=e2e_results.SKIPPED, reason="a person is working")
    assert e2e_results.line_for(skipped) == "carameli: skipped -- a person is working"
    many = red(signature=("a", "b", "c", "d"))
    assert e2e_results.line_for(many).endswith("-- a, b, c (+1 more)")


# --- the medium tier reads the same way -----------------------------------------------------


def test_a_tier_is_where_its_results_land_and_how_a_red_names_itself():
    assert e2e_results.E2E == e2e_results.Tier(
        e2e_results.RESULTS_DIR, e2e_results.CONTRACT, e2e_results.WORKFLOW, e2e_results.MISSING
    )
    assert e2e_results.MEDIUM.results_dir != e2e_results.RESULTS_DIR


def test_the_two_tiers_keep_their_results_apart(tmp_path):
    medium = e2e_results.MEDIUM
    e2e_results.write(red(), tmp_path)
    assert e2e_results.read("carameli", tmp_path, medium) is None
    e2e_results.write(red(sha="f" * 12), tmp_path, medium)
    assert e2e_results.read("carameli", tmp_path).sha != "f" * 12
    assert e2e_results.read("carameli", tmp_path, medium).sha == "f" * 12


def test_a_medium_red_is_a_local_failure_naming_its_tier(tmp_path):
    medium = e2e_results.MEDIUM
    e2e_results.write(red(base="master"), tmp_path, medium)
    failure = e2e_results.to_failure(red(base="master"), "b31b60cbbbbb", tmp_path, medium)
    assert failure is not None
    assert (failure.kind, failure.workflow) == (fix_plan.LOCAL, fix_plan.MEDIUM_TIER)
    assert failure.title == "Medium Tier red on master"
    assert failure.evidence == str(e2e_results.result_dir("carameli", tmp_path, medium))


def test_a_medium_run_naming_nothing_is_signed_by_its_own_contract():
    assert e2e_results.signature_of("", 3, "homelab", e2e_results.MEDIUM) == (
        "homelab: scripts/run-tests.py exited 3 naming no failing test",
    )


def test_a_missing_medium_tier_is_one_project_s_gap_not_a_vendored_cause():
    missing = e2e_results.MEDIUM.missing
    assert e2e_results.is_missing((missing,))
    assert not fix_plan.names_nothing((missing,))
    assert not fix_plan.is_vendored((missing,))
    result = e2e_results.missing_result("homelab", "main", "abc", "then", e2e_results.MEDIUM)
    assert (result.status, result.sha, result.signature) == (e2e_results.FAILED, "abc", (missing,))
    assert result.reason == "origin/main has no scripts/run-tests.py"


def test_collect_reads_the_tier_it_is_asked_for(tmp_path):
    (tmp_path / "carameli").mkdir()
    devkit = tmp_path / "devkit"
    e2e_results.write(red(), devkit, e2e_results.MEDIUM)
    workspace = tmp_path / "w"
    assert e2e_results.collect(workspace, ["carameli"], lambda _p: _git("b31b60c"), devkit) == []
    found = e2e_results.collect(
        workspace, ["carameli"], lambda _p: _git("b31b60c"), devkit, e2e_results.MEDIUM
    )
    assert [failure.workflow for failure in found] == [fix_plan.MEDIUM_TIER]


# --- `carried`: a tier that tests only the tip ----------------------------------------------


def _run(status: str, sha: str, *signature: str, **fields: Any) -> e2e_results.Result:
    return e2e_results.Result("x", status, sha=sha, signature=signature, **fields)


def test_a_first_run_and_a_green_run_carry_nothing():
    first = _run(e2e_results.FAILED, "b", "t")
    assert e2e_results.carried(first, None) == first
    green = _run(e2e_results.PASSED, "c")
    assert e2e_results.carried(green, _run(e2e_results.FAILED, "b", "t")) == green


def test_a_red_after_a_green_names_it_as_the_good_end():
    after = e2e_results.carried(_run(e2e_results.FAILED, "b", "t"), _run(e2e_results.PASSED, "a"))
    assert (after.last_green, after.first_red) == ("a", "")


def test_a_failure_that_came_back_is_placed_where_it_was_first_seen():
    previous = _run(e2e_results.FAILED, "b", "t", "u", last_green="a")
    after = e2e_results.carried(_run(e2e_results.FAILED, "c", "t"), previous)
    assert (after.last_green, after.first_red) == ("a", "b")
    third = e2e_results.carried(_run(e2e_results.FAILED, "d", "t"), after)
    assert (third.last_green, third.first_red) == ("a", "b")


def test_an_unrelated_failure_starts_again():
    previous = _run(e2e_results.FAILED, "c", "t", last_green="a", first_red="b")
    after = e2e_results.carried(_run(e2e_results.FAILED, "d", "other"), previous)
    assert (after.last_green, after.first_red) == ("a", "")


def test_the_same_commit_run_twice_proves_nothing_new_but_is_marked_as_the_re_test():
    previous = _run(e2e_results.FAILED, "b", "t", last_green="a")
    after = e2e_results.carried(_run(e2e_results.FAILED, "b", "t"), previous)
    assert (after.first_red, after.retested) == ("", True)
    moved = e2e_results.carried(_run(e2e_results.FAILED, "c", "t"), after)
    assert not moved.retested, "a new tip has its own one re-test"


NOW = _dt.datetime(2026, 11, 6, 14, 0)


def _red_finished(ago: _dt.timedelta, **fields: Any) -> e2e_results.Result:
    stamp = (NOW - ago).isoformat(timespec="seconds")
    return _run(e2e_results.FAILED, "b", "t", finished=stamp, **fields)


def test_a_red_is_due_its_one_re_test_after_the_delay():
    delay = e2e_results.RETEST_AFTER
    assert delay == _dt.timedelta(hours=1)
    assert e2e_results.retest_due(_red_finished(delay), NOW)
    assert not e2e_results.retest_due(_red_finished(delay / 2), NOW)
    assert not e2e_results.retest_due(_red_finished(delay, retested=True), NOW)


def test_nothing_but_a_red_run_is_due_a_re_test():
    assert not e2e_results.retest_due(_run(e2e_results.PASSED, "b"), NOW)
    assert not e2e_results.retest_due(_run(e2e_results.FAILED, "b", "t", finished="x"), NOW)
    stale = "2026-01-01T00:00:00"
    gap = _run(e2e_results.FAILED, "b", e2e_results.MEDIUM.missing, finished=stale)
    assert not e2e_results.retest_due(gap, NOW), "a missing file is not a flake"


def test_a_carried_red_behind_the_real_tip_is_held_so_a_merged_fix_gets_no_second_session(
    tmp_path,
):
    """The tip a fix moved is the whole reason for the hold. `first_red` keeps the key
    from moving; it must not stand in for the tip, or the hold never applies."""
    medium = e2e_results.MEDIUM
    once = red(sha="b" * 12)
    assert fix_plan.is_stale_local(e2e_results.to_failure(once, "c" * 12, tmp_path, medium))
    again = red(sha="c" * 12, first_red="b" * 12, last_green="a" * 12)
    behind = e2e_results.to_failure(again, "d" * 12, tmp_path, medium)
    assert behind.tip == "d" * 12
    assert fix_plan.is_stale_local(behind)
    at_tip = e2e_results.to_failure(again, "c" * 12, tmp_path, medium)
    assert not fix_plan.is_stale_local(at_tip)
    assert (at_tip.sha, at_tip.first_seen, at_tip.green_sha) == (
        "c" * 12,
        "b" * 12,
        "a" * 12,
    )


def test_two_projects_red_with_nothing_named_are_never_read_as_one_cause(tmp_path):
    """Identical lines fold into one devkit `upstream` session that never ends: a
    fixture failure in each of two projects is two defects."""
    signed = [
        e2e_results.Result(
            name,
            e2e_results.FAILED,
            sha="b",
            base="main",
            signature=e2e_results.signature_of("no ids here", 1, name, e2e_results.MEDIUM),
        )
        for name in ("alpha", "beta")
    ]
    failures = [e2e_results.to_failure(r, "b", tmp_path, e2e_results.MEDIUM) for r in signed]
    assert fix_cycle.shared_signatures(failures) == set()
    assert failures[0].signature == ("alpha: scripts/run-tests.py exited 1 naming no failing test",)


def test_a_result_with_no_signature_is_signed_by_its_own_project(tmp_path):
    bare = e2e_results.Result("alpha", e2e_results.FAILED, sha="b", exit_code=2)
    failure = e2e_results.to_failure(bare, "b", tmp_path, e2e_results.MEDIUM)
    assert failure.signature[0].startswith("alpha: ")
