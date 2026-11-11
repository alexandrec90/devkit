"""`scripts/tier-pass.py`: each project's own tests, on a free PC, at origin's tip."""

from __future__ import annotations

import argparse
import datetime as _dt
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
from support import generate, load_script

tier_pass = load_script("scripts/tier-pass.py")
results = tier_pass.e2e_results
state = tier_pass.tier_state
TIER = tier_pass.TIER

NOW = _dt.datetime(2026, 11, 6, 14, 0)
TIP = "b31b60cbbbbbcccc"


class Clock:
    """Each read is a minute on, so a test can run a pass out of time."""

    def __init__(self, start: _dt.datetime = NOW, step: float = 1.0):
        self.at = start
        self.step = _dt.timedelta(minutes=step)

    def __call__(self) -> _dt.datetime:
        self.at += self.step
        return self.at


def fake_git(
    tip: str = TIP, toml: str = "", contract: bool = True, fetch: int = 0, script: str = '"--all"'
):
    def git(*args):
        out, code = "", 0
        if args[0] == "fetch":
            code = fetch
        elif args[0] == "rev-parse":
            out, code = tip, 0 if tip else 1
        elif args[0] == "symbolic-ref":
            out = "refs/remotes/origin/main"
        elif args[0] == "show":
            body = toml if args[1].endswith(".devkit.toml") else (script if contract else "")
            out, code = body, 0 if body else 128
        return subprocess.CompletedProcess(args, code, f"{out}\n", "fatal: said so" if code else "")

    return git


class FakeRuns:
    """`sweep.run_bounded`'s shape: git's tree verbs make a directory, a suite exits `code`."""

    def __init__(self, code: int = 0, out: str = "", artifact: str = ""):
        self.code, self.out, self.artifact = code, out, artifact
        self.suites: list[tuple[list[str], str]] = []
        self.removed: list[str] = []

    def __call__(self, argv, _timeout, **kwargs):
        if argv[0] == "git":
            return self.git(argv)
        self.suites.append((list(argv), kwargs["cwd"]))
        if self.artifact:
            log = Path(kwargs["cwd"]) / "logs" / "test-failures.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(self.artifact, encoding="utf-8")
        return subprocess.CompletedProcess(argv, self.code, self.out, "")

    def git(self, argv):
        verbs = argv[3:]
        if verbs[:2] == ["worktree", "add"]:
            scripts = Path(verbs[-2]) / "scripts"
            scripts.mkdir(parents=True)
            (scripts / "run-tests.py").write_text('"--all"\n', encoding="utf-8")
        if verbs[:2] == ["worktree", "remove"]:
            self.removed.append(verbs[-1])
            shutil.rmtree(verbs[-1], ignore_errors=True)
        return subprocess.CompletedProcess(argv, 0, "", "")


def _seams(tmp_path: Path, runs=None, **fields):
    base = {
        "root": tmp_path / "devkit",
        "now": Clock(),
        "git_for": lambda _p: fake_git(),
        "runner": runs or FakeRuns(),
        "room": lambda: 64000,
        "claim": lambda _c, _j: True,
        "provision": lambda _tree: "",
    }
    return tier_pass.Seams(**(base | fields))


def _workspace(tmp_path: Path, names: list[str], mode: str = "dispatch") -> Path:
    folders = ",".join(f'{{"path": "{name}"}}' for name in names)
    workspace = tmp_path / "w.code-workspace"
    switch = f'"devkit.fixPass": "{mode}"' if mode else ""
    workspace.write_text(f'{{"folders": [{folders}], "settings": {{{switch}}}}}', encoding="utf-8")
    for name in names:
        (tmp_path / name / ".git").mkdir(parents=True)
    return workspace


def _args(**fields) -> argparse.Namespace:
    base = {"timeout_minutes": 45.0, "pass_minutes": 120.0}
    return argparse.Namespace(**(base | fields))


def _target(tmp_path: Path, name: str = "carameli", **fields):
    base = {"name": name, "checkout": tmp_path / name, "base": "main", "tip": TIP}
    return tier_pass.Target(**(base | fields))


def _stored(root: Path, project: str, **fields) -> None:
    base = {"project": project, "status": results.PASSED, "sha": "a" * 16, "base": "main"}
    results.write(results.Result(**(base | fields)), root, TIER)


# --- what runs ----------------------------------------------------------------------------


def test_the_generated_manifest_parses_and_declares_no_delivery_table(tmp_path):
    """A live header there and package B's commented one become a duplicate table when
    uncommented; the TOML then fails to parse, and `harness_config.load` returns
    defaults without a word."""
    root = generate(tmp_path, {})
    manifest = tomllib.loads((root / ".devkit.toml").read_text(encoding="utf-8"))
    assert "delivery" not in manifest
    assert "medium_tier" in (root / ".devkit.toml").read_text(encoding="utf-8")


def test_a_command_that_cannot_start_records_nothing_and_is_the_project_s(tmp_path):
    """It judges nothing, as the docstring says, and only the project's `.devkit.toml`
    can have asked for it."""

    def runner(_argv, _timeout, **_kwargs):
        raise FileNotFoundError("no such program")

    with pytest.raises(tier_pass.NoVerdict, match="could not start") as raised:
        tier_pass.run_tests(tmp_path, "nothere go", 60.0, runner)
    assert raised.value.own
    with pytest.raises(tier_pass.NoVerdict):
        tier_pass.run_tests(tmp_path, 'unbalanced "quote', 60.0, runner)


