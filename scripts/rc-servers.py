#!/usr/bin/env python3
"""Keep a `claude remote-control` server up per project, and let the CLI still update.

Remote Control puts a session running on *this* machine in front of the phone at
`claude.ai/code`. One server per project covers every conversation in that project --
it serves up to `--capacity` sessions (32 by default) and spawns them on demand from
the device -- so the unit this manages is the **project**, not the session.

**The reason this is a scheduled job and not a terminal you leave open.** Two facts
collide, and neither is optional:

- A server is a process. If it dies -- and `remote-control` gives up and *exits* after
  roughly ten minutes with no network -- nothing brings it back, and the phone shows an
  offline session with no way to act on it from the phone.
- `agent_clis` refuses to update a CLI while a process of that name is running, because
  Windows cannot replace a running image. A server that is always up therefore means
  `claude` is always running, and the 04:30 `global-tools.py` pass skips the update
  **every night, forever**. `agent_clis`' own docstring says as much: its two callers
  exist because "a night with a session left open skips".

So an always-on server silently disables the update it depends on, and the fix cannot
live in either piece alone. It lives here: this pass owns the restart, and the update
happens in the window it opens between stopping the servers and starting them again.

**One task, not two.** The liveness check wants to run every few minutes and the update
wants to run once a day, which reads like two schedules. It is one, because the daily
half is a predicate (`rc_config.due_for_update`) rather than a trigger: every tick
restarts whatever is down, and the first tick after `updateAt` that finds every server
idle also updates. A second task would double the registrations for a job whose
expensive half is guarded by a date either way.

**Restarting is cheap, and only inside four hours.** `claude remote-control` in a
directory a server was serving brings back every session it served, so a nightly cycle
costs the sessions nothing. That window is about four hours wide; past it the sessions
are gone rather than paused, which is why this restarts immediately after updating
rather than leaving the servers down until someone notices.

**Nothing is served for a project that did not ask for one.** The opt-in is
`devkit.remoteControl` in the workspace file, beside `devkit.onHold` -- a project absent
from it gets no server, because a standing server costs real memory (measured on this
desk: 300-420 MB per `claude` process) and a project nobody opens from a phone is paying
it for nothing.

This module is the pass. `rc_config.py` reads what to serve and decides when the daily
cycle is owed; `rc_machine.py` is everything that touches a process or the transcript
store. Tested in `tests/test_rc_servers.py`, with those two covered by their own suites.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_clis
import rc_config
import rc_machine
import sweep

REPO_ROOT = Path(__file__).resolve().parents[1]

# Where this job's account of itself lives. It runs windowless, so stdout goes nowhere
# at all -- see `tests/test_scheduled_jobs.py`. Relative, resolved against the checkout
# at write time, which is what `install-rc-schedule.py` advertises to `schedule_health`.
ARTIFACT = Path("logs/rc-servers.log")

# Machine-local and rewritten every pass, so it lives beside the artifact under the
# already-ignored `logs/` rather than becoming a tracked file that differs on every desk.
STATE = Path("logs/rc-servers.state.json")


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


@dataclass
class Plan:
    """Everything one pass operates on, resolved once.

    A dataclass rather than eight parameters threaded through `ensure_up` and `cycle`:
    the two took the same set, in the same order, and every addition had to be made
    twice. `structure_check` flags the parameter count, and it is right that the count
    was the symptom -- these values travel together because they are one thing.
    """

    names: list[str]
    root: Path  # the directory the checkouts live in
    state: rc_machine.State
    claude: str
    config: rc_config.Config
    logs: Path
    store: Path

    def project(self, name: str) -> Path:
        return self.root / name


def ensure_up(plan: Plan, report: Pass) -> Pass:
    """Start a server for every named project that has not got a live one."""
    for name in plan.names:
        pid = plan.state.servers.get(name, 0)
        if pid and rc_machine.pid_is_server(pid):
            report.say(f"{name}: up (pid {pid})")
            continue
        started, error = rc_machine.start_server(
            plan.project(name), name, plan.claude, plan.config, plan.logs
        )
        if started:
            plan.state.servers[name] = started
            report.say(f"{name}: started (pid {started})")
        else:
            plan.state.servers.pop(name, None)
            report.fail(f"{name}: could not start -- {error}")
    return report


def cycle(
    plan: Plan, report: Pass, update: Callable[..., object] | None = None
) -> tuple[Pass, bool]:
    """Stop every server, update the CLI, start them again. `(report, ran)`.

    `ran` is false when a project was busy, and **nothing is stopped in that case** --
    not even the idle ones. Stopping some servers to update a binary that a still-running
    one is executing fails the update and costs the stopped sessions for nothing.

    The order is the whole point of this function, and the reason the update is called
    from here rather than left to `global-tools.py`: Windows cannot replace a running
    image, so the update only has anything to do in the gap between the two loops.
    """
    busy = [
        name
        for name in plan.names
        if not rc_machine.is_idle(plan.project(name), plan.store, plan.config.idle_minutes)
    ]
    if busy:
        report.say(f"update deferred -- active in {', '.join(sorted(busy))}")
        return report, False

    for name in plan.names:
        pid = plan.state.servers.get(name, 0)
        if not pid:
            continue
        error = rc_machine.stop_server(pid)
        if error:
            report.fail(f"{name}: could not stop pid {pid} -- {error}")
        else:
            report.say(f"{name}: stopped (pid {pid})")
        plan.state.servers.pop(name, None)

    # Only Claude: `codex` has nothing to do with a Remote Control server, and updating
    # it here would make this job's verdict depend on a CLI it never stopped -- one Codex
    # session open anywhere would redden a pass that did its own work correctly.
    #
    # No `run=` either: `agent_clis` has its own `Runner`, taking a timeout that
    # `rc_machine.run_command` does not.
    updater = agent_clis.run_pass if update is None else update
    outcome = updater(agents=agent_clis.select_agents(["claude"]), yes=True)
    for line in getattr(outcome, "lines", ()):
        report.say(f"  {line}")
    summary = getattr(outcome, "summary", "")
    if getattr(outcome, "failures", 0):
        report.fail(f"claude update: {summary or 'failed'}")
    else:
        report.say(f"claude update: {summary or 'done'}")

    # Started again even when the update failed. A CLI that could not be updated is a bad
    # night; leaving the servers down over it would be a worse one, and past four hours
    # the sessions are gone rather than paused.
    ensure_up(plan, report)
    return report, True


def status(plan: Plan, report: Pass) -> Pass:
    """Report what is up and what is active, touching nothing."""
    for name in plan.names:
        pid = plan.state.servers.get(name, 0)
        live = "up" if pid and rc_machine.pid_is_server(pid) else "down"
        idle = rc_machine.is_idle(plan.project(name), plan.store, plan.config.idle_minutes)
        report.say(f"{name}: {live} (pid {pid or '-'}), {'idle' if idle else 'active'}")
    report.say(f"last update: {plan.state.last_update or 'never'}")
    return report


def down(plan: Plan, report: Pass) -> Pass:
    """Stop every server this job started."""
    for name in plan.names:
        pid = plan.state.servers.get(name, 0)
        if not pid:
            continue
        error = rc_machine.stop_server(pid)
        report.fail(f"{name}: {error}") if error else report.say(f"{name}: stopped")
        plan.state.servers.pop(name, None)
    return report


def render(lines: Sequence[str], failures: int, when: _dt.datetime) -> str:
    """The artifact. Written on every exit path, including the ones with nothing to say.

    A file whose mtime moves every fifteen minutes is how a reader tells "the job is
    healthy and quiet" from "the job has not run since Tuesday", which is exactly the
    failure `tests/test_scheduled_jobs.py` exists because of.
    """
    head = f"# rc-servers {when.isoformat(timespec='seconds')} -- {failures} failure(s)"
    return "\n".join([head, *lines, ""])


def write_artifact(text: str, root: Path = REPO_ROOT) -> None:
    path = root / ARTIFACT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def build_plan(workspace: Path, root: Path, report: Pass) -> Plan | None:
    """Resolve the workspace file into a `Plan`, or `None` with the reason reported."""
    text = workspace.read_text(encoding="utf-8")
    config = rc_config.parse_config(text)
    if not config.projects:
        report.say(f"nothing to serve -- no `{rc_config.RC_SETTING}` in {workspace.name}")
        return None
    names, notes = rc_config.selected(config, sweep.parse_workspace(text), sweep.on_hold(text))
    for note in notes:
        report.fail(note)
    if not names:
        return None
    return Plan(
        names=names,
        root=workspace.parent,
        state=rc_machine.State.load(root / STATE),
        claude="",
        config=config,
        logs=root / ARTIFACT.parent,
        store=rc_machine.sessions_store(),
    )


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "mode",
        nargs="?",
        default="status",
        choices=("status", "up", "cycle", "maintain", "down"),
        help=(
            "status: report only (default). up: start what is down. cycle: stop, update "
            "the CLI, start again. maintain: what the scheduler runs -- up, plus cycle "
            "once a day. down: stop everything."
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


def run_mode(mode: str, plan: Plan, report: Pass, now: _dt.datetime) -> None:
    """Dispatch one mode against a resolved plan."""
    if mode == "status":
        status(plan, report)
        return
    if mode == "down":
        down(plan, report)
        return
    if mode == "cycle" or (
        mode == "maintain"
        and rc_config.due_for_update(plan.state.last_update, now, plan.config.update_at)
    ):
        _, ran = cycle(plan, report)
        if ran:
            plan.state.last_update = now.date().isoformat()
        return
    ensure_up(plan, report)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.devkit.expanduser().resolve()
    workspace = args.workspace or sweep.default_workspace(root)
    now = _dt.datetime.now()
    report = Pass()

    def finish(code: int) -> int:
        """Every exit path leaves the artifact, including this one's failures."""
        text = render(report.lines, report.failures, now)
        write_artifact(text, root)
        print(text, end="")
        return code

    if workspace is None or not Path(workspace).is_file():
        report.fail(f"no workspace file at {workspace}")
        return finish(2)

    plan = build_plan(Path(workspace), root, report)
    if plan is None:
        return finish(2 if report.failures else 0)

    if args.mode not in {"status", "down"}:
        plan.claude = rc_machine.claude_executable()
        if not plan.claude:
            report.fail("claude is not on PATH and not in ~/.local/bin -- nothing can start")
            return finish(2)

    run_mode(args.mode, plan, report, now)
    if args.mode != "status":
        plan.state.save(root / STATE)
    return finish(2 if report.failures else 0)


if __name__ == "__main__":
    sys.exit(main())
