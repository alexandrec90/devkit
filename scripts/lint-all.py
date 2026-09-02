#!/usr/bin/env python3
"""Run every linter and write the failures to a single parseable artifact.

devkit's own copy of the lint runner it ships in `templates/core/scripts/`. The
contract is the same one CLAUDE.md describes: an agent fixing lint reads
`logs/lint-errors.log`, never the terminal. So this script keeps the terminal to a
status line plus the artifact path, and puts everything actionable in the file — on
failure *and* on success, where it writes an empty artifact so a stale run cannot
mislead the next agent.

Auto-fix runs before the reporting pass, so only genuinely unfixable errors are
reported and the agent never burns a cycle on something `ruff --fix` already solved.

**Two deliberate differences from the template version**, both because devkit is
upstream rather than a consumer:

  - It formats `scripts/hooks/` instead of protecting it. A generated project must
    not rewrite its vendored harness (`sync-devkit.py --check` fails the build over
    a byte of drift it cannot fix in source), so the template carries a
    `NO_FIX_SCOPE`. Here those files are the source of truth, CI gates them with
    `ruff format --check .`, and formatting them is the whole point.
  - `templates/` is excluded rather than linted. Its `.py` files are *content*: they
    are linted by the `ruff.toml` that ships alongside them into each generated
    project, which carries the `scripts/**` allowances they need. Linting them under
    devkit's own config reports findings that are correct there and wrong here.

Usage:
    python scripts/lint-all.py            # whole repo
    python scripts/lint-all.py --changed  # working-tree diff vs HEAD, plus untracked
    python scripts/lint-all.py --paths a.py b.md  # exactly these (/ship's branch diff)
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import project_python
except ImportError:
    # **Optional on purpose.** This script travels: it is copied into generated projects
    # and, in the suite, into a bare temp repo holding nothing but itself. A hard import
    # would turn "the interpreter could not be upgraded" into "the linter will not start
    # at all", which is strictly worse than the behaviour it improves on.
    project_python = None  # type: ignore[assignment]

REPO_ROOT = Path(__file__).resolve().parent.parent

# Linters that come from `pyproject.toml`'s dev group, so a missing one is a broken
# environment rather than an absent optional tool.
#
# **This distinction is the whole of the fix.** `run_tool` degrades a missing tool to a
# terminal note and returns "" — the same value it returns when a tool PASSED — so a run
# where every linter was skipped produced no sections and printed `lint-all: clean`. That
# is a false negative on every rule at once, and it is indistinguishable at a glance from
# the real thing. The node tools are genuinely optional (nothing here declares them), so
# they keep the old behaviour; these two do not.
REQUIRED_TOOLS = ("ruff", "mypy")

# Names passed to `run_tool` that were skipped for want of the tool. Module-level because
# `run_tool` is called from eight places and threading an accumulator through all of them
# would obscure the one line that matters; `main` clears it on entry.
_SKIPPED: list[str] = []
ARTIFACT = REPO_ROOT / "logs" / "lint-errors.log"

# What mypy type-checks. Unlike a generated project's copy, this includes
# `scripts/hooks/` — devkit owns that code, so a type error there is devkit's to fix.
MYPY_SCOPE = ["scripts", "tests"]

# Repo-relative prefixes that are content, not devkit source. `ruff.toml` and
# `pyproject.toml` already exclude these, but a config `exclude` does **not** apply to
# a path passed explicitly on the command line unless `force-exclude` is set — and
# `--changed` passes explicit paths. ruff.toml sets `force-exclude` for that reason;
# this filter is the same guard for mypy, which has no equivalent setting, and it keeps
# `--changed` from spending a pass on files neither tool will report on anyway.
EXCLUDED_PREFIXES = ("templates/",)
MARKDOWN_EXCLUDED_PREFIXES = (".agents/", ".pytest_cache/", "templates/")

# dotenv-linter v4 takes a subcommand; a bare file list is rejected as an
# unrecognised one, which reaches the artifact as a usage error no source edit can
# fix. `--plain` keeps ANSI colour codes out of a file something else has to parse,
# and `--skip-updates` stops the linter making a network call on every run.
#
# UnorderedKey is ignored deliberately, and narrowly — it is the one check here with
# no correctness content. It wants every key alphabetised within its blank-line
# group, which in a generated `.env.example` means DATABASE_URL resequenced after the
# POSTGRES_* components it is built from, ARCHIVE_ROOT after the S3 overrides that
# only apply when it is *not* used, and the host ports shuffled out of service order.
# That grouping is the file's entire documentation value. Per the lint policy in
# `.claude/rules/engineering.md`: a rule that fires on what a formatter would decide
# is misconfigured, so turn it off rather than train everyone to read past it.
DOTENV_CMD = [
    "dotenv-linter",
    "check",
    "--plain",
    "--skip-updates",
    "--ignore-checks",
    "UnorderedKey",
]


def changed_paths() -> list[str]:
    """Every tracked-but-modified plus untracked path, relative to the repo root."""
    tracked = _git("diff", "--name-only", "HEAD")
    untracked = _git("ls-files", "--others", "--exclude-standard")
    return sorted({n for n in (tracked + untracked) if (REPO_ROOT / n).exists()})


def explicit_paths(paths: list[str]) -> list[str]:
    """Exactly the paths given, normalised, minus any that no longer exist.

    `--paths` is how a caller that knows its own scope states it. `/ship` is the
    motivating one: it insists on a clean tree and then had only `--changed` to ask
    with, whose set is the working tree *versus HEAD* — empty, by construction, for
    every commit it was about to push. The gate passed having linted nothing.

    A deleted path is dropped rather than passed on: there is nothing left to lint,
    and ruff/mypy treat a missing argument as a usage error that fails the whole run.
    """
    return sorted({n.replace("\\", "/") for n in paths if (REPO_ROOT / n).exists()})


def python_targets(paths: list[str]) -> list[str]:
    """The lintable .py files among `paths`."""
    return [n for n in paths if n.endswith(".py") and not n.startswith(EXCLUDED_PREFIXES)]


def changed_python_files() -> list[str]:
    """Tracked-but-modified plus untracked .py files, relative to the repo root."""
    return python_targets(changed_paths())


def workflow_files(limit_to: list[str] | None = None) -> list[str]:
    """`.github/workflows/*.yml`, optionally narrowed to a changed-file list.

    Explicit paths rather than a bare `actionlint`, which discovers workflows itself:
    discovery only finds them when the cwd is the repo root, and reports success
    having checked nothing anywhere else. Returning [] when there are none is what
    keeps the pass from turning "no workflows" into a usage error in the artifact.
    """
    found = sorted(
        p.relative_to(REPO_ROOT).as_posix()
        for p in (REPO_ROOT / ".github" / "workflows").glob("*.yml")
    )
    return found if limit_to is None else [p for p in found if p in set(limit_to)]


def env_files(limit_to: list[str] | None = None) -> list[str]:
    """Root-level `.env*` files, optionally narrowed to a changed-file list.

    `.env` itself is gitignored and machine-local, so in practice this is
    `.env.example` — the file every new clone copies, and therefore the one whose
    typos are worth catching. devkit has none of either; the pass is inert here and
    live in any generated project with a Docker tier, which is the point of reading
    the filesystem instead of hardcoding a list.
    """
    found = sorted(p.name for p in REPO_ROOT.glob(".env*") if p.is_file())
    return found if limit_to is None else [p for p in found if p in set(limit_to)]


def markdown_files(limit_to: list[str] | None = None) -> list[str]:
    """Authored Markdown, excluding generated skills and rendered-template content."""
    found = sorted(
        p.relative_to(REPO_ROOT).as_posix()
        for p in REPO_ROOT.rglob("*.md")
        if not any(
            p.relative_to(REPO_ROOT).as_posix().startswith(prefix)
            for prefix in MARKDOWN_EXCLUDED_PREFIXES
        )
        and "node_modules" not in p.parts
        and ".venv" not in p.parts
    )
    return found if limit_to is None else [p for p in found if p in set(limit_to)]


def node_tool(name: str) -> str | None:
    """A project-local Node binary, with the Windows command shim when needed."""
    suffix = ".cmd" if os.name == "nt" else ""
    candidate = REPO_ROOT / "node_modules" / ".bin" / f"{name}{suffix}"
    return str(candidate) if candidate.is_file() else None


def _git(*args: str) -> list[str]:
    result = subprocess.run(["git", "-C", str(REPO_ROOT), *args], capture_output=True, text=True)
    return result.stdout.splitlines() if result.returncode == 0 else []


def _missing_module(cmd: list[str]) -> bool:
    """True when `cmd` is a `-m` invocation of a module this interpreter lacks.

    The linters run as `[sys.executable, "-m", tool, ...]`, so the executable always
    exists and `subprocess.run` never raises FileNotFoundError — the interpreter
    itself exits 1 with "No module named mypy" on stderr. Without this probe that
    text lands in the artifact as an unfixable finding, which is the exact outcome
    run_tool's contract exists to prevent. Probing beats matching the message: the
    subprocess runs under sys.executable, so find_spec here answers for the very
    interpreter that would run it.
    """
    if len(cmd) < 3 or cmd[0] != sys.executable or cmd[1] != "-m":
        return False
    try:
        return importlib.util.find_spec(cmd[2]) is None
    except (ImportError, ValueError):
        return True


def run_tool(name: str, cmd: list[str], fix_hint: str) -> str:
    """Run one linter; return its artifact section, or "" when it passed or was absent.

    A missing tool is NOT a failure. Writing "command not found" into the artifact
    would hand the agent something it cannot fix in the source tree, so it degrades
    to a terminal note instead.
    """
    if _missing_module(cmd):
        _SKIPPED.append(name)
        print(f"  {name}: not installed — skipped")
        return ""
    try:
        result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
    except FileNotFoundError:
        _SKIPPED.append(name)
        print(f"  {name}: not installed — skipped")
        return ""
    if result.returncode == 0:
        print(f"  {name}: ok")
        return ""
    body = (result.stdout + result.stderr).strip()
    print(f"  {name}: FAILED")
    return f"# {name}\n# fix: {fix_hint}\n{body}\n\n"


def main(argv: list[str] | None = None) -> int:
    _SKIPPED.clear()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--changed", action="store_true", help="lint only the working-tree diff")
    parser.add_argument(
        "--paths",
        nargs="+",
        metavar="FILE",
        default=[],
        help="lint exactly these files, scoped as --changed is (used by /ship for the branch diff)",
    )
    # Accepted, and a no-op here: devkit has no detect-secrets pass to skip. The Stop
    # hook passes `--no-secrets` unconditionally — see the same argument in
    # `templates/core/scripts/lint-all.py.tmpl` for why, and why *parsing* it is part
    # of the contract rather than optional politeness.
    parser.add_argument(
        "--no-secrets",
        action="store_true",
        help="skip the secrets pass (accepted for Stop-hook compatibility; no-op here)",
    )
    args = parser.parse_args(argv)

    # `--paths` and `--changed` are the same narrowing, differing only in who names the
    # files; everything downstream treats them identically.
    scoped = args.changed or bool(args.paths)
    selected: list[str] = []
    if scoped:
        selected = explicit_paths(args.paths) if args.paths else changed_paths()
    changed = selected if scoped else None
    targets = python_targets(selected)
    workflows = workflow_files(changed)
    envs = env_files(changed)
    markdown = markdown_files(changed)
    if scoped and not (targets or workflows or envs or markdown):
        print("lint-all: no changed files this run lints; nothing to do.")
        _write_artifact("")
        return 0
    scope = targets or ["."]

    label = f"{len(selected)} file(s)" if scoped else "whole repo"
    print(f"lint-all: {label}")

    sections = ""
    # A narrowed run with only a workflow or `.env` edit leaves `targets` empty, and
    # `scope` then falls back to `["."]` — which would silently widen a per-turn
    # check into a whole-repo pass. Gate the Python passes on having Python to lint.
    if targets or not scoped:
        # Auto-fix first, then report. Both ruff passes mutate the same files, so they
        # must stay sequential relative to each other. No `--exclude` guard here: see the
        # module docstring — devkit formats its own harness, and CI's `ruff format --check`
        # is what would fail if it did not.
        subprocess.run(
            [sys.executable, "-m", "ruff", "check", *scope, "--fix", "--unsafe-fixes"],
            cwd=REPO_ROOT,
            capture_output=True,
        )
        subprocess.run(
            [sys.executable, "-m", "ruff", "format", *scope],
            cwd=REPO_ROOT,
            capture_output=True,
        )

        sections += run_tool(
            "ruff",
            [sys.executable, "-m", "ruff", "check", *scope, "--output-format=full"],
            "ruff check . --fix --unsafe-fixes",
        )
        sections += run_tool(
            "mypy",
            [sys.executable, "-m", "mypy", *(targets or MYPY_SCOPE), "--show-error-codes"],
            f"mypy {' '.join(MYPY_SCOPE)} --show-error-codes",
        )

    # `.claude/hooks/session-start.sh` installs both of these into every session, and
    # until now nothing ever ran them — a tool downloaded on every startup and never
    # invoked. They are real executables rather than `-m` modules, so run_tool's
    # FileNotFoundError branch is what degrades a missing one to a terminal note.
    if workflows:
        sections += run_tool(
            "actionlint",
            ["actionlint", *workflows],
            f"actionlint {' '.join(workflows)}",
        )
    if envs:
        sections += run_tool("dotenv-linter", [*DOTENV_CMD, *envs], " ".join([*DOTENV_CMD, *envs]))
    if markdown:
        if markdownlint := node_tool("markdownlint-cli2"):
            sections += run_tool(
                "markdownlint",
                [markdownlint, *markdown],
                f"{markdownlint} --fix {' '.join(markdown)}",
            )
        else:
            print("  markdownlint: not installed — skipped")
        if remark := node_tool("remark"):
            sections += run_tool(
                "remark",
                [remark, "--frail", "--ignore-path", ".remarkignore", *markdown],
                f"{remark} --output {' '.join(markdown)}",
            )
        else:
            print("  remark: not installed — skipped")

    _write_artifact(sections)

    # Asked before the `clean` line, because a required linter that could not run is not
    # a finding to write into the artifact — it is a statement that the run cannot be
    # believed, and the honest exit code for that is non-zero. Printing `clean` here is
    # exactly the failure this guard exists to stop.
    absent = [name for name in _SKIPPED if name in REQUIRED_TOOLS]
    if absent:
        print(
            f"\nlint-all: NOT CLEAN — {', '.join(absent)} could not be run, so nothing was "
            f"checked for the rules they own. They are declared in pyproject.toml: install "
            f"them (`uv sync --all-extras --all-groups`) or run this under the project's "
            f".venv interpreter."
        )
        return 1

    if sections:
        print(f"\nlint-all: FAILED — details in {ARTIFACT.relative_to(REPO_ROOT)}")
        return 1
    print(f"\nlint-all: clean (artifact cleared: {ARTIFACT.relative_to(REPO_ROOT)})")
    return 0


def _write_artifact(sections: str) -> None:
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    header = "# source: scripts/lint-all.py\n" if sections else ""
    ARTIFACT.write_text(header + sections, encoding="utf-8")


if __name__ == "__main__":
    # Same reasoning as `run-tests.py`: an agent's shell is never an activated one, so
    # resolve the interpreter that holds the tools rather than hoping for a PATH. Without
    # it, this script's most common invocation skipped every linter and printed `clean`.
    _code = project_python.re_exec(REPO_ROOT, "ruff", sys.argv) if project_python else None
    if _code is not None:
        sys.exit(_code)
    sys.exit(main())
