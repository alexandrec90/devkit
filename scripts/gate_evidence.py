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
from branch_facts import branch_tip, is_behind, is_tagged

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

RUN_LIST_FIELDS = "databaseId,headSha,conclusion,status,url,workflowName"
RUN_VIEW_FIELDS = "jobs,conclusion,headSha,url"
ISSUE_FIELDS = "number,title,body,url"

# How many recent runs of the gate to look through for the one at the PR's head sha.
RUN_LIMIT = 10
ISSUE_LIMIT = 50

Gh = Callable[..., object]


def gh_json(result: object) -> object:
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
    listed = gh_json(
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
    viewed = gh_json(gh("run", "view", str(run_id), "--json", RUN_VIEW_FIELDS))
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
    listed = gh_json(
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


def newest_release(devkit: Path) -> str:
    """devkit's newest tag by version order, as written (`v0.11.21`); "" when it cannot say."""
    tags = sweep.git_for(devkit)("tag", "--sort=-v:refname")
    if tags.returncode != 0:
        return ""
    return next((line.strip() for line in tags.stdout.splitlines() if line.strip()), "")


def default_branch_runs(gh: Gh, base: str) -> list[dict]:
    """The newest gate runs on `base`, newest first; empty when `gh` cannot say."""
    listed = gh_json(
        gh(
            "run",
            "list",
            "--branch",
            base,
            "--workflow",
            GATE_WORKFLOW,
            "--limit",
            str(RUN_LIMIT),
            "--json",
            RUN_LIST_FIELDS,
        )
    )
    if not isinstance(listed, list):
        return []
    return [run for run in listed if isinstance(run, dict)]


# Conclusions that are not verdicts: the run said nothing about the commit. Anything
# else that is not `success` is red.
NO_VERDICT = frozenset({"cancelled", "skipped", "action_required", ""})


def _tip_verdict(runs: list[dict], tip: str) -> tuple[bool | str | None, dict]:
    """What the gate says about `tip`: `(verdict, the red run)`.

    `RUNNING` when the newest run is at the tip and unfinished. Otherwise the newest
    *completed* run decides -- the run for the push that just landed is the one still
    in progress, and its absence of a verdict says nothing about the branch -- and only
    when it is at the tip: a verdict about another commit is no verdict. `True` on
    success, `None` on a conclusion that judged nothing, else `False` with the run.
    """
    newest = runs[0]
    if str(newest.get("headSha", "")) == tip and str(newest.get("status", "")) != "completed":
        return fix_plan.RUNNING, {}
    run = next((r for r in runs if str(r.get("status", "")) == "completed"), {})
    if not run or str(run.get("headSha", "")) != tip:
        return None, {}
    conclusion = str(run.get("conclusion", "")).lower()
    if conclusion == "success":
        return True, {}
    return (None, {}) if conclusion in NO_VERDICT else (False, run)


def run_ids_from_rollup(rollup: object) -> tuple[str, ...]:
    """The runs behind a PR's failing checks, off `statusCheckRollup`, deduped in order.

    A check run's `detailsUrl` (a status context's `targetUrl`) names the run and the
    job; the run is what `gh run download` and `gh run view` take.
    """
    found: list[str] = []
    for node in rollup if isinstance(rollup, list) else []:
        if not isinstance(node, dict):
            continue
        verdict = str(node.get("conclusion") or node.get("state") or "").upper()
        if verdict not in ("FAILURE", "ERROR", "TIMED_OUT", "STARTUP_FAILURE"):
            continue
        hit = RUN_URL.search(str(node.get("detailsUrl") or node.get("targetUrl") or ""))
        if hit and hit.group(1) not in found:
            found.append(hit.group(1))
    return tuple(found)


# --- collecting everything red ------------------------------------------------------------


def evidence_root(workspace: Path) -> Path:
    """Where downloads land before a worktree exists to copy them into."""
    return workspace.parent / ".evidence"


def evidence_slot(failure: fix_plan.Failure) -> str:
    """The directory one failure's evidence lives in: `carameli-pr-412`, `devkit-branch-main`.

    One name per failure, used both under `evidence_root` and under `logs/gate/` when a
    devkit session gets several failures' logs side by side -- two failures of one
    project under a directory named for the project would overwrite each other.
    """
    where = failure.number or tb.slugify(failure.head or failure.base or "?")
    return f"{failure.project}-{failure.kind}-{where}"


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
        check_runs=run_ids_from_rollup(pr.get("statusCheckRollup")),
    )


def read_pr(project_dir: Path, failure: fix_plan.Failure, root: Path) -> fix_plan.Failure:
    """The PR's gate at its head sha: run, jobs, artifacts, into a signature.

    The gate workflow's run at the sha first; failing that, the runs the rollup names
    behind the failing checks. Three of nine ledger entries once read "no artifact and
    no failed step named" because the failing check belonged to another workflow, and
    each got a session sent blind at a run the rollup had the id of.
    """
    gh = sweep.gh_for(project_dir)
    conflicted = fix_plan.CONFLICT in failure.reason
    behind = not conflicted and is_behind(sweep.git_for(project_dir), failure.base, failure.sha)
    run = gate_run(gh, failure.head, failure.sha)
    gate_id = str(run.get("databaseId", "") or "")
    run_ids = [gate_id] if gate_id else list(failure.check_runs)
    if not run_ids:
        return replace(failure, signature=fix_plan.signature(conflicted, [], []), behind=behind)
    where = root / evidence_slot(failure)
    texts: list[str] = []
    jobs: list[dict] = []
    for run_id in run_ids:
        dest = where / run_id if len(run_ids) > 1 else where
        found = download_logs(gh, run_id, dest)
        texts += found
        jobs += run_jobs(gh, run_id) if not found else []
    sig = fix_plan.signature(conflicted, texts, jobs)
    return replace(
        failure,
        signature=sig,
        run_id=run_ids[0],
        evidence=str(where) if texts else "",
        behind=behind,
    )


def read_default_branch(
    project: str, project_dir: Path, root: Path
) -> tuple[bool | str | None, fix_plan.Failure | None]:
    """`(green, failure)` for the project's own default branch.

    `green` is None when the gate's verdict could not be read -- including when the
    newest completed run is not at the branch's tip, and when the run there was
    cancelled rather than judged. carameli's gate has no `push` trigger, so its "newest
    run on master" was a May run four months behind the tip, and a session was spent
    proving it stale; a verdict that is not about the current commit is no verdict. It
    is `fix_plan.RUNNING` when the run at the tip has not finished: the pass after every
    merge to devkit main used to read that as unreadable and hold every project fixer.
    A red run whose only failure is the newest-tag test is a release commit's: green
    once the tag points at that commit (the release workflow accepted it, and the test
    passes on the next push), and a failure the plan skips out loud while the tag does
    not exist yet.
    """
    gh = sweep.gh_for(project_dir)
    git = sweep.git_for(project_dir)
    base = tb.detect_default_branch(git, fallback="main")
    runs = default_branch_runs(gh, base)
    tip = branch_tip(git, base) if runs else ""
    if not tip:
        return None, None
    verdict, run = _tip_verdict(runs, tip)
    if verdict is not False:
        return verdict, None
    run_id = str(run.get("databaseId", "") or "")
    failure = fix_plan.Failure(
        kind=fix_plan.BRANCH,
        project=project,
        number=0,
        title=f"{run.get('workflowName') or GATE_WORKFLOW} red on {base}",
        url=str(run.get("url", "")),
        base=base,
        sha=str(run.get("headSha", "")),
        run_id=run_id,
        workflow=str(run.get("workflowName") or GATE_WORKFLOW),
    )
    where = root / evidence_slot(failure)
    texts = download_logs(gh, run_id, where) if run_id else []
    jobs = run_jobs(gh, run_id) if run_id and not texts else []
    sig = fix_plan.signature(False, texts, jobs)
    if fix_plan.is_release_red(sig) and is_tagged(git, failure.sha):
        return True, None
    return False, replace(failure, signature=sig, evidence=str(where) if texts else "")


def regate(project_dir: Path) -> tuple[bool, str]:
    """Run the gate on the checkout's default branch: `(dispatched, what for the record)`.

    For a branch whose verdict could not be read. The usual cause is a merge by the
    auto-merge workflow: a push made with `GITHUB_TOKEN` raises no `push` event, so
    nothing gates the new tip, and devkit's unreadable main held every project PR on
    every pass after. A `workflow_dispatch` is the one event that token may raise, and
    the run it starts is on the branch, so the next pass reads it like any other.
    """
    base = tb.detect_default_branch(sweep.git_for(project_dir), fallback="main")
    done = sweep.gh_for(project_dir)("workflow", "run", GATE_WORKFLOW, "--ref", base)
    if getattr(done, "returncode", 1) != 0:
        why = (getattr(done, "stderr", "") or getattr(done, "stdout", "") or "").strip()
        return False, f"{base} -- FAILED to re-run the gate: {why.splitlines()[-1] if why else '?'}"
    return True, f"{base} -- no verdict at the tip; gate re-run"


def collect_default_branches(
    workspace: Path, projects: list[str]
) -> dict[str, tuple[bool | str | None, fix_plan.Failure | None]]:
    """Every checkout's default branch, read the same way a PR's gate is."""
    root = evidence_root(workspace)
    verdicts = {}
    for name in projects:
        project_dir = workspace.parent / name
        if not project_dir.is_dir():
            continue
        sweep.git_for(project_dir)("fetch", "--quiet", "origin")
        verdicts[name] = read_default_branch(name, project_dir, root)
    return verdicts


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
    where = root / evidence_slot(failure)
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
        # One fetch per checkout, so `is_behind` and the tip checks compare against
        # what origin has now rather than whenever this checkout last looked.
        sweep.git_for(project_dir)("fetch", "--quiet", "origin")
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
    if source.resolve() == target.resolve():
        # A refused commit's evidence is written straight into the worktree the fixer
        # opens in; copying it onto itself would delete it first.
        return target
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(source, target)
    return target
