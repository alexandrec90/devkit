#!/usr/bin/env python3
"""What Dependabot cannot do for a project, as one failure the fix pass can send a fixer at.

The pass read the PR Gate and the harness ledger, and nothing Dependabot said reached
either. ibkr_trader's "Dependabot Updates" workflow failed 11 of its last 15 runs, and a
security update for urllib3 (< 2.8.0) never opened a PR, because every run died on
`The following path based dependencies could not be retrieved: "data-lake" at
/pyproject.toml` -- ibkr_trader declares `data-lake = { path = "../data-lake" }`, and the
Dependabot runner clones one repository. Nobody noticed for days.

Three of Dependabot's outputs are read here, per project:

- **its update runs**: the newest run of each update job (`uv in /. for urllib3`), and the
  error its failed log names (`run_errors`), less a package's error that a PR merged
  since the run has answered (`answered_since`);
- **its open alerts that no open PR answers**, once Dependabot has had `ALERT_GRACE` to
  open one -- matched by the package's name in a PR's title, branch or body, so a
  fixer's hand bump answers it as well as Dependabot's own PR would;
- **its red PRs** are not read here: `broken_pr_menu.scan` already lists every open PR,
  and `fix_plan.is_dependabot` is how the budget tells one from the rest.

Together they are one `fix_plan.DEPENDABOT` failure per project, which the plan places,
the ledger dedupes and the budget caps like any other red -- plus its own daily cap
(`fix_budget.DEPENDABOT_DAILY`). It is keyed by its signature, not by a commit: a merge
to the base changes nothing Dependabot said. A project whose only red is a path
dependency Dependabot cannot fetch, with every alert answered, is a line for the record
and no failure (`read_project`): no session can make Dependabot fetch it, and a skip
standing for a day would be filed as a stall and send the devkit session instead.

Every read answers empty on a `gh` that cannot -- alerts are a 403 where they are off.
Tested in `tests/test_fix_dependabot.py`.
"""

from __future__ import annotations

import datetime as _dt
import re
import sys
from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fix_plan
import gate_evidence
import sweep
import task_branch as tb
import worktree_env

# The dynamic workflow GitHub runs every Dependabot job under; `gh run list` takes the name.
WORKFLOW = "Dependabot Updates"
RUN_FIELDS = "databaseId,conclusion,status,url,displayTitle,createdAt,headSha"
RUN_LIMIT = 15
FAILED = frozenset({"failure", "timed_out", "startup_failure"})
# Failed jobs whose log is read per project per pass: one per update job is plenty, and
# a backlog of ten alerts is ten jobs failing for one reason.
LOGS_READ = 3
# How long Dependabot has to open a PR for an alert before the pass calls it unanswered.
ALERT_GRACE = _dt.timedelta(hours=24)
ALERTS = "repos/{owner}/{repo}/dependabot/alerts?state=open&per_page=100"
PR_FIELDS = "number,title,headRefName,body"
# Merged PRs read for a bump that answered a failed run after it ran (`answered_since`).
MERGED_FIELDS = f"{PR_FIELDS},mergedAt"
MERGED_LIMIT = 30
EVIDENCE_FILE = fix_plan.DEPENDABOT_EVIDENCE

# `uv in /. for urllib3 - Update #1607644272`: the job is everything before the counter.
_UPDATE_SUFFIX = re.compile(r"\s+-\s+Update\s+#\d+\s*$")
_ERROR = re.compile(r"\bERROR <job_\d+> (.+?)\s*$")
_HANDLED = re.compile(r"Handled error whilst updating ([^:]+): (\w+)")
# The signature entry `run_errors` makes of a handled error: `run sqlalchemy: <kind>`.
_HANDLED_ENTRY = re.compile(r"run ([^\s:]+): (\w+)")
_UNFETCHABLE = re.compile(r"path based dependencies could not be retrieved: (.+?) at \S+")
_QUOTED = re.compile(r'"([^"]+)"')
_REASON_LIMIT = 160

Gh = Callable[..., object]


# --- what each output says -----------------------------------------------------------


def update_job(run: dict) -> str:
    """The update job a run belongs to: its title less the `- Update #N` counter."""
    return _UPDATE_SUFFIX.sub("", str(run.get("displayTitle", ""))).strip()


