"""`scripts/e2e.py`: devkit's end-to-end tier -- every test that needs this workstation."""

from __future__ import annotations

import subprocess
from pathlib import Path

import support
from support import load_script

import fix_plan

e2e = load_script("scripts/e2e.py")

JUNIT = """\
<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="4">
<testcase classname="tests.test_sweep" name="test_live" file="tests/test_sweep.py" line="3"/>
<testcase classname="tests.test_a" name="test_drift" file="tests/test_a.py" line="9">
  <failure message="AssertionError: differs">trace</failure>
</testcase>
<testcase classname="tests.test_b" name="test_box[x]" file="tests/test_b.py" line="1">
  <skipped type="pytest.skip" message="the live workspace is rendered from the static checkout&#10;more">s</skipped>
</testcase>
<testcase classname="tests.test_c.TestD" name="test_broke" line="1">
  <error message="fixture blew up">e</error>
</testcase>
</testsuite></testsuites>
"""


def test_the_report_is_read_into_ids_pytest_s_summary_would_print(tmp_path):
    report = tmp_path / "junit.xml"
    report.write_text(JUNIT, encoding="utf-8")
    assert e2e.cases(report) == [
        e2e.Case("tests/test_sweep.py::test_live", "passed"),
        e2e.Case("tests/test_a.py::test_drift", "failed", "AssertionError: differs"),
        e2e.Case(
            "tests/test_b.py::test_box[x]",
            "skipped",
            "the live workspace is rendered from the static checkout\nmore",
        ),
        e2e.Case("tests/test_c.py::TestD::test_broke", "failed", "fixture blew up"),
    ]


def test_an_unreadable_report_is_no_cases(tmp_path):
    assert e2e.cases(tmp_path / "missing.xml") == []
    (tmp_path / "bad.xml").write_text("<nope", encoding="utf-8")
    assert e2e.cases(tmp_path / "bad.xml") == []


PASS = e2e.Case("tests/a.py::t", "passed")
SKIP = e2e.Case("tests/b.py::u", "skipped", "no live workspace\nsecond line")


def test_under_the_scheduler_a_skip_fails_the_run_and_signs_it():
    """Nothing has a reason left to skip in the static checkout on its default branch,
    so a skip there is the machine drifting from what the tests assume."""
    code, lines = e2e.verdict(0, [PASS, SKIP], strict=True)
    assert code == 1
    assert lines[0] == "FAILED tests/b.py::u - skipped: no live workspace"
    assert fix_plan.signature_from_logs(lines) == ("tests/b.py::u",)


def test_by_hand_a_skip_is_listed_and_does_not_fail_the_run():
    """A fixer in a box reproduces what it can; the scheduled run re-tests the rest."""
    code, lines = e2e.verdict(0, [PASS, SKIP], strict=False)
    assert code == 0
    assert lines == ["SKIPPED tests/b.py::u - skipped: no live workspace"]


def test_a_run_that_executed_nothing_is_red_whoever_ran_it():
    for strict in (True, False):
        code, lines = e2e.verdict(0, [SKIP], strict=strict)
        assert code == 1
        assert lines[-1] == "e2e: no local test executed, which is not a pass"
    assert e2e.verdict(5, [], strict=False)[0] == 5


def test_pytests_own_red_stands():
    assert e2e.verdict(1, [PASS, e2e.Case("x::y", "failed")], strict=True) == (1, [])


def test_the_run_selects_the_local_tier_and_writes_a_report_naming_files():
    argv = e2e.pytest_argv("py", Path("logs/r.xml"))
    assert argv[:4] == ["py", "-m", "pytest", "tests"]
    assert argv[argv.index("-m", 2) + 1] == "local and not paid"
    assert f"--junitxml={Path('logs/r.xml')}" in argv


def test_named_tests_are_run_in_the_tiers_place_and_stay_in_the_tier():
    """A fixer sent at one red spent 2143 s on the whole tier and began a second run on
    the machine the night's jobs run on (42c3bba5, 50917bb2). The marker stays, so a name
    outside the tier deselects to nothing, which `verdict` reads as no pass."""
    named = "tests/test_x.py::test_y"
    argv = e2e.pytest_argv("py", Path("logs/r.xml"), [named])
    assert argv[:4] == ["py", "-m", "pytest", named] and "tests" not in argv
    assert argv[argv.index("-m", 2) + 1] == "local and not paid"


def test_main_hands_its_arguments_to_pytest(monkeypatch, tmp_path, capsys):
    seen = {}

    def run(argv, **_kwargs):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(e2e, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(e2e, "cases", lambda _report: [PASS])
    monkeypatch.setattr(e2e.sweep, "run_windowless", run)
    monkeypatch.delenv(e2e.ENV_FLAG, raising=False)
    assert e2e.main(["tests/test_x.py::test_y"]) == 0
    assert seen["argv"][3] == "tests/test_x.py::test_y"
    capsys.readouterr()


def test_only_the_scheduled_run_is_strict():
    assert e2e.scheduled({e2e.ENV_FLAG: "1"})
    assert not e2e.scheduled({})


def test_a_box_never_renders_the_workspace_file(monkeypatch, tmp_path):
    monkeypatch.setattr(e2e.sweep, "source_checkout", lambda _root: tmp_path)
    monkeypatch.setattr(
        e2e.devkit_project,
        "publish_workspace",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError),
    )
    assert (
        e2e.publish(tmp_path / "live.code-workspace")
        == "e2e: a box never renders the workspace file"
    )


def test_the_static_checkout_publishes_on_publish_workspaces_terms(monkeypatch, tmp_path):
    live = tmp_path / "live.code-workspace"
    live.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(e2e.sweep, "source_checkout", lambda root: root)
    monkeypatch.setattr(
        e2e.devkit_project, "publish_workspace", lambda path: ("refused", ["a edit"])
    )
    assert e2e.publish(live) == "e2e: workspace publish -- refused\n  a edit"
    assert e2e.publish(tmp_path / "gone") == f"e2e: no live workspace file at {tmp_path / 'gone'}"


def _decorated(decorator):
    def test_x():
        pass

    return decorator(test_x)


def test_every_test_skipped_for_wanting_this_machine_is_in_the_local_tier():
    """The decorator that skips a test in CI is the one that puts it here, so no test
    can be skipped for wanting this machine without also being run on it."""
    for decorator in (support.needs_live_workspace, support.needs_the_static_checkout):
        names = [mark.name for mark in _decorated(decorator).pytestmark]
        assert sorted(names) == ["local", "skipif"], decorator


def test_the_local_marker_is_registered():
    text = (support.REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert f'"{e2e.MARKER}: ' in text
