"""`scripts/e2e-pass.py`: each project's local end-to-end run, at night, once idle."""

from __future__ import annotations

import argparse
import datetime as _dt
import subprocess
from pathlib import Path

import pytest
from support import load_script

e2e_pass = load_script("scripts/e2e-pass.py")
e2e = load_script("scripts/e2e.py")
results = e2e_pass.e2e_results

NIGHT = _dt.datetime(2026, 11, 2, 1, 0)


def test_the_flag_the_pass_sets_is_the_one_the_contract_reads():
    assert e2e_pass.ENV_FLAG == e2e.ENV_FLAG


# --- idle ---------------------------------------------------------------------------------


def test_the_deadline_is_the_next_until_after_now():
    assert e2e_pass.deadline_at("06:30", NIGHT) == _dt.datetime(2026, 11, 2, 6, 30)
    late = _dt.datetime(2026, 11, 2, 7, 0)
    assert e2e_pass.deadline_at("06:30", late) == _dt.datetime(2026, 11, 3, 6, 30)


class Clock:
    def __init__(self, start: _dt.datetime):
        self.at = start
        self.slept: list[float] = []

    def now(self) -> _dt.datetime:
        return self.at

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.at += _dt.timedelta(seconds=seconds)


def test_an_already_idle_machine_runs_at_once():
    clock = Clock(NIGHT)
    deadline = NIGHT + _dt.timedelta(hours=5)
    assert e2e_pass.wait_for_idle(600, deadline, idle=lambda: 900, now=clock.now, sleep=clock.sleep)
    assert clock.slept == []


def test_someone_working_late_is_waited_out_in_steps_no_longer_than_the_stillness_owed():
    clock = Clock(NIGHT)
    answers = iter([0.0, 30.0, 500.0, 650.0])
    deadline = NIGHT + _dt.timedelta(hours=5)
    assert e2e_pass.wait_for_idle(
        600, deadline, idle=lambda: next(answers), now=clock.now, sleep=clock.sleep, poll=120
    )
    assert clock.slept == [120, 120, 100]


def test_a_machine_never_idle_before_the_deadline_is_given_up_on():
    clock = Clock(NIGHT)
    deadline = NIGHT + _dt.timedelta(minutes=10)
    assert not e2e_pass.wait_for_idle(
        600, deadline, idle=lambda: 0.0, now=clock.now, sleep=clock.sleep, poll=120
    )
    assert sum(clock.slept) == 600


def test_idle_is_forever_where_there_is_no_session_to_disturb(monkeypatch):
    monkeypatch.setattr(e2e_pass.sys, "platform", "linux")
    assert e2e_pass.idle_seconds() == float("inf")


# --- one project --------------------------------------------------------------------------


def fake_git(branch: str = "main", dirty: str = "", fetch: int = 0, ff: int = 0, log=None):
    def git(*args):
        if log is not None:
            log.append(args)
        out, code = "", 0
        if args[0] == "fetch":
            code = fetch
        elif args[:2] == ("branch", "--show-current"):
            out = branch
        elif args[0] == "status":
            out = dirty
        elif args[0] == "merge":
            code = ff
        elif args[0] == "rev-parse":
            out = "b31b60cbbbbb"
        return subprocess.CompletedProcess(args, code, f"{out}\n", "fatal: said so" if code else "")

    return git


def test_a_checkout_on_its_clean_default_branch_is_brought_to_origins_tip():
    log: list = []
    assert e2e_pass.prepare(fake_git(log=log), "main") == ("b31b60cbbbbb", "")
    assert ("merge", "--ff-only", "--quiet", "origin/main") in log


@pytest.mark.parametrize(
    ("git", "why"),
    [
        (fake_git(fetch=1), "git fetch failed: fatal: said so"),
        (fake_git(branch="agent/x"), "checkout is on agent/x, not main"),
        (fake_git(branch=""), "checkout is on a detached HEAD, not main"),
        (fake_git(dirty=" M a.py"), "checkout has uncommitted changes to tracked files"),
        (fake_git(ff=1), "cannot fast-forward to origin/main: fatal: said so"),
    ],
)
def test_a_checkout_someone_is_working_in_is_left_alone_with_the_reason(git, why):
    assert e2e_pass.prepare(git, "main") == ("", why)


def _run(code: int, out: str = ""):
    seen: dict = {}

    def runner(argv, timeout, **kwargs):
        seen.update(argv=argv, timeout=timeout, **kwargs)
        return subprocess.CompletedProcess(argv, code, out, "")

    return runner, seen