def test_a_runner_that_cannot_start_is_not_blamed_on_the_project(tmp_path):
    with pytest.raises(tier_pass.NoVerdict) as raised:
        tier_pass.run_tests(tmp_path, "", 60.0, lambda *_a, **_k: None)
    assert not raised.value.own


def test_a_popen_failure_leaves_no_result_and_the_tree_gone(tmp_path):
    class NoSuchProgram(FakeRuns):
        def __call__(self, argv, timeout, **kwargs):
            if argv[0] != "git":
                raise OSError("no such program")
            return super().__call__(argv, timeout, **kwargs)

    runs = NoSuchProgram()
    seams = _seams(tmp_path, runs)
    with pytest.raises(tier_pass.NoVerdict):
        tier_pass.run_at(_target(tmp_path), "", 60.0, seams)
    assert results.read("carameli", seams.root, TIER) is None
    assert runs.removed, "the tree still goes"


def test_a_suite_under_xdist_is_held_to_the_tier_s_workers(tmp_path):
    """`PYTEST_XDIST_AUTO_NUM_WORKERS` is what `-n auto` itself reads, so no project's
    runner has to take a flag, and it means nothing to one that runs serially."""
    seen = {}

    def runner(argv, _timeout, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "", "")

    tier_pass.run_tests(tmp_path, "python -m x", 90.0, runner)
    assert seen["env"]["PYTEST_XDIST_AUTO_NUM_WORKERS"] == str(tier_pass.WORKERS)
    assert tier_pass.WORKERS <= 2


def test_the_run_goes_in_the_tree_under_its_own_interpreter_with_the_flag_set(tmp_path):
    script = tmp_path / "scripts" / "run-tests.py"
    script.parent.mkdir()
    script.write_text("", encoding="utf-8")
    venv = tmp_path / ".venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").write_text("", encoding="utf-8")
    seen = {}

    def runner(argv, timeout, **kwargs):
        seen.update(argv=argv, timeout=timeout, **kwargs)
        return subprocess.CompletedProcess(argv, 0, "", "")

    tier_pass.run_tests(tmp_path, "", 90.0, runner)
    assert seen["argv"] == [str(venv / "python"), str(script)]
    assert (seen["timeout"], seen["cwd"]) == (90.0, str(tmp_path))
    assert seen["env"][tier_pass.ENV_FLAG] == "1"


def test_the_output_carries_the_artifact_the_runner_wrote_instead_of_printing(tmp_path):
    """`run-tests.py` prints only "details in logs/test-failures.log", and the tree goes
    with it: without this every red would be signed by its exit code alone."""
    log = tmp_path / "logs" / "test-failures.log"
    log.parent.mkdir()
    log.write_text("FAILED tests/test_a.py::test_x - boom\n", encoding="utf-8")
    done = subprocess.CompletedProcess([], 1, "run-tests: FAILED\n", "")
    output = tier_pass.output_of(tmp_path, ["py", "run-tests.py"], done)
    assert output.startswith("$ py run-tests.py\n")
    assert results.signature_of(output, 1, TIER) == ("tests/test_a.py::test_x",)


def test_output_of_a_run_that_left_no_artifact_is_what_it_said(tmp_path):
    done = subprocess.CompletedProcess([], 0, "ok\n", "warn\n")
    assert "ok\n\nwarn" in tier_pass.output_of(tmp_path, [], done)


# --- one machine, and many ----------------------------------------------------------------


def test_the_machine_lock_refuses_a_second_holder_and_frees_on_exit(tmp_path):
    path = tmp_path / "logs" / "pass.lock"
    with tier_pass.machine_lock(path) as first:
        assert first
        with tier_pass.machine_lock(path) as second:
            assert not second
    with tier_pass.machine_lock(path) as again:
        assert again


def test_the_job_id_is_the_one_the_lead_verifies_against_package_c():
    assert tier_pass.job_id("carameli", "abcdef0123456789") == "tier-carameli-abcdef012345"


def _machines(tmp_path: Path) -> tuple[Path, Path]:
    """Two checkouts sharing a bare remote: two PCs' copies of one project."""
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    checkouts = []
    for name in ("pc-a", "pc-b"):
        checkout = tmp_path / name
        checkout.mkdir()
        _git(checkout, "init", "-q")
        _git(checkout, "remote", "add", "origin", remote.as_posix())
        checkouts.append(checkout)
    return checkouts[0], checkouts[1]


def test_one_commit_is_claimed_by_one_machine_through_the_real_claims_module(tmp_path):
    """Against `job_claims` itself, not a stand-in: the fake this replaced spelled a
    signature the module never had, and the first scheduled pass after it landed died on
    `claim() missing 1 required positional argument: 'host'`."""
    a, b = _machines(tmp_path)
    job = tier_pass.job_id("carameli", TIP)
    assert tier_pass.claimed(a, job, host=lambda: "pc-a")
    assert not tier_pass.claimed(b, job, host=lambda: "pc-b")
    assert tier_pass.claimed(a, job, host=lambda: "pc-a"), "its holder asking again"
    assert tier_pass.claimed(b, tier_pass.job_id("carameli", "c" * 16), host=lambda: "pc-b")


def test_a_claim_that_could_not_be_made_fails_open_and_says_so(tmp_path, capsys):
    checkout = tmp_path / "pc-a"
    checkout.mkdir()
    _git(checkout, "init", "-q")
    _git(checkout, "remote", "add", "origin", (tmp_path / "nowhere.git").as_posix())
    assert tier_pass.claimed(checkout, "tier-x-1", host=lambda: "pc-a")
    assert "claim failed open" in capsys.readouterr().out


