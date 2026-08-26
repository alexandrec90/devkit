#!/usr/bin/env python3
"""Keep this machine's coding-agent CLIs current, at the moments it is safe to.

`claude` and `codex` are the two binaries every session in this workspace runs, and on
this machine neither is an npm package: Claude Code is a native install under
`~/.local/bin`, Codex a signed install under `AppData/Local/Programs/OpenAI`. So the
nightly `global-tools.py` pass -- which reads its whole work list from
`npm outdated --global` -- cannot see either of them, and its
`@anthropic-ai/claude-code` skip entry describes a *different* installation shape that
is not the one here. The two most-run binaries on the machine were the two nothing was
moving.

**Each CLI ships its own updater, and this calls it rather than re-implementing it.**
`claude update` and `codex update` each know their own install layout, release channel
and signing. `codex update` in particular *is* the PowerShell one-liner people paste
(`irm https://chatgpt.com/codex/install.ps1 | iex`): it spawns exactly that line with
`CODEX_NON_INTERACTIVE=1` set, which is what disarms the "launch Codex now?" prompt the
installer ends with. Spelling that one-liner out here would re-implement the subcommand
badly *and* re-arm a prompt in a job nobody is watching.

**An agent that is running is not updated.** On Windows a running image cannot be
replaced, so an update attempted under a live session either fails on a sharing
violation or swaps the binary out from under a process that will need it again. The
check is exact-name -- a process called `claude` or `codex`, never a prefix match. Codex
keeps long-lived helpers (`codex-app-server`, `codex-command-runner`), and a prefix
match would let one of those block every update forever, silently and permanently,
which is a worse failure than the one being avoided. Enumeration that *fails* counts as
running: not knowing is the one state where proceeding is a guess about a running
process.

That constraint is also why this has two callers rather than a schedule of its own.
`resume-sessions.py` runs it just before it opens the tabs, which is the one moment the
user is provably not mid-session and the moment the new binary is about to be used; and
`global-tools.py` runs it at the tail of the 04:30 pass, where most nights nothing is
running and the update lands before the morning's first session. Neither is reliable
alone -- a night with a session left open skips, and a morning that opens a fresh tab
instead of resuming never asks -- and both together cost one scheduled task, which is
the one that already exists.

**`doctor` is recorded on failure, never summarised on success.** Both CLIs ship one,
and both print prose (`No installation issues found.`) that no substring test can
classify: the obvious marker, "issues", appears in the line saying there are none. So
the exit code is the only signal read here, the full report is kept when it is
non-zero, and `--doctor` forces it for a human who wants to look. Guessing at the prose
is how a gate starts reporting its own false positives.

**Being offline is not a failure**, for `global-tools.py`'s reason: a laptop with no
network at 04:30 is the system working, and a job whose alerts fire on the normal case
is a job whose alerts nobody reads.

Read-only by default -- `--yes` is what updates, the same shape as `global-tools.py`
and `upgrade-project.py`. Nothing here spawns a process directly; every function that
would is given a runner, and they are tested in `tests/test_agent_clis.py`.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import PurePath

# Windows gives a console-less parent a brand new console window for every console
# child, so an unattended pass under `pythonw.exe` flickers a window per CLI call
# without this. See `tests/test_scheduled_jobs.py::UNATTENDED`.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Resolved once, at import, so a test can force the Windows path without patching
# `os.name` itself -- `pathlib` reads that at call time, and patching it makes every
# later bare `Path(...)` raise on a POSIX runner.
WINDOWS = os.name == "nt"

# An update downloads and unpacks a signed release. Generous for one CLI and still
# bounded, so a wedged download cannot hold a scheduler slot -- or the tabs the user is
# waiting on -- open on its own.
UPDATE_TIMEOUT = 300

# `--version` and `doctor` are local work. A minute is already pathological.
QUICK_TIMEOUT = 60

# How much of a failed `doctor` to keep. Codex's runs to a few dozen lines of
# environment detail; the verdicts are at the top.
DOCTOR_LINES = 40

# Substrings that make a failed update the network being unreachable rather than a
# broken machine. Deliberately not shared with `global-tools.OFFLINE_MARKERS`: these are
# native binaries in Rust and Node, whose transport errors read nothing like npm's, and
# a marker list that is wrong in either direction turns a real breakage into a silent
# exit 0.
OFFLINE_MARKERS = (
    "dns error",
    "failed to lookup",
    "temporary failure in name resolution",
    "getaddrinfo",
    "connection refused",
    "connection reset",
    "network is unreachable",
    "could not resolve",
    "timed out",
    "offline",
)

# The first `x.y.z` in a `--version` line. Both CLIs bury it in prose of their own --
# `2.1.246 (Claude Code)`, `codex-cli 0.149.1` -- and both spellings have changed
# before, so the number is found rather than positionally sliced.
VERSION_RE = re.compile(r"\b(\d+\.\d+\.\d+(?:[-.][0-9A-Za-z][0-9A-Za-z.]*)?)")

Runner = Callable[[Sequence[str], int], "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class Agent:
    """One coding-agent CLI this machine runs."""

    name: str  # the executable on PATH, what `--agent` takes, and the process name
    label: str  # how the report names it


AGENTS: tuple[Agent, ...] = (
    Agent("claude", "Claude Code"),
    Agent("codex", "Codex"),
)

# What one agent's pass concluded. `failed` is the only one that reddens a caller;
# `skipped`, `absent` and `offline` are all ordinary days.
UPDATED = "updated"
CURRENT = "current"
SKIPPED = "skipped"
ABSENT = "absent"
OFFLINE = "offline"
FAILED = "failed"


@dataclass(frozen=True)
class Outcome:
    """What happened to one CLI this pass."""

    agent: str
    status: str
    before: str = ""
    after: str = ""
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status != FAILED


@dataclass(frozen=True)
class Report:
    """A whole pass, as its callers consume it: lines to print or file, and a verdict."""

    outcomes: tuple[Outcome, ...]
    lines: tuple[str, ...]

    @property
    def failures(self) -> int:
        return sum(1 for outcome in self.outcomes if not outcome.ok)

    @property
    def summary(self) -> str:
        counts = {status: 0 for status in (UPDATED, CURRENT, SKIPPED, ABSENT, OFFLINE, FAILED)}
        for outcome in self.outcomes:
            counts[outcome.status] = counts.get(outcome.status, 0) + 1
        parts = [f"{count} {status}" for status, count in counts.items() if count]
        return ", ".join(parts) if parts else "nothing to do"


def run_command(
    argv: Sequence[str], timeout: int = QUICK_TIMEOUT
) -> subprocess.CompletedProcess[str]:
    """Spawn `argv`, capturing both streams, with no console window and no stdin.

    `stdin=DEVNULL` is load-bearing rather than tidy: an updater that asks a question
    with a terminal attached would hang a scheduled task until its timeout, and both of
    these read a closed stdin as "no".

    A timeout is returned as a `CompletedProcess` rather than raised, because every
    caller here wants to write a line about it and carry on -- a traceback out of an
    unattended job goes nowhere at all.
    """
    try:
        return subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(list(argv), 1, "", f"timed out after {timeout}s")
    except OSError as exc:  # the CLI vanished between `which` and here
        return subprocess.CompletedProcess(list(argv), 1, "", str(exc))


# --- who is running ----------------------------------------------------------


def process_argv(windows: bool = WINDOWS) -> list[str]:
    """The command that lists this machine's process names."""
    if windows:
        return ["tasklist", "/FO", "CSV", "/NH"]
    return ["ps", "-e", "-o", "comm="]


