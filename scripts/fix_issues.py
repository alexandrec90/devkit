#!/usr/bin/env python3
"""Close a scheduled-failure issue once its workflow is green: the sweep behind the event.

`report-workflow-failure.py` opens a tracker issue when a scheduled workflow fails and
closes it on the next green run. That close is an event, and an event handler cannot be
the only path to a state you care about (`merge-dependabot-prs.py` says why): a reporter
run that failed, a workflow renamed or disabled after its fix, and the issue stands open
over a green branch, read by every pass as red and closed by nobody. The pass already
reads each issue's workflow at its base's tip (`gate_evidence.read_issue`), and green
there is exactly the reporter's own condition, so closing on it closes nothing the
reporter would not have.

The other half is the PR: a fixer the pass sent at an issue ships with `Closes #N`
(`ship_intent.pr_body`), so the issue closes on merge. Nothing here closes an issue a PR
merely mentions -- only the workflow's own green run at the tip decides.

Tested in `tests/test_fix_issues.py`.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fix_cycle
import gate_evidence
import sweep

Gh = Callable[..., object]


def close_comment(workflow: str, base: str, run: dict) -> str:
    """Why the pass closed it, with the run that said so."""
    sha = str(run.get("headSha", ""))[:9] or "its tip"
    return (
        f"Closed by the fix pass: the {workflow} workflow passed on origin/{base} at {sha} "
        f"({run.get('url', 'the newest run')}), and the reporter had not closed this issue."
    )


def close(gh: Gh, number: int, comment: str) -> str:
    """Close one issue as completed; "" when it closed, else what `gh` said."""
    done = gh("issue", "close", str(number), "--reason", "completed", "--comment", comment)
    if getattr(done, "returncode", 1) == 0:
        return ""
    said = (getattr(done, "stderr", "") or getattr(done, "stdout", "") or "").strip()
    return said.splitlines()[-1] if said else "?"


def sweep_project(project: str, project_dir: Path, mode: str, gh: Gh) -> list[str]:
    """One checkout's open tracker issues whose workflow is green at the tip, closed --
    or, outside dispatch, said. A line per issue; a failed close carries `FAILED`."""
    lines = []
    for issue in gate_evidence.nightly_issues(gh):
        workflow = gate_evidence.workflow_from_title(str(issue.get("title", "")))
        base, _file, runs, tip = gate_evidence.scheduled_at_tip(project_dir, workflow)
        green = gate_evidence.green_at_tip(runs, tip)
        if not green:
            continue
        number = int(issue.get("number", 0) or 0)
        where = f"{project} #{number} ({workflow})"
        if mode != fix_cycle.DISPATCH:
            lines.append(f"{where} -- would close: green on origin/{base} at the tip")
            continue
        why = close(gh, number, close_comment(workflow, base, green))
        lines.append(
            f"{where} -- FAILED to close: {why}"
            if why
            else f"{where} -- closed: green on origin/{base}"
        )
    return lines


def sweep_green(workspace: Path, projects: list[str], mode: str, gh_for=sweep.gh_for) -> list[str]:
    """Every registered checkout's tracker issues that a green tip has answered."""
    lines: list[str] = []
    for project in projects:
        project_dir = workspace.parent / project
        if project_dir.is_dir():
            lines += sweep_project(project, project_dir, mode, gh_for(project_dir))
    return lines
