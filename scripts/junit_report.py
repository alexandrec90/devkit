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


# What `write_readable` leaves beside the XML, and how much of each failure's body it keeps.
READABLE = "failures.txt"
BODY_LINES = 40
TAG = re.compile(r"<[^>]+>")
MESSAGE = re.compile(r'<(?:failure|error)\b[^>]*\bmessage="([^"]*)"')
# pytest's short-summary line, as a `.log` keeps it: `FAILED <id> - <message>`.
SUMMARY_FAILED = re.compile(r"^(?:FAILED|ERROR) (\S+::\S+)(.*)$")
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def readable(text: str) -> list[str]:
    """Each failed testcase as its id, its message, and the head of its traceback.

    The report is one line of XML, so reading it raw cost a fixer a 301.9 KB `grep`
    result, and two sweeps wrote a parser by hand. This is that parser, run once.
    """
    blocks = []
    for case in CASE.finditer(str(text)):
        body = case.group("body") or ""
        if "<failure" not in body and "<error" not in body:
            continue
        attrs = {key: html.unescape(value) for key, value in ATTR.findall(case.group("attrs"))}
        found = MESSAGE.search(body)
        lines = html.unescape(TAG.sub("", body)).strip().splitlines()[:BODY_LINES]
        head = f"FAILED {test_id(attrs.get('classname', ''), attrs.get('name', '?'))}"
        message = [f"  {html.unescape(found.group(1))}"] if found else []
        blocks.append("\n".join([head, *message, *(f"  {line}" for line in lines)]))
    return blocks


def logged_failures(dest: Path, named: set[str]) -> list[str]:
    """A block per pytest `FAILED <id>` summary line in a `.log` under `dest` that no junit
    report named, pointing at the log that holds its traceback.

    f2b938ee: only the vendored suite writes junit; the main suite's failures reach the
    gate as `run-tests.py`'s `test-failures.log`. `READABLE` listed the vendored-tier two
    of PR #576's four and left out the two the prompt sent the fixer for.
    """
    blocks = []
    for log in sorted(dest.rglob("*.log")):
        try:
            text = log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            found = SUMMARY_FAILED.match(ANSI.sub("", line).strip())
            if found is None or found.group(1) in named:
                continue
            named.add(found.group(1))
            rest = found.group(2).strip().removeprefix("- ")
            message = [f"  {rest}"] if rest else []
            where = log.relative_to(dest).as_posix()
            blocks.append(
                "\n".join([f"FAILED {found.group(1)}", *message, f"  (traceback in {where})"])
            )
    return blocks


def write_readable(dest: Path) -> Path | None:
    """Write every failure under `dest` to `dest/READABLE` -- each junit report's, then
    each one only a `.log` names (`logged_failures`); its path, or None when none does."""
    blocks = []
    for report in sorted(dest.rglob("*.xml")):
        try:
            blocks += readable(report.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    named = {block.splitlines()[0].removeprefix("FAILED ") for block in blocks}
    blocks += logged_failures(dest, named)
    if not blocks:
        return None
    path = dest / READABLE
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8", newline="\n")
    return path


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