def normalise_process(name: str) -> str:
    """One process name reduced to what it can be compared by: bare stem, lowercased."""
    stem = PurePath(name.strip().strip('"')).name.lower()
    return stem[:-4] if stem.endswith(".exe") else stem


def parse_process_names(payload: str, windows: bool = WINDOWS) -> set[str]:
    """Every running process name, from the platform lister's output.

    `tasklist /FO CSV` quotes each field and a process name may legitimately contain a
    comma, so this goes through `csv` rather than splitting on one.
    """
    if windows:
        rows = [row[0] for row in csv.reader(io.StringIO(payload)) if row]
    else:
        rows = [line for line in payload.splitlines() if line.strip()]
    return {normalise_process(row) for row in rows if row.strip()}


def running_processes(run: Runner = run_command, windows: bool = WINDOWS) -> set[str] | None:
    """Running process names, or `None` when the machine could not be asked.

    `None` is not an empty set and callers must not treat it as one: an unanswerable
    question about running processes is the one state where updating is a guess.
    """
    result = run(process_argv(windows), QUICK_TIMEOUT)
    if result.returncode != 0 or not (result.stdout or "").strip():
        return None
    return parse_process_names(result.stdout, windows)


def is_running(agent: Agent, names: set[str] | None) -> bool:
    """Whether this agent has a live process. Exact name only -- see the module docstring."""
    if names is None:
        return True
    return agent.name in names