def test_a_claim_outlasts_a_run_at_the_default_timeout():
    """Shorter, and a second PC breaks the claim of a run still going and tests it too."""
    run = tier_pass.DEFAULT_TIMEOUT_MINUTES * 60 + tier_pass.CLAIM_MARGIN_SECONDS
    assert tier_pass.job_claims.ttl_of(tier_pass.job_claims.TIER).total_seconds() > run


def test_memory_guards_only_a_probe_that_answered():
    floor = tier_pass.RUN_MB + tier_pass.host_memory.FLOOR_MB
    assert tier_pass.enough_memory(floor)
    assert not tier_pass.enough_memory(floor - 1)
    assert tier_pass.enough_memory(None), "a failed probe is no guard, as host_memory asks"
    assert tier_pass.RUN_MB == 2048


# --- one project --------------------------------------------------------------------------


def test_a_green_run_records_the_commit_and_destroys_its_tree(tmp_path):
    runs = FakeRuns()
    seams = _seams(tmp_path, runs)
    result = tier_pass.run_at(_target(tmp_path), "", 60.0, seams)
    assert (result.status, result.sha, result.base) == (results.PASSED, TIP, "main")
    assert results.read("carameli", seams.root, TIER) == result
    argv, cwd = runs.suites[0]
    assert "--all" in argv
    assert Path(cwd).name == f"disposable-{TIP[:12]}"
    assert Path(cwd).parent == tmp_path / "carameli" / ".claude" / "worktrees"
    assert runs.removed == [cwd]


def test_a_red_run_is_a_result_the_pass_reads_as_a_medium_tier_red(tmp_path):
    runs = FakeRuns(code=1, artifact="FAILED tests/test_a.py::test_x - boom\n")
    seams = _seams(tmp_path, runs)
    result = tier_pass.run_at(_target(tmp_path), "", 60.0, seams)
    assert (result.status, result.signature) == (results.FAILED, ("tests/test_a.py::test_x",))
    failure = results.to_failure(result, TIP, seams.root, TIER)
    assert failure is not None
    assert (failure.kind, failure.workflow) == (results.fix_plan.LOCAL, "Medium Tier")
    where = results.result_dir("carameli", seams.root, TIER)
    assert failure.evidence == str(where)
    assert "test_a.py::test_x" in (where / results.OUTPUT_FILE).read_text(encoding="utf-8")


def test_a_timed_out_run_is_red_and_says_so(tmp_path):
    runs = FakeRuns(code=tier_pass.sweep.TIMED_OUT)
    result = tier_pass.run_at(_target(tmp_path), "", 60.0, _seams(tmp_path, runs))
    assert (result.status, result.reason) == (results.FAILED, "timed out")


def test_a_red_remembers_the_last_green_commit_for_the_fixer_to_bisect_from(tmp_path):
    seams = _seams(tmp_path, FakeRuns(code=1))
    _stored(seams.root, "carameli", sha="9" * 16)
    result = tier_pass.run_at(_target(tmp_path), "", 60.0, seams)
    assert result.last_green == "9" * 16


def test_a_tree_git_refused_judges_nothing(tmp_path):
    class Refusing(FakeRuns):
        def git(self, argv):
            if argv[3:5] == ["worktree", "add"]:
                return subprocess.CompletedProcess(argv, 128, "", "fatal: no")
            return super().git(argv)

    seams = _seams(tmp_path, Refusing())
    with pytest.raises(tier_pass.NoVerdict, match="fatal: no"):
        tier_pass.run_at(_target(tmp_path), "", 60.0, seams)
    assert results.read("carameli", seams.root, TIER) is None


def test_a_toolchain_that_would_not_install_judges_nothing_and_leaves_a_red_in_place(tmp_path):
    """A network blip must not read as the project being red, nor hide that it is."""
    runs = FakeRuns()
    seams = _seams(tmp_path, runs, provision=lambda _tree: "uv: no network")
    _stored(seams.root, "carameli", status=results.FAILED, signature=("t",))
    before = results.read("carameli", seams.root, TIER)
    with pytest.raises(tier_pass.NoVerdict, match="uv: no network") as raised:
        tier_pass.run_at(_target(tmp_path), "", 60.0, seams)
    assert not raised.value.own
    assert results.read("carameli", seams.root, TIER) == before
    assert runs.suites == []
    assert runs.removed, "the tree still goes"


def test_dependencies_that_do_not_resolve_are_the_project_s_own(tmp_path):
    said = "error: No solution found when resolving dependencies:"
    assert tier_pass.worktree_env.unresolvable(said), "the wording the module recognises"
    seams = _seams(tmp_path, provision=lambda _tree: said)
    with pytest.raises(tier_pass.NoVerdict) as raised:
        tier_pass.run_at(_target(tmp_path), "", 60.0, seams)
    assert raised.value.own


def test_a_tree_that_would_not_go_is_said_and_does_not_lose_the_verdict(
    tmp_path, monkeypatch, capsys
):
    class Stuck(FakeRuns):
        def git(self, argv):
            if argv[3:5] == ["worktree", "remove"]:
                return subprocess.CompletedProcess(argv, 1, "", "")
            return super().git(argv)

    monkeypatch.setattr(
        tier_pass.disposable_tree.box_teardown,
        "remove_tree_longpath",
        lambda _path, set_aside=False: "a.pyd: in use",
    )
    result = tier_pass.run_at(_target(tmp_path), "", 60.0, _seams(tmp_path, Stuck()))
    assert result.status == results.PASSED
    assert "could not remove" in capsys.readouterr().out


