#!/usr/bin/env python3
"""Run the fix pass for several iterations and check, mechanically, that it held its contract.

The `supervise-fix-pass` skill's legwork. Each iteration runs the pass the way the
scheduler does -- through `fix-pass-watchdog.py` -- then waits for every session that
iteration dispatched to finish, and checks what the record and the ledgers show against
the invariant the pass is built on: every observation ends green, in flight, or filed.

What is checked, per iteration (`check_record`, `check_sessions`, `check_progress`):

- **The record names no person as the next step** -- no "needs a human", no "read the
  record", no bare `FAILED` that nothing filed.
- **Every waiting decision waits on something tracked**: already dispatched, escalated,
  backing off, a working devkit session, a fuse that filed itself.
- **The pass itself ran**: no crash, no hang, no refusal from the watchdog.
- **Every dispatched session ended with an outcome** -- an intent or a blocked report --
  and what it spent getting there: tool calls, failed calls, and the friction the
  detectors see in its transcript, which the skill compares against its own reading.
- **Something moved**: the same problem dispatched again with nothing changed, or a
  backlog that only grows, is flagged.

The wait is inside this script, so the agent running it spends no turns polling: it
starts this in the background and reads `logs/fix-pass-supervise.json` when it exits.
Exit 0 means no violation in any iteration; 1 means the report lists some.

Tested in `tests/test_fix_pass_supervise.py`.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devkit_project
import fix_findings
import fix_reports
import harness_triage as triage
import session_friction
import session_transcripts as st
import worktree

REPO_ROOT = Path(__file__).resolve().parents[1]
WATCHDOG = REPO_ROOT / "scripts" / "fix-pass-watchdog.py"
RECORD = REPO_ROOT / "logs" / "fix-pass.log"
REPORT = Path("logs") / "fix-pass-supervise.json"
LOG = Path("logs") / "fix-pass-supervise.log"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

EXIT_CLEAN = 0
EXIT_VIOLATED = 1

# How long an iteration waits for the sessions it dispatched, and the least it waits
# before the next one: a gate needs minutes to report on what the last one shipped.
SETTLE = _dt.timedelta(minutes=75)
MIN_GAP = _dt.timedelta(minutes=12)
POLL = 60

# A person named as the next step, in any wording the pass has ever used.
PERSON = re.compile(r"needs a human|read the record|waits? for (?:a person|you)\b|by hand", re.I)
# Why a decision may wait: each names what it waits on, which something tracks.
TRACKED_WAITS = (
    "already dispatched",
    "escalated",
    "backing off",
    "held until the devkit session",
    "fuse:",
)
# Record lines that are a failure, and the finding kind that must be open for each.
FAILURE_KINDS = (
    (re.compile(r"^shipped\s+(\S+) .* -- failed:"), "ship-failed"),
    (re.compile(r"^shipped\s+(\S+) .* -- NOT shipped:"), "intent-unshippable"),
    (re.compile(r"^merged\s+(\S+) .* -- FAILED"), "merge-failed"),
    (re.compile(r"^sent\s+(\S+) .* -- FAILED to (\w+)"), "{1}-failed"),
    (re.compile(r"^regate\s+(\S+) .* FAILED"), "regate-failed"),
)


@dataclass
class Session:
    """One session an iteration dispatched, and what it cost."""

    project: str
    tree: str
    what: str
    state: str
    transcript: str = ""
    calls: int = 0
    failed_calls: int = 0
    friction: list[str] = field(default_factory=list)


@dataclass
class Iteration:
    number: int
    started: str
    exit: int | None
    record: str
    violations: list[str] = field(default_factory=list)
    sessions: list[Session] = field(default_factory=list)
    filed: list[str] = field(default_factory=list)
    backlog: int = 0


# --- the checks ---------------------------------------------------------------------------


def check_record(record: str, exit_code: int | None, open_kinds: set[tuple[str, str]]) -> list[str]:
    """Every way one pass's record breaks the contract, one line each."""
    found = []
    if exit_code not in (0, 1):
        found.append(f"the watchdog exited {exit_code}: the pass itself failed or was refused")
    for line in record.splitlines():
        if PERSON.search(line):
            found.append(f"a person is named as the next step: {line.strip()}")
        if line.startswith(("fix-pass: CRASHED", "fix-pass: FAILED", "watchdog: the pass failed")):
            found.append(f"the pass did not run: {line.strip()}")
        if line.startswith("capped") and not _tracked(line):
            found.append(f"a wait nothing tracks: {line.strip()}")
        for pattern, kind in FAILURE_KINDS:
            if match := pattern.match(line):
                need = kind.format(*match.groups())
                if (need, match.group(1)) not in open_kinds:
                    found.append(f"a failure with no {need} finding open: {line.strip()}")
    return found


def _tracked(line: str) -> bool:
    why = line.split(" -- ", 1)[-1].strip().lower()
    return why.startswith(TRACKED_WAITS)


def check_sessions(sessions: list[Session]) -> list[str]:
    found = []
    for session in sessions:
        where = f"{session.project} {Path(session.tree).name}"
        if session.state != fix_reports.DONE:
            found.append(
                f"a dispatched session did not finish with an outcome ({session.state}): {where}"
            )
        if session.friction:
            found.append(
                f"a dispatched session hit friction: {where}: {'; '.join(session.friction[:5])}"
            )
    return found


def check_progress(iterations: list[Iteration]) -> list[str]:
    """What only shows across iterations: a backlog that grew on every one of three.

    A problem sent again at an unchanged failure is `fix_budget`'s to stop, and its
    tests hold that; checking it twice here would only be a second opinion to chase.
    """
    backlogs = [i.backlog for i in iterations]
    if len(backlogs) >= 3 and backlogs[-1] > backlogs[-2] > backlogs[-3]:
        return [f"the harness-defect backlog only grows: {' -> '.join(map(str, backlogs))}"]
    return []


