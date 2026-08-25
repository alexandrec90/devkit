"""Tests for `scripts/hook-tests-live.py`, the paid live-CLI smoke runner.

The runner itself never launches a CLI in here — every test either exercises a pure
function or drives `main()` with `subprocess.run` patched out. A test suite that spends
money to check a test runner would be the exact failure the runner exists to make
visible.
"""

import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from support import load_script

live = load_script("scripts/hook-tests-live.py")
codex_live = load_script("tests/test_codex_hooks_live.py")


# --- suite selection ---------------------------------------------------------


def test_the_default_choice_is_a_single_suite():
    assert [suite.key for suite in live.resolve_suites("claude")] == ["claude"]


def test_both_runs_every_suite_in_a_fixed_order():
    """Ordered so a "both" run is reproducible and the cheap suite fails first."""
    assert [suite.key for suite in live.resolve_suites("both")] == list(live.ORDER)


def test_an_unknown_suite_names_the_real_ones():
    with pytest.raises(ValueError) as excinfo:
        live.resolve_suites("gemini")
    assert "gemini" in str(excinfo.value)
    assert "claude" in str(excinfo.value)


def test_every_suite_points_at_a_test_file_that_exists():
    """The scope is one checkout precisely because these paths are devkit-only. A
    renamed live test file would otherwise be found by the task, at a cost."""
    from support import REPO_ROOT

    for suite in live.SUITES.values():
        assert (REPO_ROOT / suite.path).is_file(), suite.path


def test_every_suite_marker_is_declared_in_pyproject():
    """`-m` with an unregistered marker deselects everything and exits 0 — the silent
    no-op this runner exists to catch, arriving through its own selector."""
    from support import REPO_ROOT

    config = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for suite in live.SUITES.values():
        assert f"{suite.marker}:" in config, suite.marker


# --- the pytest selector -----------------------------------------------------


def test_the_selector_always_ands_in_paid():
    """`addopts` carries `-m "not paid"`, so a selector without it runs nothing."""
    assert live.selector(live.resolve_suites("claude")).endswith("and paid")


def test_the_selector_ors_the_markers_for_both():
    expression = live.selector(live.resolve_suites("both"))
    assert "claude_live or codex_live" in expression
    assert expression.endswith("and paid")


# --- reading pytest's summary ------------------------------------------------


@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        ("1 passed in 42.10s", 1),
        ("1 failed, 1 passed in 61.00s", 2),
        ("2 errors in 3.00s", 2),
        # The three shapes of "green, but no CLI was ever launched".
        ("2 skipped in 0.41s", 0),
        ("3 deselected in 0.02s", 0),
        ("no tests ran in 0.01s", 0),
    ],
)
def test_the_summary_line_is_read_for_tests_that_actually_ran(summary, expected):
    assert live.ran_count(f"collecting ...\n=== {summary} ===") == expected


def test_a_passing_count_is_not_taken_from_a_traceback():
    """The summary is the last line, and an earlier mention must not outrank it."""
    output = "E   assert '9 passed' in text\n" + "\n" * 3 + "=== 1 failed in 5.0s ==="
    assert live.ran_count(output) == 1


def test_an_unreadable_run_is_none_rather_than_zero():
    """Distinguished on purpose: `main` reports the two differently, and collapsing
    them would report "everything was skipped" for a pytest that never started."""
    assert live.ran_count("Traceback (most recent call last):\n  ImportError") is None


# --- preflight ---------------------------------------------------------------


def test_a_checkout_without_the_live_tests_is_named(tmp_path):
    absent = live.missing_suites(live.resolve_suites("both"), tmp_path)
    assert absent == [suite.path for suite in live.resolve_suites("both")]


def test_a_checkout_with_the_live_tests_reports_nothing_missing():
    from support import REPO_ROOT

    assert live.missing_suites(live.resolve_suites("both"), REPO_ROOT) == []


def test_a_cli_that_is_not_installed_is_named():
    suites = live.resolve_suites("both")
    assert live.missing_binaries(suites, which=lambda _name: None) == ["claude", "codex"]


def test_an_installed_cli_is_not_reported_missing():
    suites = live.resolve_suites("claude")
    assert live.missing_binaries(suites, which=lambda name: f"/usr/bin/{name}") == []


def test_the_preflight_says_it_is_paid_and_where_the_cost_is_set():
    """Never restates a model name or a dollar figure — those live in the test files,
    and a second copy here is a number with no test to catch it drifting."""
    report = live.preflight_report(live.resolve_suites("claude"))
    assert "PAID" in report
    assert "tests/test_claude_hooks_live.py" in report
    assert "CLAUDE_LIVE_HOOK_BUDGET_USD" in report
    assert "haiku" not in report


