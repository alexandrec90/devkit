#!/usr/bin/env python3
"""Run the paid live-CLI hook smokes and write failures to a parseable artifact.

The backend for the workspace's "Test: Harness Hook Tests — paid, live CLI" task, and
the deliberate counterpart to `scripts/hook-tests.py`. The free script runs the vendored
tier's pure-function tests in any checkout; this one launches a **real, authenticated
agent CLI** against a throwaway project and proves the hooks change what the agent
actually does. Only devkit has those tests -- they live in `tests/`, which is never
vendored -- so the action is scoped to this repo alone.

Two things this exists to stop, both of which a bare `pytest -m paid` does silently:

- **A skip reported as a pass.** Both suites call `pytest.skip` when their CLI is not on
  `PATH`, which is right for a test and wrong for a one-click task: the terminal goes
  green having launched nothing. The `paid` marker compounds it -- `addopts` in
  `pyproject.toml` deselects it by default, so a forgotten `-m` is also a green no-op.
  Every run here therefore reports how many tests actually executed, and a run that
  executed none is a failure.
- **Spending without being told.** A `detail` string in the task list is read once. The
  preflight prints the suites, the test count and where each one's cost knobs are
  defined, before anything is launched.

The cost defaults themselves are NOT restated here. They live in the test modules
(`LIVE_MODEL`, `LIVE_EFFORT`, `LIVE_BUDGET_USD`) with environment overrides, and a copy
in this file is a second number to keep in step with no test to catch it drifting.
The Codex suite writes its model and reasoning settings only into the throwaway
`CODEX_HOME` it launches; this runner never writes the operator's user configuration.

**Runs with the cwd set to the chosen checkout**, like every dispatched action, so the
target repo is `Path.cwd()`.

Usage:
    python scripts/hook-tests-live.py            # the Claude suite, the cheapest run
    python scripts/hook-tests-live.py codex
    python scripts/hook-tests-live.py both
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ARTIFACT = Path("logs") / "hook-test-live-failures.log"

# Same cap and reasoning as `hook-tests.py`: past this it is one traceback repeated,
# not more findings. Larger there because `-s` lets the CLI's own output through.
MAX_LINES = 400


@dataclass(frozen=True)
class Suite:
    """One live suite: its tests, the binary it needs, and where its cost is set."""

    key: str
    path: str  # relative to the checkout, so the cwd contract holds
    binary: str  # what must be on PATH for it to do anything
    marker: str  # the pytest marker selecting it, paired with `paid`
    knobs: str  # the environment variables that override its cost defaults


SUITES: dict[str, Suite] = {
    "claude": Suite(
        key="claude",
        path="tests/test_claude_hooks_live.py",
        binary="claude",
        marker="claude_live",
        knobs="CLAUDE_LIVE_HOOK_MODEL, CLAUDE_LIVE_HOOK_EFFORT, CLAUDE_LIVE_HOOK_BUDGET_USD",
    ),
    "codex": Suite(
        key="codex",
        path="tests/test_codex_hooks_live.py",
        binary="codex",
        marker="codex_live",
        knobs="CODEX_LIVE_HOOK_MODEL, CODEX_LIVE_HOOK_REASONING_EFFORT",
    ),
}

# Ordered, so "both" is reproducible and the cheaper suite is attempted first: if the
# Claude smoke fails, that is usually the answer, and the codex run is money saved.
ORDER = ("claude", "codex")


def resolve_suites(choice: str) -> list[Suite]:
    """The suites a picker value selects. Raises on an unknown one rather than guessing."""
    if choice == "both":
        return [SUITES[key] for key in ORDER]
    if choice not in SUITES:
        raise ValueError(f"unknown suite {choice!r}; expected one of: both, {', '.join(ORDER)}")
    return [SUITES[choice]]


def missing_suites(suites: list[Suite], root: Path) -> list[str]:
    """Suites whose test file is absent — i.e. this checkout is not devkit."""
    return [suite.path for suite in suites if not (root / suite.path).is_file()]


def missing_binaries(suites: list[Suite], which=None) -> list[str]:
    """CLIs not on PATH. Injected `which` so the check is testable without installing one.

    Resolved inside rather than as `which=shutil.which` in the signature: a default is
    bound once at import, so the module-level name would keep pointing at the original
    function and every patch of it — a test's, or a caller's — would be ignored.
    """
    probe = which or shutil.which
    return [suite.binary for suite in suites if probe(suite.binary) is None]


def selector(suites: list[Suite]) -> str:
    """The `-m` expression. `paid` is ANDed in because `addopts` deselects it by default,
    so omitting it turns the whole run into a silent no-op."""
    markers = " or ".join(suite.marker for suite in suites)
    return f"({markers}) and paid"


# Every outcome word pytest puts in its summary line, so the line is *recognised* even
# when nothing ran, and only the ones that mean a CLI was actually launched.
SUMMARY = re.compile(r"(\d+) (passed|failed|errors?|skipped|deselected|xfailed|xpassed|warnings?)")
EXECUTED = {"passed", "failed", "error", "errors"}


def ran_count(output: str) -> int | None:
    """How many tests pytest reported as passed or failed.

    `skipped` and `deselected` are recognised but not counted — they are the two ways
    this task goes green having launched no CLI at all, which is the thing it exists to
    catch. `None` means no summary line could be found, which the caller also treats as
    a failure: an unreadable run is not evidence that anything ran.
    """
    for line in reversed(output.strip().splitlines()[-15:]):
        found = SUMMARY.findall(line)
        if found:
            return sum(int(number) for number, word in found if word in EXECUTED)
    if re.search(r"no tests ran", output):
        return 0
    return None


def cap(text: str, limit: int = MAX_LINES) -> str:
    """Truncate to `limit` lines, saying so — pure, so it is testable without pytest."""
    lines = text.splitlines()
    if len(lines) <= limit:
        return text.strip()
    kept = lines[:limit]
    kept.append(f"... ({len(lines)} lines total, truncated)")
    return "\n".join(kept).strip()


def preflight_report(suites: list[Suite]) -> str:
    """What is about to be launched and what sets its cost. Printed before spending."""
    lines = ["hook-tests-live: PAID — this launches real, authenticated CLI sessions."]
    for suite in suites:
        lines.append(f"  {suite.key:6} {suite.path}")
        lines.append(f"         cost defaults live in that file; override with {suite.knobs}")
    return "\n".join(lines)


def write_artifact(root: Path, body: str, suites: list[Suite]) -> None:
    """Persist the failure so it is read from a file rather than scraped off a terminal."""
    artifact = root / ARTIFACT
    artifact.parent.mkdir(parents=True, exist_ok=True)
    paths = " ".join(suite.path for suite in suites)
    artifact.write_text(
        "# source: devkit scripts/hook-tests-live.py\n"
        f'# fix: python -m pytest {paths} -m "{selector(suites)}" -s --tb=long\n'
        f"# NB: rerunning spends money again.\n" + body + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "suite",
        nargs="?",
        default="claude",
        choices=["claude", "codex", "both"],
        help="which live suite to run (default: claude, the cheapest)",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="checkout to test (default: the cwd, which is how the task invokes it)",
    )
    args = parser.parse_args(argv)
    root = (args.root or Path.cwd()).resolve()
    suites = resolve_suites(args.suite)

    absent = missing_suites(suites, root)
    if absent:
        # Loud, not a clean skip — the same rule `hook-tests.py` follows. These tests are
        # devkit-only by design (`tests/` is never vendored), so a checkout without them
        # is a mis-scoped action to report, never a pass to hand back.
        print(
            f"hook-tests-live: {root.name} has no {', '.join(absent)} — the live smokes "
            f"live in devkit's own tests/, which is not vendored. Nothing to run here.",
            file=sys.stderr,
        )
        return 2

    unavailable = missing_binaries(suites)
    if unavailable:
        # pytest would skip these, and a skip reads as a pass in the task list.
        print(
            f"hook-tests-live: {', '.join(unavailable)} not on PATH. The suite would skip, "
            f"and a skipped paid smoke is a green run that proved nothing.",
            file=sys.stderr,
        )
        return 2

    print(preflight_report(suites))
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        *(suite.path for suite in suites),
        "-m",
        selector(suites),
        # `-s` because the CLI's own stdout is the diagnostic when a smoke fails; the
        # artifact cap above is what keeps that from being unbounded.
        "-s",
        "--tb=short",
    ]
    print(f"hook-tests-live: {' '.join(cmd[2:])}")
    result = subprocess.run(cmd, cwd=root, capture_output=True, text=True)
    output = result.stdout + result.stderr

    executed = ran_count(output)
    if result.returncode == 0 and executed:
        (root / ARTIFACT).parent.mkdir(parents=True, exist_ok=True)
        (root / ARTIFACT).write_text("", encoding="utf-8")
        print(f"hook-tests-live: passed, {executed} live test(s) — artifact cleared")
        return 0

    if result.returncode == 0:
        # Green with nothing executed. Reported as the failure it is, because the exit
        # code alone said the opposite and this task's whole value is the CLI it launches.
        why = (
            "its summary could not be read"
            if executed is None
            else f"having run {executed} test(s) — everything was skipped or deselected"
        )
        write_artifact(root, f"# zero exit code, {why}\n{cap(output)}", suites)
        print(
            f"hook-tests-live: FAILED — pytest exited 0 {why}. See {ARTIFACT.as_posix()}",
            file=sys.stderr,
        )
        return 1

    write_artifact(root, cap(output), suites)
    print(f"hook-tests-live: FAILED — details in {ARTIFACT.as_posix()}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
