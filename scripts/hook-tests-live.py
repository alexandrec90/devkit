#!/usr/bin/env python3
"""Run the paid live-CLI hook smokes and write failures to a parseable artifact.

The backend for the two rows marked PAID on "Test: Run Suite"'s menu -- one per CLI,
where the argument this takes used to be a picker on a task of its own -- and the
deliberate counterpart to `scripts/hook-tests.py`. The free script runs the vendored
tier's pure-function tests in any checkout; this one launches a **real, authenticated
agent CLI** against a throwaway project and proves the hooks change what the agent
actually does. Only devkit has those tests -- they live in `tests/`, which is never
vendored -- so the action is scoped to this repo alone.

Three things this exists to stop, all of which a bare `pytest -m paid` does silently:

- **A skip reported as a pass.** Both suites call `pytest.skip` when their CLI is not on
  `PATH`, which is right for a test and wrong for a one-click task: the terminal goes
  green having launched nothing. The `paid` marker compounds it -- `addopts` in
  `pyproject.toml` deselects it by default, so a forgotten `-m` is also a green no-op.
  Every run here therefore reports how many tests actually executed, and a run that
  executed none is a failure.
- **Spending without being told.** A `detail` string in the task list is read once. The
  preflight prints the model, effort and budget of every suite it is about to launch,
  before it launches anything.
- **Spending on the wrong model.** `tests/live_cost.py` owns the cheapest tier for each
  CLI, and this script does not merely read it -- it **exports** the resolved values into
  the environment pytest inherits. That closes the gap between what the preflight said
  and what the CLI does: with the model named explicitly on the command line and in the
  environment, no workstation settings file, profile default or shell export can
  substitute a pricier one.

The values themselves are still not spelled in this file. `--model` and `--effort` override
them for one run; everything else comes from `tests/live_cost.py`, which is the only copy.
The Codex suite writes its model and reasoning settings only into the throwaway
`CODEX_HOME` it launches; this runner never writes the operator's user configuration.

**Runs with the cwd set to the chosen checkout**, like every dispatched action, so the
target repo is `Path.cwd()`.

Usage:
    python scripts/hook-tests-live.py                      # the Claude suite, the cheapest run
    python scripts/hook-tests-live.py codex
    python scripts/hook-tests-live.py both
    python scripts/hook-tests-live.py claude --model sonnet --effort medium
"""

from __future__ import annotations

import argparse
import dataclasses
import importlib.util
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

ARTIFACT = Path("logs") / "hook-test-live-failures.log"

# The single source of truth for what a live run costs. Loaded by path rather than
# imported, because this script runs with an arbitrary checkout as its root.
COST_MODULE = "tests/live_cost.py"

# Same cap and reasoning as `hook-tests.py`: past this it is one traceback repeated,
# not more findings. Larger there because `-s` lets the CLI's own output through.
MAX_LINES = 400


@dataclass(frozen=True)
class Suite:
    """One live suite: its tests, the binary it needs, and the marker that selects it."""

    key: str
    path: str  # relative to the checkout, so the cwd contract holds
    binary: str  # what must be on PATH for it to do anything
    marker: str  # the pytest marker selecting it, paired with `paid`


