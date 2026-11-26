"""`scripts/e2e-pass.py`: each project's local end-to-end run, at night, once idle."""

from __future__ import annotations

import argparse
import datetime as _dt
import json
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


def fake_git(
    branch: str = "main",
    dirty: str = "",
    fetch: int = 0,
    ff: int = 0,
    log=None,
    on_origin: bool = True,
    tip: str = "b31b60cbbbbb",
):
    """`on_origin`: whether origin's tip carries the contract. `tip` "" is no origin."""

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
        elif args[0] == "cat-file":
            code = 0 if on_origin else 1
        elif args[0] == "rev-parse":
            out, code = tip, 0 if tip else 1
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


def test_a_tier_handed_to_ci_is_recorded_at_the_tip_and_never_run_here(tmp_path, monkeypatch):
    monkeypatch.setattr(e2e_pass.tb, "detect_default_branch", lambda _g, fallback: "main")
    (tmp_path / "carameli").mkdir()
    (tmp_path / "carameli" / ".devkit.toml").write_text(
        '[delivery]\nflow = "direct"\ne2e = "ci"\n', encoding="utf-8"
    )
    result, seen = _project(tmp_path)
    assert seen == {}, "the contract ran on the PC although CI owns it"
    assert (result.status, result.sha, result.reason) == (
        results.SKIPPED,
        "b31b60cbbbbb",
        e2e_pass.IN_CI,
    )
    assert results.read("carameli", tmp_path / "devkit") == result


@pytest.mark.parametrize(
    "text",
    [None, "", "[delivery]\n", '[delivery]\ne2e = "local"\n', "e2e = 'ci'\n", "[delivery\n"],
)
def test_anything_but_delivery_e2e_ci_leaves_the_tier_on_this_pc(tmp_path, text):
    if text is not None:
        (tmp_path / ".devkit.toml").write_text(text, encoding="utf-8")
    assert e2e_pass.runs_in_ci(tmp_path) is False


def test_a_tier_handed_to_ci_is_still_read_from_a_clean_checkout_only(tmp_path, monkeypatch):
    # A checkout someone works in is not this pass's to move, CI tier or not.
    monkeypatch.setattr(e2e_pass.tb, "detect_default_branch", lambda _g, fallback: "main")
    (tmp_path / "carameli").mkdir()
    (tmp_path / "carameli" / ".devkit.toml").write_text(
        '[delivery]\ne2e = "ci"\n', encoding="utf-8"
    )
    result, _ = _project(tmp_path, git=fake_git(branch="agent/x"))
    assert result.reason == "checkout is on agent/x, not main"


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


def test_a_project_whose_origin_has_no_contract_is_recorded_red_and_never_run(
    tmp_path, monkeypatch, capsys
):
    """Every project owes the tier: a missing one is red, which sends its fixer to write
    it, where it used to be listed and forgotten."""
    monkeypatch.setattr(e2e_pass.tb, "detect_default_branch", lambda _g, fallback: "main")
    workspace = _workspace(tmp_path, ["carameli", "roguelike"], opted=["carameli"])
    ran, run_one = _recording()
    seams = e2e_pass.Seams(
        root=tmp_path / "devkit",
        idle=lambda: 1e9,
        now=lambda: NIGHT,
        git_for=lambda _p: fake_git(on_origin=False),
    )
    code = e2e_pass.run_pass(workspace, [], _args(), seams, run_one)
    assert (code, ran) == (0, ["carameli"])
    recorded = results.read("roguelike", tmp_path / "devkit")
    assert recorded is not None
    assert (recorded.status, recorded.sha, recorded.signature) == (
        results.FAILED,
        "b31b60cbbbbb",
        (results.MISSING,),
    )
    assert "roguelike: FAILED at b31b60cbb -- scripts/e2e.py is missing" in capsys.readouterr().out


