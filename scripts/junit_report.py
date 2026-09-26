#!/usr/bin/env python3
"""A downloaded gate run's artifacts as text the failure signature can read.

`fix_plan.signature_from_logs` reads pytest's `FAILED <id>` summary lines and lint
findings. A gate need not upload either in a `.log`: carameli's uploads an empty
`lint-errors.log` beside `junit-*.xml`, and its adoption PR #389 read "no artifact and
no failed step named" for a day over three tests the junit report named. So a junit
report is read here and handed on as the `FAILED` lines pytest would have printed.

Stdlib only. Tested in `tests/test_junit_report.py`.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

# One `<testcase ...>`, self-closing or with its body up to `</testcase>`.
CASE = re.compile(r"<testcase\b(?P<attrs>[^>]*?)(?:/>|>(?P<body>.*?)</testcase>)", re.DOTALL)
ATTR = re.compile(r'(\w+)="([^"]*)"')


def test_id(classname: str, name: str) -> str:
    """pytest's node id for a junit `testcase`: `a.b.test_c.TestD` + `e` is
    `a/b/test_c.py::TestD::e`. The module is the last dotted part named like one."""
    parts = [part for part in str(classname).split(".") if part]
    modules = [i for i, part in enumerate(parts) if re.match(r"^test_|.*_test$", part)]
    cut = modules[-1] + 1 if modules else len(parts)
    path = "/".join(parts[:cut]) + ".py" if parts else "?"
    return "::".join([path, *parts[cut:], str(name)])


def failed_lines(text: str) -> list[str]:
    """`FAILED <id>` for each failed or errored testcase in one junit report.

    Read with a pattern, not an XML parser: the report is pytest's own flat shape, and
    what is wanted is two attributes and whether a `<failure>` or `<error>` follows --
    nothing an entity or a DTD could change.
    """
    lines = []
    for case in CASE.finditer(str(text)):
        body = case.group("body") or ""
        if "<failure" not in body and "<error" not in body:
            continue
        attrs = {key: html.unescape(value) for key, value in ATTR.findall(case.group("attrs"))}
        lines.append(f"FAILED {test_id(attrs.get('classname', ''), attrs.get('name', '?'))}")
    return lines


def read_artifacts(dest: Path) -> list[str]:
    """Every `.log` under `dest` as text, then each junit report's failures as lines."""
    texts = []
    for log in sorted(dest.rglob("*.log")):
        try:
            texts.append(log.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    for report in sorted(dest.rglob("*.xml")):
        try:
            lines = failed_lines(report.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        texts += ["\n".join(lines) + "\n"] if lines else []
    return texts
