#!/usr/bin/env python3
"""Run devkit's own suite as if the host were POSIX, and write failures to `logs/`.

The gap this closes is the second half of the one `scripts/precommit/run_push_gate.py`
closes. That hook made a push run the same *commands* CI runs; this makes one of them run
against the same *platform assumption*. devkit is written on Windows and gated on
`ubuntu-latest`, so a test that asserts a Windows-only branch without forcing it passes
here and fails there — which is a ten-minute round trip through GitHub to learn something
a local run could have said in forty seconds.

The mechanism, what it can reach and what it deliberately cannot, is in
`tests/posix_rehearsal_plugin.py`. This wrapper is only the three things that have to happen
outside the pytest process:

1. `tests/` on `PYTHONPATH`, because `-p` resolves a plugin before collection puts that
   directory on the path itself.
2. The ledger -- `tests/posix-rehearsal-ledger.txt`, the tests allowed to fail under
   POSIX. Applied here rather than as an `xfail` marker in the plugin, because that
   spelling is one the structure gate counts as a skipped test.
3. The failure artifact, in `run-tests.py`'s format and filtered by `run-tests.py`'s own
   functions — imported rather than reimplemented, so an agent reading
   `logs/posix-rehearsal.log` reads the shape it already knows.

Usage:
    python scripts/posix-rehearsal.py

Tested in `tests/test_posix_rehearsal.py`.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACT = REPO_ROOT / "logs" / "posix-rehearsal.log"
LEDGER = REPO_ROOT / "tests" / "posix-rehearsal-ledger.txt"
PLUGIN = "posix_rehearsal_plugin"

# pytest's own summary lines, which is all the ledger comparison needs. An `ERROR` counts
# as much as a `FAILED`: a test whose *setup* takes a Windows-only path off Windows is the
# same defect reported at a different phase, and reading only one of the two would let it
# through.
_OUTCOME = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.MULTILINE)

# pytest's EXIT_NOTESTSCOLLECTED. Same reasoning as `run-tests.py`: not a failure of this
# runner, and reporting it as one blocks a push over a collection scope rather than a bug.
PYTEST_NO_TESTS_COLLECTED = 5

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def _run_tests_module(path: Path | None = None):
    """`scripts/run-tests.py`, loaded by path because its name is not an identifier.

    Imported for `filter_output` and `cap_failure_blocks` alone. Those are pure and are
    already the format every failure artifact in this repo uses; a second implementation
    would drift from it the first time either was tuned.
    """
    path = REPO_ROOT / "scripts" / "run-tests.py" if path is None else path
    spec = importlib.util.spec_from_file_location("devkit_run_tests", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def parse_ledger(text: str) -> dict[str, str]:
    """`{test id: reason}` from the ledger file's text.

    Pure, so the format is testable without running a suite. Blank lines and whole-line
    `#` comments are skipped; everything else is `<test id>  # <reason>`.
    """
    entries: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        test_id, _, reason = stripped.partition("#")
        entries[test_id.strip()] = reason.strip()
    return entries


def read_ledger(path: Path = LEDGER) -> dict[str, str]:
    """The ledger, or empty when the file is absent — which is the expected steady state."""
    if not path.is_file():
        return {}
    return parse_ledger(path.read_text(encoding="utf-8"))


def unreasoned(entries: dict[str, str]) -> list[str]:
    """Ledger ids whose reason is missing or too short to be one.

    The mechanism *is* the reason, the same way `.claude/rules/engineering.md` allows a
    suppressed lint finding only with the claim written beside it. Four words is not a
    quality bar, it is a bar against an empty comment marker.
    """
    return sorted(test_id for test_id, reason in entries.items() if len(reason.split()) < 4)


def failed_tests(output: str) -> set[str]:
    """The node ids pytest reported as failed or errored, off its short summary."""
    return {match.group(1) for match in _OUTCOME.finditer(output)}


def verdict(failed: set[str], ledger: dict[str, str]) -> tuple[list[str], list[str]]:
    """`(unexpected, fixed)` — the two ways the rehearsal is not clean.

    `unexpected` is a test that assumed Windows and nobody said so: the finding.

    `fixed` is the ratchet, and the reason this is not just "did anything fail". A ledger
    entry whose test now passes under POSIX is a line that has stopped being true, and
    left alone it would go on excusing whatever test later takes that id. Same shape as
    `structure_check.py --tighten`, which drops a baseline the code no longer earns.
    """
    return sorted(failed - set(ledger)), sorted(set(ledger) - failed)


def _display(path: Path) -> str:
    """The artifact path as a reader wants it: repo-relative, or absolute when it is not
    under the repo -- which is only ever a test pointing it at a tmp dir."""
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def command(python: str = sys.executable) -> list[str]:
    """The pytest invocation, as a list, so the tests can assert it without running it."""
    return [python, "-m", "pytest", "-p", PLUGIN, "--tb=short", "-q"]


def environment(base: dict[str, str] | None = None) -> dict[str, str]:
    """`base` with `tests/` prepended to PYTHONPATH, preserving anything already there.

    Prepended rather than assigned: a caller may be running under a PYTHONPATH of its
    own (uv does), and dropping it would break the interpreter this then spawns.
    """
    env = dict(os.environ if base is None else base)
    tests = str(REPO_ROOT / "tests")
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{tests}{os.pathsep}{existing}" if existing else tests
    return env


def _reexec(module: str) -> int | None:
    """Re-run under the project's virtualenv when this interpreter lacks pytest.

    Same shape, and the same optional import, as `run-tests.py`: an agent's shell is
    never an activated one, so `python scripts/posix-rehearsal.py` is otherwise a
    "No module named pytest" with no suite behind it.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import project_python
    except ImportError:
        return None
    return project_python.re_exec(REPO_ROOT, module, sys.argv)


