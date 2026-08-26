#!/usr/bin/env python3
"""Keep this machine's globally-installed npm tooling current, unattended.

**A globally installed package is a pin nothing was moving.** Four of the MCP servers
a session in this workspace talks to -- chrome-devtools, postgres, redis, azure-devops
-- are launched from a global bin rather than through `npx`, so the version that was
installed once, by hand, months ago, is the version every session gets forever. So are
the linters a project can reach (`eslint`, `stylelint`, `markdownlint-cli2`) and the
publisher `vsce`. On the day this was written `npm outdated -g` reported half the
global set behind, one of them by 64 patch releases.

Nothing here decides *which* packages matter. The globally-installed set is read from
npm itself, so a tool installed tomorrow is covered without editing this file -- a
hand-kept list of "the ones we care about" is the thing that goes stale silently, and
the whole failure being fixed is a silent stale pin.

**Two packages are deliberately skipped**, and both for the same reason: they are the
things that would have to still work in order to undo a bad update.

- `npm` itself. It is the tool performing the update; a self-update that fails halfway
  leaves no working npm to roll back with, on a machine with no one watching.
- `@anthropic-ai/claude-code`. It ships its own updater, and it is very often the
  process that launched this job -- two updaters racing over a running binary is not a
  thing to arrange on a schedule.

Each is reported as skipped in the artifact rather than silently omitted, so a reader
who wonders why npm is still behind finds the reason instead of a bug.

**The agent CLIs are a second stage, because npm cannot see them.** `claude` and
`codex` are installed natively on this machine -- `~/.local/bin` and
`AppData/Local/Programs/OpenAI` -- so `npm outdated --global` reports neither, and the
skip above describes an installation shape that is not the one here. `agent_clis.run_pass`
runs at the head of every pass, calling each CLI's own updater and stepping over any
agent that is running; `scripts/agent_clis.py` owns that half and says why it is spelled
that way.

It is **not** a flag, deliberately. The scheduled command is registered once, in a task
document on the machine, and an installer whose argv has to change is one whose already-
registered task keeps doing half the job until somebody remembers to re-run it --
`drifted` compares the script path, so nothing would even report the gap. Folding the
stage into the same argv means the merge that adds it is the whole rollout. The cost is
that this module's tests must stub `agent_clis.run_pass` rather than merely not asking
for it, which `tests/test_global_tools.py` does once, autouse, for the whole file.

**There is no post-update smoke test, on purpose.** The tempting one -- run each
updated package's bin with `--version` -- is a false-positive generator: an MCP stdio
server started with no arguments does not exit, and a package with no `--version` flag
exits non-zero while being perfectly healthy. This repo has already paid for a gate
whose blocks were mostly its own false positives. What the artifact carries instead is
the exact `npm install -g <name>@<old>` line for every package this pass moved, so a
session that breaks the next morning is one copy-paste from the version that worked.

That is also why this artifact **keeps a bounded history** where every other devkit job
overwrites per run. An unpinned auto-update whose only record is erased by the next
night's pass is untraceable by construction: the breakage is noticed days after the
bump that caused it. The last `RUNS_KEPT` runs stay, newest first.

**Being offline is not a failure.** This runs on a laptop at 04:30; a night with no
network is the system working, and a job whose alerts fire on the normal case is a job
whose alerts nobody reads. A registry-unreachable pass is recorded and exits 0. Any
other npm failure exits 1, which is what `schedule_health` reports at session start.

Read-only by default -- `--yes` is what installs, same shape as `upgrade-project.py`.
`scripts/install-global-tools.py` registers exactly `global-tools.py --yes`.

Stdlib only, every decision an importable function, tested in
`tests/test_global_tools.py`.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_clis

REPO_ROOT = Path(__file__).resolve().parents[1]

# Where this pass writes its account of itself. `install-global-tools.ARTIFACT` and
# `schedule_health.ARTIFACTS` both name the same file; `tests/test_scheduled_jobs.py`
# checks the three against each other.
ARTIFACT = Path("logs") / "global-tools.log"

# Windows gives a console-less parent a brand new console window for every console
# child, so an unattended pass under `pythonw.exe` flickers a window per npm call
# without this. See `tests/test_scheduled_jobs.py::UNATTENDED`.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# How many passes the artifact keeps. A month of nightly runs -- long enough that "it
# broke sometime last week" is still answerable, short enough that the file stays a
# thing someone reads rather than greps.
RUNS_KEPT = 30

# Every run's report starts with this. `trim` splits on it, so it must not appear at
# the start of any other line the report emits.
RUN_MARKER = "=== "

# Packages this pass never touches, and why. See the module docstring.
SKIP: dict[str, str] = {
    "npm": "the tool doing the updating -- a half-finished self-update leaves nothing to roll back with",
    "@anthropic-ai/claude-code": "ships its own updater, and is usually the process that launched this job",
}

# Substrings that make an npm failure a network failure rather than a broken machine.
# Kept small and specific: a wrong guess here turns a real breakage into a silent
# exit 0, which is the failure mode this whole job exists to remove.
OFFLINE_MARKERS = (
    "ENOTFOUND",
    "EAI_AGAIN",
    "ETIMEDOUT",
    "ECONNREFUSED",
    "ENETUNREACH",
    "ECONNRESET",
    "network",
)

# A global install pulls a whole dependency tree over the network. Five minutes is
# generous for one package and still bounded, so a wedged install cannot hold the
# scheduler's one-hour slot open on its own.
INSTALL_TIMEOUT = 300

# `npm outdated` exits 1 when it found something outdated, which is the successful
# case. Only stdout that fails to parse tells the two apart.
OUTDATED_TIMEOUT = 180

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]

# The agent-CLI stage, taken as an argument so this module's own tests cannot spawn a
# real updater by forgetting to stub one. `agent_clis.run_pass` is the only production
# value it ever takes.
AgentPass = Callable[..., agent_clis.Report]


@dataclass(frozen=True)
class Behind:
    """One globally-installed package with a newer release available."""

    name: str
    current: str
    latest: str


@dataclass(frozen=True)
class Outcome:
    """What happened to one package this pass."""

    name: str
    current: str
    latest: str
    ok: bool
    detail: str = ""

    @property
    def rollback(self) -> str:
        return f"npm install -g {self.name}@{self.current}"


def run_command(
    argv: Sequence[str], timeout: int = OUTDATED_TIMEOUT
) -> subprocess.CompletedProcess[str]:
    """Spawn `argv`, capturing both streams, with no console window.

    A timeout is reported as a `CompletedProcess` rather than raised: every caller here
    wants to write a line about it and carry on, and a traceback out of an unattended
    job goes nowhere at all.
    """
    try:
        return subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(list(argv), 1, "", f"timed out after {timeout}s")
    except OSError as exc:  # npm vanished between `which` and here
        return subprocess.CompletedProcess(list(argv), 1, "", str(exc))


def npm_executable(which: Callable[[str], str | None] = shutil.which) -> str | None:
    """The `npm` this machine has, or `None`.

    Resolved to a path rather than run as a bare name: on Windows npm is `npm.CMD`, and
    a shell-less spawn of a bare `npm` finds nothing.
    """
    return which("npm")


def parse_outdated(payload: str) -> list[Behind]:
    """The packages `npm outdated -g --json` says are behind, sorted by name.

    Three shapes have to survive here. npm maps a name to one object normally and to a
    *list* of them when a package is present more than once; and an entry with no
    `current` is npm's spelling of "declared but not installed", which is not a thing
    this job can update. Anything unparseable is not this function's error to raise --
    the caller distinguishes "no JSON" from "empty JSON", because only one of them is a
    failure.
    """
    try:
        data = json.loads(payload or "{}")
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []
    found: list[Behind] = []
    for name, value in data.items():
        entries = value if isinstance(value, list) else [value]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            current, latest = entry.get("current"), entry.get("latest")
            if not current or not latest or current == latest:
                continue
            found.append(Behind(name, str(current), str(latest)))
            break
    return sorted(found, key=lambda item: item.name)


def looks_offline(stderr: str) -> bool:
    """Whether an npm failure is the registry being unreachable rather than a defect."""
    lowered = stderr.lower()
    return any(marker.lower() in lowered for marker in OFFLINE_MARKERS)


def partition(
    entries: Sequence[Behind], skip: dict[str, str] | None = None
) -> tuple[list[Behind], list[tuple[Behind, str]]]:
    """`(to update, [(skipped, reason)])` -- the two lists the report is built from."""
    rules = SKIP if skip is None else skip
    updating, skipped = [], []
    for entry in entries:
        reason = rules.get(entry.name)
        if reason:
            skipped.append((entry, reason))
        else:
            updating.append(entry)
    return updating, skipped


def install_argv(npm: str, entry: Behind) -> list[str]:
    """The exact version, never `@latest`.

    `npm outdated` and the install are two round trips, and a release landing between
    them would install a version this pass never reported -- so the artifact's rollback
    line would name a jump that did not happen.
    """
    return [npm, "install", "--global", f"{entry.name}@{entry.latest}"]


def update_one(npm: str, entry: Behind, run: Runner) -> Outcome:
    """Install one package's latest, and say what happened."""
    result = run(install_argv(npm, entry))
    if result.returncode == 0:
        return Outcome(entry.name, entry.current, entry.latest, True)
    detail = (result.stderr or result.stdout or "").strip().splitlines()
    return Outcome(
        entry.name, entry.current, entry.latest, False, detail[-1] if detail else "npm failed"
    )