def test_judge_tests_the_tip_as_origin_has_it_now_not_as_it_was_listed(tmp_path):
    runs = FakeRuns()
    seams = _seams(tmp_path, runs, git_for=lambda _p: fake_git(tip="c" * 16))
    result = tier_pass.judge(_target(tmp_path), 60.0, seams)
    assert result is not None
    assert result.sha == "c" * 16


def test_judge_keeps_the_listed_tip_when_origin_cannot_be_read_again(tmp_path):
    seams = _seams(tmp_path, git_for=lambda _p: fake_git(fetch=1))
    result = tier_pass.judge(_target(tmp_path), 60.0, seams)
    assert result is not None
    assert result.sha == TIP


def test_a_claim_lost_runs_nothing_and_records_nothing(tmp_path, capsys):
    runs = FakeRuns()
    asked = []

    def claim(checkout, job):
        asked.append((checkout, job))
        return False

    seams = _seams(tmp_path, runs, claim=claim)
    assert tier_pass.judge(_target(tmp_path), 60.0, seams) is None
    assert runs.suites == []
    assert results.read("carameli", seams.root, TIER) is None
    assert asked == [(tmp_path / "carameli", f"tier-carameli-{TIP[:12]}")]
    assert "claimed by another machine" in capsys.readouterr().out


def test_a_project_with_nothing_to_run_is_recorded_red_as_a_missing_tier(tmp_path):
    """Like `scripts/e2e.py`: a skip reads as green, and it would never end."""
    runs = FakeRuns()
    seams = _seams(tmp_path, runs, git_for=lambda _p: fake_git(contract=False))
    result = tier_pass.judge(_target(tmp_path), 60.0, seams)
    assert result is not None
    assert (result.status, result.signature) == (results.FAILED, (TIER.missing,))
    assert result.sha == TIP
    assert runs.suites == []
    assert results.read("carameli", seams.root, TIER) == result


def test_a_missing_tier_is_claimed_so_one_pc_sends_one_fixer(tmp_path):
    seams = _seams(
        tmp_path, git_for=lambda _p: fake_git(contract=False), claim=lambda _c, _j: False
    )
    assert tier_pass.judge(_target(tmp_path), 60.0, seams) is None
    assert results.read("carameli", seams.root, TIER) is None


def test_an_override_stands_in_for_the_script(tmp_path, monkeypatch):
    monkeypatch.setattr(tier_pass.tier_command.shutil, "which", lambda name: name)
    runs = FakeRuns()
    toml = '[delivery]\nmedium_tier = "make test"\n'
    seams = _seams(tmp_path, runs, git_for=lambda _p: fake_git(toml=toml, contract=False))
    result = tier_pass.judge(_target(tmp_path), 60.0, seams)
    assert result is not None
    assert result.status == results.PASSED
    assert runs.suites[0][0] == ["make", "test"]


def test_a_project_with_a_database_is_not_applicable_never_red(tmp_path):
    """photolab's conftest raises on an unset `DATABASE_URL`, productivity-tracker's
    calls `pytest.fail`, and a disposable tree must never get a real `.env`: one
    project's settings reach `DROP DATABASE`."""
    runs = FakeRuns()
    claims = []
    seams = _seams(
        tmp_path,
        runs,
        git_for=lambda _p: fake_git(toml="[db]\nenabled = true\n"),
        claim=lambda *a: claims.append(a) or True,
    )
    result = tier_pass.judge(_target(tmp_path), 60.0, seams)
    assert result is not None
    assert (result.status, result.sha, result.signature) == (results.SKIPPED, TIP, ())
    assert result.reason.startswith("not applicable: ")
    assert runs.suites == []
    assert claims == [], "nothing for two machines to agree on"
    assert results.to_failure(result, TIP, seams.root, TIER) is None
    assert results.read("carameli", seams.root, TIER) == result


def test_a_runner_that_drives_the_docker_stack_is_not_applicable(tmp_path):
    script = '_ARGV = [\n    "docker",\n    "compose",\n    "exec",\n]\n"--all"\n'
    runs = FakeRuns()
    seams = _seams(tmp_path, runs, git_for=lambda _p: fake_git(script=script))
    result = tier_pass.judge(_target(tmp_path), 60.0, seams)
    assert result is not None
    assert result.status == results.SKIPPED
    assert "Docker" in result.reason
    assert runs.suites == []


def test_an_override_opts_a_project_with_a_database_back_in(tmp_path, monkeypatch):
    monkeypatch.setattr(tier_pass.tier_command.shutil, "which", lambda name: name)
    runs = FakeRuns()
    toml = '[db]\nenabled = true\n[delivery]\nmedium_tier = "make unit"\n'
    seams = _seams(tmp_path, runs, git_for=lambda _p: fake_git(toml=toml))
    result = tier_pass.judge(_target(tmp_path), 60.0, seams)
    assert result is not None
    assert result.status == results.PASSED
    assert runs.suites[0][0] == ["make", "unit"]


