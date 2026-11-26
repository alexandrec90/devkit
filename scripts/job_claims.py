#!/usr/bin/env python3
"""Who takes a job, with no coordinator, no SSH and no server: a ref only one PC can create.

Several PCs run the same scheduled jobs against the same repositories, and each sees the
same work. Nothing connects them but GitHub, so GitHub is the arbiter: to claim a job a
machine makes a commit naming itself and pushes it to `refs/devkit/claim/<id>` with
`--force-with-lease=<ref>:`, expected empty. The ref either did not exist and this push
created it, or it did and this push is refused. There is no third outcome on the server,
which is all a lock has to be.

**The commit must differ per machine, or two PCs both win.** Pushing a sha the ref already
holds is not a refused push: git compares the two shas before it looks at the lease, says
"up to date" and exits 0. So a claim commit carries the host, the moment and a nonce, and
two identical inputs still make two commits. `tests/test_job_claims.py` pushes the same
sha twice and watches both succeed, which is the reasoning held to git rather than to
this paragraph.

**Winning needs a refusal to tell it from losing.** Exit 0 is won, a rejected ref is lost,
and anything else -- no network, a timeout -- is `FAILED`, which a caller must read as
neither: for a `run` or `agent` queue task it means try next cycle, never go ahead.

**A claim outlives its holder only for a TTL per job kind** (`TTL`). A machine that went
down mid-job holds its ref forever otherwise, so past the TTL another machine may break
it, by a push whose lease names the sha it read: a second breaker's lease is then stale,
and exactly one takes over. A holder asking again gets its own claim back, restamped.

**A claim says who, never that the job is still to do.** Winning one proves only that
nobody holds it now: the machine that held it may have finished and released it a moment
ago, so a winner asks the server for the done ref before it acts (`on_server`), and a
machine lets go of a claim only once the server holds that ref. What `mark_done` reports
is likewise what the server says, not what the push said.

**The push skips the pre-push hooks, and only here.** The global dispatcher runs no
framework at the push, but a machine with `pre-commit install` and no dispatcher reads a
root commit as "publish everything" and runs whatever its push stage holds over an empty
tree. `_push` refuses any
ref outside `refs/devkit/`, so `--no-verify` cannot reach a branch or a tag through it.

**Free memory orders the PCs.** Every PC fires at the same scheduled minute, so a machine
with more free memory waiting less (`delay_seconds`) means work drifts to the emptiest
one, and one under `host_memory`'s hold does not claim at all (`refusal`).

**A role orders them before memory does** (`machine_role.py`). Memory alone is a race a
person's busy desk wins whenever it has a moment of memory free, so a `workstation`
leaves a heavy job alone for `WORKER_FIRST` from when it first saw it (`Sightings`,
`worker_first`) and a `worker` claims at once. **A workstation waits only for a worker
that has said it is alive**: each worker's `devkit-after-merge` pass refreshes
`refs/devkit/worker/<host>` (`worker_beat`, at most every `WORKER_BEAT_EVERY`), and a
workstation defers only while some other host's is younger than `WORKER_ALIVE`
(`live_workers`). No heartbeat, or none readable, is a job run here now, said in the log
(`no_worker_note`): a worker offline for a fortnight once cost every heavy job half an
hour. A worker that beats and still never claims -- under its own floors, say -- is the
job the workstation takes when the window closes, late rather than lost.

**A disk floor stands beside the memory one** (`DISK_FLOOR_MB`, `disk_refusal`): every
heavy job cuts or fills a tree, so a machine short of disk on the drive holding its
checkouts takes none, and the other PC, or the next fire, does.

Tested in `tests/test_job_claims.py`, against real bare repositories wherever git's own
behaviour is the claim being made.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import os
import re
import shutil
import sys
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(1, str(Path(__file__).resolve().parent / "hooks"))
import harness_events
import host_memory
import machine_role
import sweep

Git = sweep.Git

REFS = "refs/devkit/"
CLAIM = f"{REFS}claim/"
DONE = f"{REFS}done/"
HANDED = f"{REFS}handed/"  # with a session or the worker, attempts counted in the subject
FAILED_REFS = f"{REFS}failed/"  # out of attempts, and filed
REROUTED = f"{REFS}rerouted/"  # when an agent was last sent at a late condition
HOST = f"{REFS}host/"  # a machine's heartbeat, in devkit's repository alone
WORKER_REFS = f"{REFS}worker/"  # a worker's heartbeat, likewise, and far more often
# git's empty tree, which it knows without the object existing: a claim carries no files.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # pragma: allowlist secret

# The job kinds, each with how long a claim stands before another machine may break it.
RUN = "run"  # a queue task's command
AGENT = "agent"  # a queue task's session launch
ELEVATED = "elevated"  # a queue task's spool request
TIDY = "tidy"  # the queue's daily clean-up PR
FIX = "fix"  # one fixer at one problem at one commit
HARNESS = "harness"  # the single devkit session
COLLECTOR = "collector"  # one `failover` collector, renewed every pass (`collector_lease`)
TIER = "tier"  # one medium-tier run of one project at one commit (`tier-pass.py`)
E2E = "e2e"  # one local end-to-end run of one project at one commit (`e2e-pass.py`)
TTL: dict[str, _dt.timedelta] = {
    # Past `after-merge-pass.RUN_TIMEOUT` and its fast-forward, or a live run is broken.
    RUN: _dt.timedelta(hours=2),
    AGENT: _dt.timedelta(hours=1),
    ELEVATED: _dt.timedelta(hours=1),
    TIDY: _dt.timedelta(hours=1),
    # Four passes of `devkit-collectors`: a machine that misses them all has gone.
    COLLECTOR: _dt.timedelta(hours=1),
    # A session that died leaves its claim as it leaves its ledger entry, so another PC
    # looks again when this one would: `fix_ledger.RESEND_AFTER`, held equal by a test.
    FIX: _dt.timedelta(hours=6),
    HARNESS: _dt.timedelta(hours=6),
    # Past `tier-pass.DEFAULT_TIMEOUT_MINUTES` plus its tree's cut and provision, held by
    # a test, or a run still going is broken and the same commit tested twice.
    TIER: _dt.timedelta(hours=1, minutes=15),
    # Past `e2e-pass.DEFAULT_TIMEOUT_MINUTES` and its fast-forward, held by a test.
    E2E: _dt.timedelta(hours=1, minutes=30),
}
DEFAULT_TTL = _dt.timedelta(hours=1)

# Past this a claim is litter: nothing can be waiting on it, and each is a ref on GitHub.
PRUNE_AFTER = _dt.timedelta(days=14)

# A heartbeat is refreshed at most this often, and a host silent this long is not counted
# as one that still owes a run.
HEARTBEAT_EVERY = _dt.timedelta(days=1)
HOST_UNSEEN_AFTER = _dt.timedelta(days=30)

# A push is bounded here because `sweep.git_for` bounds only the verbs that cannot run a
# hook; this one runs none, and an unanswered connection must not outlast a job's cycle.
PUSH_SECONDS = 90

WON = "won"
LOST = "lost"
FAILED = "failed"
# How git words a push the ref's state refused, locally or at the server. Anything else
# was never answered, which is what `FAILED` is for.
REJECTED = re.compile(r"\[(?:remote )?rejected\]")

# At or above this much free memory a machine claims at once.
PROMPT_MB = 16 * 1024
# The most a machine waits. Well inside the shortest job cycle, so a slow machine still
# gets a turn on the fire that saw the work.
MAX_DELAY_SECONDS = 120.0

NO_IDENTITY_SET = {
    "GIT_AUTHOR_NAME": "devkit",
    "GIT_AUTHOR_EMAIL": "devkit@users.noreply.github.com",
    "GIT_COMMITTER_NAME": "devkit",
    "GIT_COMMITTER_EMAIL": "devkit@users.noreply.github.com",
}
SUBJECT_HOST = re.compile(r"\bby (\S+)")
# How a claim `finish` restamped starts: its job ran, and the claim is never broken.
FINISHED_SUBJECT = "finished "
REF_FORMAT = "%(refname)%00%(objectname)%00%(committerdate:unix)%00%(subject)"


@dataclass(frozen=True)
class Ref:
    """One `refs/devkit/*` ref, as the last fetch left it."""

    name: str
    sha: str
    at: float
    subject: str = ""

    @property
    def host(self) -> str:
        found = SUBJECT_HOST.search(self.subject)
        return found.group(1) if found else ""


@dataclass(frozen=True)
class Attempt:
    """What a claim came to. `holder` is who has it when lost; `said` is git's words."""

    outcome: str
    holder: str = ""
    said: str = ""
    broke: str = ""  # whose stale claim this one replaced

    @property
    def won(self) -> bool:
        return self.outcome == WON