def render(
    stamp: str,
    outcomes: Sequence[Outcome],
    skipped: Sequence[tuple[Behind, str]] = (),
    note: str = "",
    agents: Sequence[str] = (),
) -> str:
    """One pass's block of the artifact.

    Every updated package gets its rollback command on its own line, because that is
    the line a reader has come here for: the session broke this morning, and the
    question is which version it was working on yesterday.

    The agent-CLI stage's lines arrive already formatted, under a heading of their own:
    they are versions this pass moved, but not npm packages, and a `claude` line loose
    among the npm ones reads as a global package that does not exist.
    """
    lines = [f"{RUN_MARKER}{stamp}"]
    if note:
        lines.append(note)
    for outcome in outcomes:
        if outcome.ok:
            lines.append(f"  updated {outcome.name} {outcome.current} -> {outcome.latest}")
            lines.append(f"    roll back with: {outcome.rollback}")
        else:
            lines.append(
                f"  FAILED  {outcome.name} {outcome.current} -> {outcome.latest}: {outcome.detail}"
            )
    for entry, reason in skipped:
        lines.append(f"  skipped {entry.name} {entry.current} -> {entry.latest} ({reason})")
    if not outcomes and not skipped and not note:
        lines.append("  every global package is current")
    if agents:
        lines.append("  agent CLIs:")
        lines += [f"  {line}" for line in agents]
    return "\n".join(lines) + "\n"