def test_a_checkout_behind_an_origin_that_has_the_contract_is_run(tmp_path, monkeypatch):
    """The fast-forward brings the script in; the tier a fixer merged must stop the red
    the next night even though the static checkout had not pulled it yet."""
    monkeypatch.setattr(e2e_pass.tb, "detect_default_branch", lambda _g, fallback: "main")
    workspace = _workspace(tmp_path, ["carameli", "roguelike"], opted=["carameli"])
    ran, run_one = _recording()
    seams = e2e_pass.Seams(
        root=tmp_path / "devkit", idle=lambda: 1e9, now=lambda: NIGHT, git_for=lambda _p: fake_git()
    )
    assert e2e_pass.run_pass(workspace, [], _args(), seams, run_one) == 0
    assert ran == ["carameli", "roguelike"]
    assert results.read("roguelike", tmp_path / "devkit") is None


def test_record_missing_answers_none_when_origin_has_the_contract(tmp_path):
    seams = e2e_pass.Seams(root=tmp_path, now=lambda: NIGHT, git_for=lambda _p: fake_git())
    assert e2e_pass.record_missing("carameli", tmp_path / "carameli", seams) is None
    assert not results.result_dir("carameli", tmp_path).exists()


def test_with_no_origin_a_missing_tier_is_a_skip_no_fixer_could_branch_from(tmp_path):
    seams = e2e_pass.Seams(
        root=tmp_path, now=lambda: NIGHT, git_for=lambda _p: fake_git(on_origin=False, tip="")
    )
    result = e2e_pass.record_missing("homelab", tmp_path / "homelab", seams)
    assert result is not None
    assert (result.status, result.reason, result.signature) == (
        results.SKIPPED,
        "no origin/main",
        (),
    )


def test_to_run_after_all_returns_only_what_origin_already_has(tmp_path, monkeypatch, capsys):
    workspace = _workspace(tmp_path, ["a", "b"], opted=[])
    asked = {"a": fake_git(), "b": fake_git(on_origin=False)}
    seams = e2e_pass.Seams(
        root=tmp_path / "devkit", now=lambda: NIGHT, git_for=lambda path: asked[path.name]
    )
    assert e2e_pass.to_run_after_all(workspace, ["a", "b"], seams) == ["a"]
    assert "b: FAILED" in capsys.readouterr().out


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


# --- the scheduled run ----------------------------------------------------------------------

NOON = _dt.datetime(2026, 11, 25, 12, 0)


def _last(status, sha="old", hours=5.0) -> object:
    finished = (NOON - _dt.timedelta(hours=hours)).isoformat()
    return results.Result("carameli", status, sha=sha, finished=finished)


@pytest.mark.parametrize(
    ("name", "last", "due"),
    [
        ("carameli", None, True),
        ("carameli", _last(results.PASSED), True),
        ("carameli", _last(results.PASSED, sha="tip"), False),
        ("carameli", _last(results.FAILED, sha="tip"), False),
        ("carameli", _last(results.PASSED, hours=1), False),
        ("carameli", _last(results.FAILED, hours=1.5), True),
        ("carameli", _last(results.SKIPPED, sha=""), True),
        ("devkit", _last(results.PASSED), False),
        ("devkit", _last(results.PASSED, hours=21), True),
        ("devkit", _last(results.FAILED), True),
        ("carameli", results.Result("carameli", results.PASSED, sha="old"), True),
    ],
)
def test_a_project_is_owed_a_run_when_its_tip_moved_and_not_just_now(name, last, due):
    assert e2e_pass.is_due(name, last, "tip", NOON) is due
    assert not e2e_pass.is_due(name, last, "", NOON), "origin could not be read"


