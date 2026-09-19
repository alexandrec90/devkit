#!/usr/bin/env python3
"""The harness-defect ledger's open backlog, as one failure for the fix pass.

`harness_triage.py` reads the ledger the `/triage-harness` skill worked by hand. Every
entry on it is a devkit defect, whichever project filed it, so it rides in the one
devkit session with everything else harness-shaped rather than waiting for a person to
run the skill. The signature is one line per group and the sha is the digest of the
open ids: the dispatch ledger sends nothing twice at the same backlog and looks again
when a new item lands or a session retires one. The groups themselves go to the
session as evidence, `harness_triage.render`'s own text under `logs/gate/`.

Tested in `tests/test_fix_backlog.py`.
"""

from __future__ import annotations

import hashlib
import shutil
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fix_cycle
import fix_plan
import gate_evidence
import harness_triage as triage
import sweep
import task_branch as tb


def ledger_failure(devkit_dir: Path, root: Path) -> fix_plan.Failure | None:
    """The open backlog as a `LEDGER` failure with its groups as evidence; None when empty."""
    items = triage.open_items(triage.load(devkit_dir))
    if not items:
        return None
    grouped = triage.groups(items)
    signature = tuple(
        f"{members[0].event} {members[0].project} [{members[0].id}] x{len(members)}"
        for _, members in grouped
    )
    ids = hashlib.sha256("\n".join(sorted(i.id for i in items)).encode()).hexdigest()
    failure = fix_plan.Failure(
        kind=fix_plan.LEDGER,
        project=fix_cycle.DEVKIT,
        number=0,
        title=f"{len(grouped)} open group(s) on the harness-defect ledger",
        url="",
        base=tb.detect_default_branch(sweep.git_for(devkit_dir), fallback="main"),
        sha=ids[: fix_plan.KEY_DIGEST],
        workflow="harness ledger",
        signature=signature,
    )
    where = root / gate_evidence.evidence_slot(failure)
    shutil.rmtree(where, ignore_errors=True)
    where.mkdir(parents=True, exist_ok=True)
    (where / triage.ARTIFACT.name).write_text(triage.render(items), encoding="utf-8")
    return replace(failure, evidence=str(where))
