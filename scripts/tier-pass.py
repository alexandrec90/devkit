#!/usr/bin/env python3
"""Run each project's own tests on this PC, at origin's tip, whoever is at the keyboard.

The PR gate is Linux, so a pre-commit run on a person's machine was the only check that
ever ran on Windows. Take pre-commit out of the merge path and nothing does. This is the
medium tier: the suite, on a free PC, within minutes of a merge. It blocks nothing, so it
applies to every project it can run in, however that project delivers.

    tier-pass.py run                  # what `devkit-medium-tier` runs every 15 minutes
    tier-pass.py run PROJECT ...      # by hand: these, now, whatever the schedule thinks
    tier-pass.py status               # each project's last result, and any wait

`scripts/CLAUDE.md` has the table of tiers this sits in.

**What runs.** `scripts/run-tests.py` in the tree, or the `medium_tier` command in the
`[delivery]` table of `.devkit.toml` at that commit (`tier_command.py`). The script's
default picks the tests for what changed since `origin/<default>`, which at the tip is
nothing, and it says "nothing to run" and exits 0; so it is run with `--all` wherever its
source spells that flag, and a project whose script has none runs its suite bare already.

**Where it does not.** A project with a database or a runner that drives the Docker stack
has nothing to run against here, and no `medium_tier` to say otherwise: it is recorded
as not applicable, never as red (`tier_command.not_applicable`).

**What is tested is the tip, in a tree of its own** (`disposable_tree.py`). The static
checkout is the person's, and this job does not wait for it. A project several merges
behind tests only the tip: that is why its fixer is told to bisect, and why a red run
remembers the last commit the tier saw green (`e2e_results.carried`). A red at a tip that
has not moved gets one second run an hour on (`e2e_results.retest_due`), then waits for
the tip.

**One project at a time, on one machine, oldest-unjudged first.** The pass holds an OS
lock for its whole length (`machine_lock`), released by the process dying, and works
through what origin has moved past, never-judged and longest-waiting projects first, so
a busy project cannot starve a quiet one. A fire that lands during a pass is dropped by
the scheduler (`IgnoreNew`), which loses nothing: the pass reads every tip again as it
gets to it. Before each run it asks `host_memory`; a run that would not fit is counted,
not run (`tier_state.py`). A run is bounded (`--timeout-minutes`, and `WORKERS` under
`pytest-xdist`) and so is the pass (`--pass-minutes`).

**Only where `devkit.fixPass` is `dispatch`.** A result stays on the machine that made
it, and that machine's fix pass sends the fixer, so a red on any other machine would be
lost. There a pass says why in one line and exits 0, which registers no failure.

**Across machines each such PC runs this**, so a run is claimed as `tier-<project>-<sha12>`
through `job_claims.claim` (`claimed`, the one place that spells the call). A claim that
could not be made fails open, and that PC tests for itself.

**Every project owes the script** (`e2e_results.MEDIUM`). One whose tip has neither the
script nor a command is recorded red as a missing tier, as `e2e-pass.py` records a missing
`scripts/e2e.py`.

**A result, not an exit code**, for `e2e_results.py`'s reason: red is a finding about a
project, and this job exits 0 on it. A red reaches the fix pass as the project's red
default branch. Only a verdict goes in the result file, which is the record of the last
commit judged: a run that could not start (no tree, no toolchain, no such program) writes
nothing there, so it never hides a red. It also exits 0, and waits out a backoff
(`tier_state.py`); the job exits non-zero only when one of those has lasted a day.

Tested in `tests/test_tier_pass.py`.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import os
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(1, str(Path(__file__).resolve().parent / "hooks"))
import devkit_project
import disposable_tree
import e2e_results
import fix_switch
import harness_events
import host_memory
import job_claims
import sweep
import task_branch as tb
import tier_command
import tier_state
import worktree_env

REPO_ROOT = Path(__file__).resolve().parents[1]

TIER = e2e_results.MEDIUM

# Set in the environment of every run, so a project's suite can tell this from a person's.
ENV_FLAG = "DEVKIT_MEDIUM_TIER"

# Where `run-tests.py` writes what failed: its own output says only that it did, and the
# tree is gone by the time anyone reads this. `scripts/run-tests.py` owns the path.
TEST_ARTIFACT = Path("logs") / "test-failures.log"
DEVKIT_TOML = ".devkit.toml"

# What a run is assumed to need, on top of `host_memory.FLOOR_MB`, with `WORKERS` workers.
# An estimate, not a measurement.
RUN_MB = 2048
# How many `pytest-xdist` workers `-n auto` means here, so a run on a PC already short of
# memory cannot take it below the floor `fix_send` keeps for a session. Named through the
# variable xdist reads itself, so a project's runner has nothing to take; a runner that
# fixes its own count, or a test runner that is not pytest, is not bound by it.
WORKERS = 2
WORKERS_ENV = "PYTEST_XDIST_AUTO_NUM_WORKERS"

# One project's ceiling. Ended with its whole process tree and recorded red, naming the
# timeout. A suite past this is too slow for a tier that fires every 15 minutes.
DEFAULT_TIMEOUT_MINUTES = 45
# When the pass stops starting runs. It may then finish one, so the installer's task limit
# allows for this plus one run and its setup.
DEFAULT_PASS_MINUTES = 120

# How long a claim must hold beyond the run, for its tree and toolchain: with the default
# timeout, inside `job_claims.TTL[TIER]`, which a test holds.
CLAIM_MARGIN_SECONDS = disposable_tree.CUT_TIMEOUT + disposable_tree.PROVISION_TIMEOUT

LOCK_FILE = "pass.lock"

# The last line of a pass that owes a filing: `log-wrap.py` reads the cause from it, so it
# names the kind and nothing that differs between runs.
BROKE = "error: the medium tier could not judge a project for a day"

Clock = Callable[[], _dt.datetime]
Git = sweep.Git


class NoVerdict(Exception):
    """A run that judged nothing, so it records no result. `own`: the project's defect,
    which is not devkit's to be billed for."""

    def __init__(self, why: str, own: bool = False):
        super().__init__(why)
        self.own = own