def _due_run(tmp_path, names, idle: float):
    tmp_path.mkdir(exist_ok=True)
    workspace = _workspace(tmp_path, names, opted=names)
    ran, run_one = _recording()
    log: list = []
    seams = e2e_pass.Seams(
        root=tmp_path / "devkit",
        idle=lambda: idle * 60.0,
        now=lambda: NOON,
        git_for=lambda _path: fake_git(log=log),
    )
    code = e2e_pass.run_due(workspace, _args(due=True), seams, run_one)
    return code, ran, log


def test_a_pc_in_use_is_left_alone_without_so_much_as_a_fetch(tmp_path, capsys):
    assert _due_run(tmp_path, ["carameli"], idle=2) == (0, [], [])
    assert "the PC is in use" in capsys.readouterr().out


def test_a_tier_that_opens_windows_waits_for_a_pc_that_was_left(tmp_path):
    assert "devkit" in e2e_pass.OPENS_WINDOWS
    assert _due_run(tmp_path / "a", ["carameli", "devkit"], idle=15)[1] == ["carameli"]
    assert _due_run(tmp_path / "b", ["carameli", "devkit"], idle=90)[1] == ["carameli", "devkit"]
    assert e2e_pass.idle_minutes_for("carameli", 10) == 10
    assert e2e_pass.idle_minutes_for("devkit", 10) == e2e_pass.WINDOWED_IDLE_MINUTES


def test_a_judged_tip_is_not_run_again(tmp_path):
    judged = results.Result("carameli", results.PASSED, sha="b31b60cbbbbb", finished="x")
    results.write(judged, tmp_path / "devkit")
    assert _due_run(tmp_path, ["carameli"], idle=90)[1] == []


def test_the_tip_is_read_fresh_or_not_at_all(tmp_path):
    seams = e2e_pass.Seams(git_for=lambda _p: fake_git())
    assert e2e_pass.tip_of(tmp_path, seams) == "b31b60cbbbbb"
    offline = e2e_pass.Seams(git_for=lambda _p: fake_git(fetch=1))
    assert e2e_pass.tip_of(tmp_path, offline) == ""


def test_a_checkout_someone_is_working_in_never_hides_a_red(tmp_path, monkeypatch):
    monkeypatch.setattr(e2e_pass.tb, "detect_default_branch", lambda _g, fallback: "main")
    red = results.Result("carameli", results.FAILED, sha="a", signature=("t::x",))
    results.write(red, tmp_path / "devkit")
    result, _ = _project(tmp_path, git=fake_git(branch="agent/x"))
    assert result.status == results.SKIPPED
    assert results.read("carameli", tmp_path / "devkit") == red


def _assigned(workspace: Path, hosts) -> None:
    """Add `devkit.localE2e` with `hosts` to a `_workspace` file."""
    text = workspace.read_text(encoding="utf-8").rstrip()[:-1]
    setting = json.dumps({e2e_pass.SETTING: {"hosts": hosts}})
    workspace.write_text(f'{text}, "settings": {setting}}}', encoding="utf-8")


def test_a_machine_hosts_does_not_name_tests_nothing_and_says_who_does(tmp_path, capsys):
    """Every PC registers the job and the workspace file is the same on all of them, so
    without an owner each would test every project and send its own fixer at one red."""
    workspace = _workspace(tmp_path, ["carameli", "devkit"], opted=["carameli", "devkit"])
    _assigned(workspace, "desktop-a")
    ran, run_one = _recording()
    seams = e2e_pass.Seams(idle=lambda: 1e9, now=lambda: NIGHT, host="desktop-b")
    assert e2e_pass.run_pass(workspace, [], _args(), seams, run_one) == 0
    assert ran == []
    assert "carameli: tested by desktop-a, not here (desktop-b)" in capsys.readouterr().out


