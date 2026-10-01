#!/usr/bin/env python3
"""Run devkit's own test suite and write failures to a parseable artifact.

devkit's copy of the test runner it ships in `templates/core/scripts/`. Same contract
as `lint-all.py`: the agent fixing a failure reads `logs/test-failures.log`, not the
terminal. Each failure block is capped so one broken test cannot flood the artifact
and bury the other twenty.

Scope comes from `pyproject.toml`'s `testpaths = ["tests"]` — devkit's own suite (the
generator, the port registry, the renderer). The vendored tier,
`scripts/hooks/tests/`, is deliberately outside it and runs as its own step: it ships
into every consuming project and must stay separately runnable there.

**The default is the tests named by what changed**, not the suite: every file
changed since the branch left `origin/<default>`, mapped to `tests/test_<stem>.py`
(and, for anything under `templates/`, to `GENERATED_TREE_TESTS` too; a `.tmpl` also
to the tests that spell its name), plus `CONTRACT_TESTS`, which read every module and
so are named by none of them.
The whole suite is CI's, the push gate's (`PRE_COMMIT` is in the environment under
pre-commit) and `--all`'s. Where git cannot say what changed, the suite runs.

Usage:
    python scripts/run-tests.py             # the tests for what changed
    python scripts/run-tests.py --all       # the whole suite
    python scripts/run-tests.py --changed   # pytest's last-failed subset
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACT = REPO_ROOT / "logs" / "test-failures.log"

# Where the whole suite is the point: CI, and the push gate that mirrors it (pre-commit
# exports `PRE_COMMIT` into every hook's environment).
FULL_SUITE_ENV = ("CI", "PRE_COMMIT")
# Where a changed module's test is looked for: the project's suite, then the vendored
# tier's beside the scripts it tests. With `tests/` alone, a change to
# `scripts/log-wrap.py` ran no test of it at all.
TEST_DIRS = ("tests", "scripts/hooks/tests")
# A template's suffix. A `.tmpl` of any kind is read by the tests that spell its name;
# without that reading, a change to `ruff.toml.tmpl` or `pr-gate.yml.tmpl` named no test
# but the generated-tree checks.
TEMPLATE_SUFFIX = ".tmpl"

# The checks over a generated project as a whole -- format-clean, for one -- which every
# change under `templates/` names in addition to its own tests.
GENERATED_TREE_TESTS = "tests/test_generated_tree.py"

# Per-failure line cap. Chosen to hold a first-party traceback plus the assertion
# without letting a single deep failure crowd out the rest of the run.
MAX_LINES_PER_FAILURE = 25

# pytest's EXIT_NOTESTSCOLLECTED, which is not a failure of this runner. It matters
# because `stop.py` calls this script with explicit targets (the changed files under
# tests/): editing a helper that holds no tests of its own — a conftest.py, a support
# module — collects nothing, and reporting that as a failure blocks the stop with "no
# tests ran", which no source edit can resolve.
PYTEST_NO_TESTS_COLLECTED = 5

# The tests that hold every module to a contract -- has a test module, is checked where
# a scheduled job reaches it, is watched by the tray, cites paths that exist -- so no
# changed file's name maps to them, and a run of "the tests for what changed" skipped
# exactly the ones a new import or a new script breaks. #467 added an import to
# fix-pass.py, ran its targeted tests green, and needed a second fixer for
# test_scheduled_jobs (54bb72df). About twenty seconds together; a listed file that is
# gone is dropped, and `tests/test_run_tests.py` fails the list when one is.
CONTRACT_TESTS = (
    "tests/test_test_contract.py",
    "tests/test_scheduled_jobs.py",
    "tests/test_install_tray.py",
    "tests/test_self_hosting.py",
    "tests/test_doc_claims.py",
    "tests/test_installer_contract.py",
    "tests/test_worktree_tiers_single_source.py",
    "tests/test_dispatch_coherence.py",
    "tests/test_gate_parity.py",
    "scripts/hooks/tests/test_repo_contract.py",
)


def filter_output(raw: str) -> str:
    """Keep the failure sections; drop passing noise and third-party frames.

    Pure, so it is unit-testable without running pytest.
    """
    lines = raw.splitlines()
    keep: list[str] = []
    in_failures = False
    for line in lines:
        if "=== FAILURES ===" in line or "= FAILURES =" in line:
            in_failures = True
        if "= short test summary info =" in line:
            in_failures = True
        if in_failures:
            # Library internals are noise: an agent cannot fix a frame inside
            # site-packages, and a long third-party traceback hides the one
            # first-party frame that matters.
            if "site-packages" in line or "/lib/python" in line:
                continue
            keep.append(line)
    return "\n".join(keep).strip()


SUMMARY_BANNER = "= short test summary info ="

# The short summary is the run's index -- one `FAILED <id>` line per failure, which is
# what `fix_plan.signature_from_logs` builds a fix prompt's test list from -- so it is
# never capped as a block. It used to ride on the last failure's block, and that block's
# cap cut every FAILED line after the first long message (57d473e9: two red, one named).
# Each entry keeps its headline and this many of the lines its message continues on;
# the whole message is in the failure's own block above.
SUMMARY_LINES_PER_ENTRY = 3


def cap_failure_blocks(text: str, limit: int = MAX_LINES_PER_FAILURE) -> str:
    """Truncate each `___ test_name ___` block to `limit` lines, noting the cut, and
    each short-summary entry to its headline plus `SUMMARY_LINES_PER_ENTRY` lines."""
    lines = text.splitlines()
    cut = next((i for i, line in enumerate(lines) if SUMMARY_BANNER in line), len(lines))
    return "\n".join([*_cap_blocks(lines[:cut], limit), *_cap_summary(lines[cut:])])


def _cap_summary(lines: list[str]) -> list[str]:
    """The summary with every entry's headline kept; an indented continuation is capped."""
    out: list[str] = []
    extra = kept = 0
    for line in lines:
        if line and not line[0].isspace():
            out.extend(_more(extra))
            out.append(line)
            extra = kept = 0
        elif kept < SUMMARY_LINES_PER_ENTRY:
            out.append(line)
            kept += 1
        else:
            extra += 1
    return out + _more(extra)