class NoRoom(Exception):
    """A run that would not fit in this machine's free memory."""


# --- what runs ----------------------------------------------------------------------------


def run_tests(
    tree: Path, command: str, timeout: float, runner: Callable[..., object]
) -> tuple[list[str], object]:
    """`(argv, finished run)`. What cannot start judges nothing, and only a project's own
    `.devkit.toml` can have named a command that does not exist."""
    python = e2e_results.venv_interpreter(tree, sweep.console_python())
    env = {**os.environ, ENV_FLAG: "1", "PYTHONUTF8": "1", WORKERS_ENV: str(WORKERS)}
    try:
        argv = tier_command.argv_for(tree, command, python)
        return argv, runner(argv, timeout, cwd=str(tree), env=env)
    except (OSError, ValueError) as exc:
        said = f"{command or e2e_results.MEDIUM_CONTRACT} could not start: {exc}"
        raise NoVerdict(said, own=bool(command)) from exc


def output_of(tree: Path, argv: list[str], done: object) -> str:
    """What the run said, and what it wrote to its artifact, before the tree goes."""
    said = f"{getattr(done, 'stdout', '') or ''}\n{getattr(done, 'stderr', '') or ''}"
    try:
        artifact = (tree / TEST_ARTIFACT).read_text(encoding="utf-8", errors="replace")
    except OSError:
        artifact = ""
    return f"$ {' '.join(argv)}\n{said}\n{artifact}"


# --- one machine, and many ----------------------------------------------------------------