def failing_updates(runs: Iterable[dict]) -> list[dict]:
    """The newest run of each update job, where that run failed; newest first.

    A job that failed last week and passed since is not failing, so only its newest
    run counts -- `runs` is `gh run list`'s order, newest first.
    """
    seen: set[str] = set()
    failing = []
    for run in runs:
        job = update_job(run)
        if not job or job in seen:
            continue
        seen.add(job)
        if str(run.get("conclusion", "")).lower() in FAILED:
            failing.append(run)
    return failing


def run_errors(log: str) -> list[str]:
    """The signature entries one failed job's log names; deduped, in order.

    An unfetchable path dependency is named by the dependency alone, so every job that
    dies on it is one entry. A handled error is the package and its kind. Anything else
    the updater logged as an `ERROR` is its text, cut short.
    """
    found: list[str] = []
    for line in log.splitlines():
        if unfetchable := _UNFETCHABLE.search(line):
            names = _QUOTED.findall(unfetchable.group(1)) or [unfetchable.group(1)]
            found += [f"{fix_plan.UNFETCHABLE_ENTRY}{name.strip()}" for name in names]
        elif handled := _HANDLED.search(line):
            found.append(f"run {handled.group(1).strip()}: {handled.group(2)}")
        elif error := _ERROR.search(line):
            found.append(f"run {error.group(1)[:_REASON_LIMIT]}")
    return list(dict.fromkeys(found))


def _norm(name: str) -> str:
    """A package name as PEP 503 compares them."""
    return re.sub(r"[-_.]+", "-", str(name)).lower()


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", str(text)))


def _names(pr: dict, package: str) -> bool:
    """Whether a PR names `package` in its title, head branch or body, as a whole name:
    `urllib3` in `dependabot/uv/urllib3-2.8.0`, not `requests` in `requests-oauthlib`."""
    text = _norm(" ".join(str(pr.get(k, "")) for k in ("title", "headRefName", "body")))
    whole = rf"(?<![a-z0-9-]){re.escape(package)}(?![a-z0-9]|-[a-z])"
    return re.search(whole, text) is not None


def _stamp(row: dict, key: str) -> _dt.datetime | None:
    try:
        when = _dt.datetime.fromisoformat(str(row.get(key, "")).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=_dt.UTC)


def _created(alert: dict) -> _dt.datetime | None:
    return _stamp(alert, "created_at")


def answered_since(entry: str, run: dict, merged: Iterable[dict]) -> str:
    """`#N` of a merged PR naming the package `entry`'s handled error names, merged after
    `run` started; "" when there is none, or `entry` names no package.

    Dependabot runs a version-update job on its schedule, a week apart, so its newest
    run of a job can predate the bump that answered it by days. data-lake's run on
    135bfa8 said `sqlalchemy: dependency_file_content_not_changed`; #50 bumped sqlalchemy
    two hours later, and the next pass sent a fixer at the same run anyway (6b4570a4).
    A run after the merge that says it again is a fresh failure, and is read as one.
    """
    handled = _HANDLED_ENTRY.fullmatch(entry)
    started = _stamp(run, "createdAt")
    if not handled or started is None:
        return ""
    package = _norm(handled.group(1))
    for pr in merged:
        at = _stamp(pr, "mergedAt")
        if at is not None and at > started and _names(pr, package):
            return f"#{pr.get('number', '')}"
    return ""


def _patched(alert: dict) -> str:
    vuln = alert.get("security_vulnerability")
    first = vuln.get("first_patched_version") if isinstance(vuln, dict) else None
    return str(first.get("identifier", "")) if isinstance(first, dict) else ""


def package_of(alert: dict) -> str:
    """The alert's package, normalized; "" for a shape that names none."""
    dependency = alert.get("dependency")
    package = dependency.get("package") if isinstance(dependency, dict) else None
    return _norm(package.get("name", "")) if isinstance(package, dict) else ""


def unanswered(alerts: Iterable[dict], prs: Iterable[dict], now: _dt.datetime) -> dict[str, str]:
    """`package -> the version that fixes every open alert on it`, for the packages no
    open PR names, among alerts older than `ALERT_GRACE` with a patched version at all."""
    listed = [pr for pr in prs if isinstance(pr, dict)]
    wanted: dict[str, str] = {}
    for alert in alerts:
        if not isinstance(alert, dict):
            continue
        package, patched, created = package_of(alert), _patched(alert), _created(alert)
        if not package or not patched or created is None or now - created < ALERT_GRACE:
            continue
        if any(_names(pr, package) for pr in listed):
            continue
        if _version(patched) > _version(wanted.get(package, "")):
            wanted[package] = patched
    return wanted


