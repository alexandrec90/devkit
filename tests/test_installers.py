"""`installers.py`: the pass that keeps every installer's work current on a machine.

What is asserted here is the *decisions*: which files count as installers, how an
installer's exit code is read, when `--yes` runs and with what, and what the artifact
and exit code say afterwards. Nothing here calls a real installer or `schtasks`; the
installers' own suites cover what each one registers.
"""

from __future__ import annotations

import datetime as _dt
import subprocess
from pathlib import Path

import pytest
from support import REPO_ROOT, load_script

installers = load_script("scripts/installers.py")

WHEN = _dt.datetime(2026, 9, 8, 8, 45)


def a_checkout(root: Path, *names: str) -> Path:
    """A directory shaped like a devkit checkout holding installers called `names`."""
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    for name in names:
        stem = name.removeprefix("install-").removesuffix(".py")
        (root / "scripts" / name).write_text(f'TASK_NAME = "devkit-{stem}"\n', encoding="utf-8")
    return root


class Answers:
    """A runner that answers each installer's `--check` and `--yes` from a table and
    records every argv it was handed."""

    def __init__(self, checks: dict[str, int], yeses: dict[str, int] | None = None):
        self.checks = checks
        self.yeses = yeses or {}
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        script = Path(argv[1]).name
        mode = argv[2]
        code = self.checks[script] if mode == "--check" else self.yeses.get(script, 0)
        return subprocess.CompletedProcess(list(argv), code, f"{script} said {mode}\n", "")

    def modes_for(self, script: str) -> list[str]:
        return [argv[2] for argv in self.calls if Path(argv[1]).name == script]


# --- discovery -------------------------------------------------------------------


def test_installers_are_found_by_name_and_reported_in_order(tmp_path):
    root = a_checkout(tmp_path, "install-zed.py", "install-alpha.py")
    (root / "scripts" / "installed.py").write_text("", encoding="utf-8")
    (root / "scripts" / "install-notes.md").write_text("", encoding="utf-8")
    assert [p.name for p in installers.discover(root)] == ["install-alpha.py", "install-zed.py"]


def test_every_real_installer_is_discovered():
    """The property `tests/test_installer_contract.py` builds on: a new `install-*.py`
    is in scope the moment it exists, with no list to forget it in."""
    found = {p.name for p in installers.discover(REPO_ROOT)}
    assert found == {p.name for p in (REPO_ROOT / "scripts").glob("install-*.py")}
    assert "install-installers-schedule.py" in found


def test_the_task_name_is_read_off_the_source_without_importing_it(tmp_path):
    root = a_checkout(tmp_path, "install-tray.py")
    assert installers.task_name(root / "scripts" / "install-tray.py") == "devkit-tray"


@pytest.mark.parametrize("text", ["", "TASK_NAME = other\n", "def f(:\n"])
def test_an_installer_with_no_task_name_reports_none(tmp_path, text):
    """`install-git-policy.py` registers no job; an unparsable file is not a reason for
    the pass to stop, since this only feeds a report line about the ledger."""
    script = tmp_path / "install-x.py"
    script.write_text(text, encoding="utf-8")
    assert installers.task_name(script) == ""
    assert installers.task_name(tmp_path / "absent.py") == ""


# --- the options an installer should keep ----------------------------------------


def test_options_are_read_per_installer_from_the_workspace_settings():
    text = (
        '{"folders": [], "settings": {"devkit.installers": '
        '{"install-reconcile-task.py": ["--merge"], "install-tray.py": []}}}'
    )
    assert installers.parse_options(text) == {
        "install-reconcile-task.py": ["--merge"],
        "install-tray.py": [],
    }


def test_options_survive_the_comments_a_workspace_file_carries():
    text = '{\n  // why\n  "settings": {"devkit.installers": {"install-x.py": ["--a", "b"]}},\n}'
    assert installers.parse_options(text) == {"install-x.py": ["--a", "b"]}


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        '{"settings": {}}',
        '{"settings": {"devkit.installers": ["--merge"]}}',
        '{"settings": {"devkit.installers": {"install-x.py": "--merge"}}}',
        '{"settings": {"devkit.installers": {"install-x.py": ["--merge", 3]}}}',
    ],
    ids=["empty", "not-json", "no-setting", "not-a-map", "not-a-list", "not-strings"],
)
def test_anything_but_a_map_of_string_lists_yields_no_options(text):
    """A scheduled task whose stdout goes nowhere must not crash on a hand-edited file,
    and must not hand an installer an argument that is not a string either."""
    assert installers.parse_options(text) == {}


def test_a_machine_with_no_workspace_file_has_no_options(tmp_path):
    assert installers.read_options(None) == {}
    assert installers.read_options(tmp_path / "absent.code-workspace") == {}
    present = tmp_path / "w.code-workspace"
    present.write_text('{"settings": {"devkit.installers": {"a.py": ["-x"]}}}', encoding="utf-8")
    assert installers.read_options(present) == {"a.py": ["-x"]}


