#!/usr/bin/env python3
"""Whether GitHub says a PR still merges, and what to do while it has not decided.

GitHub does not store mergeability; it **computes it on demand and invalidates it every
time the base branch moves**. So `mergeable` is a three-valued field -- `MERGEABLE`,
`CONFLICTING`, `UNKNOWN` -- and the third value is not rare on a machine like this one:
`worktree.py reconcile` squash-merges every green labelled PR on a quarter-hour schedule,
and each merge puts every open PR under that branch back to `UNKNOWN` at once.

**An unresolved verdict is not a clean one, and reading it as one is silent.** A PR that
conflicts can have a perfectly green gate, so the conflict is the only thing there is to
notice it by -- drop it and the PR simply is not in the list, which is indistinguishable
from it being fine. That is a defect this repo has already had twice: once when `UNKNOWN`
was treated as clean outright, and once when it was asked about exactly one more time. The
ask is *also* what triggers the calculation, so one more ask is frequently answered
`UNKNOWN` again. A broken-PR menu drawn 42 seconds after three merges to `main` listed
three of five, and both missing rows were conflicts against the commits that had landed.

So `settle` asks again, on a **budget**: a caller is usually a person watching an empty
quick-pick, and the honest bound is a small number of asks rather than a poll that ends
when GitHub feels like answering. A row that never settles keeps what it had, which reads
as clean -- the last case left that can be wrong here, and the one that is bounded.

The caller supplies the ask, so nothing in this module knows about `gh`, a checkout or a
subprocess. Tested in `tests/test_pr_mergeability.py`.
"""

from __future__ import annotations

import concurrent.futures as futures
import time
from collections.abc import Callable

# GitHub's own vocabulary, in the two fields that answer "does this still merge" and the
# one state either of them is worth asking about. `mergeStateStatus` is the second
# signal rather than a nicer spelling of the first: it reports `DIRTY` for a conflict
# that `mergeable` is still calling `UNKNOWN`, which is a conflict this module knows
# about an ask sooner.
CONFLICTING = "CONFLICTING"
DIRTY = "DIRTY"
UNKNOWN = "UNKNOWN"
OPEN = "OPEN"

# How many times a row whose verdict has not arrived is asked about, and the wait before
# every ask after the first -- none before it, because the list it came in was just made.
# Three asks is what bounds the wait a picker can inflict; the waits are paid only while
# something is unresolved, which on a settled repository is never.
ASKS = 3
WAIT = 0.8

# How many of the re-asks run at once. A checkout's whole open set goes unresolved
# together whenever its default branch moves, so the fan-out is what keeps that case one
# round trip rather than one per PR.
WORKERS = 8


def conflicted(pr: dict) -> bool:
    """Whether GitHub says this PR no longer merges cleanly, by either field.

    `UNKNOWN` is deliberately not a conflict: a PR opened seconds ago reports it, and a
    caller that read it as one would call every fresh PR broken. It is not clean either
    -- that is what `settle` is for, and calling this without settling first is the bug
    this module exists to have one place to fix.
    """
    return (
        str(pr.get("mergeable") or "").upper() == CONFLICTING
        or str(pr.get("mergeStateStatus") or "").upper() == DIRTY
    )


def unresolved(entries: list[dict]) -> list[dict]:
    """The rows worth asking about again: no verdict yet, and not already known dirty.

    Drafts are out because no caller acts on one, and a row with no integer `number`
    cannot be asked about. A row that has left the open set is out for a reason of cost
    rather than correctness: GitHub never computes mergeability for a closed or merged
    PR, so without this the whole budget, waits included, would be spent on the one
    answer that cannot arrive.
    """
    return [
        entry
        for entry in entries
        if not entry.get("isDraft")
        and str(entry.get("state") or OPEN).upper() == OPEN
        and str(entry.get("mergeable") or UNKNOWN).upper() == UNKNOWN
        and str(entry.get("mergeStateStatus") or "").upper() != DIRTY
        and isinstance(entry.get("number"), int)
    ]


def settle(
    view: Callable[[int], dict], entries: list[dict], sleep: Callable[[float], None] = time.sleep
) -> None:
    """Ask `view` about every unjudged row until GitHub answers or the budget runs out.

    `entries` is mutated in place, each row updated with what `view` returned for it, so
    a caller holding the list holds the settled version. `view` answering `{}` -- a `gh`
    that failed -- leaves the row's other fields alone, which matters because the row may
    carry a known check failure and an `updatedAt` that a single-PR query does not return.

    The unresolved set is rebuilt between rounds rather than carried, so a row that
    settles on the first ask costs nothing further and a round with nothing left in it
    ends the loop without a wait.
    """
    for attempt in range(ASKS):
        pending = unresolved(entries)
        if not pending:
            return
        if attempt:
            sleep(WAIT)
        with futures.ThreadPoolExecutor(max_workers=min(WORKERS, len(pending))) as pool:
            asked = {pool.submit(view, entry["number"]): entry for entry in pending}
            for future in futures.as_completed(asked):
                asked[future].update(future.result())