def trim(existing: str, kept: int = RUNS_KEPT) -> str:
    """The previous artifact with only its most recent `kept - 1` passes left.

    Split on `RUN_MARKER` at the start of a line, so a package name or an npm error
    containing the marker text cannot cut a run in half.
    """
    if kept <= 1 or not existing.strip():
        return ""
    blocks: list[str] = []
    for line in existing.splitlines(keepends=True):
        if line.startswith(RUN_MARKER):
            blocks.append(line)
        elif blocks:
            blocks[-1] += line
    return "".join(blocks[: kept - 1])


def write_artifact(block: str, root: Path | None = None, kept: int = RUNS_KEPT) -> Path:
    """Prepend this pass to the artifact, newest first, and return its path."""
    path = (root or REPO_ROOT) / ARTIFACT
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(block + trim(previous, kept), encoding="utf-8")
    return path


def outdated_entries(npm: str, run: Runner) -> tuple[list[Behind], str]:
    """`(behind, problem)` -- `problem` empty when the query itself succeeded.

    `npm outdated` exits 1 *because* it found something, so the return code says
    nothing. Parseable JSON on stdout is the only signal that the query ran.
    """
    result = run([npm, "outdated", "--global", "--json"])
    stdout = (result.stdout or "").strip()
    try:
        json.loads(stdout or "{}")
    except json.JSONDecodeError:
        stderr = (result.stderr or stdout or "npm produced no output").strip()
        return [], stderr
    return parse_outdated(stdout), ""


def main(
    argv: Sequence[str] | None = None,
    run: Runner | None = None,
    root: Path | None = None,
    now: Callable[[], _dt.datetime] = _dt.datetime.now,
    which: Callable[[str], str | None] = shutil.which,
    agent_pass: AgentPass | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Keep globally-installed npm tooling current.")
    parser.add_argument(
        "--yes", action="store_true", help="install the updates (default: report only)"
    )
    parser.add_argument("--kept", type=int, default=RUNS_KEPT, help="passes the artifact keeps")
    options = parser.parse_args(list(argv) if argv is not None else None)

    runner: Runner = run or (lambda command: run_command(command, INSTALL_TIMEOUT))
    stamp = now().strftime("%Y-%m-%d %H:%M")

    # Before npm, and outside every early return below: the agent CLIs are installed
    # natively here, so nothing about their pass depends on npm being present or the
    # registry being reachable. Running it after the npm lookup would tie the two
    # together, and the npm-missing branch would silently skip it.
    report = (agent_pass or agent_clis.run_pass)(yes=options.yes)
    agents, agent_failures, agent_summary = report.lines, report.failures, report.summary

    npm = npm_executable(which)
    if not npm:
        path = write_artifact(
            render(stamp, [], note="npm is not on PATH -- nothing to do", agents=agents),
            root,
            options.kept,
        )
        print(f"global-tools: npm not found; see {path}", file=sys.stderr)
        return 1

    behind, problem = outdated_entries(npm, runner)
    if problem:
        offline = looks_offline(problem)
        note = (
            "registry unreachable" if offline else "npm outdated failed"
        ) + f": {problem.splitlines()[-1]}"
        path = write_artifact(render(stamp, [], note=note, agents=agents), root, options.kept)
        print(f"global-tools: {note} (see {path})", file=sys.stderr)
        return 1 if agent_failures or not offline else 0

    updating, skipped = partition(behind)
    if not options.yes:
        outcomes = [
            Outcome(item.name, item.current, item.latest, True, "dry run") for item in updating
        ]
        note = "dry run -- pass --yes to install" if updating else ""
        path = write_artifact(render(stamp, outcomes, skipped, note, agents), root, options.kept)
        print(f"global-tools: {len(updating)} behind, nothing installed; see {path}")
        return 1 if agent_failures else 0

    outcomes = [update_one(npm, item, runner) for item in updating]
    path = write_artifact(render(stamp, outcomes, skipped, agents=agents), root, options.kept)
    failed = [item for item in outcomes if not item.ok]
    updated = len(outcomes) - len(failed)
    tail = f"; agent CLIs: {agent_summary}" if agent_summary else ""
    print(
        f"global-tools: {updated} updated, {len(failed)} failed, "
        f"{len(skipped)} skipped{tail}; see {path}"
    )
    return 1 if failed or agent_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