SUITES: dict[str, Suite] = {
    "claude": Suite(
        key="claude",
        path="tests/test_claude_hooks_live.py",
        binary="claude",
        marker="claude_live",
    ),
    "codex": Suite(
        key="codex",
        path="tests/test_codex_hooks_live.py",
        binary="codex",
        marker="codex_live",
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


def load_cost_module(root: Path) -> ModuleType:
    """Load `tests/live_cost.py` from the checkout under test.

    By path, not by import: the runner's own `sys.path` belongs to whichever checkout
    invoked it, and `--root` exists so the tests can aim it somewhere else. Loading the
    module that ships beside the suites is the only way the printed cost is guaranteed to
    be the cost those suites will resolve.
    """
    path = root / COST_MODULE
    spec = importlib.util.spec_from_file_location("devkit_live_cost", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    # Registered *before* executing, not as a cache. `live_cost` uses `@dataclass` under
    # `from __future__ import annotations`, so the decorator resolves its own field
    # annotations by looking the defining module up in `sys.modules` by name — and an
    # unregistered module makes that lookup return None and the import die inside
    # `dataclasses`. A loader that skips this works on plain modules and fails on the
    # first dataclass, which is why it is a line of code and not a habit.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def required_paths(suites: list[Suite]) -> list[str]:
    """Every file this run needs from the checkout, suites plus the cost table."""
    return [suite.path for suite in suites] + [COST_MODULE]


def missing_suites(suites: list[Suite], root: Path) -> list[str]:
    """Required files that are absent — i.e. this checkout is not devkit."""
    return [path for path in required_paths(suites) if not (root / path).is_file()]


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


def resolve_costs(suites: list[Suite], cost_module: ModuleType, model=None, effort=None) -> dict:
    """What each suite will spend, after the environment and then this run's overrides.

    `--model` is refused for more than one suite by the caller, because the CLIs do not
    share a model namespace and one name cannot be right for both. `--effort` is safe to
    apply across suites: both accept the same low/medium/high vocabulary.
    """
    costs = {}
    for suite in suites:
        cost = cost_module.resolve(suite.key)
        overrides = {}
        if model:
            overrides["model"] = model
        if effort:
            overrides["effort"] = effort
        if overrides:
            cost = dataclasses.replace(cost, **overrides)
        costs[suite.key] = cost
    return costs


def child_env(costs: dict, cost_module: ModuleType, base=None) -> dict[str, str]:
    """The environment pytest is launched with: the caller's, plus the resolved cost.

    Exporting rather than trusting the defaults is the point. It means the numbers in the
    preflight are the numbers the CLI is invoked with even when a shell export, a profile
    or a settings file says otherwise, and it makes `--model` work without every suite
    needing to grow a flag.
    """
    env = dict(os.environ if base is None else base)
    for key, cost in costs.items():
        env.update(cost_module.as_env(key, cost))
    return env


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


def unexercised(suites: list[Suite]) -> list[str]:
    """The suite keys this run did not launch, in `ORDER`."""
    ran = {suite.key for suite in suites}
    return [key for key in ORDER if key not in ran]


def pass_line(suites: list[Suite], executed: int) -> str:
    """The one line a passing run prints -- and it has to name what it did *not* cover.

    This used to read `passed, N live test(s)`, which is a clean bill of health for the
    harness as a whole. It is not one. The default suite here is `claude`, the cheapest,
    so the run that reads as proving the hooks work is precisely the run that never
    launched Codex -- while the failures worth catching are translation failures, which
    only a Codex session can produce. A green line naming no gap is how a real Codex
    defect coexisted with a passing task for months.

    The free `hook-tests.py` tier now carries a static translation gate
    (`scripts/hooks/tests/test_codex_translation.py`), so an unexercised `codex` here is
    no longer *nothing* -- but it is still not a live session, and this line must not
    let the two be confused.
    """
    ran = ", ".join(suite.key for suite in suites)
    missing = unexercised(suites)
    if not missing:
        return f"hook-tests-live: passed, {executed} live test(s) across {ran} — artifact cleared"
    names = ", ".join(missing)
    return (
        f"hook-tests-live: passed, {executed} live test(s) in {ran} — "
        f"NOT exercised: {names}. This run is no evidence about {names}; "
        f"re-run as `{' '.join(missing)}` or `both` for that."
    )


def preflight_report(suites: list[Suite], costs: dict, cost_module: ModuleType) -> str:
    """What is about to be launched and what it costs. Printed before anything spends."""
    lines = ["hook-tests-live: PAID — this launches real, authenticated CLI sessions."]
    for suite in suites:
        cost = costs[suite.key]
        knobs = ", ".join(cost_module.ENV_VARS[suite.key].values())
        lines.append(f"  {suite.key:6} {cost.summary()}")
        lines.append(f"         {suite.path}")
        lines.append(f"         exported to the run; override with --model/--effort or {knobs}")
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


def build_parser() -> argparse.ArgumentParser:
    """The CLI, as its own function so a caller can check flags against the real parser.

    A task that offers `--reasoning=medium` where the script spells it `--effort` fails
    only when someone picks that option — in a paid run, after the session that got as
    far as argparse has already been charged for. Anything that surfaces these flags
    should compare against this parser, not restate it: a restated copy keeps passing
    through exactly the rename it exists to catch.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "suite",
        nargs="?",
        default="claude",
        choices=["claude", "codex", "both"],
        help="which live suite to run (default: claude, the cheapest)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="override the model for this run; only valid for a single suite, since the "
        "two CLIs do not share a model namespace",
    )
    parser.add_argument(
        "--effort",
        default=None,
        choices=["low", "medium", "high"],
        help="override the reasoning effort for this run (default: the lowest each CLI takes)",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="checkout to test (default: the cwd, which is how the task invokes it)",
    )
    return parser


def refusal(suites: list[Suite], root: Path, model: str | None) -> str | None:
    """The reason this run must not start, or None — `main` prints it and exits 2.

    Loud, not a clean skip, the same rule `hook-tests.py` follows. The live suites are
    devkit-only by design (`tests/` is never vendored), so a checkout without them is a
    mis-scoped action to report rather than a pass to hand back; a CLI that is not on
    PATH would make pytest skip, and a skipped paid smoke is a green run that proved
    nothing.
    """
    if model and len(suites) > 1:
        return (
            "hook-tests-live: --model needs a single suite — 'haiku' is not a codex model "
            "and 'gpt-5.6-luna' is not a Claude one. Run each suite in turn to override both."
        )
    absent = missing_suites(suites, root)
    if absent:
        return (
            f"hook-tests-live: {root.name} has no {', '.join(absent)} — the live smokes "
            f"live in devkit's own tests/, which is not vendored. Nothing to run here."
        )
    unavailable = missing_binaries(suites)
    if unavailable:
        return (
            f"hook-tests-live: {', '.join(unavailable)} not on PATH. The suite would skip, "
            f"and a skipped paid smoke is a green run that proved nothing."
        )
    return None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = (args.root or Path.cwd()).resolve()
    suites = resolve_suites(args.suite)

    reason = refusal(suites, root, args.model)
    if reason:
        print(reason, file=sys.stderr)
        return 2

    cost_module = load_cost_module(root)
    costs = resolve_costs(suites, cost_module, model=args.model, effort=args.effort)
    print(preflight_report(suites, costs, cost_module))

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
    result = subprocess.run(
        cmd,
        cwd=root,
        capture_output=True,
        text=True,
        env=child_env(costs, cost_module),
    )
    output = result.stdout + result.stderr

    executed = ran_count(output)
    if result.returncode == 0 and executed:
        (root / ARTIFACT).parent.mkdir(parents=True, exist_ok=True)
        (root / ARTIFACT).write_text("", encoding="utf-8")
        print(pass_line(suites, executed))
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
