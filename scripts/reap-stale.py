#!/usr/bin/env python3
"""Reap what agent sessions leave behind: idle phone sessions, stray servers, orphaned
dev servers.

The job this exists because of, from a 16 GB desktop on 2026-09-07: seven agents were
working and the machine was paging. Eleven of the twenty-one gigabytes the agents owned
were not theirs -- five Remote Control sessions nobody had touched since breakfast, six
`remote-control` servers where three were configured, and four `vite` servers from
agent runs two days earlier. Nothing on the machine had the job of noticing any of it.
`reap_machine` names the three kinds and the test each one gets; this is the pass that
applies them, and the only one of the two the scheduler names.

**Read-only by default, and the record outlives the pass.** `status` reports every
finding with its verdict and touches nothing; `reap` and `maintain` act. The artifact
`logs/reap-stale.log` is rewritten every fire so its mtime says the job is alive, and
every process actually stopped is also appended to `logs/reap-stale.history.log`, which
is never rewritten -- fifteen minutes later the artifact describes a quiet machine, and
"what killed my dev server at 15:40" needs an answer that is still there at 16:00.

**Nothing here touches a session a person is in.** An interactive `claude` has no
`--sdk-url` and is never a candidate; a spawned session whose transcript moved inside
`sessionIdleMinutes` is kept; a server the state file knows is `rc-servers.py`'s to
restart, not this job's to stop; a dev server whose ancestry reaches a living editor,
terminal or agent is owned however old it is. The settings live beside
`devkit.remoteControl` in the workspace file:

    "devkit.reapStale": {"sessionIdleMinutes": 120, "devServers": true}

Stdlib only, and every decision is an importable function tested in
`tests/test_reap_stale.py`.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devkit_jsonc
import rc_config
import rc_machine
import reap_machine
import sweep

REPO_ROOT = Path(__file__).resolve().parents[1]

# Rewritten every pass; its mtime is the liveness signal `tests/test_scheduled_jobs.py`
# exists for. `install-reap-schedule.py` advertises the same path to `schedule_health`.
ARTIFACT = Path("logs/reap-stale.log")

# Append-only, one line per process stopped. Never rewritten, for the docstring's reason.
HISTORY = Path("logs/reap-stale.history.log")

# Where `rc-servers.py` records the servers it started; a named server absent from it is
# a stray. Read, never written -- that file is the other job's.
RC_STATE = Path("logs/rc-servers.state.json")

SETTING = "devkit.reapStale"

# Two hours. A phone conversation that has been silent that long is one the person has
# put down; the transcript survives the process and `claude --resume` brings it back.
# Longer than `rc_config.DEFAULT_IDLE_MINUTES` by design -- that one gates a *restart*
# that resumes every session, this one gates a stop that does not.
DEFAULT_SESSION_IDLE_MINUTES = 120


@dataclass(frozen=True)
class Settings:
    session_idle_minutes: int = DEFAULT_SESSION_IDLE_MINUTES
    dev_servers: bool = True
    dev_server_pattern: str = reap_machine.DEV_SERVER_PATTERN


def parse_settings(text: str) -> Settings:
    """Read `SETTING` out of a workspace file, defaults for anything missing or malformed.

    Same leniency as `rc_config.parse_config`, for its reason: a scheduled task whose
    stdout goes nowhere must not crash on a hand-edited file. `devServers` is a bare
    bool only, so a string cannot switch a reap on.
    """
    try:
        payload = devkit_jsonc.loads(text)
    except (json.JSONDecodeError, TypeError):
        return Settings()
    settings = payload.get("settings") if isinstance(payload, dict) else None
    raw = settings.get(SETTING) if isinstance(settings, dict) else None
    if not isinstance(raw, dict):
        return Settings()
    idle = raw.get("sessionIdleMinutes")
    pattern = raw.get("devServerPattern")
    return Settings(
        session_idle_minutes=(
            idle
            if isinstance(idle, int) and not isinstance(idle, bool) and idle > 0
            else DEFAULT_SESSION_IDLE_MINUTES
        ),
        dev_servers=raw.get("devServers") is not False,
        dev_server_pattern=(
            pattern if isinstance(pattern, str) and pattern else reap_machine.DEV_SERVER_PATTERN
        ),
    )


@dataclass
class Pass:
    """What one run did, as the artifact and the exit code consume it."""

    lines: list[str] = field(default_factory=list)
    failures: int = 0

    def say(self, line: str) -> None:
        self.lines.append(line)

    def fail(self, line: str) -> None:
        self.lines.append(line)
        self.failures += 1


@dataclass(frozen=True)
class Finding:
    """One candidate, its verdict, and why. `reap` is the verdict; `label` is how the
    artifact names it; `reason` is the evidence in a reader's words."""

    row: reap_machine.Process
    label: str
    reason: str
    reap: bool