def signature(errors: Iterable[str], wanted: dict[str, str]) -> tuple[str, ...]:
    """The alerts first, by package, then what the runs said."""
    alerts = [f"{fix_plan.ALERT_ENTRY}{pkg} >= {wanted[pkg]}" for pkg in sorted(wanted)]
    return (*alerts, *sorted(set(errors)))


def actionable(sig: Iterable[str]) -> bool:
    """Whether a session can change what the signature says: an alert to bump, or a run
    failing on something other than a path dependency Dependabot cannot fetch."""
    return any(not entry.startswith(fix_plan.UNFETCHABLE_ENTRY) for entry in sig)


def unfetchable(sig: Iterable[str]) -> list[str]:
    """The path dependencies the signature says Dependabot cannot fetch."""
    return [
        e.removeprefix(fix_plan.UNFETCHABLE_ENTRY)
        for e in sig
        if e.startswith(fix_plan.UNFETCHABLE_ENTRY)
    ]


# --- asking GitHub -------------------------------------------------------------------


def _listed(result: object) -> list[dict]:
    parsed = gate_evidence.gh_json(result)
    return [row for row in parsed if isinstance(row, dict)] if isinstance(parsed, list) else []


def update_runs(gh: Gh) -> list[dict]:
    return _listed(
        gh("run", "list", "--workflow", WORKFLOW, "--limit", str(RUN_LIMIT), "--json", RUN_FIELDS)
    )


def open_alerts(gh: Gh) -> list[dict]:
    """The open alerts; empty where alerts are off (a 403) or `gh` cannot say."""
    return _listed(gh("api", ALERTS))


def open_prs(gh: Gh) -> list[dict]:
    return _listed(gh("pr", "list", "--state", "open", "--limit", "100", "--json", PR_FIELDS))


def merged_prs(gh: Gh) -> list[dict]:
    return _listed(
        gh("pr", "list", "--state", "merged", "--limit", str(MERGED_LIMIT), "--json", MERGED_FIELDS)
    )


def failed_log(gh: Gh, run: dict) -> str:
    done = gh("run", "view", str(run.get("databaseId", "")), "--log-failed")
    return str(getattr(done, "stdout", "") or "") if getattr(done, "returncode", 1) == 0 else ""


# --- one project ---------------------------------------------------------------------


def _sibling_lines(project_dir: Path, names: list[str]) -> list[str]:
    """Where each unfetchable dependency comes from, and the ref this repo's CI pins it at."""
    sources = worktree_env.path_sources(project_dir)
    lines = []
    for name in names:
        path = next((p for p in sources if p.rstrip("/").rsplit("/", 1)[-1] == name), "")
        pin = worktree_env.pinned_ref(project_dir, name)
        declared = f"declared as a [tool.uv.sources] path, {path}" if path else "a path dependency"
        pinned = f"; the PR gate checks it out at {pin}" if pin else ""
        lines.append(f"- {name}: {declared}{pinned}")
    return lines


def ran_on(run: dict) -> str:
    """`run on 135bfa8 at 2026-10-04T20:22:50Z`: what a fixer holds against the base's
    log to tell a run that predates a fix from one that outlived it."""
    sha = str(run.get("headSha", "") or "")[:7] or "an unknown commit"
    return f"run on {sha} at {run.get('createdAt', '') or 'an unknown time'}"


def read_runs(gh: Gh) -> tuple[list[tuple[dict, list[str]]], list[dict], list[str]]:
    """`(failing jobs read, with what each still says; every failing run still counted;
    a line per run a later merge answered)`.

    A run's handled errors that a bump merged since answers are dropped
    (`answered_since`), and a run left saying nothing is not failing at all: it is a
    line for the record until Dependabot runs the job again.
    """
    failing_runs = failing_updates(update_runs(gh))
    read = [(run, run_errors(failed_log(gh, run))) for run in failing_runs[:LOGS_READ]]
    handled = any(_HANDLED_ENTRY.fullmatch(e) for _, said in read for e in said)
    merged = merged_prs(gh) if handled else []
    failing, stale, notes = [], [], []
    for run, said in read:
        answered = {e: answered_since(e, run, merged) for e in said}
        live = [e for e in said if not answered[e]]
        if said and not live:
            stale.append(run)
            by = ", ".join(sorted(set(answered.values())))
            notes.append(f"{update_job(run)} failed ({ran_on(run)}), and {by} answered it since")
            continue
        failing.append((run, live))
    return failing, [run for run in failing_runs if run not in stale], notes


