"""`scripts/e2e_results.py`: a local end-to-end run, as a file and as the fix pass's red."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import e2e_results
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
        "signature": e2e_results.signature_of(OUTPUT, 1),
    }
    base.update(fields)
    return e2e_results.Result(**base)


def test_the_signature_is_the_failing_ids_pytest_printed():
    assert e2e_results.signature_of(OUTPUT, 1) == (
        "tests/test_tasks_live.py::test_escape_cancels[agent]",
        "tests/test_tasks_live.py::test_every_picker_draws",
    )


def test_a_red_run_naming_no_test_is_signed_by_its_exit_code_never_empty():
    """An empty signature matches every other artifact-less failure, and the plan holds
    a failure that names nothing as unread -- forever, for a file nothing re-reads."""
    sig = e2e_results.signature_of("Traceback ...\nImportError: no module x\n", 2)
    assert sig == ("scripts/e2e.py exited 2 naming no failing test",)
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
    assert failure.signature == ("scripts/e2e.py exited 1 naming no failing test",)


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