def test_not_applicable_writes_a_skip_at_the_tip(tmp_path):
    seams = _seams(tmp_path, now=lambda: NOW)
    result = tier_pass.not_applicable(_target(tmp_path), "no database here", seams)
    assert result.reason == "not applicable: no database here"
    assert results.line_for(result) == "carameli: skipped -- not applicable: no database here"
    assert results.read("carameli", seams.root, TIER) == result


def test_a_run_that_will_not_fit_is_refused_before_anyone_claims_it(tmp_path):
    claims = []
    seams = _seams(tmp_path, room=lambda: 100, claim=lambda *a: claims.append(a) or True)
    with pytest.raises(tier_pass.NoRoom, match="100 MB free"):
        tier_pass.judge(_target(tmp_path), 60.0, seams)
    assert claims == []


def test_not_applicable_is_not_asked_again_until_the_tip_moves(tmp_path):
    workspace = _workspace(tmp_path, ["a"])
    seams = _seams(tmp_path, git_for=lambda _p: fake_git(toml="[db]\nenabled = true\n"))
    assert tier_pass.run_pass(workspace, [], _args(), seams) == 0
    assert tier_pass.unjudged(workspace, ["a"], seams) == []


def test_not_applicable_is_decided_before_memory_so_a_short_pc_can_say_it(tmp_path):
    workspace = _workspace(tmp_path, ["a"])
    seams = _seams(
        tmp_path, git_for=lambda _p: fake_git(toml="[db]\nenabled = true\n"), room=lambda: 100
    )
    assert tier_pass.run_pass(workspace, [], _args(), seams) == 0
    last = results.read("a", seams.root, TIER)
    assert last is not None
    assert last.status == results.SKIPPED
    assert state.read("a", seams.root).memory_skips == 0


# --- the pass -----------------------------------------------------------------------------


def test_the_registered_projects_are_the_workspace_file_s(tmp_path):
    assert tier_pass.registered(_workspace(tmp_path, ["a", "b"])) == ["a", "b"]


def test_origin_is_fetched_before_its_tip_is_read(monkeypatch):
    monkeypatch.setattr(tier_pass.tb, "detect_default_branch", lambda _g, fallback: "trunk")
    assert tier_pass.origin_at(fake_git()) == ("trunk", TIP)
    assert tier_pass.origin_at(fake_git(fetch=1)) == ("", "")


def test_nothing_to_judge_is_a_quiet_pass(tmp_path, capsys):
    seams = _seams(tmp_path)
    assert tier_pass.judge_all([], 60.0, NOW, seams) == 0
    assert capsys.readouterr().out == ""


def test_only_what_origin_has_moved_past_is_listed_never_judged_then_longest_waiting(tmp_path):
    workspace = _workspace(tmp_path, ["atlas", "busy", "quiet", "same"])
    seams = _seams(tmp_path)
    _stored(seams.root, "busy", finished="2026-11-06T13:00:00")
    _stored(seams.root, "quiet", finished="2026-11-05T09:00:00")
    _stored(seams.root, "same", sha=TIP, finished="2026-11-04T09:00:00")
    listed = tier_pass.unjudged(workspace, ["atlas", "busy", "quiet", "same"], seams)
    assert [target.name for target in listed] == ["atlas", "quiet", "busy"]


def test_a_red_at_the_tip_is_judged_and_not_run_again_every_fire(tmp_path):
    workspace = _workspace(tmp_path, ["carameli"])
    seams = _seams(tmp_path, now=lambda: NOW)
    finished = (NOW - _dt.timedelta(minutes=30)).isoformat(timespec="seconds")
    _stored(
        seams.root,
        "carameli",
        sha=TIP,
        status=results.FAILED,
        signature=("t",),
        finished=finished,
    )
    assert tier_pass.unjudged(workspace, ["carameli"], seams) == []


def test_a_red_at_an_unmoved_tip_is_re_tested_once_after_the_delay(tmp_path):
    """A flake would otherwise stay red until someone merged, and a fixer who found
    nothing could not clear it."""
    workspace = _workspace(tmp_path, ["a"])
    runs = FakeRuns(code=1)
    seams = _seams(tmp_path, runs, now=lambda: NOW)
    finished = (NOW - results.RETEST_AFTER).isoformat(timespec="seconds")
    _stored(seams.root, "a", sha=TIP, status=results.FAILED, signature=("t",), finished=finished)
    assert tier_pass.run_pass(workspace, [], _args(), seams) == 0
    assert len(runs.suites) == 1
    last = results.read("a", seams.root, TIER)
    assert last is not None
    assert last.retested
    later = _seams(tmp_path, runs, now=lambda: NOW + _dt.timedelta(days=30))
    assert tier_pass.run_pass(workspace, [], _args(), later) == 0
    assert len(runs.suites) == 1, "once, and after that only when the tip moves"


def test_a_flake_that_passes_its_re_test_clears_the_red(tmp_path):
    workspace = _workspace(tmp_path, ["a"])
    seams = _seams(tmp_path, FakeRuns(), now=lambda: NOW)
    finished = (NOW - results.RETEST_AFTER).isoformat(timespec="seconds")
    _stored(seams.root, "a", sha=TIP, status=results.FAILED, signature=("t",), finished=finished)
    assert tier_pass.run_pass(workspace, [], _args(), seams) == 0
    last = results.read("a", seams.root, TIER)
    assert last is not None
    assert last.status == results.PASSED