def _more(count: int) -> list[str]:
    return [f"  ... ({count} more lines, truncated)"] if count else []


def _cap_blocks(lines: list[str], limit: int) -> list[str]:
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line.startswith("_" * 5) and current:
            blocks.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append(current)

    out: list[str] = []
    for block in blocks:
        if len(block) > limit:
            out.extend(block[:limit])
            out.append(f"... ({len(block)} lines total, truncated)")
        else:
            out.extend(block)
    return out


def default_branch(root: Path, run=subprocess.run) -> str:
    """`origin/HEAD`'s branch, else whichever of `main` and `master` origin has, else `main`."""

    def git(*args: str):
        return run(["git", *args], cwd=root, capture_output=True, text=True, check=False)

    head = git("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    ref = (head.stdout or "").strip()
    if head.returncode == 0 and ref.startswith("refs/remotes/origin/"):
        return ref.rsplit("/", 1)[1]
    for candidate in ("main", "master"):
        if (
            git("rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{candidate}").returncode
            == 0
        ):
            return candidate
    return "main"


def changed_paths(root: Path, run=subprocess.run) -> list[str] | None:
    """Every path changed since the branch left origin's default: committed, staged,
    unstaged and untracked. None when git cannot say -- no repository, no origin."""

    def git(*args: str):
        return run(["git", *args], cwd=root, capture_output=True, text=True, check=False)

    base = git("merge-base", "HEAD", f"origin/{default_branch(root, run)}")
    if base.returncode != 0 or not (base.stdout or "").strip():
        return None
    diff = git("diff", "--name-only", base.stdout.strip())
    untracked = git("ls-files", "--others", "--exclude-standard")
    if diff.returncode != 0 or untracked.returncode != 0:
        return None
    seen = (diff.stdout or "").splitlines() + (untracked.stdout or "").splitlines()
    return sorted({line.strip() for line in seen if line.strip()})


def tests_for(paths: list[str], root: Path = REPO_ROOT) -> tuple[list[str], list[str]]:
    """`(test files to run, changed files that name none)`.

    A test file names itself; any other `.py` names `test_<stem>.py` in each of
    `TEST_DIRS` with hyphens read as underscores (`scripts/fix-pass.py` ->
    `tests/test_fix_pass.py`), when that file exists; a `.py.tmpl` names that and
    `test_<stem>_template.py`, and anything under `templates/` names
    `GENERATED_TREE_TESTS`. A `.tmpl` also names every test
    whose source spells its file name, since that is how a test reads one. Everything
    else -- a document, a workflow, a module with no test of its own -- is reported so
    the caller can see what the run did not cover.
    """
    tests: list[str] = []
    unnamed: list[str] = []
    for path in paths:
        posix = path.replace("\\", "/")
        found = [name for name in _named_tests(posix) if (root / name).is_file()]
        found += [name for name in _reading_tests(posix, root) if name not in found]
        tests.extend(name for name in found if name not in tests)
        if not found:
            unnamed.append(posix)
    return tests, unnamed


def _named_tests(posix: str) -> list[str]:
    """The test files the changed file `posix` could name, existing or not."""
    named = _named_by_stem(posix)
    # Any template also names the checks over the generated tree as a whole, which no
    # template's own name leads to: #479 reshaped `run-tests.py.tmpl`, ran its tests
    # green, and a generated project failed `ruff format --check` on arrival.
    if posix.startswith("templates/") and GENERATED_TREE_TESTS not in named:
        named.append(GENERATED_TREE_TESTS)
    return named


def _named_by_stem(posix: str) -> list[str]:
    stem = posix.rsplit("/", 1)[-1]
    if stem.endswith(".py.tmpl"):
        # A template of a script is tested as the script it renders to, and on its own.
        name = stem.removesuffix(".py.tmpl").replace("-", "_")
        return [f"{d}/test_{name}{kind}.py" for d in TEST_DIRS for kind in ("", "_template")]
    if not stem.endswith(".py"):
        return []
    if stem.startswith("test_") and any(posix.startswith(f"{d}/") for d in TEST_DIRS):
        return [posix]
    return [f"{d}/test_{stem[:-3].replace('-', '_')}.py" for d in TEST_DIRS]


def _reading_tests(posix: str, root: Path) -> list[str]:
    """The test files under `root` whose source spells the template `posix`'s file name."""
    name = posix.rsplit("/", 1)[-1]
    if not name.endswith(TEMPLATE_SUFFIX):
        return []
    found: list[str] = []
    for directory in TEST_DIRS:
        for test in sorted((root / directory).glob("test_*.py")):
            if name in test.read_text(encoding="utf-8", errors="replace"):
                found.append(f"{directory}/{test.name}")
    return found


def with_contracts(tests: list[str], root: Path = REPO_ROOT) -> list[str]:
    """`tests` followed by every `CONTRACT_TESTS` file `root` holds that it lacks."""
    extra = [t for t in CONTRACT_TESTS if t not in tests and (root / t).is_file()]
    return [*tests, *extra]


def _reexec(module: str) -> int | None:
    """Re-run this process under the project's virtualenv, or None to carry on here.

    The import is local, and optional, on purpose. This script is copied into generated
    projects and, in the suite, into a bare temp repo holding nothing but itself; a
    module-level import would turn "the interpreter could not be upgraded" into "this
    will not start at all", which is worse than the behaviour it improves on.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import project_python
    except ImportError:
        return None
    return project_python.re_exec(REPO_ROOT, module, sys.argv)


def _parallel_args() -> list[str]:
    """`-n auto` when xdist is installed, nothing when it is not.

    Imported by path and optional, for the same reason `_reexec` imports
    `project_python` that way: this file is copied into a bare temp repo holding
    nothing but itself, and a module-level import would turn a missing sibling into
    "this will not start at all". A suite that runs serially is the behaviour this
    script had for its whole life; a suite that does not run is not.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import pytest_parallel
    except ImportError:
        return []
    return pytest_parallel.args()


def with_basetemp(cmd: list[str], basetemp: str) -> list[str]:
    """`cmd` given a pytest temp root of its own, right after `-m pytest`.

    Left to itself pytest roots every run on the machine under one `pytest-of-<user>`,
    and on the way out stats every link there -- `pytest-current` included, which a run
    in another session is replacing or holding. On Windows that stat is an access-denied
    error raised after the last test, so a green suite exits 1 with no failed test to
    name (97d20f01, reproduced 2026-09-26). A directory only this run knows about cannot
    be contended; the caller creates and removes it.
    """
    return [*cmd[:3], f"--basetemp={basetemp}", *cmd[3:]]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="run the whole suite")
    parser.add_argument("--changed", action="store_true", help="run pytest's last-failed subset")
    args, extra = parser.parse_known_args(argv)
    targets = [a for a in extra if a]

    cmd = [sys.executable, "-m", "pytest", "--tb=short", "-q"]
    cmd += _parallel_args()
    if args.changed:
        cmd += ["--last-failed", "--last-failed-no-failures", "all"]
    whole = args.all or args.changed or targets or any(os.environ.get(k) for k in FULL_SUITE_ENV)
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    if not whole:
        changed = changed_paths(REPO_ROOT)
        if changed is None:
            print("run-tests: git cannot say what changed; running the suite")
        else:
            targets, unnamed = tests_for(changed, REPO_ROOT)
            print(
                f"run-tests: {len(targets)} test file(s) for {len(changed)} changed path(s), "
                "and the contract tests; --all runs the suite"
            )
            if changed:
                targets = with_contracts(targets, REPO_ROOT)
            for path in unnamed:
                print(f"run-tests:   no test named for {path}")
            if not targets:
                ARTIFACT.write_text("", encoding="utf-8")
                print("run-tests: nothing to run (artifact cleared)")
                return 0
    cmd += targets

    print(f"run-tests: {' '.join(cmd[2:])}")
    with tempfile.TemporaryDirectory(prefix="pytest-", ignore_cleanup_errors=True) as basetemp:
        run = with_basetemp(cmd, basetemp)
        result = subprocess.run(run, cwd=REPO_ROOT, capture_output=True, text=True)
    raw = result.stdout + result.stderr

    if result.returncode in (0, PYTEST_NO_TESTS_COLLECTED):
        # Clear on pass, so a stale artifact never sends the next agent chasing a
        # failure that is already fixed.
        ARTIFACT.write_text("", encoding="utf-8")
        print(f"run-tests: passed (artifact cleared: {ARTIFACT.relative_to(REPO_ROOT)})")
        return 0

    body = cap_failure_blocks(filter_output(raw))
    # Never leave the agent with nothing: if filtering stripped everything (an
    # unexpected pytest output shape, a collection error), fall back to raw.
    if not body.strip():
        body = raw.strip()
    ARTIFACT.write_text(
        "# source: scripts/run-tests.py\n"
        "# fix: pytest <the failing test id> --tb=long\n" + body + "\n",
        encoding="utf-8",
    )
    print(f"run-tests: FAILED — details in {ARTIFACT.relative_to(REPO_ROOT)}")
    return 1


if __name__ == "__main__":
    # Re-exec under the project's own virtualenv when this interpreter cannot import
    # pytest. Invoked as `python scripts/run-tests.py` from an agent's shell, `python` is
    # whatever is on PATH -- which on a workstation is the bare install, not the `.venv`
    # holding the dev tools -- and the whole run died on "No module named pytest" with an
    # artifact carrying only that line. `project_python` explains why an interpreter is
    # resolved rather than a PATH: an agent's shell is never an activated one.
    #
    # In the `__main__` guard rather than inside `main()` so that a test calling `main()`
    # directly stays in-process and testable.
    code = _reexec("pytest")
    sys.exit(main() if code is None else code)
