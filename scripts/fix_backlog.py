#!/usr/bin/env python3
"""The harness-defect ledger's open backlog, as one failure for the fix pass.

`harness_triage.py` reads the ledger the `/triage-harness` skill worked by hand. Every
entry on it is a devkit defect, whichever project filed it, so it rides in the one
devkit session with everything else harness-shaped rather than waiting for a person to
run the skill. The signature is one line per group and the sha is the digest of the
open groups: the dispatch ledger sends nothing twice at the same backlog and looks again
when a new group opens or a session retires one -- not each time an open group recurs,
which a job filing every failed run does dozens of times a day. The groups go to the
session as evidence, `harness_triage.render`'s own text under `logs/gate/`, beside a
copy of each group's `artifact=` file (`copy_artifact`).

Tested in `tests/test_fix_backlog.py`.
"""

from __future__ import annotations

import hashlib
import shutil
import sys
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fix_cycle
import fix_ledger
import fix_plan
import gate_evidence
import harness_triage as triage
import sweep
import task_branch as tb


def ledger_failure(
    devkit_dir: Path, root: Path, in_flight: Mapping[str, str] | None = None
) -> fix_plan.Failure | None:
    """The open backlog as a `LEDGER` failure with its groups as evidence; None when empty.

    A group whose fix is still in flight (`in_flight`, `fix_verify.in_flight`'s) is shown
    as pending and sends nothing: its new rows are the defect waiting on a merge, and
    each one used to change the sha and send a session to re-prove it (d677ea57).
    """
    history = triage.load(devkit_dir)
    items = triage.open_items(history)
    pending = triage.pending_groups(history, in_flight or {})
    live = [i for i in items if i.signature not in pending]
    if not live:
        return None
    grouped = triage.groups(live)
    signature = tuple(
        f"{members[0].event} {members[0].project} [{members[0].id}] x{len(members)}"
        for _, members in grouped
    )
    # Keyed on the groups -- each by its earliest row -- not on every open row: a job
    # filing each failed run puts ~96 rows a day in one group, and each one sent a session.
    firsts = sorted(min(members, key=lambda item: item.stamp).id for _, members in grouped)
    ids = hashlib.sha256("\n".join(firsts).encode()).hexdigest()
    failure = fix_plan.Failure(
        kind=fix_plan.LEDGER,
        project=fix_cycle.DEVKIT,
        number=0,
        title=f"{len(grouped)} open group(s) on the harness-defect ledger",
        url="",
        base=tb.detect_default_branch(sweep.git_for(devkit_dir), fallback="main"),
        sha=ids[: fix_ledger.KEY_DIGEST],
        workflow="harness ledger",
        signature=signature,
    )
    where = root / gate_evidence.evidence_slot(failure)
    shutil.rmtree(where, ignore_errors=True)
    where.mkdir(parents=True, exist_ok=True)
    copied = [copy_artifact(members[0], devkit_dir, where) for _, members in grouped]
    notes = [line for line in copied if line]
    text = triage.render(items, history, pending)
    if notes:
        text += "\ncopied beside this file, as they were when the pass read the ledger:\n"
        text += "".join(f"  {line}\n" for line in notes)
    (where / triage.ARTIFACT.name).write_text(text, encoding="utf-8")
    return replace(failure, evidence=str(where))


def artifact_path(item: triage.Item, devkit_dir: Path) -> Path | None:
    """The file a row's `artifact=` names, when it exists: relative to the checkout whose
    `logs/` the job wrote -- devkit's own, or the project's beside it in the workspace."""
    ref = item.fields.get("artifact", "").strip(" -")
    if not ref:
        return None
    path = Path(ref)
    if not path.is_absolute():
        home = (
            devkit_dir
            if item.project == fix_cycle.DEVKIT
            else sweep.default_workspace(devkit_dir).parent / item.project
        )
        path = home / path
    return path if path.is_file() else None


def copy_artifact(item: triage.Item, devkit_dir: Path, where: Path) -> str:
    """Copy `item`'s artifact into the evidence slot `where`; the line saying so, or "".

    8751095b: a scheduled job's ledger group reached its session as the triage text alone,
    and the failed run's kept output -- which named the 240 s timeout -- was found only by
    searching the devkit checkout's `logs/`. The copy is named for the group, so two
    projects' `collector.log` cannot overwrite each other, and is taken now because the
    job rewrites its `.failed.log` on its next failure.
    """
    source = artifact_path(item, devkit_dir)
    if source is None:
        return ""
    name = f"{item.id}-{source.name}"
    try:
        shutil.copyfile(source, where / name)
    except OSError as exc:
        return f"[{item.id}] {source} could not be copied: {exc}"
    return f"[{item.id}] {name} <- {source}"