# --- the commit and the push --------------------------------------------------------------


def _identity(now: float) -> dict[str, str]:
    """The environment a commit is made in: a named identity, since a scheduled job may
    have none, and the injected clock, which is what a later reader judges the TTL by."""
    stamp = f"{int(now)} +0000"
    return {
        **os.environ,
        **NO_IDENTITY_SET,
        "GIT_AUTHOR_DATE": stamp,
        "GIT_COMMITTER_DATE": stamp,
    }


def make_commit(git: Git, subject: str, now: float, nonce: str = "") -> str:
    """A root commit of the empty tree saying `subject`, unique to this call; "" on failure.

    `nonce` is what makes it unique (see the module docstring), so two machines handing in
    one subject and one second still make two commits.
    """
    body = f"nonce {nonce or uuid.uuid4().hex}"
    made = git("commit-tree", EMPTY_TREE, "-m", subject, "-m", body, env=_identity(now))
    return (made.stdout or "").strip() if made.returncode == 0 else ""


def _push(git: Git, source: str, ref: str, expected: str) -> tuple[str, str]:
    """`source` (a commit, or "" to delete) onto `ref` only if `ref` holds `expected` ("" for
    nothing); `(outcome, what git said)`."""
    if not ref.startswith(REFS):
        raise ValueError(f"job_claims pushes under {REFS} only, not {ref}")
    with sweep.until(time.monotonic() + PUSH_SECONDS):
        done = git(
            "push",
            "--quiet",
            "--no-verify",
            f"--force-with-lease={ref}:{expected}",
            "origin",
            f"{source}:{ref}",
        )
    said = " ".join(f"{done.stderr or ''} {done.stdout or ''}".split())
    if done.returncode == 0:
        return WON, said
    return (LOST if REJECTED.search(said) else FAILED), said or f"exit {done.returncode}"