# --- versions ----------------------------------------------------------------


def version_of(text: str) -> str:
    """The version in a `--version` line, or "" when it is not shaped like one."""
    match = VERSION_RE.search(text or "")
    return match.group(1) if match else ""


def installed_version(executable: str, run: Runner = run_command) -> str:
    """What `<cli> --version` reports, or "" when it cannot be asked.

    "" is never treated as a version: it makes the before/after comparison decline to
    claim anything, which is what a failed probe should do.
    """
    result = run([executable, "--version"], QUICK_TIMEOUT)
    if result.returncode != 0:
        return ""
    return version_of(result.stdout or result.stderr or "")


def looks_offline(text: str) -> bool:
    """Whether a failed update is the network being unreachable rather than a defect."""
    lowered = (text or "").lower()
    return any(marker in lowered for marker in OFFLINE_MARKERS)


def last_line(text: str) -> str:
    """The last non-empty line of a CLI's output -- where each of these puts its error."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1] if lines else ""


# --- one agent ---------------------------------------------------------------


def update_argv(executable: str) -> list[str]:
    """`<cli> update`. Both CLIs spell it the same way, and both are non-interactive."""
    return [executable, "update"]


def doctor_argv(executable: str) -> list[str]:
    """`<cli> doctor`. Read-only on both, and on Codex it is the slower of the two."""
    return [executable, "doctor"]


def update_one(agent: Agent, executable: str, run: Runner = run_command) -> Outcome:
    """Run one CLI's own updater and say what came of it.

    The version is probed on both sides of the update rather than parsed out of the
    updater's chatter: `codex update` reinstalls the current release when there is
    nothing newer and reports success either way, so "did anything move" is a question
    only the two probes can answer.
    """
    before = installed_version(executable, run)
    result = run(update_argv(executable), UPDATE_TIMEOUT)
    if result.returncode != 0:
        detail = last_line(result.stderr) or last_line(result.stdout) or "the updater failed"
        status = OFFLINE if looks_offline(f"{result.stderr}\n{result.stdout}") else FAILED
        return Outcome(agent.name, status, before, before, detail)
    after = installed_version(executable, run)
    if before and after and before != after:
        return Outcome(agent.name, UPDATED, before, after)
    return Outcome(agent.name, CURRENT, before, after or before)


def doctor_text(executable: str, run: Runner = run_command, lines: int = DOCTOR_LINES) -> str:
    """`<cli> doctor`'s report, trimmed to its head, or "" when it had nothing to say."""
    result = run(doctor_argv(executable), QUICK_TIMEOUT)
    body = (result.stdout or "") + (result.stderr or "")
    kept = [line.rstrip() for line in body.splitlines() if line.strip()][:lines]
    return "\n".join(kept)