@contextlib.contextmanager
def machine_lock(path: Path) -> Iterator[bool]:
    """Hold `path` for this process; yields False when another process does.

    An OS lock on an open file rather than the `mkdir` mutex `worktree.py` uses, because
    that one is broken by age: a machine that lost power mid-pass would hold this job off
    for as long as a pass may run. This one goes with the process.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        try:
            _lock(handle.fileno())
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            with contextlib.suppress(OSError):
                _unlock(handle.fileno())


if sys.platform == "win32":
    import msvcrt

    def _lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


def job_id(project: str, sha: str) -> str:
    return f"tier-{project}-{sha[:12]}"


def claimed(
    checkout: Path,
    job: str,
    git_for: Callable[[Path], Git] = sweep.git_for,
    host: Callable[[], str] = harness_events.host_name,
) -> bool:
    """Whether this machine may run `job`, claimed in `checkout`'s repository.

    The one place that spells `job_claims.claim`, as a `TIER` job whose TTL outlasts a
    run. Only a lost claim stops the run: one that could not be made (no network, a
    refused push) fails open, as the fix pass's do (`fix_claims.py`), since a commit
    tested twice costs a run and one tested nowhere hides a red.
    """
    attempt = job_claims.claim(git_for(checkout), job, job_claims.TIER, host())
    if attempt.outcome == job_claims.FAILED:
        print(f"{job}: claim failed open -- {attempt.said[:120]}", flush=True)
    return attempt.outcome != job_claims.LOST


def enough_memory(room: int | None) -> bool:
    """`None` is a probe that failed, which `host_memory` asks to be read as no guard."""
    return room is None or room >= RUN_MB + host_memory.FLOOR_MB


# --- one project --------------------------------------------------------------------------


@dataclass(frozen=True)
class Seams:
    """Everything the pass reaches outside itself, so a test replaces it in one argument."""

    root: Path = REPO_ROOT
    now: Clock = _dt.datetime.now
    git_for: Callable[[Path], Git] = sweep.git_for
    runner: Callable[..., object] = sweep.run_bounded
    room: Callable[[], int | None] = host_memory.available_mb
    claim: Callable[[Path, str], bool] = claimed
    provision: Callable[[Path], str] = disposable_tree.provision


@dataclass(frozen=True)
class Target:
    """A project to judge. `forced`: a person named it, so the schedule's reasons not to
    (a judged tip, a backoff, a claim, low memory) do not apply."""

    name: str
    checkout: Path
    base: str
    tip: str
    judged_at: str = ""  # when it was last judged; "" for never, which sorts first
    forced: bool = False


def origin_at(git: Git) -> tuple[str, str]:
    """`(base, tip)` with origin freshly fetched; empty when origin cannot be read."""
    if not e2e_results.ok(git("fetch", "--quiet", "origin")):
        return "", ""
    base = tb.detect_default_branch(git, fallback="main")
    return base, e2e_results.origin_tip(git, base)


def run_at(target: Target, command: str, timeout: float, seams: Seams) -> e2e_results.Result:
    """The tests at `target.tip`, in a tree of its own, recorded with their output."""
    started = seams.now().isoformat(timespec="seconds")
    previous = e2e_results.read(target.name, seams.root, TIER)
    for line in disposable_tree.sweep_leftovers(target.checkout, seams.runner):
        print(f"{target.name}: {line}", flush=True)
    tree, why = disposable_tree.cut(target.checkout, target.tip, seams.runner)
    if tree is None:
        raise NoVerdict(f"cannot cut a tree at {target.tip[:9]}: {why}")
    try:
        if why := seams.provision(tree):
            own = bool(worktree_env.unresolvable(why))
            raise NoVerdict(f"cannot provision a tree at {target.tip[:9]}: {why}", own=own)
        argv, done = run_tests(tree, command, timeout, seams.runner)
        output = output_of(tree, argv, done)
    finally:
        if left := disposable_tree.destroy(target.checkout, tree, seams.runner):
            print(f"{target.name}: {left}", flush=True)
    code = int(getattr(done, "returncode", 1))
    ended = e2e_results.Result(
        project=target.name,
        status=e2e_results.SKIPPED,
        sha=target.tip,
        base=target.base,
        started=started,
        finished=seams.now().isoformat(timespec="seconds"),
    )
    reason = "timed out" if code == sweep.TIMED_OUT else ""
    result = e2e_results.verdict(ended, code, output, TIER, reason)
    result = e2e_results.carried(result, previous)
    e2e_results.record(result, output, seams.root, TIER)
    return result


def _blob(git: Git, sha: str, path: str) -> str | None:
    """`path` at `sha`; None when the commit does not have it."""
    done = git("show", f"{sha}:{path}")
    return e2e_results.out(done) if e2e_results.ok(done) else None


def not_applicable(target: Target, why: str, seams: Seams) -> e2e_results.Result:
    """A project this tier cannot run in, at this tip. A skip, so never a red, and written
    so the next fire does not read two blobs to reach the same answer."""
    stamp = seams.now().isoformat(timespec="seconds")
    result = e2e_results.Result(
        project=target.name,
        status=e2e_results.SKIPPED,
        sha=target.tip,
        base=target.base,
        started=stamp,
        finished=stamp,
        reason=f"not applicable: {why}",
    )
    e2e_results.write(result, seams.root, TIER)
    return result


def judge(target: Target, timeout: float, seams: Seams) -> e2e_results.Result | None:
    """One project at the tip as origin has it now; None when another machine has it.

    The tip is read again because a pass can reach a project an hour after listing it,
    and a run at a commit already behind would only be held. Whether the tier applies is
    decided first: it takes no memory and no machine's word, and a PC short of memory
    should still be able to say so. A missing tier is claimed as a run is: recording one
    takes no time, but every PC recording it would each send a fixer at the same script.
    """
    git = seams.git_for(target.checkout)
    base, tip = origin_at(git)
    fresh = replace(target, base=base or target.base, tip=tip or target.tip)
    toml = _blob(git, fresh.tip, DEVKIT_TOML) or ""
    script = _blob(git, fresh.tip, e2e_results.MEDIUM_CONTRACT)
    command = tier_command.medium_command(toml)
    why = "" if command else tier_command.not_applicable(toml, script or "")
    if why:
        return not_applicable(fresh, why, seams)
    if not fresh.forced:
        room = seams.room()
        if not enough_memory(room):
            raise NoRoom(f"{room} MB free, a run needs {RUN_MB + host_memory.FLOOR_MB}")
        if not seams.claim(target.checkout, job_id(target.name, fresh.tip)):
            print(f"{target.name}: claimed by another machine at {fresh.tip[:9]}", flush=True)
            return None
    if not command and script is None:
        stamp = seams.now().isoformat(timespec="seconds")
        result = e2e_results.missing_result(target.name, fresh.base, fresh.tip, stamp, TIER)
        result = e2e_results.carried(result, e2e_results.read(target.name, seams.root, TIER))
        e2e_results.write(result, seams.root, TIER)
        return result
    print(f"{target.name}: running at {fresh.tip[:9]}", flush=True)
    return run_at(fresh, command, timeout, seams)


# --- the pass -----------------------------------------------------------------------------


def registered(workspace: Path) -> list[str]:
    return devkit_project.known_projects(workspace.read_text(encoding="utf-8"))


def waiting(name: str, last: e2e_results.Result | None, tip: str, seams: Seams) -> bool:
    """Whether the schedule leaves this project alone at `tip`: judged there and owing no
    second run, or waiting out a backoff, which it says."""
    now = seams.now()
    if last is not None and last.sha == tip and not e2e_results.retest_due(last, now):
        return True
    state = tier_state.read(name, seams.root)
    if tier_state.due(state, now):
        return False
    for line in tier_state.notes(name, state, now):
        print(line, flush=True)
    return True


def unjudged(workspace: Path, names: list[str], seams: Seams, forced: bool = False) -> list[Target]:
    """What is owed a run now, longest-waiting first: every name when `forced`.

    The workspace file is the same on every PC, so a name with no checkout here is this
    machine not holding that project: said, not recorded.
    """
    found: list[Target] = []
    for name in names:
        checkout = workspace.parent / name
        if not (checkout / ".git").exists():
            print(f"{name}: no checkout on this machine", flush=True)
            continue
        base, tip = origin_at(seams.git_for(checkout))
        if not tip:
            print(f"{name}: skipped -- cannot read origin's default branch", flush=True)
            continue
        last = e2e_results.read(name, seams.root, TIER)
        if not forced and waiting(name, last, tip, seams):
            continue
        found.append(Target(name, checkout, base, tip, last.finished if last else "", forced))
    return sorted(found, key=lambda target: (target.judged_at, target.name))


@dataclass
class Tally:
    """What became of each project a pass reached, for the pass's record."""

    judged: int = 0
    claimed_elsewhere: int = 0
    not_applicable: int = 0
    could_not_judge: int = 0
    for_memory: int = 0

    def line(self) -> str:
        return (
            f"tier-pass: judged {self.judged}, claimed elsewhere {self.claimed_elsewhere}, "
            f"not applicable {self.not_applicable}, could not judge {self.could_not_judge}, "
            f"skipped for memory {self.for_memory}"
        )