def test_by_hand_a_project_always_runs(tmp_path):
    """A person named it: a judged tip, a backoff, a claim and low memory are none of
    them a reason to refuse."""
    workspace = _workspace(tmp_path, ["a"])
    runs = FakeRuns()
    seams = _seams(
        tmp_path,
        runs,
        now=lambda: NOW,
        room=lambda: 100,
        claim=lambda _c, _j: False,
    )
    _stored(seams.root, "a", sha=TIP, finished=NOW.isoformat(timespec="seconds"))
    state.write("a", state.could_not_judge(state.State(), NOW, "no network", False), seams.root)
    assert tier_pass.run_pass(workspace, ["a"], _args(), seams) == 0
    assert len(runs.suites) == 1
    assert tier_pass.run_pass(workspace, [], _args(), seams) == 0
    assert len(runs.suites) == 1, "the schedule still respects what it found"


def test_by_hand_a_project_that_cannot_run_is_still_not_run(tmp_path):
    """Forcing it would only record a false red and send a fixer at it."""
    workspace = _workspace(tmp_path, ["a"])
    runs = FakeRuns()
    seams = _seams(tmp_path, runs, git_for=lambda _p: fake_git(toml="[db]\nenabled = true\n"))
    assert tier_pass.run_pass(workspace, ["a"], _args(), seams) == 0
    assert runs.suites == []


def test_a_project_this_pc_does_not_hold_and_one_it_cannot_read_are_said(tmp_path, capsys):
    workspace = _workspace(tmp_path, ["here"])
    seams = _seams(tmp_path, git_for=lambda _p: fake_git(fetch=1))
    assert tier_pass.unjudged(workspace, ["here", "elsewhere"], seams) == []
    out = capsys.readouterr().out
    assert "elsewhere: no checkout on this machine" in out
    assert "here: skipped -- cannot read origin's default branch" in out


def test_one_pass_works_through_every_project_one_after_another(tmp_path, capsys):
    workspace = _workspace(tmp_path, ["a", "b"])
    runs = FakeRuns()
    seams = _seams(tmp_path, runs)
    assert tier_pass.run_pass(workspace, [], _args(), seams) == 0
    assert [Path(cwd).parts[-4] for _argv, cwd in runs.suites] == ["a", "b"]
    assert f"a: passed at {TIP[:9]}" in capsys.readouterr().out


def test_low_memory_runs_nothing_and_counts_each_skip_in_the_pass_s_record(tmp_path, capsys):
    workspace = _workspace(tmp_path, ["a", "b"])
    runs = FakeRuns()
    seams = _seams(tmp_path, runs, room=lambda: 1000)
    assert tier_pass.run_pass(workspace, [], _args(), seams) == 0
    assert runs.suites == []
    out = capsys.readouterr().out
    assert "a: skipped -- 1000 MB free" in out
    assert "b: skipped -- 1000 MB free" in out
    assert "skipped for memory 2" in out
    assert state.read("a", seams.root).memory_skips == 1


def test_a_day_unjudged_for_memory_is_said_and_files_nothing(tmp_path, capsys):
    """It is not a finding, because no session can fix it. The job's exit code stays 0,
    and `schedule_health` reading healthy is why the line has to be here instead."""
    workspace = _workspace(tmp_path, ["a"])
    day_ago = NOW - state.STARVED_AFTER
    seams = _seams(tmp_path, room=lambda: 1000, now=lambda: NOW)
    state.write("a", state.skipped_for_memory(state.State(), day_ago), seams.root)
    assert tier_pass.run_pass(workspace, [], _args(), seams) == 0
    out = capsys.readouterr().out
    assert "a: unjudged for 24 hours: short of memory" in out
    assert tier_pass.status(workspace, seams.root, lambda: NOW) == 0
    assert "a: unjudged for 24 hours: short of memory" in capsys.readouterr().out


def test_a_verdict_ends_the_wait_for_memory(tmp_path):
    workspace = _workspace(tmp_path, ["a"])
    seams = _seams(tmp_path, now=lambda: NOW)
    state.write("a", state.skipped_for_memory(state.State(), NOW), seams.root)
    assert tier_pass.run_pass(workspace, [], _args(), seams) == 0
    assert state.read("a", seams.root) == state.State()


def test_memory_is_asked_again_before_each_run(tmp_path):
    workspace = _workspace(tmp_path, ["a", "b"])
    runs = FakeRuns()
    answers = iter([64000, 1000])
    seams = _seams(tmp_path, runs, room=lambda: next(answers))
    assert tier_pass.run_pass(workspace, [], _args(), seams) == 0
    assert len(runs.suites) == 1


def test_a_pass_out_of_time_leaves_the_rest_for_the_next_fire(tmp_path, capsys):
    workspace = _workspace(tmp_path, ["a", "b"])
    runs = FakeRuns()
    seams = _seams(tmp_path, runs, now=Clock(step=60.0))
    assert tier_pass.run_pass(workspace, [], _args(pass_minutes=1.0), seams) == 0
    assert runs.suites == []
    assert "out of time; next fire picks up: a, b" in capsys.readouterr().out


class OneRefused(FakeRuns):
    """A tree for project `a` cannot be cut; everything else works."""

    def git(self, argv):
        if argv[3:5] == ["worktree", "add"] and Path(argv[2]).name == "a":
            return subprocess.CompletedProcess(argv, 128, "", "fatal: no")
        return super().git(argv)


