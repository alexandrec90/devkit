"""`scripts/rehearse-upgrade.py`: the PR gate's upgrade rehearsal, runnable anywhere.

85c4fb7f: the gate job was an inline shell block, so #424 went green on every local
check and red on the one job nothing local could run -- and the block itself registered
its probe in the machine's real workspace. The steps are driven here with a fake runner;
the real run is the gate job, which now calls the script.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from support import load_script

rehearse = load_script("scripts/rehearse-upgrade.py")


def done(code: int = 0, out: str = "", err: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, out, err)


def a_plan(tmp_path: Path) -> list:
    return rehearse.plan(
        "py", tmp_path / "src", tmp_path / "prev", tmp_path / "up", tmp_path / "no-hooks"
    )


def test_a_step_names_its_devkit_dir_only_when_given_one(tmp_path):
    assert rehearse.Step("x", ("a",), tmp_path).devkit_dir == ""


def test_a_program_that_is_not_there_is_a_failed_step_not_a_traceback(tmp_path):
    missing = rehearse.run_command([str(tmp_path / "no-such-program")])
    assert missing.returncode == 127
    ran = rehearse.run_command([sys.executable, "-c", "print('ok')"])
    assert ran.returncode == 0 and ran.stdout.strip() == "ok"


def test_the_previous_release_is_the_newest_tag():
    """Newest, not reachable: on a release PR the newest tag is still the previous one."""
    assert rehearse.previous_tag(Path("."), lambda *_a, **_k: done(0, "v0.11.28\nv0.11.9\n")) == (
        "v0.11.28"
    )
    assert rehearse.previous_tag(Path("."), lambda *_a, **_k: done(0, "")) == ""
    assert rehearse.previous_tag(Path("."), lambda *_a, **_k: done(128, "v1\n")) == ""


def test_a_generator_is_asked_whether_it_can_leave_the_workspace_alone(tmp_path):
    old, new = tmp_path / "old.py", tmp_path / "new.py"
    old.write_text("print('registers')\n", encoding="utf-8")
    new.write_text("parser.add_argument('--no-register')\n", encoding="utf-8")
    assert rehearse.knows_no_register(new) is True
    assert rehearse.knows_no_register(old) is False
    assert rehearse.knows_no_register(tmp_path / "missing.py") is False


def test_the_plan_is_the_gate_job_step_for_step(tmp_path):
    steps = a_plan(tmp_path)
    render, pull, again = steps[0], steps[1], steps[2]
    assert render.argv[1] == str(tmp_path / "prev" / "scripts" / "new-project.py")
    assert "--no-register" in render.argv and render.cwd == tmp_path / "up"
    assert pull.argv == again.argv, "the second pull runs the pulled sync-devkit the same way"
    assert {"--allow-untagged", "--allow-dirty"} <= set(pull.argv)
    project = tmp_path / "up" / rehearse.PROJECT
    assert all(step.cwd == project for step in steps[1:])
    names = [step.name for step in steps]
    assert names[-4:] == ["vendored hook tests", "run-tests", "lint-all", "drift check"]


def test_the_adoption_commit_runs_no_hook_as_on_the_runner(tmp_path):
    """This machine's global branch policy refuses a commit on the probe's default
    branch; the runner has no hooks at all."""
    (commit,) = [step for step in a_plan(tmp_path) if step.name == "commit the adoption"]
    assert f"core.hooksPath={tmp_path / 'no-hooks'}" in commit.argv


def test_devkit_dir_reaches_only_the_drift_check(tmp_path):
    steps = a_plan(tmp_path)
    base = {"DEVKIT_DIR": "/elsewhere", "PATH": "p"}
    envs = {step.name: rehearse.step_env(step, base) for step in steps}
    assert envs["drift check"]["DEVKIT_DIR"] == str(tmp_path / "src")
    assert all("DEVKIT_DIR" not in env for name, env in envs.items() if name != "drift check")
    assert all(env["PATH"] == "p" for env in envs.values())


def test_the_first_failing_step_stops_the_run_and_is_reported(tmp_path):
    steps = a_plan(tmp_path)
    seen: list[list[str]] = []

    def runner(argv, **_kwargs):
        seen.append(argv)
        return done(1, "", "boom") if argv[-1] == "--check" or "run-tests" in argv[-1] else done()

    failed = rehearse.run_steps(steps, runner, {})
    assert failed is not None and failed[0].name == "run-tests"
    assert len(seen) == [step.name for step in steps].index("run-tests") + 1
    text = rehearse.report(failed, "v0.11.28")
    assert "FAILED at 'run-tests'" in text and "v0.11.28" in text and "boom" in text
    assert rehearse.run_steps(steps, lambda *_a, **_k: done(), {}) is None
    assert rehearse.report(None, "v0.11.28") == ""


def fake_git(previous_source: str, calls: list):
    """A runner whose `git worktree add` checks out a release whose generator is
    `previous_source`, and which passes every other step."""

    def runner(argv, **_kwargs):
        calls.append(argv)
        if argv[:3] == ["git", "worktree", "add"]:
            generator = Path(argv[4]) / "scripts" / "new-project.py"
            generator.parent.mkdir(parents=True)
            generator.write_text(previous_source, encoding="utf-8")
        return done()

    return runner


def test_a_release_that_would_register_its_probe_is_not_rendered(tmp_path):
    """The inline block registered `probe_upgrade` in the machine's real workspace; a
    release whose generator cannot be told not to is refused, and its checkout removed."""
    calls: list = []
    text = rehearse.rehearse("v0.1.0", tmp_path, fake_git("no flag here", calls), {})
    assert "--no-register" in text
    assert calls[-1][:3] == ["git", "worktree", "remove"]
    assert not any("new-project.py" in " ".join(argv) for argv in calls)


def test_a_clean_rehearsal_reports_nothing_and_removes_its_checkout(tmp_path):
    calls: list = []
    text = rehearse.rehearse("v0.1.0", tmp_path, fake_git("--no-register", calls), {})
    assert text == ""
    assert calls[-1][:3] == ["git", "worktree", "remove"]
    assert sum(argv[-1] == "--allow-dirty" for argv in calls) == 2


def test_a_checkout_that_fails_is_the_reported_step(tmp_path):
    text = rehearse.rehearse("v0.1.0", tmp_path, lambda *_a, **_k: done(128, "", "no tag"), {})
    assert "check out the previous release" in text and "no tag" in text


def test_main_writes_the_artifact_and_fails_without_a_tag(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(rehearse, "REPO_ROOT", tmp_path)
    assert rehearse.main([], lambda *_a, **_k: done(0, "")) == rehearse.EXIT_FAILED
    assert "no release tag" in (tmp_path / rehearse.ARTIFACT).read_text(encoding="utf-8")
    monkeypatch.setattr(rehearse, "rehearse", lambda *_a: "")
    assert rehearse.main([], lambda *_a, **_k: done(0, "v1.0.0\n")) == rehearse.EXIT_OK
    assert (tmp_path / rehearse.ARTIFACT).read_text(encoding="utf-8") == ""
    assert "adopts this tree cleanly" in capsys.readouterr().out
