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