# --- the sessions -------------------------------------------------------------------------


def measure(transcript: Path | None) -> tuple[int, int, list[str]]:
    """`(tool calls, failed calls, friction the detectors see)` in one transcript."""
    if transcript is None or not transcript.is_file():
        return 0, 0, []
    chunk = st.read_new(transcript, 0, 0)
    events = st.events(transcript, chunk.rows)
    calls = sum(1 for e in events if e.kind == "call")
    failed = sum(1 for e in events if e.kind == "result" and e.error)
    friction = [f"{cls}: {what}" for cls, what, _ in session_friction.detect(events)]
    return calls, failed, friction


def dispatched_since(
    root: Path, projects: list[str], since: _dt.datetime
) -> list[fix_reports.Tree]:
    """The stamped trees whose dispatch is this iteration's."""
    found = []
    for tree in fix_reports.read_trees(root, projects):
        try:
            when = _dt.datetime.fromisoformat(str(tree.stamp.get("when", "")))
        except ValueError:
            continue
        if when >= since:
            found.append(tree)
    return found


def settle(
    trees: list[fix_reports.Tree],
    until: _dt.datetime,
    clock: Callable[[], _dt.datetime],
    sleep: Callable[[float], None] = time.sleep,
) -> list[Session]:
    """Wait until every tree's session is done or dead, or `until`; what each ended as."""
    while True:
        now = clock()
        states = {t.path: fix_reports.session_state(t.path, now) for t in trees}
        if now >= until or all(
            state not in ("", fix_reports.WORKING) for state, _ in states.values()
        ):
            break
        sleep(POLL)
    sessions = []
    for tree in trees:
        state, transcript = states[tree.path]
        path = Path(transcript) if transcript else fix_reports.newest_transcript(tree.path)
        calls, failed, friction = measure(path)
        what = str(tree.stamp.get("what", ""))
        sessions.append(
            Session(
                tree.project,
                str(tree.path),
                what,
                state or "unknown",
                str(path or ""),
                calls,
                failed,
                friction,
            )
        )
    return sessions


# --- the loop -----------------------------------------------------------------------------


def run_pass(workspace: Path, mode: str) -> tuple[int | None, str]:
    argv = [sys.executable, str(WATCHDOG), "--mode", mode, "--workspace", str(workspace)]
    done = subprocess.run(
        argv, capture_output=True, text=True, check=False, creationflags=NO_WINDOW
    )
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def open_kinds(devkit_dir: Path) -> set[tuple[str, str]]:
    """`(kind, project)` of every open fix-pass finding."""
    kinds = set()
    for item in triage.open_items(triage.load(devkit_dir), (fix_findings.FINDING,)):
        kinds.add((item.detail.split(":", 1)[0], item.project))
    return kinds


def iterate(
    workspace: Path, number: int, mode: str, clock: Callable[[], _dt.datetime]
) -> Iteration:
    root = workspace.parent
    devkit_dir = root / "devkit"
    before = {i.id for i in triage.open_items(triage.load(devkit_dir))}
    started = clock()
    code, _output = run_pass(workspace, mode)
    record = RECORD.read_text(encoding="utf-8") if RECORD.is_file() else ""
    items = triage.open_items(triage.load(devkit_dir))
    iteration = Iteration(number, started.isoformat(timespec="seconds"), code, record)
    iteration.filed = [f"{i.event} {i.project}: {i.detail}" for i in items if i.id not in before]
    iteration.backlog = len(items)
    iteration.violations = check_record(record, code, open_kinds(devkit_dir))
    if mode == "dispatch":
        projects = devkit_project.known_projects(workspace.read_text(encoding="utf-8"))
        trees = dispatched_since(root, projects, started)
        iteration.sessions = settle(trees, started + SETTLE, clock)
        iteration.violations += check_sessions(iteration.sessions)
    return iteration


def write_report(iterations: list[Iteration], root: Path | None = None) -> Path:
    base = root or REPO_ROOT
    path = base / REPORT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(i) for i in iterations], indent=1) + "\n", encoding="utf-8")
    lines = []
    for i in iterations:
        lines.append(
            f"iteration {i.number} at {i.started}: exit {i.exit}, backlog {i.backlog}, filed {len(i.filed)}"
        )
        lines += [
            f"  session {s.state:24} {s.calls:3} calls {s.failed_calls:2} failed  {s.tree}"
            for s in i.sessions
        ]
        lines += [f"  VIOLATION {v}" for v in i.violations]
    (base / LOG).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.UTC)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--mode", choices=("plan", "dispatch"), default="dispatch")
    parser.add_argument("--workspace", type=Path, default=worktree.DEFAULT_WORKSPACE)
    parser.add_argument(
        "--min-gap", type=float, default=MIN_GAP.total_seconds() / 60, help="minutes"
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    iterations: list[Iteration] = []
    for number in range(1, args.iterations + 1):
        iteration = iterate(args.workspace.resolve(), number, args.mode, utc_now)
        iterations.append(iteration)
        iteration.violations += check_progress(iterations)
        write_report(iterations)
        print(
            f"iteration {number}: exit {iteration.exit}, {len(iteration.violations)} violation(s)",
            flush=True,
        )
        if number < args.iterations:
            gap = _dt.timedelta(minutes=args.min_gap) - (
                utc_now() - _dt.datetime.fromisoformat(iteration.started)
            )
            time.sleep(max(0.0, gap.total_seconds()))
    path = write_report(iterations)
    print(
        f"fix-pass-supervise: {sum(len(i.violations) for i in iterations)} violation(s) -- {path}"
    )
    return EXIT_VIOLATED if any(i.violations for i in iterations) else EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main())