def test_projects_can_be_spread_across_machines(tmp_path):
    workspace = _workspace(tmp_path, ["carameli", "devkit"], opted=["carameli", "devkit"])
    _assigned(workspace, {"*": "desktop-a", "carameli": "desktop-b"})
    assert e2e_pass.tested_here(workspace, ["carameli", "devkit"], "desktop-b") == ["carameli"]
    for host, expected in (("desktop-a", ["devkit"]), ("desktop-b", ["carameli"])):
        ran, run_one = _recording()
        seams = e2e_pass.Seams(idle=lambda: 1e9, now=lambda: NIGHT, host=host)
        e2e_pass.run_pass(workspace, [], _args(), seams, run_one)
        assert ran == expected, host


def test_a_missing_tier_is_recorded_only_by_the_machine_assigned_it(tmp_path):
    workspace = _workspace(tmp_path, ["roguelike"], opted=[])
    _assigned(workspace, "desktop-a")
    seams = e2e_pass.Seams(
        root=tmp_path / "devkit",
        now=lambda: NIGHT,
        host="desktop-b",
        git_for=lambda _p: pytest.fail("another machine's project was fetched"),
    )
    assert e2e_pass.run_pass(workspace, [], _args(), seams, _recording()[1]) == 0
    assert results.read("roguelike", tmp_path / "devkit") is None


def _role(workspace: Path, role: str, hosts=None) -> None:
    """Give a `_workspace` file `devkit.machineRole`, and `devkit.localE2e` hosts if any."""
    text = workspace.read_text(encoding="utf-8").rstrip()[:-1]
    settings: dict[str, object] = {"devkit.machineRole": role}
    if hosts is not None:
        settings[e2e_pass.SETTING] = {"hosts": hosts}
    workspace.write_text(f'{text}, "settings": {json.dumps(settings)}}}', encoding="utf-8")


def _role_run(tmp_path, role: str, disk=None, hosts=None):
    tmp_path.mkdir(exist_ok=True)
    workspace = _workspace(tmp_path, ["carameli"], opted=["carameli"])
    _role(workspace, role, hosts)
    ran, run_one = _recording()
    git = fake_git()
    seams = e2e_pass.Seams(
        root=tmp_path / "devkit",
        idle=lambda: 1e9,
        now=lambda: NOON,
        # `job_claims` hands git an environment, which the fake has no use for.
        git_for=lambda _path: lambda *args, **_kw: git(*args),
        host="pc-a",
        disk=lambda: disk,
    )
    return e2e_pass.run_due(workspace, _args(due=True), seams, run_one), ran, seams


def test_a_role_with_no_hosts_claims_each_run_and_a_named_host_stands(tmp_path):
    workspace = _workspace(tmp_path, ["carameli"], opted=["carameli"])
    seams = e2e_pass.Seams(root=tmp_path / "devkit")
    assert e2e_pass.shared(workspace, seams) == "", "no role is today's behaviour"
    _role(workspace, "worker")
    assert e2e_pass.shared(workspace, seams) == "worker"
    (tmp_path / "b").mkdir()
    _role(_workspace(tmp_path / "b", ["carameli"], opted=[]), "worker", hosts="pc-a")
    assert e2e_pass.shared(tmp_path / "b" / "w.code-workspace", seams) == ""
    assert e2e_pass.shared(tmp_path / "missing.code-workspace", seams) == ""
    assert e2e_pass.job_id("carameli", "b31b60cbbbbbcccc") == "e2e-carameli-b31b60cbbbbb"


def _worker_alive(monkeypatch, alive: bool = True) -> None:
    """Stand in for the heartbeat read off devkit's origin: a worker beating, or none."""

    def workers(_root: Path, now: float, _me: str) -> dict[str, float]:
        return {"laptop": now} if alive else {}

    monkeypatch.setattr(e2e_pass.job_claims, "workers_alive", workers)


def test_a_workstation_leaves_a_due_run_to_a_worker_and_records_when_it_first_saw_it(
    tmp_path, capsys, monkeypatch
):
    _worker_alive(monkeypatch)
    code, ran, seams = _role_run(tmp_path, "workstation")
    assert (code, ran) == (0, [])
    assert "held for a worker (laptop" in capsys.readouterr().out
    seen = (seams.root / e2e_pass.job_claims.SIGHTINGS_DIR / "e2e.json").read_text("utf-8")
    assert "e2e-carameli-b31b60cbbbbb" in seen