def describe(outcome: Outcome) -> str:
    """One agent's line in the report."""
    if outcome.status == UPDATED:
        return f"  updated {outcome.agent} {outcome.before} -> {outcome.after}"
    if outcome.status == CURRENT:
        return f"  current {outcome.agent} {outcome.after or 'version unknown'}"
    if outcome.status == ABSENT:
        return f"  absent  {outcome.agent} ({outcome.detail})"
    if outcome.status == SKIPPED:
        return f"  skipped {outcome.agent} {outcome.before} ({outcome.detail})"
    if outcome.status == OFFLINE:
        return f"  offline {outcome.agent} {outcome.before} ({outcome.detail})"
    return f"  FAILED  {outcome.agent} {outcome.before}: {outcome.detail}"


# --- the pass ----------------------------------------------------------------


def select_agents(names: Sequence[str]) -> tuple[Agent, ...]:
    """The agents named, in `AGENTS` order; all of them for an empty selection."""
    if not names:
        return AGENTS
    wanted = {name.strip().lower() for name in names if name.strip()}
    return tuple(agent for agent in AGENTS if agent.name in wanted)


def run_pass(
    agents: Sequence[Agent] = AGENTS,
    *,
    yes: bool = False,
    doctor: bool = False,
    run: Runner = run_command,
    which: Callable[[str], str | None] = shutil.which,
    processes: set[str] | object | None = None,
) -> Report:
    """Update (or report on) each agent CLI, and return the whole pass.

    `processes` defaults to asking the machine once, for every agent -- an enumeration
    per CLI would be two answers to one question, and they could disagree.
    """
    running = running_processes(run) if processes is None else processes
    names = running if running is None or isinstance(running, set) else None
    outcomes: list[Outcome] = []
    for agent in agents:
        executable = which(agent.name)
        if not executable:
            outcomes.append(Outcome(agent.name, ABSENT, detail="not on PATH"))
            continue
        # Probed inside the branches that report a version rather than once up front:
        # `update_one` takes its own before-and-after readings, and a probe here as well
        # would spawn `<cli> --version` three times to answer one question.
        if is_running(agent, names):
            reason = (
                f"a {agent.name} process is running"
                if names is not None
                else "could not tell what is running"
            )
            version = installed_version(executable, run)
            outcomes.append(Outcome(agent.name, SKIPPED, version, version, reason))
            continue
        if not yes:
            version = installed_version(executable, run)
            outcomes.append(
                Outcome(agent.name, SKIPPED, version, version, "dry run -- pass --yes to update")
            )
            continue
        outcomes.append(update_one(agent, executable, run))

    lines = [describe(outcome) for outcome in outcomes]
    for outcome in outcomes:
        if outcome.status in {ABSENT, SKIPPED}:
            continue
        if not doctor and outcome.ok:
            continue
        executable = which(outcome.agent)
        if not executable:
            continue
        report = doctor_text(executable, run)
        if report:
            lines.append(f"  {outcome.agent} doctor:")
            lines += [f"    {line}" for line in report.splitlines()]
    if not lines:
        lines = ["  no agent CLI to update"]
    return Report(tuple(outcomes), tuple(lines))


def main(
    argv: Sequence[str] | None = None,
    run: Runner = run_command,
    which: Callable[[str], str | None] = shutil.which,
) -> int:
    parser = argparse.ArgumentParser(description="Keep this machine's agent CLIs current.")
    parser.add_argument(
        "--agent",
        dest="agents",
        default="",
        metavar="NAME",
        help="comma-separated: claude, codex (default: both)",
    )
    parser.add_argument(
        "--yes", action="store_true", help="run each CLI's updater (default: report only)"
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="record each CLI's doctor report, not only a failing one",
    )
    options = parser.parse_args(list(argv) if argv is not None else None)

    selected = select_agents([name for name in options.agents.split(",") if name])
    if options.agents and not selected:
        parser.error(f"--agent takes {', '.join(agent.name for agent in AGENTS)}")

    report = run_pass(selected, yes=options.yes, doctor=options.doctor, run=run, which=which)
    print("agent CLIs: " + report.summary)
    for line in report.lines:
        print(line)
    return 1 if report.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