def _claim_subject(kind: str, host: str, now: float) -> str:
    moment = _dt.datetime.fromtimestamp(now, _dt.UTC).isoformat(timespec="seconds")
    return f"claim {kind} by {host} at {moment}"


def claim_ref(claim_id: str) -> str:
    return f"{CLAIM}{claim_id}"


def done_ref(claim_id: str, host: str = "") -> str:
    """`refs/devkit/done/<id>`, with `/<host>` for a job done once per machine."""
    return f"{DONE}{claim_id}/{host}" if host else f"{DONE}{claim_id}"


def handed_ref(key: str) -> str:
    return f"{HANDED}{key}"


def failed_ref(key: str) -> str:
    return f"{FAILED_REFS}{key}"


def rerouted_ref(key: str) -> str:
    return f"{REROUTED}{key}"


def host_ref(host: str) -> str:
    return f"{HOST}{host}"


def worker_ref(host: str) -> str:
    return f"{WORKER_REFS}{host}"


def ttl_of(kind: str) -> _dt.timedelta:
    return TTL.get(kind, DEFAULT_TTL)


def expired(held: Ref, kind: str, now: float) -> bool:
    """`held` has stood longer than `kind`'s TTL, so its holder is taken to be gone."""
    return now - held.at >= ttl_of(kind).total_seconds()


