#!/usr/bin/env python3
"""What the gate actually said, fetched once so the agent never has to go looking.

The half of `fix-prs.py`'s planning that talks to GitHub. `fix_plan.py` decides; this
gathers what it decides over: for each red PR, the gate run at its head sha, that run's
failed jobs and its `logs/` artifacts; for each open scheduled-failure issue, the run
the reporter named. The artifacts land on disk so the session opens with the failing
test's own output in its worktree, which is the discovery cost this saves -- a fixer
told only "the gate is red" spends its first turns finding out what red means.

Every function here answers empty on a `gh` that cannot: offline, unauthenticated, a
run whose artifacts expired. The plan then falls back to the failed step names, and a
failure with nothing at all still gets its row -- with the run URL in its note -- rather
than vanishing from a list because the evidence did.

Stdlib plus this repo's modules. Tested in `tests/test_gate_evidence.py`.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "precommit"))
import broken_pr_menu as menu
import fix_plan
import sweep
import task_branch as tb
from _loader import load_by_path

REPO_ROOT = Path(__file__).resolve().parents[1]

# The reporter that opens a tracker issue per failing scheduled workflow. Hyphenated,
# so loaded by path, and loaded at all for one value: how it titles the issue, which is
# the only way this can tell its issues from anyone else's. Asked of the module rather
# than spelled again here, so the two cannot drift.
reporter = load_by_path(
    "report_workflow_failure", REPO_ROOT / "scripts" / "report-workflow-failure.py"
)

# The workflow file every consumer's gate lives at; `sync-devkit.PR_GATE_FILE` is the
# path, this is the name `gh run list --workflow` filters by.
GATE_WORKFLOW = "pr-gate.yml"

# How the tracker issue is titled, less the workflow name: `issue_title` is a pure
# function of that name, so asking it about an empty one yields the suffix.
ISSUE_SUFFIX = reporter.issue_title("")

# The run URL in that issue's facts table.
RUN_URL = re.compile(r"/actions/runs/(\d+)")

RUN_LIST_FIELDS = "databaseId,headSha,conclusion,status,url"
RUN_VIEW_FIELDS = "jobs,conclusion,headSha,url"
ISSUE_FIELDS = "number,title,body,url"

# How many recent runs of the gate to look through for the one at the PR's head sha.
RUN_LIMIT = 10
ISSUE_LIMIT = 50

Gh = Callable[..., object]


def _json(result: object) -> object:
    """The parsed stdout of a `gh --json` call, or None for any failure shape."""
    code = getattr(result, "returncode", 1)
    if code != 0:
        return None
    try:
        return json.loads(str(getattr(result, "stdout", "") or ""))
    except ValueError:
        return None


# --- the gate run behind a PR ----------------------------------------------------------


def gate_run(gh: Gh, head: str, sha: str) -> dict:
    """The gate run at `sha` on `head`, or the newest on `head` when `sha` is unknown."""
    listed = _json(
        gh(
            "run",
            "list",
            "--branch",
            head,
            "--workflow",
            GATE_WORKFLOW,
            "--limit",
            str(RUN_LIMIT),
            "--json",
            RUN_LIST_FIELDS,
        )
    )
    if not isinstance(listed, list):
        return {}
    runs = [run for run in listed if isinstance(run, dict)]
    for run in runs:
        if sha and str(run.get("headSha", "")) == sha:
            return run
    return {} if sha else (runs[0] if runs else {})


def run_jobs(gh: Gh, run_id: str) -> list[dict]:
    """The run's jobs with their steps, for the coarse signature."""
    viewed = _json(gh("run", "view", str(run_id), "--json", RUN_VIEW_FIELDS))
    if not isinstance(viewed, dict):
        return []
    jobs = viewed.get("jobs", [])
    return [job for job in jobs if isinstance(job, dict)] if isinstance(jobs, list) else []


