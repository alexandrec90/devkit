"""The release side of the fix pass: merge green adoptions, name the open ones, cut a tag.

A fix merged into devkit's `main` reaches no consumer until a tag carries it, so a pass
that only sends fixers can spend a night sending them at reds the next release would
clear -- a consumer's vendored test failing against the copy it holds is fixed upstream
and waits on the tag, not on a session. The nightly `devkit-release` job used to be the
only thing that cut one, and the day it died on a dubious-ownership refusal nothing else
noticed. So the pass cuts it too, under the same predicate the nightly job uses
(`release-pipeline.release_needed`), and it never waits for it.

**Started, never awaited.** `release-pipeline.py` blocks on two CI gates; run inline, a
half-hourly pass would spend twenty minutes on one release. It is spawned detached
under `log-wrap.py` with its own label, so its record is `logs/<slug>.log` in the devkit
checkout and a click or the nightly run cannot overwrite it. A start is refused while:

- devkit's own default branch is not green -- the pipeline stops on any red but the
  release test, so starting would only open a PR it will abandon;
- a `release/` PR is already open -- one is in flight;
- this pass started one less than `STARTED_COOLDOWN` ago -- the gap between that PR
  merging and the tag landing has neither a PR nor a tag to see.

Each refusal is one line on the record, as is the start.
"""

from __future__ import annotations

import datetime as _dt
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "precommit"))
import adoption_prs
import fix_cycle
import fix_plan
import gate_evidence
import sweep
import worktree
from _loader import load_by_path

SCRIPTS = Path(__file__).resolve().parent
release_pipeline = load_by_path("_fix_release_pipeline", SCRIPTS / "release-pipeline.py")
# The nightly job's installer, for the pieces of its command this start shares with it.
release_schedule = load_by_path("_fix_release_schedule", SCRIPTS / "install-release-schedule.py")

# Its own label, so its `log-wrap.py` record is neither the click's nor the nightly's.
LABEL = "Fix pass: Devkit Release"
# Where log-wrap.py writes under that label, in the devkit checkout.
RECORD = Path("logs") / "fix-pass-devkit-release.log"
# Written in the devkit checkout when a start goes out; its mtime is the cooldown clock.
STARTED = Path("logs") / "fix-pass-release.started"
# Longer than the pipeline's slowest honest run: two gates, the tag workflow, adoption.
STARTED_COOLDOWN = _dt.timedelta(hours=2)
# A child that outlives the pass must not share its console control group.
NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

adoption_prefixes = adoption_prs.adoption_prefixes


def pending_adoptions(root: Path, projects: list[str], tag: str) -> list[str]:
    """Projects with the newest release still up for adoption -- the harness mid-flight."""
    if not tag:
        return []
    return [
        name
        for name in projects
        if name != fix_cycle.DEVKIT
        and (root / name).is_dir()
        and adoption_prs.open_adoption_pr(root / name, tag)
    ]


def merge_green_adoptions(root: Path, projects: list[str]) -> list[str]:
    """The one merge the pass makes; `(lines for the record)`."""
    merged: list[str] = []
    prefixes = adoption_prefixes()
    for name in projects:
        project_dir = root / name
        if name == fix_cycle.DEVKIT or not project_dir.is_dir():
            continue
        gh = sweep.gh_for(project_dir)
        listed = gh(
            "pr",
            "list",
            "--state",
            "open",
            "--limit",
            "50",
            "--json",
            "number,headRefName,isDraft,labels,mergeable,statusCheckRollup",
        )
        rows = gate_evidence.gh_json(listed)
        if not isinstance(rows, list):
            rows = []
        for row in adoption_prs.green_adoptions(rows, prefixes, sweep.AUTOMERGE_LABEL):
            ok, message = worktree.merge_pr(gh, int(row.get("number", 0)))
            merged.append(
                f"{name} #{row.get('number')} -- {message if ok else 'FAILED: ' + message}"
            )
    return merged


def release_refusal(
    owed: list[str],
    tag: str,
    devkit_green: bool | str | None,
    in_flight: int,
    started: _dt.datetime | None,
    now: _dt.datetime,
) -> str:
    """Why no release starts this pass, or "" when one should. Pure."""
    if not tag:
        return "devkit has no release tag to measure against; cut the first one by hand"
    if not owed:
        return ""
    what = f"{len(owed)} change(s) {tag} cannot deliver"
    if devkit_green is not True:
        return f"{what}, but devkit's default branch is not green; the release waits for it"
    if in_flight:
        return f"{what}; release PR #{in_flight} is already in flight"
    if started and now - started < STARTED_COOLDOWN:
        return f"{what}; this pass started one at {started.isoformat(timespec='seconds')}"
    return ""


def release_pr_in_flight(devkit: Path) -> int:
    """The number of an open `release/` PR in devkit, or 0."""
    listed = sweep.gh_for(devkit)(
        "pr", "list", "--state", "open", "--limit", "50", "--json", "number,headRefName"
    )
    rows = gate_evidence.gh_json(listed)
    for row in rows if isinstance(rows, list) else []:
        if str(row.get("headRefName") or "").startswith(fix_plan.RELEASE_PREFIX):
            return int(row.get("number") or 0)
    return 0


def last_started(devkit: Path) -> _dt.datetime | None:
    try:
        stamp = (devkit / STARTED).stat().st_mtime
    except OSError:
        return None
    return _dt.datetime.fromtimestamp(stamp, _dt.UTC)


def release_argv(devkit: Path, workspace: Path, python: str) -> list[str]:
    """The nightly job's command, under this pass's label: log-wrap around the pipeline.

    `python` is a console interpreter for both hops (`sweep.console_python`), for the
    reason `windowless-jobs.md` gives: `CREATE_NO_WINDOW` is ignored for `pythonw.exe`.
    """
    return [
        python,
        str(devkit / "scripts" / "log-wrap.py"),
        "--always",
        LABEL,
        "--",
        python,
        str(devkit / "scripts" / "release-pipeline.py"),
        *release_schedule.PIPELINE_ARGS,
        "--workspace",
        str(workspace),
    ]


def start_release(devkit: Path, workspace: Path) -> str:
    """Spawn the pipeline detached and stamp the start; "" on success, else why not."""
    try:
        subprocess.Popen(
            release_argv(devkit, workspace, sweep.console_python()),
            cwd=str(devkit),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=sweep.NO_WINDOW | NEW_GROUP,
        )
    except OSError as exc:
        return str(exc)
    stamp = devkit / STARTED
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text("", encoding="utf-8")
    return ""


def cut_release(
    workspace: Path,
    tag: str,
    devkit_green: bool | str | None,
    dispatching: bool,
    now: _dt.datetime,
) -> str:
    """The pass's release step: one line for the record, or "" when nothing is owed."""
    devkit = workspace.parent / fix_cycle.DEVKIT
    if not devkit.is_dir():
        return ""
    sweep.git_for(devkit)("fetch", "--quiet", "--tags", "origin")
    owed = release_pipeline.release_needed(devkit, tag) if tag else []
    in_flight = release_pr_in_flight(devkit) if owed else 0
    if why := release_refusal(owed, tag, devkit_green, in_flight, last_started(devkit), now):
        return f"not started -- {why}"
    if not owed:
        return ""
    what = f"{len(owed)} change(s) {tag} cannot deliver: {', '.join(owed)}"
    if not dispatching:
        return f"would start -- {what}"
    if failed := start_release(devkit, workspace):
        return f"FAILED to start -- {failed}"
    return f"started -- {what}; record at {RECORD.as_posix()}"