def claim(git: Git, claim_id: str, kind: str, host: str, now: float | None = None) -> Attempt:
    """Try to take `claim_id`. Fetches nothing unless the ref turns out to be held."""
    now = time.time() if now is None else now
    ref = claim_ref(claim_id)
    commit = make_commit(git, _claim_subject(kind, host, now), now)
    if not commit:
        return Attempt(FAILED, said="could not make the claim commit")
    outcome, said = _push(git, commit, ref, "")
    if outcome != LOST:
        return Attempt(outcome, said=said if outcome == FAILED else "")
    held = holder(git, ref)
    if held is None:
        # Refused, then not readable: released in between, or the network went. Neither
        # is a win, and the next cycle finds out which.
        return Attempt(FAILED, said=f"held, then unreadable: {said}")
    mine = held.host == host
    if not mine and (finished(held) or not expired(held, kind, now)):
        return Attempt(LOST, holder=held.host)
    outcome, said = _push(git, commit, ref, held.sha)
    if outcome == LOST:
        return Attempt(LOST, holder=held.host)
    if outcome == FAILED:
        return Attempt(FAILED, said=said)
    return Attempt(WON, broke="" if mine else held.host)


def finished(held: Ref) -> bool:
    """`held` was restamped by `finish`: its job ran, so no TTL makes it another's."""
    return held.subject.startswith(FINISHED_SUBJECT)


def finish(git: Git, claim_id: str, kind: str, host: str, now: float | None = None) -> bool:
    """Mark `host`'s claim of `claim_id` as a job that ran; whether the server took it.

    For a job keyed by what it judges (a project at a commit) and recorded only on the
    machine that ran it, so another PC would otherwise read "not judged here" and break
    the claim at its TTL to run the same commit again: the medium tier, the local e2e.
    Kept on the claim ref itself rather than a done ref, so it goes with `prune`.
    """
    now = time.time() if now is None else now
    ref = claim_ref(claim_id)
    held = holder(git, ref)
    if held is None or held.host != host:
        return False
    moment = _dt.datetime.fromtimestamp(now, _dt.UTC).isoformat(timespec="seconds")
    subject = f"{FINISHED_SUBJECT}{kind} by {host} at {moment}"
    return stamp(git, ref, subject, held.sha, now)


def holder(git: Git, ref: str) -> Ref | None:
    """`ref` as origin has it now, or None when it is not there or cannot be read."""
    fetched = git("fetch", "--quiet", "origin", f"+{ref}:{ref}")
    if fetched.returncode != 0:
        return None
    found = read_refs(git, ref)
    return found[0] if found else None


def release(git: Git, claim_id: str, host: str) -> bool:
    """Give `claim_id` back, only if `host` still holds it; whether it is gone."""
    ref = claim_ref(claim_id)
    held = holder(git, ref)
    if held is None or held.host != host:
        return False
    return _push(git, "", ref, held.sha)[0] == WON


def on_server(git: Git, ref: str) -> str | None:
    """`ref`'s sha as origin has it now, "" when it has none, None when origin cannot say.
    Not the last fetch: this is what a machine asks before it acts on a job."""
    with sweep.until(time.monotonic() + PUSH_SECONDS):
        listed = git("ls-remote", "origin", ref)
    if listed.returncode != 0:
        return None
    fields = (listed.stdout or "").split()
    return fields[0] if fields else ""


def mark_done(git: Git, ref: str, subject: str, now: float | None = None) -> bool:
    """Record a job finished: whether the server now holds `ref`, by this push or another.

    A refused push proves nothing: a `[remote rejected]` from a failing server reads like a
    lost race and leaves no ref, so said to be done, the claim would go back and another
    PC would run the job again. So the server is asked, and an answer of anything but
    yes is not done: the caller keeps its claim and tries next cycle.
    """
    now = time.time() if now is None else now
    commit = make_commit(git, subject, now)
    if not commit:
        return False
    return _push(git, commit, ref, "")[0] == WON or bool(on_server(git, ref))


