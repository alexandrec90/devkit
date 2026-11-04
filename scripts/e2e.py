#!/usr/bin/env python3
"""devkit's end-to-end tier: every test that needs this workstation rather than a runner.

devkit's copy of the contract `scripts/e2e-pass.py` runs in every registered checkout
(`e2e_results.py` owns it): exit 0 green, anything else red, each failing test printed as
pytest's `FAILED <id>` line. What it runs is every test marked `local` -- which
`tests/support.py`'s `needs_live_workspace` and `needs_the_static_checkout` apply
themselves, so a test that skips in CI for wanting this machine is in this tier by the
same decorator that skips it there.

**A skip is a failure when the scheduler runs it.** Every test here skips somewhere by
design: CI has no live workspace, a box is not the static checkout. Under the scheduled
pass (`e2e-pass.ENV_FLAG` set) the checkout is the static one on its default branch, so
nothing has a reason left to skip, and one that does is the machine drifting from what
the tests assume -- a moved workspace file, a checkout off its branch. Each skip is
printed as a `FAILED` line naming its reason, so it signs the result like any failure.
By hand, in a box, the skips are listed and do not fail the run: that is a fixer
reproducing what it can, and the scheduled run re-tests the rest.

**A run that executed nothing is red**, whoever ran it: a green from zero tests is the
skip-as-pass `hook-tests-live.py` exists to refuse.

**Under the scheduler it publishes the workspace file first.** The live file is rendered
from the canonical copy once a day (`devkit-workspace-status`), so a task merged in the
afternoon is unrendered at night and the drift check would send a fixer at the normal
latency. The pass has just put this checkout on origin's tip with no tracked edit --
exactly `workspace-status.publish_verdict`'s two conditions -- so it renders on the same
terms (`devkit_project.publish_workspace`), which still refuses a live edit nobody
adopted. A box never renders.

Tested in `tests/test_e2e.py`.
"""

from __future__ import annotations

import html
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devkit_project
import e2e_results
import junit_report
import sweep

REPO_ROOT = Path(__file__).resolve().parents[1]

# Spelled as `e2e-pass.ENV_FLAG` spells it; a test pins the two together.
ENV_FLAG = "DEVKIT_E2E"
MARKER = "local"
JUNIT = Path("logs") / "e2e-junit.xml"


@dataclass(frozen=True)
class Case:
    """One test case from the junit report: `tests/x.py::name`, and how it ended."""

    ident: str
    outcome: str  # "passed", "failed" or "skipped"
    message: str = ""


def scheduled(env: dict[str, str] | os._Environ[str] = os.environ) -> bool:
    return bool(env.get(ENV_FLAG))


# The element a case's body opens with when it did not pass, and its `message`.
OUTCOME = re.compile(r"<(?P<tag>skipped|failure|error)\b(?P<attrs>[^>]*)")


def cases(report: Path) -> list[Case]:
    """Every case in a pytest junit report, named as pytest's summary names it.

    Read with `junit_report`'s patterns rather than an XML parser, for its reason: the
    report is pytest's own flat shape, and an entity or a DTD can change none of what is
    wanted here. Unreadable is no cases, which `verdict` reads as a run that ran nothing.
    """
    try:
        text = report.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found: list[Case] = []
    for match in junit_report.CASE.finditer(text):
        attrs = dict(junit_report.ATTR.findall(match.group("attrs")))
        name = html.unescape(attrs.get("name", "?"))
        ident = junit_report.test_id(attrs.get("classname", ""), name)
        ended = OUTCOME.search(match.group("body") or "")
        if ended is None:
            found.append(Case(ident, "passed"))
            continue
        message = html.unescape(dict(junit_report.ATTR.findall(ended["attrs"])).get("message", ""))
        found.append(Case(ident, "skipped" if ended["tag"] == "skipped" else "failed", message))
    return found


def verdict(code: int, found: list[Case], strict: bool) -> tuple[int, list[str]]:
    """`(exit code, lines to print)` for pytest's `code` and the cases it reported.

    pytest already printed its own `FAILED` lines; these add the skips and the empty run.
    """
    ran = [case for case in found if case.outcome != "skipped"]
    skips = [case for case in found if case.outcome == "skipped"]
    lines: list[str] = []
    for case in skips:
        shown = case.message.strip().splitlines()[0] if case.message.strip() else "no reason"
        lines.append(f"{'FAILED' if strict else 'SKIPPED'} {case.ident} - skipped: {shown}")
    if not ran:
        lines.append(f"e2e: no {MARKER} test executed, which is not a pass")
        return (code or 1), lines
    if strict and skips:
        lines.append(f"e2e: {len(skips)} test(s) skipped under the scheduled run")
        return (code or 1), lines
    return code, lines


def publish(live: Path = devkit_project.DEFAULT_WORKSPACE) -> str:
    """Render the canonical workspace over the live file, on `publish_workspace`'s terms."""
    if sweep.source_checkout(REPO_ROOT) != REPO_ROOT:
        return "e2e: a box never renders the workspace file"
    if not live.is_file():
        return f"e2e: no live workspace file at {live}"
    outcome, differences = devkit_project.publish_workspace(live)
    return f"e2e: workspace publish -- {outcome}" + "".join(f"\n  {d}" for d in differences)


def pytest_argv(python: str, report: Path) -> list[str]:
    return [
        python,
        "-m",
        "pytest",
        "tests",
        "-m",
        f"{MARKER} and not paid",
        "-rfE",
        "-p",
        "no:cacheprovider",
        f"--junitxml={report}",
    ]


def main(argv: list[str] | None = None) -> int:
    del argv  # no options: the scheduled pass and a fixer run it the same way
    strict = scheduled()
    if strict:
        print(publish(), flush=True)
    report = REPO_ROOT / JUNIT
    report.parent.mkdir(parents=True, exist_ok=True)
    report.unlink(missing_ok=True)
    # The pass already runs this under the checkout's venv; by hand in a fresh box the
    # bare `python` may lack pytest, which the box's own `.venv` has.
    python = e2e_results.venv_interpreter(REPO_ROOT, sweep.console_python())
    # Captured and re-printed rather than inherited: a `NO_WINDOW` child that captures
    # nothing writes to the hidden console it was given, not to the pipe the pass reads,
    # and the result's `output.log` would hold none of pytest's `FAILED` lines.
    done = sweep.run_windowless(
        pytest_argv(python, report),
        cwd=str(REPO_ROOT),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    print(done.stdout or "", end="", flush=True)
    print(done.stderr or "", end="", file=sys.stderr, flush=True)
    code, lines = verdict(done.returncode, cases(report), strict)
    for line in lines:
        print(line, flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