def _short_of_memory(target: Target, why: str, seams: Seams) -> None:
    now = seams.now()
    state = tier_state.skipped_for_memory(tier_state.read(target.name, seams.root), now)
    tier_state.write(target.name, state, seams.root)
    print(f"{target.name}: skipped -- {why}", flush=True)
    for line in tier_state.notes(target.name, replace(state, failures=0), now):
        print(line, flush=True)


def _could_not_judge(target: Target, error: NoVerdict, seams: Seams) -> bool:
    """Record it, and say whether this is the attempt that owes the ledger a filing."""
    now = seams.now()
    before = tier_state.read(target.name, seams.root)
    state = tier_state.could_not_judge(before, now, str(error), error.own)
    owed = tier_state.owes_filing(state, now)
    tier_state.write(target.name, tier_state.filed(state) if owed else state, seams.root)
    print(f"{target.name}: could not judge -- {error}", flush=True)
    return owed


def judge_all(targets: list[Target], timeout: float, stop_at: _dt.datetime, seams: Seams) -> int:
    """Each target in turn until the pass's time runs out; 1 when one owes a filing."""
    tally = Tally()
    owed = False
    for index, target in enumerate(targets):
        if seams.now() >= stop_at:
            left = ", ".join(later.name for later in targets[index:])
            print(f"tier-pass: out of time; next fire picks up: {left}", flush=True)
            break
        try:
            result = judge(target, timeout, seams)
        except NoRoom as exc:
            tally.for_memory += 1
            _short_of_memory(target, str(exc), seams)
            continue
        except NoVerdict as exc:
            tally.could_not_judge += 1
            owed = _could_not_judge(target, exc, seams) or owed
            continue
        if result is None:
            tally.claimed_elsewhere += 1
            continue
        tier_state.clear(target.name, seams.root)
        if result.status == e2e_results.SKIPPED:
            tally.not_applicable += 1
        else:
            tally.judged += 1
        print(e2e_results.line_for(result), flush=True)
    if targets:
        print(tally.line(), flush=True)
    if owed:
        print(BROKE, file=sys.stderr, flush=True)
    return 1 if owed else 0


