#!/usr/bin/env python3
"""Step 3 of the fix pass: everything red, and a gate re-run where nothing could be read.

`collect_red` gathers every PR's failure, every default branch's own, and the
harness-defect backlog. A default branch with no verdict at its tip gets its gate
re-run (`regate`). The usual cause is a merge by the auto-merge workflow: a push made
with `GITHUB_TOKEN` raises no `push` event, so nothing gates the new tip, and devkit's
unreadable main held every project PR on every pass after. A `workflow_dispatch` is the
one event that token may raise, and the run it starts is on the branch, so the next pass
reads it like any other.

Cut out of `fix-pass.py`, which was at its `file_lines` and `imports` ceilings, and
`gate_evidence.py`, at its `definitions` one: that module reads what a gate said, and
the re-run is the one thing the pass does *to* a gate.

Tested in `tests/test_fix_red.py`, and through the pass in `tests/test_fix_pass.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import broken_pr_menu as menu
import fix_backlog
import fix_cycle
import fix_plan
import gate_evidence
import sweep
import task_branch as tb


def collect_red(
    workspace: Path, projects: list[str], refused: list[fix_plan.Failure]
) -> tuple[list[fix_plan.Failure], bool | str | None, list[str]]:
    """Everything red, devkit's default-branch verdict (None: unreadable), and the
    checkouts whose default-branch verdict could not be read.

    Each default branch's own gate is read beside the PRs: a red one is a failure to
    send a session at (devkit's is the harness itself), not only a reason to hold. The
    harness-defect ledger's open backlog rides along as one failure of its own.
    """
    found = menu.scan(workspace, projects)
    branches = gate_evidence.collect_default_branches(workspace, projects)
    failures = refused + gate_evidence.collect(workspace, found)
    failures += [failure for _, failure in branches.values() if failure]
    devkit_dir = workspace.parent / fix_cycle.DEVKIT
    if devkit_dir.is_dir():
        backlog = fix_backlog.ledger_failure(devkit_dir, gate_evidence.evidence_root(workspace))
        failures += [backlog] if backlog else []
    green, _ = branches.get(fix_cycle.DEVKIT, (None, None))
    unread = [name for name, (verdict, _) in branches.items() if verdict is None]
    return failures, green, unread


def regate(project_dir: Path) -> tuple[bool, str]:
    """Run the gate on the checkout's default branch: `(dispatched, what for the record)`."""
    base = tb.detect_default_branch(sweep.git_for(project_dir), fallback="main")
    done = sweep.gh_for(project_dir)("workflow", "run", gate_evidence.GATE_WORKFLOW, "--ref", base)
    if getattr(done, "returncode", 1) != 0:
        why = (getattr(done, "stderr", "") or getattr(done, "stdout", "") or "").strip()
        return False, f"{base} -- FAILED to re-run the gate: {why.splitlines()[-1] if why else '?'}"
    return True, f"{base} -- no verdict at the tip; gate re-run"


def regate_unread(root: Path, unread: list[str], mode: str) -> tuple[list[str], set[str]]:
    """Re-run the gate wherever the default branch had no verdict: `(lines, re-run)`.

    An unreadable devkit main holds every project fixer, and nothing else ever makes it
    readable. A checkout re-run here is `RUNNING` for this pass, as after any merge.
    Outside `dispatch` mode it only says what it would do.
    """
    lines: list[str] = []
    rerun: set[str] = set()
    for name in unread:
        if mode != fix_cycle.DISPATCH:
            lines.append(f"{name} -- would re-run the gate: no verdict at the tip")
            continue
        ok, line = regate(root / name)
        lines.append(f"{name} {line}")
        if ok:
            rerun.add(name)
    return lines, rerun