def test_a_run_that_could_not_start_files_nothing_and_does_not_stop_the_next_project(
    tmp_path, capsys
):
    """A network blip is no reason to bill the devkit session: `log-wrap.py` files any
    non-zero exit, and every ledger row goes there."""
    workspace = _workspace(tmp_path, ["a", "b"])
    runs = OneRefused()
    seams = _seams(tmp_path, runs, now=lambda: NOW)
    assert tier_pass.run_pass(workspace, [], _args(), seams) == 0
    assert len(runs.suites) == 1
    said = capsys.readouterr()
    assert "a: could not judge -- cannot cut a tree" in said.out
    assert said.err == ""
    assert results.read("a", seams.root, TIER) is None


def test_a_project_that_could_not_be_judged_waits_out_its_backoff(tmp_path):
    """It retried every fire at up to 25 minutes each."""
    workspace = _workspace(tmp_path, ["a"])
    runs = OneRefused()
    seams = _seams(tmp_path, runs, now=lambda: NOW)
    tier_pass.run_pass(workspace, [], _args(), seams)
    assert tier_pass.unjudged(workspace, ["a"], seams) == []
    later = _seams(tmp_path, runs, now=lambda: NOW + _dt.timedelta(hours=1))
    assert [target.name for target in tier_pass.unjudged(workspace, ["a"], later)] == ["a"]


def test_a_day_of_could_not_judge_is_filed_once(tmp_path, capsys):
    workspace = _workspace(tmp_path, ["a"])
    runs = OneRefused()
    start = NOW - state.FILE_AFTER
    seams = _seams(tmp_path, runs, now=lambda: NOW)
    state.write("a", state.could_not_judge(state.State(), start, "fatal: no", False), seams.root)
    assert tier_pass.run_pass(workspace, [], _args(), seams) == 1
    assert capsys.readouterr().err.strip() == tier_pass.BROKE
    again = _seams(tmp_path, runs, now=lambda: NOW + _dt.timedelta(days=2))
    assert tier_pass.run_pass(workspace, [], _args(), again) == 0


def test_a_project_s_own_broken_lock_is_never_filed_on_devkit(tmp_path):
    workspace = _workspace(tmp_path, ["a"])
    said = "error: No solution found when resolving dependencies:"
    start = NOW - state.FILE_AFTER
    seams = _seams(tmp_path, now=lambda: NOW, provision=lambda _tree: said)
    state.write("a", state.could_not_judge(state.State(), start, said, True), seams.root)
    assert tier_pass.run_pass(workspace, [], _args(), seams) == 0
    assert state.read("a", seams.root).own


def test_a_verdict_clears_the_backoff(tmp_path):
    workspace = _workspace(tmp_path, ["a"])
    seams = _seams(tmp_path, now=lambda: NOW + _dt.timedelta(hours=1))
    state.write("a", state.could_not_judge(state.State(), NOW, "no", False), seams.root)
    assert tier_pass.run_pass(workspace, [], _args(), seams) == 0
    assert state.read("a", seams.root) == state.State()


def test_the_tally_is_one_line_a_person_can_read():
    tally = tier_pass.Tally(judged=2, for_memory=1)
    assert tally.line() == (
        "tier-pass: judged 2, claimed elsewhere 0, not applicable 0, could not judge 0, "
        "skipped for memory 1"
    )


def test_a_project_in_its_backoff_says_so_and_waits(tmp_path, capsys):
    seams = _seams(tmp_path, now=lambda: NOW)
    state.write("a", state.could_not_judge(state.State(), NOW, "no network", False), seams.root)
    assert tier_pass.waiting("a", None, TIP, seams)
    assert "a: could not judge since" in capsys.readouterr().out
    assert not tier_pass.waiting("b", None, TIP, seams), "nothing recorded, nothing to wait for"


def test_the_pass_s_record_counts_what_became_of_each_project(tmp_path, capsys):
    workspace = _workspace(tmp_path, ["a", "b"])
    assert tier_pass.run_pass(workspace, [], _args(), _seams(tmp_path, OneRefused())) == 0
    assert (
        "tier-pass: judged 1, claimed elsewhere 0, not applicable 0, could not judge 1, "
        "skipped for memory 0"
    ) in capsys.readouterr().out


def test_a_red_is_a_result_and_the_pass_exits_zero(tmp_path):
    workspace = _workspace(tmp_path, ["a"])
    seams = _seams(tmp_path, FakeRuns(code=1))
    assert tier_pass.run_pass(workspace, [], _args(), seams) == 0
    last = results.read("a", seams.root, TIER)
    assert last is not None
    assert last.status == results.FAILED


@pytest.mark.parametrize(("mode", "named"), [("off", "off"), ("plan", "plan"), ("", "off")])
def test_a_machine_that_cannot_dispatch_runs_nothing_and_says_why_once(
    tmp_path, capsys, mode, named
):
    """A result stays on the PC that made it, so a red here would reach no fixer. Exit 0,
    since a machine switched off is working as configured, and `log-wrap.py` files any
    other code. Nothing past the switch is reached: not git, not the lock."""

    def no_git(_path):
        raise AssertionError("a machine that cannot dispatch fetched")

    workspace = _workspace(tmp_path, ["a"], mode)
    seams = _seams(tmp_path, git_for=no_git)
    assert tier_pass.run_pass(workspace, [], _args(), seams) == 0
    said = capsys.readouterr()
    assert said.out == (
        f"tier-pass: devkit.fixPass is {named} here, so a red would reach no fixer; not run\n"
    )
    assert said.err == ""
    assert not (seams.root / TIER.results_dir).exists()


