#!/usr/bin/env python3
"""Which one of the machines assigned a `failover` collector may run it right now.

Every machine set to `run` one asks on every pass for its claim (`job_claims`: a ref only
one PC can create), and one that loses acts as if set to `stop` until it wins. The holder
renews every pass, so a machine that is off or asleep stops renewing and another takes
the collector over once the claim has stood `job_claims.TTL`. The assignment file is never
changed: a machine's own answer is still `run`, and `RECORD` says what that came to.

**A machine that cannot reach GitHub stops before anyone else may start.** It goes on for
`GRACE` after its last renewal and not past it, which is shorter than the TTL by more than
one pass. That holds for a machine whose passes run; an engine that restarts a container
by itself at boot (`restart: unless-stopped`) may run it until the first pass.

**No data moves.** A `failover` collector is one whose copies merge afterwards.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collectors_config as config
import host_memory
import job_claims
import sweep

# Machine-local, beside the assignment: `{project: {held, holder, at}}`.
RECORD = Path("logs/collectors.claims.json")
HELD, HOLDER, AT = "held", "holder", "at"

# Seconds a holder keeps running on a claim it can no longer renew.
GRACE = 40 * 60

# The longest one claim takes: its turn, a push, the read of who holds it, a second push.
BOUND = job_claims.MAX_DELAY_SECONDS + 2 * job_claims.PUSH_SECONDS + sweep.NETWORK_TIMEOUT

Say = Callable[[str], None]


def claim_id(project: str) -> str:
    return f"collector-{project}"


def load(path: Path) -> dict[str, dict]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): v for k, v in raw.items() if isinstance(v, dict)}


def save(path: Path, record: Mapping[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def effective(
    collectors: Sequence[config.Collector],
    assignment: Mapping[str, str],
    record: Mapping[str, dict],
) -> dict[str, str]:
    """`assignment` as the last pass acted on it: `stop` where the claim was not held."""
    acted = dict(assignment)
    for c in collectors:
        known = record.get(c.project)
        if c.failover and acted.get(c.project) == config.RUN and known and not known.get(HELD):
            acted[c.project] = config.STOP
    return acted


def resolve(
    collectors: Sequence[config.Collector],
    assignment: Mapping[str, str],
    path: Path,
    git: job_claims.Git,
    host: str,
    say: Say,
    *,
    now: Callable[[], float] = time.time,
    free_mb: Callable[[], int | None] = host_memory.available_mb,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, str]:
    """Claim, renew or give back each `failover` collector's claim; the assignment to act
    on. Touches nothing, and asks nothing, where none is declared."""
    record = load(path)
    acted = dict(assignment)
    changed = False
    for collector in collectors:
        if not collector.failover:
            continue
        name = collector.project
        known = record.get(name, {})
        if assignment.get(name) == config.RUN:
            if not _hold(name, known, record, git, host, say, now, free_mb, sleep):
                acted[name] = config.STOP
            changed = True
        elif known and _let_go(name, known, git, host, now()):
            record.pop(name)
            changed = True
    if changed:
        save(path, record)
    return acted


def _let_go(name: str, known: Mapping, git: job_claims.Git, host: str, moment: float) -> bool:
    """Give the claim back; whether its record is done with. One that could not be given
    back is kept, so the next pass retries, until it is old enough for anyone to break."""
    if not known.get(HELD) or job_claims.release(git, claim_id(name), host):
        return True
    ttl = job_claims.ttl_of(job_claims.COLLECTOR).total_seconds()
    return moment - float(known.get(AT, 0)) >= ttl


def _hold(
    name: str,
    known: Mapping,
    record: dict[str, dict],
    git: job_claims.Git,
    host: str,
    say: Say,
    now: Callable[[], float],
    free_mb: Callable[[], int | None],
    sleep: Callable[[float], None],
) -> bool:
    """Whether this machine may run `name` for this pass; what it learned, in `record`."""
    mine = bool(known.get(HELD))
    if not mine:
        # A short machine only goes last: refused, it would run nowhere with no other PC on.
        job_claims.wait_turn(free_mb(), sleep)
    # Read before the push: the claim's commit carries this moment, so a slow push must
    # not make this machine believe it renewed later than the ref says.
    moment = now()
    got = job_claims.claim(git, claim_id(name), job_claims.COLLECTOR, host, moment)
    if got.won:
        record[name] = {HELD: True, HOLDER: host, AT: moment}
        if got.broke:
            say(f"{name}: taken over from {got.broke}, which stopped renewing")
        elif not mine:
            say(f"{name}: claimed by this machine")
        return True
    if got.outcome == job_claims.LOST:
        record[name] = {HELD: False, HOLDER: got.holder, AT: moment}
        say(f"{name}: {got.holder or 'another machine'} runs it -- standing by here")
        return False
    if mine and 0 <= moment - float(known.get(AT, 0)) < GRACE:
        say(f"{name}: could not renew the claim ({got.said}) -- still inside its grace")
        return True
    record[name] = {HELD: False, HOLDER: "", AT: moment}
    say(f"{name}: no claim could be made ({got.said}) -- not run here")
    return False
