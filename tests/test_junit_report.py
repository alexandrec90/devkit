"""`scripts/junit_report.py`: a gate run's artifacts as the lines a signature reads."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fix_plan
import junit_report

REPORT = (
    '<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest">'
    '<testcase classname="scripts.hooks.tests.test_contract" name="test_ok" />'
    '<testcase classname="scripts.hooks.tests.test_contract" name="test_drop">'
    '<failure message="KeyError">boom</failure></testcase>'
    '<testcase classname="tests.test_unit.TestShape" name="test_p[a-b]">'
    '<error message="x">x</error></testcase>'
    "</testsuite></testsuites>"
)


def test_a_testcase_becomes_its_pytest_id():
    assert junit_report.test_id("scripts.hooks.tests.test_c", "test_x") == (
        "scripts/hooks/tests/test_c.py::test_x"
    )
    assert junit_report.test_id("tests.test_u.TestShape", "test_p[a]") == (
        "tests/test_u.py::TestShape::test_p[a]"
    )
    assert junit_report.test_id("pkg.checks", "test_x") == "pkg/checks.py::test_x"
    assert junit_report.test_id("", "test_x") == "?::test_x"


def test_only_failed_and_errored_cases_become_lines():
    assert junit_report.failed_lines(REPORT) == [
        "FAILED scripts/hooks/tests/test_contract.py::test_drop",
        "FAILED tests/test_unit.py::TestShape::test_p[a-b]",
    ]
    assert junit_report.failed_lines("not xml") == []


def test_an_empty_log_beside_a_report_still_reads_as_the_reports_failures(tmp_path):
    """carameli's gate: an empty `lint-errors.log`, the failures only in the junit."""
    (tmp_path / "lint").mkdir()
    (tmp_path / "lint" / "lint-errors.log").write_text("", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "junit-hooks.xml").write_text(REPORT, encoding="utf-8")
    (tmp_path / "tests" / "green.xml").write_text("<testsuites/>", encoding="utf-8")
    texts = junit_report.read_artifacts(tmp_path)
    assert fix_plan.signature_from_logs(texts) == (
        "scripts/hooks/tests/test_contract.py::test_drop",
        "tests/test_unit.py::TestShape::test_p[a-b]",
    )
    assert len(texts) == 2, "a report with nothing failed adds nothing"


def test_nothing_downloaded_reads_as_nothing(tmp_path):
    assert junit_report.read_artifacts(tmp_path) == []


def test_readable_keeps_the_message_and_skips_what_passed():
    [drop, shape] = junit_report.readable(REPORT)
    assert drop.splitlines()[:3] == [
        "FAILED scripts/hooks/tests/test_contract.py::test_drop",
        "  KeyError",
        "  boom",
    ]
    assert shape.startswith("FAILED tests/test_unit.py::TestShape::test_p[a-b]")
    assert junit_report.readable("not xml") == []


def test_each_failure_is_written_out_readable_beside_the_xml(tmp_path):
    """The junit report is one line of XML: a fixer's `grep -A40 "<failure"` returned
    301.9 KB, and two sweeps wrote an XML parser by hand to read their evidence. The
    failures are written out once, as the fixer would want them, and the prompt names
    the file."""
    body = "\n".join(f"line {n} &amp; &lt;x&gt;" for n in range(200))
    report = REPORT.replace(">boom<", f">{body}<")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "junit-hooks.xml").write_text(report, encoding="utf-8")
    written = junit_report.write_readable(tmp_path)
    assert written == tmp_path / junit_report.READABLE
    text = written.read_text(encoding="utf-8")
    assert "FAILED scripts/hooks/tests/test_contract.py::test_drop\n  KeyError\n" in text
    assert "  line 0 & <x>" in text and "line 199" not in text, "a traceback is capped"
    assert "FAILED tests/test_unit.py::TestShape::test_p[a-b]" in text
    empty = tmp_path / "none"
    empty.mkdir()
    assert junit_report.write_readable(empty) is None
    assert not (empty / junit_report.READABLE).exists()


def test_a_failure_only_the_test_log_names_is_listed_too(tmp_path):
    """f2b938ee: only the vendored suite writes junit, so PR #576's `failures.txt` named
    its two vendored-tier failures and not the two `tests/test_scheduled_jobs.py` ones
    that were only in `run-tests.py`'s `test-failures.log`."""
    (tmp_path / "test-failures").mkdir()
    (tmp_path / "test-failures" / "junit-hooks.xml").write_text(REPORT, encoding="utf-8")
    (tmp_path / "test-failures" / "test-failures.log").write_text(
        "____ test_spawn ____\nE   assert 1 == 2\n"
        "= short test summary info =\n"
        "FAILED tests/test_scheduled_jobs.py::test_spawn - assert 1 == 2\n"
        "\x1b[31mFAILED tests/test_scheduled_jobs.py::test_other\x1b[0m\n"
        "FAILED scripts/hooks/tests/test_contract.py::test_drop - KeyError\n",
        encoding="utf-8",
    )
    text = junit_report.write_readable(tmp_path).read_text(encoding="utf-8")

    assert "FAILED tests/test_scheduled_jobs.py::test_spawn\n  assert 1 == 2\n" in text
    assert "  (traceback in test-failures/test-failures.log)" in text
    assert "FAILED tests/test_scheduled_jobs.py::test_other\n  (traceback in" in text
    assert text.count("test_contract.py::test_drop") == 1, "junit's own is not listed twice"


def test_logged_failures_skips_an_id_already_named_and_lists_each_id_once(tmp_path):
    line = "FAILED tests/test_a.py::test_b - boom\n"
    (tmp_path / "one.log").write_text(line, encoding="utf-8")
    (tmp_path / "two.log").write_text(line + "FAILED tests/test_a.py::test_c\n", encoding="utf-8")
    named = {"tests/test_a.py::test_c"}
    blocks = junit_report.logged_failures(tmp_path, named)
    assert blocks == ["FAILED tests/test_a.py::test_b\n  boom\n  (traceback in one.log)"]
    assert named == {"tests/test_a.py::test_b", "tests/test_a.py::test_c"}
    assert junit_report.logged_failures(tmp_path / "absent", set()) == []


def test_a_log_alone_is_enough_for_a_readable_file(tmp_path):
    (tmp_path / "run.log").write_text("ERROR tests/test_a.py::test_b - boom\n", encoding="utf-8")
    text = junit_report.write_readable(tmp_path).read_text(encoding="utf-8")
    assert text == "FAILED tests/test_a.py::test_b\n  boom\n  (traceback in run.log)\n"
    status = tmp_path / "status"
    status.mkdir()
    (status / "run.log").write_text("FAILED -- details in logs/x.log\n", encoding="utf-8")
    assert junit_report.write_readable(status) is None, "a status line names no test"