def _project(tmp_path: Path, name: str = "carameli", git=None, code: int = 0, out: str = ""):
    runner, seen = _run(code, out)
    seams = e2e_pass.Seams(
        root=tmp_path / "devkit",
        now=lambda: NIGHT,
        git_for=lambda _p: git or fake_git(),
        runner=runner,
    )
    return e2e_pass.run_project(name, tmp_path / name, 60, seams), seen


def test_the_contract_runs_in_the_checkout_under_its_own_interpreter_with_the_flag(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(e2e_pass.tb, "detect_default_branch", lambda _g, fallback: "main")
    venv = tmp_path / "carameli" / ".venv" / "Scripts" / "python.exe"
    venv.parent.mkdir(parents=True)
    venv.write_text("", encoding="utf-8")
    _result, seen = _project(tmp_path)
    assert seen["argv"] == [str(venv), str(tmp_path / "carameli" / "scripts" / "e2e.py")]
    assert seen["cwd"] == str(tmp_path / "carameli")
    assert seen["env"][e2e_pass.ENV_FLAG] == "1"
    assert seen["timeout"] == 60


def test_a_green_run_is_recorded_at_the_commit_it_tested(tmp_path, monkeypatch):
    monkeypatch.setattr(e2e_pass.tb, "detect_default_branch", lambda _g, fallback: "main")
    result, _ = _project(tmp_path, out="4 passed\n")
    assert (result.status, result.sha, result.signature) == (results.PASSED, "b31b60cbbbbb", ())
    assert results.read("carameli", tmp_path / "devkit") == result
    output = results.result_dir("carameli", tmp_path / "devkit") / results.OUTPUT_FILE
    assert "4 passed" in output.read_text(encoding="utf-8")


def test_a_red_run_is_recorded_with_what_it_named(tmp_path, monkeypatch):
    monkeypatch.setattr(e2e_pass.tb, "detect_default_branch", lambda _g, fallback: "main")
    result, _ = _project(tmp_path, code=1, out="FAILED tests/t.py::test_a - boom\n")
    assert result.status == results.FAILED
    assert result.signature == ("tests/t.py::test_a",)
    assert results.read("carameli", tmp_path / "devkit") == result


def test_a_hung_run_is_red_and_says_it_timed_out(tmp_path, monkeypatch):
    monkeypatch.setattr(e2e_pass.tb, "detect_default_branch", lambda _g, fallback: "main")
    result, _ = _project(tmp_path, code=e2e_pass.sweep.TIMED_OUT)
    assert (result.status, result.reason) == (results.FAILED, "timed out")


def test_a_skipped_checkout_is_recorded_and_its_contract_never_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(e2e_pass.tb, "detect_default_branch", lambda _g, fallback: "main")
    result, seen = _project(tmp_path, git=fake_git(branch="agent/x"))
    assert (result.status, result.reason) == (results.SKIPPED, "checkout is on agent/x, not main")
    assert seen == {}
    assert results.read("carameli", tmp_path / "devkit") == result


# --- the pass -----------------------------------------------------------------------------


def _workspace(tmp_path: Path, names: list[str], opted: list[str]) -> Path:
    folders = ",".join(f'{{"path": "{name}"}}' for name in names)
    workspace = tmp_path / "w.code-workspace"
    workspace.write_text(f'{{"folders": [{folders}]}}', encoding="utf-8")
    for name in names:
        (tmp_path / name / "scripts").mkdir(parents=True)
        if name in opted:
            (tmp_path / name / "scripts" / "e2e.py").write_text("", encoding="utf-8")
    return workspace


def _args(**fields) -> argparse.Namespace:
    base = {"no_wait": False, "idle_minutes": 10.0, "until": "06:30", "timeout_minutes": 45.0}
    return argparse.Namespace(**(base | fields))


def _recording():
    ran: list[str] = []

    def run_one(name, _project_dir, _timeout, _seams):
        ran.append(name)
        return results.Result(project=name, status=results.PASSED, sha="abc")

    return ran, run_one


def test_only_projects_shipping_the_contract_run_and_the_rest_are_named(tmp_path, capsys):
    workspace = _workspace(tmp_path, ["carameli", "roguelike"], opted=["carameli"])
    ran, run_one = _recording()
    seams = e2e_pass.Seams(idle=lambda: 1e9, now=lambda: NIGHT)
    code = e2e_pass.run_pass(workspace, [], _args(), seams, run_one)
    assert (code, ran) == (0, ["carameli"])
    assert "roguelike: no scripts/e2e.py, so no end-to-end tier" in capsys.readouterr().out


def test_a_night_with_no_idle_runs_nothing_and_is_not_a_failure(tmp_path, capsys):
    """The job exits 0: a failure would file a ledger row for someone working late."""
    workspace = _workspace(tmp_path, ["carameli"], opted=["carameli"])
    ran, run_one = _recording()
    clock = Clock(_dt.datetime(2026, 11, 2, 6, 0))
    seams = e2e_pass.Seams(idle=lambda: 0.0, now=clock.now, sleep=clock.sleep)
    code = e2e_pass.run_pass(workspace, [], _args(), seams, run_one)
    assert (code, ran) == (0, [])
    assert "never idle before 06:30; not run tonight: carameli" in capsys.readouterr().out


def test_by_hand_no_wait_runs_whoever_is_at_the_keyboard(tmp_path):
    workspace = _workspace(tmp_path, ["carameli", "devkit"], opted=["carameli", "devkit"])
    ran, run_one = _recording()
    seams = e2e_pass.Seams(idle=lambda: 0.0)
    code = e2e_pass.run_pass(workspace, ["devkit"], _args(no_wait=True), seams, run_one)
    assert (code, ran) == (0, ["devkit"])


def test_registered_reads_the_workspace_and_opted_in_splits_it_by_the_contract(tmp_path):
    workspace = _workspace(tmp_path, ["carameli", "roguelike"], opted=["roguelike"])
    names = e2e_pass.registered(workspace)
    assert names == ["carameli", "roguelike"]
    assert e2e_pass.opted_in(workspace, names) == (["roguelike"], ["carameli"])


def test_run_contract_hands_the_runner_the_timeout_it_was_given(tmp_path):
    runner, seen = _run(0)
    e2e_pass.run_contract(tmp_path, 12.5, runner)
    assert seen["timeout"] == 12.5 and seen["cwd"] == str(tmp_path)
    assert seen["argv"][-1] == str(tmp_path / "scripts" / "e2e.py")


def test_main_reports_each_last_result_and_refuses_a_missing_workspace(
    tmp_path, monkeypatch, capsys
):
    workspace = _workspace(tmp_path, ["carameli"], opted=["carameli"])
    monkeypatch.setattr(e2e_pass.sweep, "default_workspace", lambda _root: workspace)
    results.write(
        results.Result("carameli", results.PASSED, sha="b31b60cbbbbb"), tmp_path / "devkit"
    )
    read = results.read
    monkeypatch.setattr(results, "read", lambda name: read(name, tmp_path / "devkit"))
    assert e2e_pass.main(["status"]) == 0
    assert "carameli: passed at b31b60cbb" in capsys.readouterr().out
    monkeypatch.setattr(e2e_pass.sweep, "default_workspace", lambda _root: tmp_path / "none")
    assert e2e_pass.main(["status"]) == 2
    assert "no workspace file at" in capsys.readouterr().err


def test_a_name_the_workspace_does_not_register_is_refused(tmp_path, capsys):
    workspace = _workspace(tmp_path, ["carameli"], opted=[])
    assert e2e_pass.run_pass(workspace, ["nope"], _args(no_wait=True)) == 2
    assert "not registered in the workspace: nope" in capsys.readouterr().err


def test_each_result_is_one_line_that_says_what_happened():
    line = e2e_pass.line_for
    assert line(results.Result("a", results.PASSED, sha="b31b60cbbbbb")) == "a: passed at b31b60cbb"
    assert line(results.Result("a", results.SKIPPED, reason="why")) == "a: skipped -- why"
    many = results.Result("a", results.FAILED, sha="b31b60c", signature=("w", "x", "y", "z"))
    assert line(many) == "a: FAILED at b31b60c -- w, x, y (+1 more)"


def test_the_cli_takes_the_projects_and_its_bounds():
    args = e2e_pass.build_parser().parse_args(["run", "devkit", "--no-wait", "--until", "05:00"])
    assert (args.verb, args.projects, args.no_wait, args.until) == (
        "run",
        ["devkit"],
        True,
        "05:00",
    )
    assert e2e_pass.build_parser().parse_args(["status"]).verb == "status"