def stamp(git: Git, ref: str, subject: str, expected: str, now: float | None = None) -> bool:
    """Point `ref` at a new commit saying `subject`, only if it still holds `expected` (""
    for nothing), so of two machines acting on one reading exactly one moves it."""
    now = time.time() if now is None else now
    commit = make_commit(git, subject, now)
    return bool(commit) and _push(git, commit, ref, expected)[0] == WON


# --- reading what the last fetch brought ----------------------------------------------------


def parse_refs(text: str) -> list[Ref]:
    """`for-each-ref` output in `REF_FORMAT` as refs; a line of any other shape is skipped."""
    found = []
    for line in text.splitlines():
        fields = line.split("\0")
        if len(fields) == 4 and fields[2].isdigit():
            found.append(Ref(fields[0], fields[1], float(fields[2]), fields[3]))
    return found


def read_refs(git: Git, *patterns: str) -> list[Ref]:
    """The local copies of `patterns`, by default every `refs/devkit/*` ref."""
    listed = git("for-each-ref", f"--format={REF_FORMAT}", *(patterns or (REFS,)))
    return parse_refs(listed.stdout or "") if listed.returncode == 0 else []


def keys_under(refs: Iterable[Ref], prefix: str) -> dict[str, Ref]:
    """Each ref under `prefix`, by what follows it: `<id>`, or `<id>/<host>`."""
    return {ref.name.removeprefix(prefix): ref for ref in refs if ref.name.startswith(prefix)}


def done_ids(refs: Iterable[Ref]) -> frozenset[str]:
    """Each `<id>` and `<id>/<host>` a done ref names."""
    return frozenset(keys_under(refs, DONE))


def live_hosts(
    refs: Iterable[Ref], now: float, unseen: _dt.timedelta = HOST_UNSEEN_AFTER
) -> frozenset[str]:
    """The machines whose heartbeat is recent enough to count as still owing a run."""
    seconds = unseen.total_seconds()
    return frozenset(host for host, ref in keys_under(refs, HOST).items() if now - ref.at < seconds)


def heartbeat(git: Git, refs: Iterable[Ref], host: str, now: float) -> float | None:
    """Say `host` is alive, at most `HEARTBEAT_EVERY`; when it was last seen before, None
    for a machine with no heartbeat yet or one refreshed too recently to need another."""
    held = keys_under(refs, HOST).get(host)
    if held is not None and now - held.at < HEARTBEAT_EVERY.total_seconds():
        return None
    stamp(git, host_ref(host), f"host {host} seen", held.sha if held else "", now)
    return held.at if held else None


def prunable(refs: Iterable[Ref], now: float, keep: _dt.timedelta = PRUNE_AFTER) -> list[str]:
    """The claim refs old enough that nothing can still be waiting on them."""
    return [
        ref.name
        for ref in refs
        if ref.name.startswith(CLAIM) and now - ref.at >= keep.total_seconds()
    ]


def prune(git: Git, names: list[str]) -> bool:
    """Delete `names` from origin, all or none; whether that went through."""
    if not names:
        return True
    if not all(name.startswith(REFS) for name in names):
        raise ValueError(f"job_claims pushes under {REFS} only")
    with sweep.until(time.monotonic() + PUSH_SECONDS):
        done = git("push", "--quiet", "--no-verify", "origin", *(f":{name}" for name in names))
    return done.returncode == 0


# --- who may claim ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Machine:
    """What a job can ask of the PC about to claim it."""

    host: str
    free_mb: int | None = None  # `host_memory.available_mb`; None is a probe that failed
    docker: bool = False
    checkouts: frozenset[str] = frozenset()
    idle_seconds: float = float("inf")  # since the last keyboard or mouse input
    free_disk_mb: int | None = None  # `free_disk_mb`; None is a probe that failed
    role: str = machine_role.UNSET


