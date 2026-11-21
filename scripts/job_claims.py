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

**The push skips the pre-push hooks, and only here.** The global dispatcher already lets a
ref outside `refs/heads/` through (`git_policy.framework._push_publishes_nothing`), but a
machine with `pre-commit install` and no dispatcher reads a root commit as "publish
everything" and runs the whole gate, for minutes, over an empty tree. `_push` refuses any
ref outside `refs/devkit/`, so `--no-verify` cannot reach a branch or a tag through it.

**Free memory orders the PCs.** Every PC fires at the same scheduled minute, so a machine
with more free memory waiting less (`delay_seconds`) means work drifts to the emptiest
one, and one under `host_memory`'s hold does not claim at all (`refusal`).

Tested in `tests/test_job_claims.py`, against real bare repositories wherever git's own
behaviour is the claim being made.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
import sys
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import host_memory
import sweep

Git = sweep.Git

REFS = "refs/devkit/"
CLAIM = f"{REFS}claim/"
DONE = f"{REFS}done/"
HANDED = f"{REFS}handed/"  # with a session or the worker, attempts counted in the subject
FAILED_REFS = f"{REFS}failed/"  # out of attempts, and filed
REROUTED = f"{REFS}rerouted/"  # when an agent was last sent at a late condition
HOST = f"{REFS}host/"  # a machine's heartbeat, in devkit's repository alone
# git's empty tree, which it knows without the object existing: a claim carries no files.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # pragma: allowlist secret

# The job kinds, each with how long a claim stands before another machine may break it.
RUN = "run"  # a queue task's command
AGENT = "agent"  # a queue task's session launch
ELEVATED = "elevated"  # a queue task's spool request
TIDY = "tidy"  # the queue's daily clean-up PR
FIX = "fix"  # one fixer at one problem at one commit
HARNESS = "harness"  # the single devkit session
TIER = "tier"  # one medium-tier run of one project at one commit (`tier-pass.py`)
TTL: dict[str, _dt.timedelta] = {
    # Past `after-merge-pass.RUN_TIMEOUT` and its fast-forward, or a live run is broken.
    RUN: _dt.timedelta(hours=2),
    AGENT: _dt.timedelta(hours=1),
    ELEVATED: _dt.timedelta(hours=1),
    TIDY: _dt.timedelta(hours=1),
    # A session that died leaves its claim as it leaves its ledger entry, so another PC
    # looks again when this one would: `fix_ledger.RESEND_AFTER`, held equal by a test.
    FIX: _dt.timedelta(hours=6),
    HARNESS: _dt.timedelta(hours=6),
    # Past `tier-pass.DEFAULT_TIMEOUT_MINUTES` plus its tree's cut and provision, held by
    # a test, or a run still going is broken and the same commit tested twice.
    TIER: _dt.timedelta(hours=1, minutes=15),
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
    if not mine and not expired(held, kind, now):
        return Attempt(LOST, holder=held.host)
    outcome, said = _push(git, commit, ref, held.sha)
    if outcome == LOST:
        return Attempt(LOST, holder=held.host)
    if outcome == FAILED:
        return Attempt(FAILED, said=said)
    return Attempt(WON, broke="" if mine else held.host)


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


@dataclass(frozen=True)
class Needs:
    """What a job requires of a machine; the defaults ask for nothing but its memory."""

    docker: bool = False
    checkout: str = ""  # a project that must be checked out here
    idle_seconds: float = 0.0  # stillness a job that opens windows must wait for
    memory: bool = True  # False for a job that starts no session and no heavy process


def refusal(needs: Needs, machine: Machine) -> str:
    """Why this machine must not claim a job with `needs`, or "" when it may."""
    if needs.memory and not host_memory.room_for_session(machine.free_mb):
        return f"held for memory: {machine.free_mb} MB free"
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