def download_logs(gh: Gh, run_id: str, dest: Path) -> list[str]:
    """Every `.log` the run uploaded, downloaded under `dest`, as text.

    `dest` is emptied first: an earlier download of a different run in the same slot
    would otherwise hand the plan a signature from the wrong commit.
    """
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    done = gh("run", "download", str(run_id), "-D", str(dest))
    if getattr(done, "returncode", 1) != 0:
        return []
    texts = []
    for log in sorted(dest.rglob("*.log")):
        try:
            texts.append(log.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return texts


# --- scheduled failures ----------------------------------------------------------------


def workflow_from_title(title: str) -> str:
    """`Nightly workflow is failing` -> `Nightly`; "" for a title that is not the reporter's."""
    text = str(title)
    return text[: -len(ISSUE_SUFFIX)] if text.endswith(ISSUE_SUFFIX) else ""


def run_id_from_body(body: str) -> str:
    found = RUN_URL.search(str(body))
    return found.group(1) if found else ""


def nightly_issues(gh: Gh) -> list[dict]:
    """The open tracker issues in one checkout: the reporter's title shape only."""
    listed = _json(
        gh("issue", "list", "--state", "open", "--limit", str(ISSUE_LIMIT), "--json", ISSUE_FIELDS)
    )
    if not isinstance(listed, list):
        return []
    return [
        issue
        for issue in listed
        if isinstance(issue, dict) and workflow_from_title(str(issue.get("title", "")))
    ]


# --- the newest release, for the superseded rule ------------------------------------------


def latest_tag(devkit: Path) -> str:
    """devkit's newest tag by version order, slugified the way branch names are.

    Empty when git cannot say, which the plan reads as "call nothing superseded".
    """
    tags = sweep.git_for(devkit)("tag", "--sort=-v:refname")
    if tags.returncode != 0:
        return ""
    first = next((line.strip() for line in tags.stdout.splitlines() if line.strip()), "")
    return tb.slugify(first) if first else ""


# --- collecting everything red ------------------------------------------------------------


def evidence_root(workspace: Path) -> Path:
    """Where downloads land before a worktree exists to copy them into."""
    return workspace.parent / ".evidence"


def pr_failure(project: str, pr: dict) -> fix_plan.Failure:
    return fix_plan.Failure(
        kind=fix_plan.PR,
        project=project,
        number=int(pr.get("number", 0) or 0),
        title=str(pr.get("title", "")),
        url=str(pr.get("url", "")),
        head=str(pr.get("headRefName", "")),
        base=str(pr.get("baseRefName", "") or "main"),
        sha=str(pr.get("headRefOid", "")),
        reason=menu.broken_reason(pr),
    )


def read_pr(project_dir: Path, failure: fix_plan.Failure, root: Path) -> fix_plan.Failure:
    """The PR's gate at its head sha: run, jobs, artifacts, into a signature."""
    gh = sweep.gh_for(project_dir)
    conflicted = fix_plan.CONFLICT in failure.reason
    run = gate_run(gh, failure.head, failure.sha)
    run_id = str(run.get("databaseId", "") or "")
    if not run_id:
        return replace(failure, signature=fix_plan.signature(conflicted, [], []))
    where = root / f"{failure.project}-pr-{failure.number}"
    texts = download_logs(gh, run_id, where)
    jobs = run_jobs(gh, run_id) if not texts else []
    sig = fix_plan.signature(conflicted, texts, jobs)
    return replace(failure, signature=sig, run_id=run_id, evidence=str(where) if texts else "")


def read_issue(project: str, project_dir: Path, issue: dict, root: Path) -> fix_plan.Failure:
    """One tracker issue: the run it names, read the same way a PR's gate is."""
    gh = sweep.gh_for(project_dir)
    git = sweep.git_for(project_dir)
    number = int(issue.get("number", 0) or 0)
    run_id = run_id_from_body(str(issue.get("body", "")))
    failure = fix_plan.Failure(
        kind=fix_plan.NIGHTLY,
        project=project,
        number=number,
        title=str(issue.get("title", "")),
        url=str(issue.get("url", "")),
        base=tb.detect_default_branch(git, fallback="main"),
        run_id=run_id,
        workflow=workflow_from_title(str(issue.get("title", ""))),
    )
    if not run_id:
        return failure
    where = root / f"{project}-nightly-{number}"
    texts = download_logs(gh, run_id, where)
    jobs = run_jobs(gh, run_id) if not texts else []
    sig = fix_plan.signature(False, texts, jobs)
    return replace(failure, signature=sig, run_id=run_id, evidence=str(where) if texts else "")


def collect(workspace: Path, found: dict[str, list[dict]]) -> list[fix_plan.Failure]:
    """Every red PR in `found` and every open tracker issue, each with its evidence.

    `found` is `broken_pr_menu.scan`'s answer, passed in rather than taken here so the
    one scan the CLI already runs is not run twice. Issues are asked about per project
    in `found`, which is the registry.
    """
    root = evidence_root(workspace)
    failures: list[fix_plan.Failure] = []
    for project, prs in found.items():
        project_dir = workspace.parent / project
        for pr in prs:
            failures.append(read_pr(project_dir, pr_failure(project, pr), root))
        for issue in nightly_issues(sweep.gh_for(project_dir)):
            failures.append(read_issue(project, project_dir, issue, root))
    return failures


def place(failure: fix_plan.Failure, tree: Path, subdir: str = "") -> Path | None:
    """Copy the downloaded evidence into the worktree at `logs/gate[/<subdir>]`."""
    if not failure.evidence:
        return None
    source = Path(failure.evidence)
    if not source.is_dir():
        return None
    target = tree / fix_plan.EVIDENCE_DIR / subdir if subdir else tree / fix_plan.EVIDENCE_DIR
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(source, target)
    return target