def test_a_machine_that_dispatches_runs_the_pass(tmp_path):
    workspace = _workspace(tmp_path, ["a"], "dispatch")
    runs = FakeRuns()
    seams = _seams(tmp_path, runs)
    assert tier_pass.run_pass(workspace, [], _args(), seams) == 0
    assert len(runs.suites) == 1
    assert results.read("a", seams.root, TIER) is not None


def test_a_second_pass_on_this_machine_runs_nothing(tmp_path, capsys):
    workspace = _workspace(tmp_path, ["a"])
    runs = FakeRuns()
    seams = _seams(tmp_path, runs)
    lock = seams.root / TIER.results_dir / tier_pass.LOCK_FILE
    with tier_pass.machine_lock(lock):
        assert tier_pass.run_pass(workspace, [], _args(), seams) == 0
    assert runs.suites == []
    assert "another pass holds this machine" in capsys.readouterr().out


def test_an_unregistered_project_is_refused_before_anything_runs(tmp_path, capsys):
    workspace = _workspace(tmp_path, ["a"])
    assert tier_pass.run_pass(workspace, ["typo"], _args(), _seams(tmp_path)) == 2
    assert "not registered in the workspace: typo" in capsys.readouterr().err


def test_status_lists_each_last_result(tmp_path, capsys):
    workspace = _workspace(tmp_path, ["a", "b"])
    _stored(tmp_path, "a", finished="2026-11-06T13:00:00")
    assert tier_pass.status(workspace, tmp_path) == 0
    assert capsys.readouterr().out == "a: passed at aaaaaaaaa (2026-11-06T13:00:00)\n"


def test_the_cli_takes_the_pass_s_bounds():
    args = tier_pass.build_parser().parse_args(["run", "a", "--timeout-minutes", "5"])
    assert (args.projects, args.timeout_minutes) == (["a"], 5.0)
    assert args.pass_minutes == tier_pass.DEFAULT_PASS_MINUTES


def test_main_refuses_a_machine_with_no_workspace_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(tier_pass.sweep, "default_workspace", lambda _r: tmp_path / "absent")
    assert tier_pass.main(["status"]) == 2
    assert "no workspace file" in capsys.readouterr().err


def test_main_dispatches_each_verb(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path, [])
    monkeypatch.setattr(tier_pass.sweep, "default_workspace", lambda _r: workspace)
    monkeypatch.setattr(tier_pass, "run_pass", lambda _w, only, _a: 7 if only == ["x"] else 8)
    assert tier_pass.main(["run", "x"]) == 7
    assert tier_pass.main(["status"]) == 0


# --- against real git ---------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def _project(tmp_path: Path, name: str, suite: str) -> tuple[Path, str]:
    """A checkout on a dirty `main` behind an origin one commit ahead of it."""
    origin = tmp_path / "origin" / f"{name}.git"
    origin.parent.mkdir(exist_ok=True)
    _git(tmp_path, "init", "--quiet", "--bare", "--initial-branch=main", str(origin))
    no_hooks = str(tmp_path / "no-hooks")  # the machine's global git policy refuses main
    checkout = tmp_path / name
    _git(tmp_path, "clone", "--quiet", str(origin), str(checkout))
    _git(checkout, "config", "core.hooksPath", no_hooks)
    _git(checkout, "checkout", "--quiet", "-B", "main")
    (checkout / "notes.txt").write_text("mine\n", encoding="utf-8")
    _git(checkout, "add", "notes.txt")
    _git(checkout, "commit", "--quiet", "-m", "first")
    _git(checkout, "push", "--quiet", "origin", "main")
    other = tmp_path / f"{name}-other"
    _git(tmp_path, "clone", "--quiet", str(origin), str(other))
    _git(other, "config", "core.hooksPath", no_hooks)
    (other / "scripts").mkdir()
    (other / "scripts" / "run-tests.py").write_text(suite, encoding="utf-8")
    _git(other, "add", "scripts/run-tests.py")
    _git(other, "commit", "--quiet", "-m", "second")
    _git(other, "push", "--quiet", "origin", "main")
    (checkout / "notes.txt").write_text("a person is working here\n", encoding="utf-8")
    return checkout, _git(other, "rev-parse", "HEAD")


def test_a_real_run_at_origin_s_tip_leaves_the_static_checkout_as_it_was(tmp_path):
    suite = 'import sys\nprint("FAILED tests/test_a.py::test_x - no")\nsys.exit(1)\n'
    checkout, tip = _project(tmp_path, "carameli", suite)
    workspace = tmp_path / "w.code-workspace"
    workspace.write_text(
        '{"folders": [{"path": "carameli"}], "settings": {"devkit.fixPass": "dispatch"}}',
        encoding="utf-8",
    )
    head_before = _git(checkout, "rev-parse", "HEAD")
    seams = tier_pass.Seams(
        root=tmp_path / "devkit",
        room=lambda: None,
        claim=lambda _c, _j: True,
        provision=lambda _tree: "",
    )
    assert tier_pass.run_pass(workspace, [], _args(), seams) == 0
    result = results.read("carameli", seams.root, TIER)
    assert result is not None
    assert (result.status, result.sha) == (results.FAILED, tip)
    assert result.signature == ("tests/test_a.py::test_x",)
    assert _git(checkout, "rev-parse", "HEAD") == head_before
    assert (checkout / "notes.txt").read_text(encoding="utf-8") == "a person is working here\n"
    assert not list((checkout / ".claude" / "worktrees").glob("disposable-*"))
    assert "disposable-" not in _git(checkout, "worktree", "list")