def test_a_workstation_with_no_worker_beating_runs_a_due_project_at_once(
    tmp_path, capsys, monkeypatch
):
    _worker_alive(monkeypatch, alive=False)
    code, ran, _seams = _role_run(tmp_path, "workstation")
    assert (code, ran) == (0, ["carameli"])
    assert "no worker to wait for" in capsys.readouterr().out


def test_a_worker_runs_a_due_project_at_once(tmp_path):
    """The fake git makes no claim commit, which is a claim that failed open."""
    code, ran, _seams = _role_run(tmp_path, "worker")
    assert (code, ran) == (0, ["carameli"])


def test_no_run_starts_below_the_disk_floor_whatever_the_role(tmp_path, capsys):
    low = e2e_pass.job_claims.DISK_FLOOR_MB - 1
    for role in ("worker", ""):
        code, ran, _seams = _role_run(tmp_path / (role or "unset"), role, disk=low)
        assert (code, ran) == (0, []), role
    assert "held for disk" in capsys.readouterr().out


def test_not_ours_names_the_holder_and_a_worker_wins_an_open_claim(tmp_path, monkeypatch):
    _worker_alive(monkeypatch)
    seams = e2e_pass.Seams(root=tmp_path / "devkit", host="pc-b", git_for=lambda _p: None)
    claims = e2e_pass.job_claims
    answers = iter([claims.Attempt(claims.LOST, holder="pc-a"), claims.Attempt(claims.WON)])
    monkeypatch.setattr(claims, "claim", lambda *a: next(answers))
    job = "e2e-carameli-abc"
    assert e2e_pass.not_ours("carameli", tmp_path, job, "worker", seams) == "claimed by pc-a"
    assert e2e_pass.not_ours("carameli", tmp_path, job, "worker", seams) == ""
    held = e2e_pass.not_ours("carameli", tmp_path, job, "workstation", seams)
    assert held.startswith(claims.HELD_FOR_WORKER), "asked before any claim"


def test_a_run_s_claim_is_marked_finished_on_the_machine_that_holds_it(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    checkouts = []
    for name in ("pc-a", "pc-b"):
        checkout = tmp_path / name
        checkout.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=checkout, check=True)
        subprocess.run(
            ["git", "remote", "add", "origin", remote.as_posix()], cwd=checkout, check=True
        )
        checkouts.append(checkout)
    a, b = checkouts
    claims = e2e_pass.job_claims
    job = e2e_pass.job_id("carameli", "b31b60cbbbbbcccc")
    assert claims.claim(e2e_pass.sweep.git_for(a), job, claims.E2E, "pc-a").won
    e2e_pass.finished(a, job, host="pc-a")
    later = claims.time.time() + claims.ttl_of(claims.E2E).total_seconds() * 2
    attempt = claims.claim(e2e_pass.sweep.git_for(b), job, claims.E2E, "pc-b", later)
    assert attempt.outcome == claims.LOST


@pytest.mark.parametrize(
    "text",
    ["{", "[]", '{"settings": []}', '{"settings": {"devkit.localE2e": "pc"}}', "{}"],
)
def test_an_absent_or_broken_setting_assigns_nobody_so_every_machine_tests_its_own(text):
    assert e2e_pass.assignment(text).hosts == ()


def test_the_canonical_workspace_assigns_the_nightly_run_to_a_machine():
    """With several PCs registering the job, an absent setting would have each of them
    test every project; the committed file names the one that does."""
    canonical = (Path(__file__).resolve().parents[1] / "workspace.jsonc").read_text(
        encoding="utf-8"
    )
    assert e2e_pass.assignment(canonical).hosts


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