@dataclass
class Plan:
    """Everything one pass operates on, resolved once."""

    table: list[reap_machine.Process]
    store: Path
    settings: Settings
    root: Path | None = None  # the directory the checkouts live in, when known
    projects: tuple[str, ...] = ()
    known: frozenset[int] = frozenset()  # server pids `rc-servers.py` owns
    now: float = field(default_factory=time.time)

    def table_row(self, pid: int) -> reap_machine.Process:
        for row in self.table:
            if row.pid == pid:
                return row
        raise KeyError(pid)


# --- the three assessments --------------------------------------------------------


def assess_sessions(plan: Plan) -> list[Finding]:
    """Every spawned session, idle ones marked for reaping."""
    findings = []
    for session in reap_machine.sessions(plan.table):
        project = plan.root / session.project if plan.root else None
        label = f"session {session.session_id} (pid {session.row.pid}, {session.project})"
        latest = reap_machine.session_activity(session.session_id, project, plan.store)
        if latest is None:
            findings.append(Finding(session.row, label, "activity unknown", False))
            continue
        minutes = int((plan.now - latest) // 60)
        idle = minutes >= plan.settings.session_idle_minutes
        findings.append(Finding(session.row, label, f"idle {minutes} min", idle))
    return findings


def assess_strays(plan: Plan, session_findings: Sequence[Finding]) -> list[Finding]:
    """Named servers the state file does not own, reapable once nothing live is under them.

    A stray with an active session is kept whole: `taskkill /T` on the server takes the
    session with it, and the session's own verdict is the one that matters. Only a
    project the workspace serves is ever a stray -- a server for some other project was
    started by hand, and by-hand is not this job's.
    """
    verdicts = {finding.row.pid: finding.reap for finding in session_findings}
    findings = []
    for pid, name in sorted(reap_machine.daemons(plan.table).items()):
        if pid in plan.known or name not in plan.projects:
            continue
        children = [row.pid for row in plan.table if row.ppid == pid and row.pid in verdicts]
        active = [child for child in children if not verdicts[child]]
        label = f"stray server {name} (pid {pid}, not in rc-servers state)"
        if active:
            reason = f"{len(active)} active session(s) under it"
        elif children:
            reason = f"{len(children)} idle session(s) under it"
        else:
            reason = "no sessions under it"
        findings.append(Finding(plan.table_row(pid), label, reason, not active))
    return findings


def assess_dev_servers(plan: Plan) -> list[Finding]:
    """Orphaned dev servers, every one of which is reapable: the test *is* the verdict."""
    if not plan.settings.dev_servers:
        return []
    findings = []
    for row in reap_machine.dev_servers(plan.table, plan.settings.dev_server_pattern):
        argv = reap_machine.argv_of(row.cmdline)
        shown = " ".join(re.split(r"[\\/]", token.strip('"'))[-1] for token in argv[:4])
        label = f"dev server pid {row.pid} `{shown}`"
        findings.append(Finding(row, label, "no living owner above it", True))
    return findings


def assess(plan: Plan) -> list[Finding]:
    """Every finding, sessions first so a stray's verdict can read theirs.

    A session under a stray that is itself being reaped is reported but not acted on
    twice: `act` skips a row whose ancestor is also on the list.
    """
    session_findings = assess_sessions(plan)
    return [*session_findings, *assess_strays(plan, session_findings), *assess_dev_servers(plan)]


# --- acting ----------------------------------------------------------------------


Stopper = Callable[[int], str]


def act(
    plan: Plan,
    findings: Sequence[Finding],
    report: Pass,
    stop: Stopper,
    history: Path | None,
    when: _dt.datetime,
) -> int:
    """Stop every finding marked for reaping. The count stopped.

    A row under another row on the list is left to that one's `/T`; reporting it as
    "with <parent>" rather than stopping it separately is what keeps the history honest
    about how many processes the pass actually ended.
    """
    reaping = {finding.row.pid for finding in findings if finding.reap}
    stopped = 0
    for finding in findings:
        if not finding.reap:
            report.say(f"{finding.label}: {finding.reason} -- kept")
            continue
        covered = [
            row for row in reap_machine.ancestors(plan.table, finding.row.pid) if row.pid in reaping
        ]
        if covered:
            report.say(f"{finding.label}: {finding.reason} -- with pid {covered[0].pid}")
            continue
        error = stop(finding.row.pid)
        if error:
            report.fail(f"{finding.label}: {finding.reason} -- could not stop: {error}")
            continue
        stopped += 1
        report.say(f"{finding.label}: {finding.reason} -- reaped")
        if history is not None:
            append_history(
                history, f"{when.isoformat(timespec='seconds')} {finding.label}: {finding.reason}"
            )
    return stopped


def append_history(path: Path, line: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        # The reap already happened; a history line that could not be written is not a
        # reason to report the reap as failed, and the artifact still carries it.
        return


def describe(findings: Sequence[Finding], report: Pass) -> None:
    """`status`: every finding with the verdict it *would* get."""
    for finding in findings:
        verdict = "would reap" if finding.reap else "kept"
        report.say(f"{finding.label}: {finding.reason} -- {verdict}")


# --- the artifact ------------------------------------------------------------------


def render(lines: Sequence[str], failures: int, when: _dt.datetime, mode: str) -> str:
    head = f"# reap-stale {when.isoformat(timespec='seconds')} [{mode}] -- {failures} failure(s)"
    return "\n".join([head, *lines, ""])


def write_artifact(text: str, root: Path = REPO_ROOT) -> None:
    path = root / ARTIFACT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def build_plan(
    table: list[reap_machine.Process],
    workspace: Path | None,
    root: Path,
    report: Pass,
    store: Path | None = None,
) -> Plan:
    """Resolve the workspace file and the state file into a `Plan`.

    A missing workspace is reported and the pass goes on with what needs no workspace:
    dev servers, and sessions whose store directory carries their id. Strays need the
    served-project list, so none are found without one -- reported, not assumed.
    """
    plan = Plan(
        table=table,
        store=rc_machine.sessions_store() if store is None else store,
        settings=Settings(),
        known=frozenset(rc_machine.State.load(root / RC_STATE).servers.values()),
    )
    if workspace is None or not workspace.is_file():
        report.say(f"no workspace file at {workspace} -- no served projects, no stray servers")
        return plan
    text = workspace.read_text(encoding="utf-8")
    plan.settings = parse_settings(text)
    plan.root = workspace.parent
    plan.projects = rc_config.parse_config(text).projects
    return plan


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "mode",
        nargs="?",
        default="status",
        choices=("status", "reap", "maintain"),
        help=(
            "status: report what would be reaped (default). reap: stop it. maintain: what "
            "the scheduler runs -- the same as reap, named so a changed default cannot "
            "silently make the job a no-op."
        ),
    )
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument(
        "--devkit",
        type=Path,
        default=REPO_ROOT,
        help="the devkit checkout to write the artifact under (default: this one)",
    )
    return parser.parse_args(sys.argv[1:] if argv is None else argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.devkit.expanduser().resolve()
    workspace = args.workspace or sweep.default_workspace(root)
    now = _dt.datetime.now()
    report = Pass()

    def finish(code: int) -> int:
        text = render(report.lines, report.failures, now, args.mode)
        write_artifact(text, root)
        print(text, end="")
        return code

    table = reap_machine.process_table()
    if table is None:
        report.fail("the process table could not be read -- nothing assessed")
        return finish(2)

    plan = build_plan(table, Path(workspace) if workspace else None, root, report)
    findings = assess(plan)
    if not findings:
        report.say("nothing left behind")
    if args.mode == "status":
        describe(findings, report)
        return finish(0)
    stopped = act(plan, findings, report, reap_machine.stop_tree, root / HISTORY, now)
    report.say(f"stopped {stopped} process tree(s)")
    return finish(2 if report.failures else 0)


if __name__ == "__main__":
    sys.exit(main())