@dataclass(frozen=True)
class Needs:
    """What a job requires of a machine; the defaults ask for nothing but its memory."""

    docker: bool = False
    checkout: str = ""  # a project that must be checked out here
    idle_seconds: float = 0.0  # stillness a job that opens windows must wait for
    # False for a job that starts no session and no heavy process: it is then asked for
    # neither memory nor disk.
    memory: bool = True


def refusal(needs: Needs, machine: Machine) -> str:
    """Why this machine must not claim a job with `needs`, or "" when it may."""
    if needs.memory and not host_memory.room_for_session(machine.free_mb):
        return f"held for memory: {machine.free_mb} MB free"
    if needs.memory and (why := disk_refusal(machine.free_disk_mb)):
        return why
    if needs.docker and not machine.docker:
        return "needs a working docker"
    if needs.checkout and needs.checkout not in machine.checkouts:
        return f"needs a checkout of {needs.checkout}"
    if machine.idle_seconds < needs.idle_seconds:
        return f"needs {needs.idle_seconds:.0f}s of idle screen"
    return ""


def delay_seconds(free_mb: int | None) -> float:
    """How long a machine with `free_mb` waits before claiming: none at `PROMPT_MB`, all
    of `MAX_DELAY_SECONDS` at the hold, linear between. A probe that failed waits half,
    so it neither beats a machine that knows it is empty nor loses to every other."""
    if free_mb is None:
        return MAX_DELAY_SECONDS / 2
    floor = host_memory.SESSION_MB + host_memory.FLOOR_MB
    if free_mb >= PROMPT_MB:
        return 0.0
    share = (PROMPT_MB - max(free_mb, floor)) / (PROMPT_MB - floor)
    return round(share * MAX_DELAY_SECONDS, 1)


def wait_turn(
    free_mb: int | None,
    sleep: Callable[[float], None] = time.sleep,
    limit: float = float("inf"),
) -> float:
    """Sleep this machine's `delay_seconds`, at most `limit`; how long that was."""
    pause = min(delay_seconds(free_mb), max(limit, 0.0))
    if pause > 0:
        sleep(pause)
    return pause


# --- disk ------------------------------------------------------------------------------------

# Below this much free on the drive holding the checkouts, a machine claims no heavy job.
# Each one cuts or fills a tree: a fixer's provisioned worktree with its `.venv` and
# `node_modules` is one to two gigabytes, a medium-tier tree the same until it is
# destroyed, and Windows itself wants several free for updates and its page file. Eight
# leaves a laptop with 30 GB free room for a dozen trees before it stops, and stops it
# while there is still room to clean up rather than at a full disk nothing can write to.
DISK_FLOOR_MB = 8 * 1024
HELD_FOR_DISK = "held for disk"


