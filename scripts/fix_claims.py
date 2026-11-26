#!/usr/bin/env python3
"""The fix pass's half of the claims: so a second PC can run `devkit.fixPass: dispatch`.

Two PCs reading one red would each send a fixer at it, since each reads the same gates
and keeps its own `dispatch.json`. So before a send the pass claims it (`job_claims`):

- **One claim per problem and commit**, in the project's repository: `fix-<digest>`, a
  digest of `fix_ledger.decision_key`, which names the failures and their commit. The
  same red at a new sha is a new key and a new claim, as it is in the ledger.
- **One machine-wide claim for the single harness session**, `harness`, in devkit's
  repository. `fix_loop.Closed.harness_busy` holds that to one per machine; this makes it
  one across machines. A pass with something to send gives back the claim of a session
  of its own that has finished (`Turn._first`), so the other PC does not sit out the TTL.

A machine holding its own claim goes ahead, restamping it: its ledger, not the claim, is
what says whether this machine should send again. A claim another PC holds is a wait,
and it ends on its own at the TTL.

**A send that opens a session is a heavy job** (`machine_role.py`): below the disk floor
it waits (`job_claims.disk_refusal`), and on a `workstation` it waits out
`job_claims.WORKER_FIRST` from its first sight before it is claimed at all, so a worker
running `devkit.fixPass: dispatch` sends it first -- but only while a worker's heartbeat
is fresh (`job_claims.live_workers`): with none, the send goes now and its record line
says why (`Turn.note`). Both waits come before the claim, so neither pushes anything.

**A claim that could not be made fails open.** The send goes and its record line says so
(`Turn.note`): every fixer on every project stopping for a push problem is far worse than
the rare duplicate this lets through. Queue tasks are the other way round, since a task
run twice is what they exist to avoid, and a task can wait for the next cycle.

**Nothing here may hold up a send it does not stop.** A pass with nothing to send makes no
call at all, and every call is bounded by the seconds named here and by the pass's own
deadline (`Turn._within`): `send` runs past that deadline on purpose, to record what it
held, so an unbounded fetch there is the watchdog's stop with nothing written.

What this does not solve is in `scripts/CLAUDE.md`: the claim stops two sends, but the
ledgers behind it stay per machine.

Tested in `tests/test_fix_claims.py`.
"""

from __future__ import annotations

import hashlib
import sys
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fix_cycle
import fix_findings
import fix_ledger
import fix_plan
import job_claims
import sweep

CLAIMED_BY = "claimed by"  # how a held decision's reason starts, tracked by `fix_stall`
HARNESS_CLAIM = "harness"
# The fetch and push that give back a finished session's claim, on every pass with a send.
RELEASE_SECONDS = 20
# The file a workstation keeps its first sight of each send in (`job_claims.Sightings`).
SIGHTINGS = "fix"

host_name = fix_findings.harness_events.host_name


def claim_of(decision: fix_plan.Decision) -> tuple[str, str]:
    """`(claim id, job kind)` for `decision`."""
    if decision.action == fix_plan.UPSTREAM:
        return HARNESS_CLAIM, job_claims.HARNESS
    digest = hashlib.sha256(fix_ledger.decision_key(decision).encode("utf-8")).hexdigest()
    return f"fix-{digest[: fix_ledger.KEY_DIGEST]}", job_claims.FIX


def repo_of(decision: fix_plan.Decision) -> str:
    """The checkout whose repository holds `decision`'s claim."""
    if decision.action == fix_plan.UPSTREAM:
        return fix_cycle.DEVKIT
    return decision.failures[0].project


@dataclass
class Turn:
    """One `send_all`'s claims: the wait for this machine's turn, taken once, and what it
    took, so a send that failed can give its claim back."""

    root: Path
    writes: bool
    free_mb: int | None
    harness_busy: str = ""
    left: Callable[[], float] | None = None
    role: str = ""
    free_disk_mb: int | None = None
    sightings: job_claims.Sightings = field(init=False)
    host: str = field(init=False)
    started: bool = field(init=False, default=False)
    unclaimed: dict[str, str] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        self.host = host_name()
        self.sightings = job_claims.Sightings(self.root / fix_cycle.DEVKIT, SIGHTINGS, self.host)

    def _left(self) -> float:
        return self.left() if self.left else float("inf")

    def _within(self, seconds: float) -> AbstractContextManager[None]:
        """Every call inside ends in `seconds`, or at the pass's deadline if that is sooner."""
        return sweep.until(time.monotonic() + min(seconds, max(self._left(), 0.0)))

    def _first(self) -> None:
        """What the pass's first send costs, once: a finished session's claim, then the wait."""
        if self.started:
            return
        self.started = True
        if not self.harness_busy:
            git = sweep.git_for(self.root / fix_cycle.DEVKIT)
            with self._within(RELEASE_SECONDS):
                job_claims.release(git, HARNESS_CLAIM, self.host)
        job_claims.wait_turn(self.free_mb, limit=self._left())

    def held(self, decision: fix_plan.Decision) -> str:
        """Why `decision` is another PC's, or "": this machine sends it, claimed or not."""
        if not self.writes:
            return ""
        claim_id, kind = claim_of(decision)
        if why := self._heavy_hold(decision, claim_id):
            return why
        self._first()
        git = sweep.git_for(self.root / repo_of(decision))
        with self._within(job_claims.PUSH_SECONDS):
            attempt = job_claims.claim(git, claim_id, kind, self.host)
        if attempt.outcome == job_claims.LOST:
            return f"{CLAIMED_BY} {attempt.holder or 'another PC'}, which sent it"
        if attempt.outcome == job_claims.FAILED:
            self.unclaimed[claim_id] = attempt.said
        return ""

    def _heavy_hold(self, decision: fix_plan.Decision, claim_id: str) -> str:
        """Why a send that opens a session waits on this machine, or "": the disk floor,
        then a workstation's window for a worker (`job_claims.worker_first`). An update
        or a rerun opens no session, is one call, and waits for neither."""
        if decision.action in fix_plan.NO_SESSION:
            return ""
        if why := job_claims.disk_refusal(self.free_disk_mb):
            return why
        # The heartbeat read is a fetch, held to the pass's deadline like every other call.
        with self._within(job_claims.PUSH_SECONDS):
            return self.sightings.hold(self.role, claim_id)

    def note(self, decision: fix_plan.Decision) -> str:
        """What `decision`'s record line owes: that a workstation found no worker to
        leave it to, and that it went unclaimed, when either is so."""
        claim_id = claim_of(decision)[0]
        alone = self.sightings.notes.get(claim_id)
        said = self.unclaimed.get(claim_id)
        return "".join(
            (
                "" if alone is None else f" ({alone})",
                "" if said is None else f" (claim failed open: {said[:120]})",
            )
        )

    def failed(self, decision: fix_plan.Decision) -> None:
        """Hand back the claim of a send that did not open: nothing is on it."""
        if self.writes:
            claim_id, _ = claim_of(decision)
            git = sweep.git_for(self.root / repo_of(decision))
            with self._within(RELEASE_SECONDS):
                job_claims.release(git, claim_id, self.host)