def main(argv: list[str] | None = None, runner: Runner = subprocess.run) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.parse_args(argv)

    cmd = command()
    print(f"posix-rehearsal: {' '.join(cmd[2:])}", flush=True)
    result = runner(
        cmd,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=environment(),
    )
    raw = (result.stdout or "") + (result.stderr or "")

    # `LEDGER` passed rather than defaulted: a default argument binds at definition time,
    # so a test pointing this at a fixture ledger would silently be read against the real
    # one -- and would pass by agreeing with it.
    ledger = read_ledger(LEDGER)
    unexpected, fixed = verdict(failed_tests(raw), ledger)
    # A run that could not start at all -- a collection error, a missing plugin -- exits
    # non-zero with no summary line to parse, and reading that as "nothing failed" would
    # make the whole gate silently inert. Anything but a clean or empty run must be
    # explained by a node id.
    started = result.returncode in (0, PYTEST_NO_TESTS_COLLECTED) or failed_tests(raw)

    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    if started and not unexpected and not fixed:
        ARTIFACT.write_text("", encoding="utf-8")
        print(f"posix-rehearsal: passed (artifact cleared: {_display(ARTIFACT)})")
        return 0

    helpers = _run_tests_module()
    body = helpers.cap_failure_blocks(helpers.filter_output(raw)) or raw.strip()
    if fixed:
        body = (
            "# these are in tests/posix-rehearsal-ledger.txt but now pass under POSIX.\n"
            "# Delete their lines; do not re-explain them:\n"
            + "".join(f"#   {test_id}\n" for test_id in fixed)
            + body
        )
    ARTIFACT.write_text(
        "# source: scripts/posix-rehearsal.py\n"
        "# what this is: devkit's suite with sys.platform forced to POSIX and every\n"
        "#   module-level WINDOWS constant forced False. A failure here is a test that\n"
        "#   asserts a Windows branch without forcing it -- it will fail on CI's runner.\n"
        "# fix: force the branch in the test (monkeypatch the module's WINDOWS), or, if\n"
        "#   the test truly cannot hold off Windows, add it to\n"
        "#   tests/posix-rehearsal-ledger.txt with a reason.\n" + body + "\n",
        encoding="utf-8",
    )
    print(f"posix-rehearsal: FAILED — details in {_display(ARTIFACT)}")
    return 1


if __name__ == "__main__":
    code = _reexec("pytest")
    sys.exit(main() if code is None else code)
