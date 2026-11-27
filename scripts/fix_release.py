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
  release test, so starting would only open a PR it will abandon. A main red on that
  test alone is a merged bump waiting on its tag, which the start is what delivers
  (`release_verdict`);
- a `release/` PR is already open -- one is in flight;
- this pass started one less than `STARTED_COOLDOWN` ago -- the gap between that PR
  merging and the tag landing has neither a PR nor a tag to see.

Each refusal is one line on the record, as is the start.
"""

from __future__ import annotations

import datetime as _dt
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "precommit"))
import adoption_prs
import devkit_project
import fix_cycle
import fix_plan
import gate_evidence
import project_registry
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
# Past the pipeline's own run: two gates, the tag workflow, adoption. Its PR and its tag
# workflow are both seen while they last (`release_pr_in_flight`, `tag_in_flight`), so this
# covers only the minutes neither exists. At two hours it was the longest wait between a
# merged fix and the projects that needed it.
STARTED_COOLDOWN = _dt.timedelta(minutes=45)

# An adoption pass of its own, for a release whose adoptions did not all open.
ADOPT_LABEL = "Fix pass: Adopt Release"
ADOPT_RECORD = Path("logs") / "fix-pass-adopt-release.log"
ADOPT_STARTED = Path("logs") / "fix-pass-adopt.started"
# How long a tag is the pipeline's own to adopt, and how long between retries: a project
# whose adoption someone closed is behind for good, and is asked again no oftener.
ADOPT_AFTER = _dt.timedelta(hours=1)
ADOPT_COOLDOWN = _dt.timedelta(hours=6)
PINNED_REF = re.compile(r"^\s*ref:\s*(v\d+\.\d+\.\d+)\s*$", re.M)
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
            merged.append(f"{name} #{row.get('number')} -- {merge_line(ok, message)}")
    return merged


def merge_line(ok: bool, message: str) -> str:
    """A merge's outcome as the record says it. One that GitHub failed on its own side is
    deferred, without the `FAILED` that files it: the PR is still green and open, so the
    next pass merges it (a0e74427: roguelike #165, merged by the pass after)."""
    if ok:
        return message
    if sweep.transient(message):
        return f"merge deferred to the next pass: {message}"
    return f"FAILED: {message}"


def release_verdict(
    devkit_green: bool | str | None, failures: list[fix_plan.Failure]
) -> bool | str | None:
    """devkit's default-branch verdict as the release step reads it. Pure.

    A main red on the newest-tag test alone is a release whose bump merged and whose tag
    never landed, and only the tag clears that red: holding the release for a green main
    then holds it for good. On 2026-11-11 v0.11.87's tag run failed, main stayed red on that
    test alone, `green` stopped advancing, and the scheduled jobs ran a fix merged hours
    before from a checkout that could not reach it (c6c538d9, devkit #763). So that red reads
    as green here: the pipeline resumes at the tag (`release-pipeline.run_pipeline`), and
    `release.yml` runs the full suite on the commit before it pushes one.
    """
    if devkit_green is not False:
        return devkit_green
    return any(
        f.kind == fix_plan.BRANCH
        and f.project == fix_cycle.DEVKIT
        and fix_plan.is_release_red(f.signature)
        for f in failures
    )


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


def tag_in_flight(devkit: Path) -> int:
    """The tag workflow's run still going, or 0: a release between its merge and its tag."""
    return release_pipeline.tag_run_in_flight(devkit)


def last_started(devkit: Path, stamp_file: Path = STARTED) -> _dt.datetime | None:
    try:
        stamp = (devkit / stamp_file).stat().st_mtime
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
    return spawn(devkit, release_argv(devkit, workspace, sweep.console_python()), STARTED)


def spawn(devkit: Path, argv: list[str], stamp_file: Path) -> str:
    """`argv` started detached, and `stamp_file` written; "" on success, else why not."""
    try:
        subprocess.Popen(
            argv,
            cwd=str(devkit),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=sweep.NO_WINDOW | NEW_GROUP,
        )
    except OSError as exc:
        return str(exc)
    stamp = devkit / stamp_file
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text("", encoding="utf-8")
    return ""


def unadopted(root: Path, projects: list[str], tag: str) -> list[str]:
    """Projects whose default branch pins another release than `tag`, with no PR open that
    would change it. A pin nobody can read is not behind."""
    behind = []
    for name in projects:
        if name == fix_cycle.DEVKIT or not (root / name).is_dir():
            continue
        shown = sweep.git_for(root / name)("show", "origin/HEAD:.github/workflows/pr-gate.yml")
        pins = PINNED_REF.findall(shown.stdout or "") if shown.returncode == 0 else []
        if pins and tag not in pins and not adoption_prs.open_adoption_pr(root / name, tag):
            behind.append(name)
    return behind


def tagged_at(devkit: Path, tag: str) -> _dt.datetime | None:
    """When `tag`'s commit was made, or None."""
    done = sweep.git_for(devkit)("log", "-1", "--format=%ct", tag)
    stamp = (done.stdout or "").strip() if done.returncode == 0 else ""
    return _dt.datetime.fromtimestamp(int(stamp), _dt.UTC) if stamp.isdigit() else None


def retry_adoptions(workspace: Path, tag: str, dispatching: bool, now: _dt.datetime) -> str:
    """Open the adoptions a release's own pass did not: a line for the record, or "".

    On 2026-11-25 GitHub answered 500 to six of them, and the next thing to try was the
    03:00 job (8a1e0b41).
    """
    devkit = workspace.parent / fix_cycle.DEVKIT
    made = tagged_at(devkit, tag)
    if made is None or now - made < ADOPT_AFTER:
        return ""
    last = last_started(devkit, ADOPT_STARTED)
    if last and now - last < ADOPT_COOLDOWN:
        return ""
    try:
        # Off the project registry; `workspace` only names where the checkouts sit.
        projects = devkit_project.known_projects()
    except project_registry.RegistryError:
        return ""
    behind = unadopted(workspace.parent, projects, tag)
    if not behind:
        return ""
    what = f"{tag} has no adoption PR in {', '.join(behind)}"
    if not dispatching:
        return f"would open the adoptions -- {what}"
    if failed := spawn(devkit, adopt_argv(devkit, sweep.console_python()), ADOPT_STARTED):
        return f"FAILED to start the adoptions -- {failed}"
    return f"opening the adoptions -- {what}; record at {ADOPT_RECORD.as_posix()}"


def adopt_argv(devkit: Path, python: str) -> list[str]:
    """The daily upgrade job's command, under this pass's own label."""
    upgrade = [python, str(devkit / "scripts" / "upgrade-project.py"), "--all", "--yes"]
    wrapper = [python, str(devkit / "scripts" / "log-wrap.py"), "--always", ADOPT_LABEL, "--"]
    return wrapper + upgrade


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
        return retry_adoptions(workspace, tag, dispatching, now)
    what = f"{len(owed)} change(s) {tag} cannot deliver: {', '.join(owed)}"
    if not dispatching:
        return f"would start -- {what}"
    if run_id := tag_in_flight(devkit):
        return f"not started -- {what}; run {run_id} is cutting a tag"
    if failed := start_release(devkit, workspace):
        return f"FAILED to start -- {failed}"
    return f"started -- {what}; record at {RECORD.as_posix()}"