def evidence_text(
    project: str, failing: list[tuple[dict, list[str]]], alerts: list[dict], wanted: dict[str, str]
) -> str:
    """The Markdown the fixer reads first: every failing job and every unanswered alert."""
    lines = [f"# What Dependabot cannot do in {project}", ""]
    if failing:
        lines += ["## Failing update jobs", ""]
        for run, errors in failing:
            lines.append(f"- {update_job(run)} -- {run.get('url', '')} ({ran_on(run)})")
            lines += [f"  - {error}" for error in errors] or ["  - (no error line in the log)"]
        lines.append("")
    if wanted:
        lines += ["## Open alerts no PR answers", ""]
        for package in sorted(wanted):
            lines.append(f"- {package}: bump to {wanted[package]} or later")
            for alert in alerts:
                if package_of(alert) == package:
                    advisory = alert.get("security_advisory") or {}
                    summary = advisory.get("summary", "") if isinstance(advisory, dict) else ""
                    lines.append(f"  - {alert.get('html_url', '')} {summary}".rstrip())
        lines.append("")
    return "\n".join(lines)


def _title(project: str, wanted: dict[str, str], failing: int) -> str:
    parts = [f"{len(wanted)} package(s) with alerts no PR answers"] if wanted else []
    parts += [f"{failing} update job(s) failing"] if failing else []
    return f"Dependabot in {project}: {', '.join(parts)}"


def _alerts_page(alerts: list[dict]) -> str:
    url = str(alerts[0].get("html_url", "")) if alerts else ""
    return url.rsplit("/", 1)[0] if url else ""


def read_project(
    project: str, project_dir: Path, root: Path, now: _dt.datetime, gh: Gh
) -> tuple[fix_plan.Failure | None, str]:
    """`(failure, note)`: a failure when a session has something to do, a line for the
    record when Dependabot is red on what no session can change or a merge has answered
    since its run, neither when green."""
    failing, failing_runs, answered = read_runs(gh)
    stale = "; ".join(f"{project} -- {WORKFLOW}: {line}" for line in answered)
    errors = [e for _, run_said in failing for e in run_said] or [
        f"run {update_job(run)}: failed" for run, _ in failing
    ]
    alerts = open_alerts(gh)
    wanted = unanswered(alerts, open_prs(gh), now) if alerts else {}
    sig = signature(errors, wanted)
    if not sig:
        return None, stale
    names = unfetchable(sig)
    if not actionable(sig):
        return None, (
            f"{project} -- {WORKFLOW} fails only because it cannot fetch the path "
            f"dependency {', '.join(names)}, and every alert has a PR: nothing to send. "
            f"A source Dependabot can fetch for it would turn it green."
        )
    failure = fix_plan.Failure(
        kind=fix_plan.DEPENDABOT,
        project=project,
        number=0,
        title=_title(project, wanted, len(failing_runs)),
        url=str(failing_runs[0].get("url", "")) if failing_runs else _alerts_page(alerts),
        base=tb.detect_default_branch(sweep.git_for(project_dir), fallback="main"),
        workflow=WORKFLOW,
        signature=sig,
    )
    where = root / gate_evidence.evidence_slot(failure)
    where.mkdir(parents=True, exist_ok=True)
    text = evidence_text(project, failing, alerts, wanted)
    if names:
        text += "\n## Path dependencies Dependabot cannot fetch\n\n"
        text += "\n".join(_sibling_lines(project_dir, names)) + "\n"
    (where / EVIDENCE_FILE).write_text(text, encoding="utf-8")
    return replace(failure, evidence=str(where)), stale


def collect(
    workspace: Path, projects: list[str], now: _dt.datetime | None = None, gh_for=sweep.gh_for
) -> tuple[list[fix_plan.Failure], list[str]]:
    """Every registered checkout's Dependabot red: `(failures, notes for the record)`."""
    now = now or _dt.datetime.now(_dt.UTC)
    root = gate_evidence.evidence_root(workspace)
    failures: list[fix_plan.Failure] = []
    notes: list[str] = []
    for project in projects:
        project_dir = workspace.parent / project
        if not project_dir.is_dir():
            continue
        failure, note = read_project(project, project_dir, root, now, gh_for(project_dir))
        failures += [failure] if failure else []
        notes += [note] if note else []
    return failures, notes