def test_the_options_follow_the_mode_on_both_spellings():
    """On `--check` as well as `--yes`: an option passed to the repair alone would be
    re-applied every pass by a check that never expected it."""
    script = Path("x/install-reconcile-task.py")
    assert installers.installer_argv("py", script, "--check", ["--merge"]) == [
        "py",
        str(script),
        "--check",
        "--merge",
    ]
    assert installers.installer_argv("py", script, "--yes", []) == ["py", str(script), "--yes"]


# --- reading one installer -------------------------------------------------------


def test_the_installer_s_last_word_is_what_the_report_carries():
    done = subprocess.CompletedProcess([], 1, "plan\nmore plan\n", "first\nthe cause\n")
    assert installers.last_line(done) == "the cause"
    assert installers.last_line(subprocess.CompletedProcess([], 1, "only out\n", "")) == "only out"
    assert installers.last_line(subprocess.CompletedProcess([], 7, "", "")) == "exit 7"


@pytest.mark.parametrize(
    ("code", "verdict"),
    [(0, installers.CURRENT), (1, installers.STALE), (2, installers.LEFT_ALONE)],
)
def test_the_three_contract_codes_are_read_as_verdicts(code, verdict):
    runner = Answers({"install-x.py": code})
    outcome = installers.check(Path("s/install-x.py"), "py", [], runner)
    assert outcome.verdict == verdict
    assert outcome.installer == "install-x.py"


def test_any_other_code_is_the_installer_failing_not_a_verdict():
    """A traceback exits neither 0, 1 nor 2. Reading it as "stale" would have `maintain`
    run `--yes` on a broken installer."""
    runner = Answers({"install-x.py": 3})
    outcome = installers.check(Path("s/install-x.py"), "py", [], runner)
    assert outcome.verdict == installers.FAILED
    assert "exited 3" in outcome.detail


def test_a_rejected_command_line_is_a_failure_not_a_job_left_alone():
    """`argparse` exits 2, which is the contract's "left alone". Seen live: the pass ran
    against installers that did not know `--check` yet and reported each as a verdict.
    An option in `devkit.installers` the installer does not take must not hide there."""
    usage = (
        "usage: install-x.py [-h] [--yes]\ninstall-x.py: error: unrecognized arguments: --merge\n"
    )

    def runner(argv):
        return subprocess.CompletedProcess(list(argv), 2, "", usage)

    outcome = installers.check(Path("s/install-x.py"), "py", ["--merge"], runner)
    assert outcome.verdict == installers.FAILED
    assert "unrecognized arguments: --merge" in outcome.detail
    assert installers.usage_error(subprocess.CompletedProcess([], 2, "", usage))
    assert not installers.usage_error(subprocess.CompletedProcess([], 2, "", "not installed here"))
    assert not installers.usage_error(subprocess.CompletedProcess([], 1, "", usage))


def test_a_repair_reports_what_the_installer_said_or_that_it_refused():
    ok = installers.repair(Path("s/install-x.py"), "py", [], Answers({}, {"install-x.py": 0}))
    assert ok.verdict == installers.REINSTALLED
    refused = installers.repair(Path("s/install-x.py"), "py", [], Answers({}, {"install-x.py": 2}))
    assert refused.verdict == installers.FAILED and "exited 2" in refused.detail


# --- the pass --------------------------------------------------------------------


def test_status_asks_every_installer_and_repairs_none(tmp_path):
    root = a_checkout(tmp_path, "install-a.py", "install-b.py")
    runner = Answers({"install-a.py": 0, "install-b.py": 1})
    outcomes = installers.reconcile(root, False, {}, runner, python="py")
    assert [o.verdict for o in outcomes] == [installers.CURRENT, installers.STALE]
    assert all(argv[2] == "--check" for argv in runner.calls)


def test_maintain_repairs_exactly_the_stale_ones(tmp_path):
    root = a_checkout(tmp_path, "install-a.py", "install-b.py", "install-c.py")
    runner = Answers({"install-a.py": 0, "install-b.py": 1, "install-c.py": 2})
    outcomes = installers.reconcile(root, True, {}, runner, python="py")
    assert [o.verdict for o in outcomes] == [
        installers.CURRENT,
        installers.REINSTALLED,
        installers.LEFT_ALONE,
    ]
    assert runner.modes_for("install-b.py") == ["--check", "--yes"]
    assert runner.modes_for("install-a.py") == ["--check"]
    assert runner.modes_for("install-c.py") == ["--check"]


def test_an_installer_s_options_reach_both_its_check_and_its_repair(tmp_path):
    root = a_checkout(tmp_path, "install-a.py", "install-b.py")
    runner = Answers({"install-a.py": 1, "install-b.py": 1})
    installers.reconcile(root, True, {"install-a.py": ["--merge"]}, runner, python="py")
    a_calls = [argv for argv in runner.calls if argv[1].endswith("install-a.py")]
    assert all(argv[3:] == ["--merge"] for argv in a_calls) and len(a_calls) == 2
    assert all(argv[3:] == [] for argv in runner.calls if argv[1].endswith("install-b.py"))