def free_disk_mb(path: Path) -> int | None:
    """Free megabytes on the drive holding `path`; None when it cannot be read."""
    try:
        return int(shutil.disk_usage(path).free // (1024 * 1024))
    except (OSError, ValueError):
        return None


def disk_here() -> int | None:
    """`free_disk_mb` of the drive devkit's checkout is on, which is the one every
    registered checkout sits beside, and so where every tree a heavy job cuts goes."""
    return free_disk_mb(Path(__file__).resolve().parents[1])


def disk_refusal(free_mb: int | None, floor: int = DISK_FLOOR_MB) -> str:
    """Why a heavy job may not start on `free_mb` of disk, or "". A probe that failed is
    read as no guard, as `host_memory` reads a failed memory probe."""
    if free_mb is None or free_mb >= floor:
        return ""
    return f"{HELD_FOR_DISK}: {free_mb} MB free on the checkouts' drive, a heavy job needs {floor}"


# --- the role --------------------------------------------------------------------------------

# How long a workstation leaves a heavy job to a worker, from when it first saw it. Long
# enough for a worker to see it on its own schedule: six `devkit-after-merge` fires, two
# of `devkit-medium-tier` and one of `devkit-local-e2e`, with the laptop's slower reads
# before its claim. Short enough that a worker that is off costs a job half an hour, not a
# night. And shorter than every heavy kind's TTL (a test holds it), so a claim a worker
# won and finished is still standing when the window closes, and is lost to, not broken.
WORKER_FIRST = _dt.timedelta(minutes=30)
# A job not seen for this long is seen afresh: the key came back after going (a red that
# went green and red again), and the window starts again rather than reading as long past.
FORGET_SIGHTING = _dt.timedelta(hours=6)
HELD_FOR_WORKER = "held for a worker"
SIGHTINGS_DIR = Path("logs") / "claims"

# A worker says it is alive from `devkit-after-merge`, which fires every 5 minutes on every
# PC and already fetches devkit's refs. Refreshed at most this often: under the cadence,
# so every fire's beat goes (a 5-minute floor skips each fire that came a second early,
# halving the rate), and one push per worker per fire is all it costs.
WORKER_BEAT_EVERY = _dt.timedelta(minutes=4)
# A worker whose beat is older than this is not waited for: three fires, so a worker
# that missed two still counts, and one that is off, asleep or offline stops costing the
# workstation anything within a quarter of an hour. A worker deep in a pass longer than
# this reads as gone too; the claims still keep the job to one PC, so that costs only
# which PC ran it.
WORKER_ALIVE = _dt.timedelta(minutes=15)
# How long one process reuses a read of the worker heartbeats: one fetch per pass, not
# one per job, and still fresh across a pass that runs for many minutes.
WORKERS_CACHED_SECONDS = 60.0

# Each worker host and when it last beat, or None when no heartbeat could be read.
Workers = dict[str, float] | None


def worker_beat(git: Git, refs: Iterable[Ref], host: str, now: float) -> bool:
    """Say `host` is a worker and alive, at most `WORKER_BEAT_EVERY`; whether it pushed.
    `refs` is the last fetch of devkit's refs, whose copy of the beat is the lease."""
    held = keys_under(refs, WORKER_REFS).get(host)
    if held is not None and now - held.at < WORKER_BEAT_EVERY.total_seconds():
        return False
    return stamp(git, worker_ref(host), f"worker {host} alive", held.sha if held else "", now)


def live_workers(
    refs: Iterable[Ref], now: float, myself: str = "", alive: _dt.timedelta = WORKER_ALIVE
) -> dict[str, float]:
    """Each worker host but `myself` whose beat is younger than `alive`, with its moment."""
    seconds = alive.total_seconds()
    return {
        host: ref.at
        for host, ref in keys_under(refs, WORKER_REFS).items()
        if host != myself and now - ref.at < seconds
    }


def read_worker_refs(git: Git) -> list[Ref] | None:
    """The worker heartbeats as origin has them now; None when origin cannot be read."""
    spec = f"+{WORKER_REFS}*:{WORKER_REFS}*"
    with sweep.until(time.monotonic() + PUSH_SECONDS):
        fetched = git("fetch", "--quiet", "--prune", "origin", spec)
    return read_refs(git, WORKER_REFS) if fetched.returncode == 0 else None


_WORKER_READS: dict[str, tuple[float, list[Ref] | None]] = {}


def workers_alive(root: Path, now: float, myself: str) -> Workers:
    """`live_workers` off the devkit checkout at `root`, read at most once a
    `WORKERS_CACHED_SECONDS`; None when its origin could not be read."""
    key = str(root)
    cached = _WORKER_READS.get(key)
    if cached is None or time.monotonic() - cached[0] >= WORKERS_CACHED_SECONDS:
        cached = (time.monotonic(), read_worker_refs(sweep.git_for(root)))
        _WORKER_READS[key] = cached
    refs = cached[1]
    return None if refs is None else live_workers(refs, now, myself)


def no_worker_note(workers: Workers) -> str:
    """What a workstation says when it runs a heavy job at once because no worker is
    there to wait for; "" when one is."""
    if workers is None:
        return "no worker to wait for: the worker heartbeats could not be read, so it runs here now"
    if not workers:
        minutes = WORKER_ALIVE.total_seconds() / 60
        return f"no worker to wait for: none has beaten in {minutes:.0f} min, so it runs here now"
    return ""


def worker_first(
    role: str,
    first_seen: float,
    now: float,
    workers: Workers,
    window: _dt.timedelta = WORKER_FIRST,
) -> str:
    """Why a machine with `role` leaves a heavy job first seen at `first_seen` to a
    worker for now, or "" when it may claim. Only a workstation waits, and only for a
    worker in `workers` (`live_workers`): with none alive, or none readable, it claims."""
    if role != machine_role.WORKSTATION or not workers:
        return ""
    left = window.total_seconds() - (now - first_seen)
    if left <= 0:
        return ""
    names = ", ".join(sorted(workers))
    ago = (now - max(workers.values())) / 60
    return (
        f"{HELD_FOR_WORKER} ({names}, seen {max(ago, 0):.0f} min ago): this PC is a "
        f"workstation, so it takes this in {left / 60:.0f} min if {names} has not claimed it"
    )


def _span(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and all(isinstance(t, int | float) and not isinstance(t, bool) for t in value)
    )


def sighted(
    seen: dict, key: str, now: float, forget: _dt.timedelta = FORGET_SIGHTING
) -> tuple[dict, float]:
    """`(seen with key's sighting at now, when key was first seen)`.

    `seen` maps a key to `[first, last]`. A key last seen longer than `forget` ago starts
    again, and every other such entry is dropped, so the file stays the size of what is
    open. A malformed entry reads as never seen.
    """
    horizon = now - forget.total_seconds()
    kept = {name: span for name, span in seen.items() if _span(span) and span[1] >= horizon}
    first = kept[key][0] if key in kept else now
    kept[key] = [first, now]
    return kept, first


class Sightings:
    """When this machine first saw each heavy job, kept between fires in one file per
    job (`SIGHTINGS_DIR`), so a workstation's window spans the fires it takes.

    `root` is the devkit checkout, whose origin holds the worker heartbeats; `workers`
    replaces that read (`workers_alive`) with one of the caller's, given the moment.
    `notes` keeps, per key, why a workstation did not wait (`no_worker_note`), printed
    when it is said and kept for a caller's record line."""

    def __init__(
        self,
        root: Path,
        job: str,
        host: str = "",
        workers: Callable[[float], Workers] | None = None,
    ) -> None:
        self.root = root
        self.path = root / SIGHTINGS_DIR / f"{job}.json"
        self.host = host or harness_events.host_name()
        self.workers = workers
        self.notes: dict[str, str] = {}

    def _workers(self, now: float) -> Workers:
        if self.workers is not None:
            return self.workers(now)
        return workers_alive(self.root, now, self.host)

    def _read(self) -> dict:
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def hold(self, role: str, key: str, now: float | None = None) -> str:
        """`worker_first` for `key`, recording the sighting. A machine that is not a
        workstation reads and writes nothing: the file is the workstation's alone. The
        heartbeats are read only while the window is open, which is the only time they
        can change the answer."""
        if role != machine_role.WORKSTATION:
            return ""
        now = time.time() if now is None else now
        seen, first = sighted(self._read(), key, now)
        # Unrecorded, the window starts again next fire: the job is later, never lost.
        with contextlib.suppress(OSError):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(seen, indent=1, sort_keys=True) + "\n", encoding="utf-8"
            )
        if now - first >= WORKER_FIRST.total_seconds():
            return ""
        workers = self._workers(now)
        if note := no_worker_note(workers):
            self.notes[key] = note
            print(f"{key}: {note}", flush=True)
        return worker_first(role, first, now, workers)