# --- Codex configuration isolation ------------------------------------------


def test_codex_cost_settings_live_only_in_the_throwaway_home(tmp_path, monkeypatch):
    """The smoke may be cheap without changing the operator's normal sessions."""
    user_home = tmp_path / "user-codex"
    user_home.mkdir()
    user_config = user_home / "config.toml"
    original = 'model = "human-model"\nmodel_reasoning_effort = "high"\n'
    user_config.write_text(original, encoding="utf-8")
    (user_home / "auth.json").write_text('{"token":"test-only"}', encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()

    monkeypatch.setenv("CODEX_HOME", str(user_home))
    monkeypatch.setattr(codex_live, "LIVE_MODEL", "cheapest-test-model")
    monkeypatch.setattr(codex_live, "LIVE_REASONING_EFFORT", "low")

    env = codex_live._isolated_codex_env(project)

    isolated_home = Path(env["CODEX_HOME"])
    config = tomllib.loads((isolated_home / "config.toml").read_text(encoding="utf-8"))
    assert config["model"] == "cheapest-test-model"
    assert config["model_reasoning_effort"] == "low"
    assert config["model_reasoning_summary"] == "none"
    assert config["model_verbosity"] == "low"
    assert user_config.read_text(encoding="utf-8") == original


def test_codex_command_does_not_override_human_model_preferences(tmp_path, monkeypatch):
    calls = []

    def record(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(codex_live.subprocess, "run", record)
    isolated_env = {"CODEX_HOME": str(tmp_path / ".codex-home")}

    codex_live._run_codex("codex", tmp_path, isolated_env, "test prompt")

    command, kwargs = calls[0]
    assert "--model" not in command
    assert not any("model_reasoning_effort" in argument for argument in command)
    assert kwargs["env"] is isolated_env


# --- the artifact ------------------------------------------------------------


def test_the_artifact_names_the_command_that_reproduces_it(tmp_path):
    suites = live.resolve_suites("claude")
    live.write_artifact(tmp_path, "boom", suites)
    body = (tmp_path / live.ARTIFACT).read_text(encoding="utf-8")
    assert "tests/test_claude_hooks_live.py" in body
    assert "boom" in body
    # The one artifact in this repo whose remediation line costs money to follow.
    assert "spends money again" in body


def test_a_long_run_is_truncated_and_says_so():
    capped = live.cap("line\n" * 900, limit=10)
    assert capped.count("\n") == 10
    assert "900 lines total, truncated" in capped


def test_a_short_run_is_kept_whole():
    assert live.cap("one\ntwo") == "one\ntwo"


# --- main() ------------------------------------------------------------------


class _Result:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


@pytest.fixture
def never_spends(monkeypatch):
    """Every `main()` test goes through here, so no test in this file can launch a CLI
    by accident — including one added later that forgets to patch."""
    calls = []

    def _refuse(cmd, **kwargs):
        calls.append(cmd)
        return _Result(0, "=== 1 passed in 40.00s ===")

    monkeypatch.setattr(live.subprocess, "run", _refuse)
    monkeypatch.setattr(live.shutil, "which", lambda name: f"/usr/bin/{name}")
    return calls


def test_a_passing_run_clears_the_artifact(tmp_path, never_spends, capsys):
    (tmp_path / "tests").mkdir()
    for suite in live.SUITES.values():
        (tmp_path / suite.path).write_text("", encoding="utf-8")
    (tmp_path / live.ARTIFACT).parent.mkdir(parents=True)
    (tmp_path / live.ARTIFACT).write_text("stale failure", encoding="utf-8")

    assert live.main(["claude", "--root", str(tmp_path)]) == 0
    assert (tmp_path / live.ARTIFACT).read_text(encoding="utf-8") == ""
    assert "1 live test" in capsys.readouterr().out


def test_a_green_run_that_launched_nothing_fails(tmp_path, never_spends, monkeypatch, capsys):
    """The headline case. pytest exits 0 on an all-skipped run, and a task that trusts
    the exit code reports a paid smoke as passing without having spent a cent — or
    proved anything."""
    (tmp_path / "tests").mkdir()
    for suite in live.SUITES.values():
        (tmp_path / suite.path).write_text("", encoding="utf-8")
    monkeypatch.setattr(
        live.subprocess, "run", lambda cmd, **kw: _Result(0, "=== 2 skipped in 0.40s ===")
    )

    assert live.main(["claude", "--root", str(tmp_path)]) == 1
    assert "skipped or deselected" in capsys.readouterr().err
    assert "skipped" in (tmp_path / live.ARTIFACT).read_text(encoding="utf-8")


def test_a_missing_cli_is_refused_before_anything_is_launched(tmp_path, monkeypatch, capsys):
    (tmp_path / "tests").mkdir()
    for suite in live.SUITES.values():
        (tmp_path / suite.path).write_text("", encoding="utf-8")
    monkeypatch.setattr(live.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        live.subprocess, "run", lambda *a, **k: pytest.fail("launched a CLI anyway")
    )

    assert live.main(["claude", "--root", str(tmp_path)]) == 2
    assert "not on PATH" in capsys.readouterr().err


def test_a_checkout_without_the_tests_is_refused_loudly(tmp_path, never_spends, capsys):
    """Exit 2, not a clean skip: the action is scoped to devkit, so reaching here at all
    means the scope and the task disagree — which is worth reporting, not swallowing."""
    assert live.main(["both", "--root", str(tmp_path)]) == 2
    assert "not vendored" in capsys.readouterr().err
    assert never_spends == []


def test_the_default_suite_is_the_cheapest_one(tmp_path, never_spends):
    (tmp_path / "tests").mkdir()
    for suite in live.SUITES.values():
        (tmp_path / suite.path).write_text("", encoding="utf-8")

    live.main(["--root", str(tmp_path)])
    assert "tests/test_codex_hooks_live.py" not in never_spends[0]


def test_a_failing_run_writes_the_artifact(tmp_path, monkeypatch, capsys):
    (tmp_path / "tests").mkdir()
    for suite in live.SUITES.values():
        (tmp_path / suite.path).write_text("", encoding="utf-8")
    monkeypatch.setattr(live.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        live.subprocess,
        "run",
        lambda cmd, **kw: _Result(1, "CLAUDE_CAPPED_SHELL_OK missing\n=== 1 failed in 9.0s ==="),
    )

    assert live.main(["claude", "--root", str(tmp_path)]) == 1
    assert "CLAUDE_CAPPED_SHELL_OK" in (tmp_path / live.ARTIFACT).read_text(encoding="utf-8")
    assert live.ARTIFACT.as_posix() in capsys.readouterr().err


def test_the_runner_never_calls_pytest_without_the_paid_selector(tmp_path, never_spends):
    (tmp_path / "tests").mkdir()
    for suite in live.SUITES.values():
        (tmp_path / suite.path).write_text("", encoding="utf-8")

    live.main(["both", "--root", str(tmp_path)])
    command = never_spends[0]
    # The LAST `-m`: the first one is `python -m pytest`.
    marker = len(command) - 1 - command[::-1].index("-m")
    assert "and paid" in command[marker + 1]


# --- what a green run is evidence *about* -------------------------------------
#
# This task reported "passed, N live test(s)" and nothing else, while its default suite
# is `claude` -- the cheapest, and the one that cannot exercise the Claude-to-Codex
# translation at all. So the reading an operator took from a green task ("the ported
# hooks work") was the one claim the run had made no observation about.


def test_a_single_suite_run_names_the_one_it_skipped():
    line = live.pass_line(live.resolve_suites("claude"), 3)
    assert "codex" in line
    assert "NOT exercised" in line


def test_a_both_run_claims_no_gap():
    line = live.pass_line(live.resolve_suites("both"), 5)
    assert "NOT exercised" not in line
    assert "claude, codex" in line


def test_the_pass_line_still_reports_how_many_ran():
    """The count is the half that catches a green run which launched nothing."""
    for choice in ("claude", "both"):
        assert "7 live test(s)" in live.pass_line(live.resolve_suites(choice), 7)


def test_unexercised_is_the_complement_in_order():
    assert live.unexercised(live.resolve_suites("codex")) == ["claude"]
    assert live.unexercised(live.resolve_suites("both")) == []


def test_a_passing_run_prints_the_gap_rather_than_a_bare_pass(tmp_path, monkeypatch, capsys):
    """The reversion check: revert `pass_line` and this is the assertion that fails."""
    (tmp_path / "tests").mkdir()
    for suite in live.SUITES.values():
        (tmp_path / suite.path).write_text("", encoding="utf-8")
    monkeypatch.setattr(live.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        live.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="1 passed in 2s\n", stderr=""),
    )

    assert live.main(["claude", "--root", str(tmp_path)]) == 0
    assert "NOT exercised: codex" in capsys.readouterr().out


def test_subprocess_is_the_only_way_this_script_spends(tmp_path):
    """A guard on the guard: the fixture above only protects `subprocess.run`, so a
    future edit reaching for `os.system` or `subprocess.call` would slip past it."""
    from support import REPO_ROOT

    source = (REPO_ROOT / "scripts/hook-tests-live.py").read_text(encoding="utf-8")
    for spelling in ("os.system", "subprocess.call", "subprocess.Popen", "check_output"):
        assert spelling not in source, spelling
    assert source.count("subprocess.run") == 1