def run_pass(
    workspace: Path, only: list[str], args: argparse.Namespace, seams: Seams | None = None
) -> int:
    seams = seams or Seams()
    # A result stays on the PC that made it, so a red here reaches a fixer only if this
    # PC's own fix pass dispatches. Every free PC becomes right once each PC can dispatch,
    # the lead's rollout step, and this check goes then.
    mode = fix_switch.workspace_mode(workspace)
    if mode != fix_switch.DISPATCH:
        print(f"tier-pass: devkit.fixPass is {mode} here, so a red would reach no fixer; not run")
        return 0
    known = registered(workspace)
    unknown = [name for name in only if name not in known]
    if unknown:
        print(f"tier-pass: not registered in the workspace: {', '.join(unknown)}", file=sys.stderr)
        return 2
    with machine_lock(seams.root / TIER.results_dir / LOCK_FILE) as held:
        if not held:
            print("tier-pass: another pass holds this machine", flush=True)
            return 0
        stop_at = seams.now() + _dt.timedelta(minutes=args.pass_minutes)
        targets = unjudged(workspace, only or known, seams, forced=bool(only))
        return judge_all(targets, args.timeout_minutes * 60.0, stop_at, seams)


def status(workspace: Path, root: Path = REPO_ROOT, now: Clock = _dt.datetime.now) -> int:
    for name in registered(workspace):
        result = e2e_results.read(name, root, TIER)
        if result is not None:
            print(f"{e2e_results.line_for(result)} ({result.finished or result.started})")
        for line in tier_state.notes(name, tier_state.read(name, root), now()):
            print(line)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    verbs = parser.add_subparsers(dest="verb", required=True)
    run = verbs.add_parser("run", help="test each project origin has moved past")
    run.add_argument("projects", nargs="*", help="registered checkouts; none means all")
    run.add_argument("--timeout-minutes", type=float, default=DEFAULT_TIMEOUT_MINUTES)
    run.add_argument("--pass-minutes", type=float, default=DEFAULT_PASS_MINUTES)
    verbs.add_parser("status", help="each project's last result, and any wait")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    workspace = sweep.default_workspace(REPO_ROOT)
    if not workspace.is_file():
        print(f"tier-pass: no workspace file at {workspace}", file=sys.stderr)
        return 2
    if args.verb == "status":
        return status(workspace)
    return run_pass(workspace, list(args.projects), args)


if __name__ == "__main__":
    sys.exit(main())