def test_the_interpreter_is_the_console_one_beside_this_process(tmp_path, monkeypatch):
    """Under the scheduled `pythonw.exe` a console-less child gets a visible console for
    every `schtasks` it runs; `sweep.console_python` is the spelling that does not."""
    root = a_checkout(tmp_path, "install-a.py")
    monkeypatch.setattr(installers.sweep, "console_python", lambda: r"C:\py\python.exe")
    runner = Answers({"install-a.py": 0})
    installers.reconcile(root, False, {}, runner)
    assert runner.calls[0][0] == r"C:\py\python.exe"


def test_a_ledger_name_no_installer_registers_is_reported(tmp_path):
    root = a_checkout(tmp_path, "install-tray.py")
    names = frozenset({"devkit-tray", "devkit-retired-job"})
    assert installers.stood_down_without_installer(names, root) == ["devkit-retired-job"]


# --- the artifact and the exit code ----------------------------------------------


def outcomes(*verdicts: str) -> list:
    return [installers.Outcome(f"install-{i}.py", v, f"detail {i}") for i, v in enumerate(verdicts)]


def test_the_report_counts_what_needs_attention_and_names_each_installer():
    text = installers.render(
        outcomes(installers.CURRENT, installers.STALE, installers.FAILED, installers.REINSTALLED),
        ["devkit-gone"],
        WHEN,
        "maintain",
    )
    head, *lines = text.splitlines()
    assert head.startswith("# installers 2026-09-08T08:45:00 [maintain] -- 2 need attention")
    assert lines[1] == "install-1.py: stale -- detail 1"
    assert any("devkit-gone" in line and "--on --job devkit-gone" in line for line in lines)
    assert text.endswith("\n")


@pytest.mark.parametrize(
    ("verdicts", "code"),
    [
        ((installers.CURRENT, installers.LEFT_ALONE, installers.REINSTALLED), 0),
        ((installers.CURRENT, installers.STALE), 1),
        ((installers.STALE, installers.FAILED), 2),
    ],
)
def test_the_exit_code_ranks_failure_over_pending_over_clean(verdicts, code):
    assert installers.exit_code(outcomes(*verdicts)) == code


def test_the_artifact_lands_under_the_checkout(tmp_path):
    installers.write_artifact("# x\n", tmp_path)
    assert (tmp_path / installers.ARTIFACT).read_text(encoding="utf-8") == "# x\n"


# --- the CLI ---------------------------------------------------------------------


def test_the_default_mode_is_read_only(monkeypatch):
    args = installers.parse_args([])
    assert args.mode == "status"


def test_main_reconciles_the_static_checkout_and_writes_its_artifact(tmp_path, monkeypatch, capsys):
    """Run from a `claude --worktree` checkout, the pass works on the static checkout it
    belongs to: that is the checkout the registered command lines name."""
    static = a_checkout(tmp_path / "devkit", "install-a.py", "install-b.py")
    worktree = static / ".claude" / "worktrees" / "lovely-lake"
    a_checkout(worktree, "install-only-here.py")
    runner = Answers({"install-a.py": 0, "install-b.py": 1})
    monkeypatch.setattr(installers, "run_command", runner)
    monkeypatch.setattr(installers.harness_state, "stood_down", lambda: frozenset())

    assert installers.main(["maintain", "--devkit", str(worktree)]) == 0
    assert runner.modes_for("install-b.py") == ["--check", "--yes"]
    assert runner.modes_for("install-only-here.py") == []
    assert (static / installers.ARTIFACT).is_file()
    assert "install-b.py: reinstalled" in capsys.readouterr().out


def test_main_in_status_mode_exits_one_on_a_pending_install(tmp_path, monkeypatch):
    root = a_checkout(tmp_path, "install-a.py")
    monkeypatch.setattr(installers, "run_command", Answers({"install-a.py": 1}))
    monkeypatch.setattr(installers.harness_state, "stood_down", lambda: frozenset())
    assert installers.main(["--devkit", str(root)]) == 1
    assert "install-a.py: stale" in (root / installers.ARTIFACT).read_text(encoding="utf-8")


def test_main_passes_the_workspace_file_s_options_through(tmp_path, monkeypatch):
    root = a_checkout(tmp_path, "install-a.py")
    workspace = tmp_path / "w.code-workspace"
    workspace.write_text(
        '{"settings": {"devkit.installers": {"install-a.py": ["--merge"]}}}', encoding="utf-8"
    )
    runner = Answers({"install-a.py": 0})
    monkeypatch.setattr(installers, "run_command", runner)
    monkeypatch.setattr(installers.harness_state, "stood_down", lambda: frozenset())
    assert installers.main(["--devkit", str(root), "--workspace", str(workspace)]) == 0
    assert runner.calls[0][2:] == ["--check", "--merge"]


def test_run_command_captures_and_reports_a_spawn_failure_as_a_code(tmp_path):
    """A missing interpreter is a returncode the pass reports, not a traceback that
    ends it with nine installers unchecked."""
    missing = installers.run_command([str(tmp_path / "no-such-python"), "x", "--check"])
    assert missing.returncode == 3 and missing.stderr
