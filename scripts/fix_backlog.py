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

import datetime as _dt
import hashlib
import json
import shutil
import sys
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import broken_pr_menu as menu
import fix_cycle
import fix_ledger
import fix_plan
import gate_evidence
import harness_triage as triage
import sweep
import task_branch as tb

# `log-wrap.py`'s kept copy says when it was written in its header (`artifact_body`).
WHEN_PREFIX = "# when:"
WHEN_LINES = 8
WHEN_SLACK = _dt.timedelta(minutes=5)


def ledger_failure(
    devkit_dir: Path,
    root: Path,
    in_flight: Mapping[str, str] | None = None,
    gh_for: Callable[[Path], Callable[..., object]] = sweep.gh_for,
) -> fix_plan.Failure | None:
    """The open backlog as a `LEDGER` failure with its groups as evidence; None when empty.

    A group whose fix is still in flight (`in_flight`, `fix_verify.in_flight`'s) is shown
    as pending and sends nothing: its new rows are the defect waiting on a merge, and
    each one used to change the sha and send a session to re-prove it (d677ea57). A
    group naming a branch with an open PR names the PR too (`open_prs`).
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
    text = triage.render(items, history, pending, prs=open_prs(devkit_dir, grouped, gh_for))
    if notes:
        text += "\ncopied beside this file, as they were when the pass read the ledger:\n"
        text += "".join(f"  {line}\n" for line in notes)
    (where / triage.ARTIFACT.name).write_text(text, encoding="utf-8")
    return replace(failure, evidence=str(where))


def open_prs(
    devkit_dir: Path,
    grouped: list[tuple[tuple[str, str, str, str], list[triage.Item]]],
    gh_for: Callable[[Path], Callable[..., object]] = sweep.gh_for,
) -> dict[str, str]:
    """`branch -> what its open PR is`, for each branch a group names that has one
    (`triage.named_branches`), looked up in devkit and in the group's own project.

    Only named branches are asked about, so a backlog naming none costs no `gh` call. A
    `gh` that cannot answer names no PR: the group still reaches its session as before.
    """
    workspace = sweep.default_workspace(devkit_dir).parent
    found: dict[str, str] = {}
    for _, members in grouped:
        homes = [home for home in (devkit_dir, workspace / members[0].project) if home.is_dir()]
        for branch in triage.named_branches(members):
            for home in dict.fromkeys(homes):
                if branch not in found and (said := open_pr(gh_for(home), branch)):
                    found[branch] = said
    return found


def open_pr(gh: Callable[..., object], branch: str) -> str:
    """`#N <url> -- <its gate>` for `branch`'s open PR, or "" when it has none."""
    fields = "number,url,statusCheckRollup"
    try:
        done = gh(
            "pr", "list", "--head", branch, "--state", "open", "--limit", "1", "--json", fields
        )
        rows = json.loads(getattr(done, "stdout", "") or "[]")
    except (OSError, ValueError):  # no `gh` on PATH, or an answer that is not JSON
        return ""
    if getattr(done, "returncode", 1) != 0 or not isinstance(rows, list) or not rows:
        return ""
    row = rows[0]
    if not isinstance(row, dict) or not row.get("number"):
        return ""
    failed = menu.failing_checks(row.get("statusCheckRollup"))
    gate = f"its gate is red ({failed} failing)" if failed else "no failing check"
    url = f" {row['url']}" if row.get("url") else ""
    return f"#{row['number']}{url} -- {gate}"


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


def later_failure(source: Path, stamp: str) -> str | None:
    """The `# when:` of `source` when it is not the failure filed at `stamp`, else None.

    96d2638e / e711daa0: a row older than `log-wrap.py`'s per-failure copies names the
    job's `.failed.log`, which the job's next failure overwrites, and five groups were
    each handed the newest one labelled as their own. `log-wrap` stamps the file in local
    time just before it files the row in UTC, so the two agree to within `WHEN_SLACK`. A
    file with no stamp, or a row whose stamp does not parse, is not judged.
    """
    try:
        with source.open(encoding="utf-8", errors="replace") as f:
            head = [next(f, "") for _ in range(WHEN_LINES)]
        filed = _dt.datetime.fromisoformat(stamp)
    except (OSError, ValueError):
        return None
    for line in head:
        if line.startswith(WHEN_PREFIX):
            written = line[len(WHEN_PREFIX) :].strip()
            try:
                local = _dt.datetime.strptime(written, "%Y-%m-%d %H:%M:%S").astimezone()
            except ValueError:
                return None
            if filed.tzinfo is None:
                filed = filed.replace(tzinfo=_dt.UTC)
            return None if abs(local - filed) <= WHEN_SLACK else written
    return None


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
    if (written := later_failure(source, item.stamp)) is not None:
        return (
            f"[{item.id}] {source} not copied: it holds the failure of {written}, not this "
            "group's -- its cause/said lines are the evidence"
        )
    name = f"{item.id}-{source.name}"
    try:
        shutil.copyfile(source, where / name)
    except OSError as exc:
        return f"[{item.id}] {source} could not be copied: {exc}"
    return f"[{item.id}] {name} <- {source}"
