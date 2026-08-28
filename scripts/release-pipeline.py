#!/usr/bin/env python3
"""One click from a merged vendored fix to every consumer's adoption PR.

`release.py` automates the two *halves* of a release and `RELEASING.md` narrates the
seam between them: open the prepare PR, wait for its gate, read the gate correctly,
merge, dispatch the tag workflow, wait again, fetch, then upgrade every consumer. Each
of those is one command, and every one of them was a human's -- which in this workspace
means a coding agent's, since that is who runs commands here. A release therefore cost
a session, and the cost fell exactly when nobody wanted to spend one: right after
merging the fix that made the release necessary.

So the seam is the thing this automates, and `Devkit: Cut Release` is the click that
runs it. It is **also** a scheduled job -- see `install-release-schedule.py` -- because
the decision it encodes turned out to be smaller than it looked.

"Tag every merge to main" really is the wrong automation, and not for the ordering
reason `release.yml`'s header gives: that objection is to tagging the merge commit
*itself*, which this script never does. The real objection is cost. A doc fix, a
generator change or a test is not a thing any consumer can adopt, and tagging one
spends a release plus an adoption PR in every project to deliver nothing.

But **"release what a consumer cannot otherwise reach" is not a judgement at all.**
`release_needed` computes it from the diff between the newest tag and `origin/main`,
restricted to the two tiers a consumer actually receives -- the vendored `MANIFEST`
and the published pre-commit channel. That is the same condition `upgrade-project.py`
already detects and warns about, so the nightly pass was in the position of announcing
a problem it had everything it needed to fix. `--if-needed` is that predicate as a
flag, and it is the entire difference between the click and the 2am pass.

What it does, in order:

1.  pick the version (`patch`/`minor`/`major` off the newest tag, or an explicit `vX.Y.Z`)
2.  bump `FALLBACK_DEVKIT_REF` on a `release/vX.Y.Z` branch cut from `origin/main`
3.  open its PR **as the authenticated user**, never as a token -- a bot-authored PR
    gets no PR Gate, which is the trap `RELEASING.md` warns about
4.  wait for the gate in one blocking call
5.  **verify the red is the expected red** -- see `gate_verdict`
6.  squash-merge it
7.  dispatch `release.yml phase=tag`, which runs the suite against the tagged commit
    before pushing the tag, and wait for it
8.  fetch the new tag and hand off to `upgrade-project.py`, which opens an adoption PR
    per consumer and labels each one for auto-merge

Step 8's *scope* is the one thing the click asks that the schedule cannot: `--projects`
takes the consumers ticked in `Devkit: Cut Release`'s checklist, and omitting it means
`--all`, which is what the 2am pass wants. Narrowing it does not narrow the release --
the tag is the tag -- it decides only whose adoption PR opens tonight.

Step 5 is the one that earns the script. Every other step is a command someone could
type; that one is a judgement -- *this* failing check, and only this one, is the
release's own chicken-and-egg -- and a judgement typed at 1am against a red PR is how a
genuinely broken release gets merged. Encoded, it is also the only step that can refuse.

**Nothing here touches the static checkout's working tree.** The bump happens in a
throwaway worktree cut from `origin/main`, so a release can be cut while the checkout
sits on someone else's branch with uncommitted work -- unlike `release.py --yes`, which
runs `git checkout -b` in place. It writes no artifact of its own, because both of its
callers wrap it in `log-wrap.py`: `devkit_project.plan_command` does it for the click,
and `install-release-schedule.py` does it for the scheduled pass, under a *different*
label so a click cannot overwrite the unattended run's only record.

Pure and stdlib-only; every decision is an importable function tested in
`tests/test_release_pipeline.py`.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release
import sweep
import task_branch

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent

# Spelled once, from `sweep`, so this module holds no second definition of the flag the
# scheduled-job suite scans for. See `_run`.
NO_WINDOW = sweep.NO_WINDOW

# The check whose failure a release is *supposed* to cause, and the job that reports it.
#
# `test_fallback_devkit_ref_tracks_the_newest_tag` compares `FALLBACK_DEVKIT_REF`
# against `git describe --tags`. On a prepare PR the constant already names the release
# and the tag does not exist yet, so it fails by construction -- and it has to stay that
# way round: the alternative orderings either tag a commit whose constant is stale, or
# delete the one check that catches a fallback nobody bumped. `RELEASING.md` calls this
# "expect exactly one red test"; this is that sentence as code.
EXPECTED_RED_TEST = "test_fallback_devkit_ref_tracks_the_newest_tag"
GATE_TEST_JOB = "Lint + hook-script tests"
GATE_TEST_ARTIFACT = "test-failures"
GATE_TEST_ARTIFACT_FILE = "logs/test-failures.log"

RELEASE_WORKFLOW = "release.yml"

# The second tier a release delivers, and the one `MANIFEST` does not cover.
#
# devkit ships through two channels (see `scripts/CLAUDE.md`, "The two channels"). The vendored
# tier is copied in by `sync-devkit.py --pull` and is exactly `MANIFEST`. The pre-commit
# tier is *not*: a consumer pins devkit by `rev` in its `.pre-commit-config.yaml` and
# pre-commit clones this repo at that rev, so these files reach a consumer only when a
# tag exists to pin. That is the failure `RELEASING.md` records -- a rendered config
# requesting hook ids its pinned tag could not serve, which aborted a new project's
# first commit -- and a trigger reading `MANIFEST` alone would miss every instance of
# it, because neither of these paths is vendored.
PUBLISHED_CHANNEL = (".pre-commit-hooks.yaml", "scripts/precommit/")

# Bump levels, and how each moves (major, minor, patch).
BUMPS: dict[str, tuple[int, int, int]] = {
    "major": (1, 0, 0),
    "minor": (0, 1, 0),
    "patch": (0, 0, 1),
}

# The first release of a repo that has none. Only reachable in a fresh clone of a
# fork -- devkit itself has tags -- but "no tags" must resolve to a version rather
# than to a crash, since the whole point of this script is to be the thing that ends
# the untagged state.
FIRST_VERSION = "v0.1.0"

# How long to wait for a dispatched workflow to appear in `gh run list`. The dispatch
# returns before the run is queryable, so the id has to be discovered by polling; this
# is a subprocess loop in a terminal, not an agent poll, and costs nothing but seconds.
RUN_DISCOVERY_TIMEOUT = 120.0
RUN_DISCOVERY_INTERVAL = 5.0

# `gh pr checks --watch` fails immediately when a PR has no checks yet, which is the
# normal state for the first few seconds after `gh pr create`. Retried rather than
# treated as "the gate is done".
CHECKS_APPEAR_TIMEOUT = 180.0
CHECKS_APPEAR_INTERVAL = 10.0

VERDICT_PROCEED = "PROCEED"
VERDICT_WAIT = "WAIT"
VERDICT_STOP = "STOP"


# --- pure decisions ---------------------------------------------------------


def parse_version(tag: str) -> tuple[int, int, int] | None:
    """`(major, minor, patch)` for a release tag, or None when it is not one."""
    if not release.valid_version(tag):
        return None
    major, minor, patch = tag.lstrip("v").split(".")
    return int(major), int(minor), int(patch)


def newest_release(tags: Sequence[str]) -> str:
    """The highest release tag in `tags`, or "" when there are none.

    Ordered numerically, never lexically: `v0.9.0` sorts *after* `v0.11.1` as a string,
    and a release pipeline that believes that cuts `v0.9.1` over the top of eleven
    releases. `git tag --sort=-v:refname` gets this right and this function exists so
    the answer does not depend on the caller having remembered to ask for it.
    """
    parsed = [(parse_version(tag), tag) for tag in tags]
    ranked = [(version, tag) for version, tag in parsed if version is not None]
    if not ranked:
        return ""
    return max(ranked)[1]


def next_version(tags: Sequence[str], level: str) -> tuple[str, str]:
    """`(version, refusal)` for `level` against the tags that already exist.

    `level` is a bump name or an explicit `vX.Y.Z`. An explicit version is still
    checked against `tags`: releases are immutable, and re-cutting one silently would
    move a ref that consumers pin.
    """
    if level in BUMPS:
        newest = newest_release(tags)
        if not newest:
            return FIRST_VERSION, ""
        # `newest_release` only ever returns a tag it could parse, so `parse_version`
        # cannot be None here -- written as a fallback rather than an assertion because
        # `S101` is on, and because a release pipeline is a poor place to discover that
        # `-O` strips your checks.
        major, minor, patch = parse_version(newest) or (0, 0, 0)
        if level == "major":
            bumped = (major + 1, 0, 0)
        elif level == "minor":
            bumped = (major, minor + 1, 0)
        else:
            bumped = (major, minor, patch + 1)
        return f"v{bumped[0]}.{bumped[1]}.{bumped[2]}", ""
    if not release.valid_version(level):
        return "", (
            f"{level!r} is neither a bump level ({', '.join(sorted(BUMPS))}) "
            f"nor a vMAJOR.MINOR.PATCH tag"
        )
    if level in tags:
        return "", f"{level} already exists -- releases are immutable, pick the next patch"
    return level, ""


def failing_test_names(artifact_text: str) -> list[str]:
    """Every test named as failing in a `logs/test-failures.log`, sorted and deduped.

    Read from both spellings pytest offers, because the artifact may hold either: the
    `___ test_name ___` banner that opens each failure block, and the `FAILED
    path::test_name - reason` line in the short summary. `run-tests.py` keeps both
    sections, but it also *caps* each block, so a run with many failures can lose a
    banner to truncation while its summary line survives -- and reading one source
    only would then under-report, which for `gate_verdict` means merging a release on
    the strength of a failure list that was quietly incomplete.
    """
    names: set[str] = set()
    for line in artifact_text.splitlines():
        stripped = line.strip()
        banner = re.fullmatch(r"_{2,}\s+(.+?)\s+_{2,}", stripped)
        if banner:
            names.add(banner.group(1).strip())
            continue
        if stripped.startswith("FAILED "):
            target = stripped[len("FAILED ") :].split(" - ", 1)[0].strip()
            names.add(target.rsplit("::", 1)[-1].strip())
    return sorted(name for name in names if name)


@dataclass(frozen=True)
class Gate:
    """What GitHub says about a PR's checks, reduced to the three answers that matter."""

    state: str  # PENDING / PASSED / FAILED / NONE
    failed: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()


def gate_state(rollup: Sequence[dict]) -> Gate:
    """Reduce a `statusCheckRollup` to a `Gate`.

    NONE rather than PASSED for an empty rollup. "No checks reported" and "every check
    passed" are different answers and only one of them may be merged on -- the same
    distinction `worktree.rollup_conclusion` draws, kept here because this caller needs
    the failing checks *by name* and that reduction throws them away.
    """
    failed: list[str] = []
    pending: list[str] = []
    for check in rollup:
        name = str(check.get("name") or check.get("context") or "?")
        status = str(check.get("status") or "").upper()
        # A CheckRun reports `conclusion`; a legacy commit status reports `state`.
        conclusion = str(check.get("conclusion") or check.get("state") or "").upper()
        if status and status != "COMPLETED":
            pending.append(name)
        elif conclusion in ("SUCCESS", "NEUTRAL", "SKIPPED"):
            continue
        elif conclusion:
            failed.append(name)
        else:
            pending.append(name)
    if pending:
        return Gate("PENDING", tuple(failed), tuple(pending))
    if failed:
        return Gate("FAILED", tuple(failed), ())
    if not rollup:
        return Gate("NONE")
    return Gate("PASSED")


def gate_verdict(gate: Gate, failing_tests: Sequence[str]) -> tuple[str, str]:
    """`(verdict, reason)`: may this release PR be merged?

    The whole judgement, in one place, because it is the step a tired human gets wrong.
    A prepare PR is red **on purpose** and the temptation is to read any red as that
    red. It is only the expected one when three things hold at once: the sole failing
    check is the test job, the artifact could actually be read, and the sole failing
    test in it is `EXPECTED_RED_TEST`. A second failing test in the same job, or a
    failing `pre-commit`, is a release that would ship broken.

    A fully green gate proceeds too. That is not the documented case -- it means CI
    could not resolve a tag to compare against, so the check skipped -- but green is
    not a reason to refuse, and refusing it would strand the release with nothing a
    human could do about it.
    """
    if gate.state == "PENDING":
        return VERDICT_WAIT, f"still running: {', '.join(gate.pending)}"
    if gate.state == "NONE":
        return VERDICT_STOP, (
            "the PR reports no checks at all. A PR opened by a token gets no gate -- "
            "check that `gh auth status` names a user, not an app."
        )
    if gate.state == "PASSED":
        return VERDICT_PROCEED, (
            f"every check passed ({EXPECTED_RED_TEST} skipped -- CI could resolve no tag)"
        )
    others = [name for name in gate.failed if name != GATE_TEST_JOB]
    if others:
        return VERDICT_STOP, (
            f"{', '.join(others)} failed, which no release is expected to cause. "
            f"Fix it on main and re-run this task."
        )
    if not failing_tests:
        return VERDICT_STOP, (
            f"{GATE_TEST_JOB} failed but its {GATE_TEST_ARTIFACT} artifact named no "
            f"tests, so the failure cannot be identified as the expected one. It may "
            f"be a lint or hook-test failure, which runs before the suite."
        )
    unexpected = [name for name in failing_tests if name != EXPECTED_RED_TEST]
    if unexpected:
        return VERDICT_STOP, (
            f"{len(unexpected)} test(s) beyond the expected {EXPECTED_RED_TEST} "
            f"failed: {', '.join(unexpected)}"
        )
    return VERDICT_PROCEED, (
        f"{EXPECTED_RED_TEST} is the only failure, which is what a prepare PR is "
        f"supposed to cause -- the tag it looks for does not exist yet"
    )


def pr_number_from_url(url: str) -> int:
    """The PR number `gh pr create` printed, or 0 when its output was not a PR URL."""
    match = re.search(r"/pull/(\d+)", url.strip())
    return int(match.group(1)) if match else 0


def in_published_channel(path: str) -> bool:
    """True for a repo-relative path a consumer receives through the pre-commit `rev`.

    A prefix match on a directory and an exact match on a file, deliberately: everything
    under `scripts/precommit/` is executed straight out of pre-commit's clone, while at
    the top level only `.pre-commit-hooks.yaml` is published -- `.pre-commit-config.yaml`
    beside it is devkit's own wiring and reaches nobody.
    """
    cleaned = path.strip().replace("\\", "/")
    if not cleaned:
        return False
    return any(
        cleaned == entry or (entry.endswith("/") and cleaned.startswith(entry))
        for entry in PUBLISHED_CHANNEL
    )


def deliverable_changes(paths: Sequence[str], vendored: Sequence[str]) -> list[str]:
    """The subset of `paths` that a release is the only way to deliver.

    Pure, and split out from the git call for that reason: this is the whole trigger
    predicate, and "would tonight cut a release?" has to be answerable from a list of
    filenames in a test rather than from a repository in a particular state.
    """
    manifest = {entry.replace("\\", "/") for entry in vendored}
    hit = {
        cleaned
        for path in paths
        if (cleaned := path.strip().replace("\\", "/"))
        and (cleaned in manifest or in_published_channel(cleaned))
    }
    return sorted(hit)


def adoption_scope(projects: Sequence[str]) -> str:
    """The one argv token that tells `upgrade-project.py` which consumers to adopt in.

    `--all` when the checklist ticked nothing, which is what the scheduled pass wants and
    what this script did unconditionally before the picker existed -- a release nobody is
    watching should reach every consumer, and only a human at the dropdown has a reason
    to narrow it.

    One token rather than a flag and a value, because a ticked selection is a positional
    for that script; and spelled once here because it appears three times -- the command,
    the dry run's plan, and the retry line printed when the adoption pass fails. A retry
    naming a different scope from the run it retries is a remedy for something else.
    """
    return ",".join(projects) if projects else "--all"


def plan_steps(version: str, adopt: bool, projects: Sequence[str] = ()) -> list[str]:
    """The dry run's account of itself: what `--yes` would do, in order."""
    steps = [
        f"cut {release.branch_for(version)} from origin/main in a throwaway worktree",
        f"set {release.FALLBACK_CONST} = {version!r} in scripts/new-project.py, and commit it",
        f"push {release.branch_for(version)} and open its PR against main",
        f"wait for PR Gate, then verify {EXPECTED_RED_TEST} is its only failure",
        "squash-merge the PR",
        f"dispatch {RELEASE_WORKFLOW} phase=tag, which tests the tagged commit then pushes {version}",
        "fetch the new tag",
    ]
    if adopt:
        who = ", ".join(projects) if projects else "every consumer"
        steps.append(
            f"run upgrade-project.py {adoption_scope(projects)} --yes "
            f"to open an adoption PR in {who}"
        )
    return steps


# --- process plumbing -------------------------------------------------------


def inherited_streams() -> dict[str, object]:
    """`stdout`/`stderr` to hand a non-capturing child, or `{}` when there is nothing.

    The other half of `NO_WINDOW`, and the half that is easy to ship without noticing:
    the flag gives the child a console of its own, so a child that captures nothing
    writes to *that* console rather than to the handles it inherited. Naming them puts
    the output back where the caller can see it -- which under the scheduled pass is
    `log-wrap.py`'s pipe, i.e. the artifact.

    Empty when a stream has no file descriptor to pass down: `None` under a bare
    `pythonw.exe`, and a capture object under pytest. Both would raise if handed to
    `subprocess`. Same shape and same reasoning as `docker-maint.inherited_streams`,
    duplicated rather than imported for the reason `install-docker-prune.windowless`
    gives: six lines are not worth coupling two scripts' lifecycles.
    """
    streams: dict[str, Any] = {}
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        try:
            stream.fileno()  # type: ignore[union-attr]
        except (AttributeError, OSError, ValueError):
            continue
        streams[name] = stream
    return streams


def _run(
    cmd: Sequence[str], cwd: Path | None = None, capture: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run `cmd`, capturing by default.

    The single spawn site in this module, which is what lets the console discipline be
    one keyword rather than a rule every future call has to remember. It is needed
    because this script is no longer only a click: under `devkit-release` the parent is
    `pythonw.exe`, and a console child spawned from a console-less parent gets a fresh
    **visible** window -- at 2am, once per `gh` call, which is dozens.

    `NO_WINDOW` alone would trade that flicker for a silent artifact, so the two travel
    together: the flag, and `inherited_streams()` for the steps that stream (`gh run
    watch` and the upgrade hand-off). Capturing calls need neither, and pass neither.
    """
    streams: dict[str, Any] = {} if capture else inherited_streams()
    return subprocess.run(
        list(cmd),
        cwd=str(cwd) if cwd else None,
        capture_output=capture,
        text=True,
        check=False,
        creationflags=NO_WINDOW,
        **streams,
    )


def _gh_json(args: Sequence[str], cwd: Path) -> object | None:
    result = _run(["gh", *args], cwd=cwd)
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout or "null")
    except json.JSONDecodeError:
        return None


def existing_tags(devkit: Path) -> list[str]:
    result = _run(["git", "-C", str(devkit), "tag", "--list"])
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def upgrade_module():
    """`scripts/upgrade-project.py`, loaded by path.

    Hyphenated filename, hence the loader. Two questions are answered from there rather
    than reimplemented here -- what the `MANIFEST` vendors, and how a VS Code checklist
    spells its selection -- and both for the same reason: the adoption pass *is* that
    script, so a second spelling in this file would agree with it until the day one of
    them was the reason a release did not fire, or adopted somewhere nobody ticked.
    """
    loader_dir = SCRIPTS_DIR / "precommit"
    if str(loader_dir) not in sys.path:
        sys.path.insert(0, str(loader_dir))
    # Resolved by the insert above; `scripts/precommit/` is not an importable package.
    from _loader import load_by_path

    return load_by_path("_upgrade_project", SCRIPTS_DIR / "upgrade-project.py")


def vendored_paths() -> list[str]:
    """Every path in the vendored `MANIFEST`, or [] when it cannot be read.

    Goes through `upgrade-project.manifest_paths` rather than reading `sync-devkit.py`
    again here. That function already resolves the manifest from its owner, and a second
    spelling of "what is vendored" is the kind of copy that nothing compares -- the two
    would agree until one of them was the reason a release did not fire.
    """
    return list(upgrade_module().manifest_paths())


def changed_since_tag(devkit: Path, tag: str) -> list[str]:
    """Repo-relative paths `origin/<default>` carries that `tag` does not; [] on error."""
    git = sweep.git_for(devkit)
    default_branch = task_branch.detect_default_branch(git, fallback="main")
    if not default_branch:
        return []
    result = git("diff", "--name-only", f"{tag}..origin/{default_branch}")
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def release_needed(devkit: Path, tag: str) -> list[str]:
    """What `tag` cannot deliver that `origin/main` has: the `--if-needed` predicate.

    Empty means every change on main since the last release is devkit's own -- a doc,
    a generator change, a test, this file -- and cutting a tag for it would spend an
    adoption PR in every consumer to deliver nothing they can run.

    Non-empty is the state that made `sync-devkit.py --check` red in all five consumers
    at once, and it is not a judgement call: those files exist on main, no tag carries
    them, and a consumer has no way to reach them until one does.

    Best-effort in the same way `upgrade-project.unreleased_vendored_changes` is -- an
    unreadable checkout answers "nothing owed" rather than failing. The bias is
    deliberate: a false negative costs a night's delay and the nightly `upgrade` pass
    still says a release is owed, while a false positive would cut a release from a
    repository state this could not read.
    """
    try:
        return deliverable_changes(changed_since_tag(devkit, tag), vendored_paths())
    except Exception:
        return []


def main_fallback(devkit: Path) -> str:
    """`FALLBACK_DEVKIT_REF` as `origin/main` currently has it, or "" if unreadable."""
    relative = release.NEW_PROJECT.relative_to(release.REPO_ROOT).as_posix()
    result = _run(["git", "-C", str(devkit), "show", f"origin/main:{relative}"])
    if result.returncode != 0:
        return ""
    return release.bump_fallback(result.stdout, "unused")[1] or ""


def open_release_pr(devkit: Path, branch: str) -> int:
    """The number of the open PR for `branch`, or 0 when there is none."""
    data = _gh_json(["pr", "view", branch, "--json", "number,state"], devkit)
    if not isinstance(data, dict) or data.get("state") != "OPEN":
        return 0
    return int(data.get("number") or 0)


def prepare(devkit: Path, version: str) -> tuple[bool, str]:
    """Bump, commit and push `release/<version>`, in a worktree cut from origin/main."""
    branch = release.branch_for(version)
    with tempfile.TemporaryDirectory(prefix="devkit-release-") as tmp:
        path = Path(tmp) / branch.replace("/", "-")
        add = _run(
            ["git", "-C", str(devkit), "worktree", "add", "-b", branch, str(path), "origin/main"]
        )
        if add.returncode != 0:
            return False, (add.stderr or add.stdout).strip()
        try:
            target = path / release.NEW_PROJECT.relative_to(release.REPO_ROOT)
            updated, previous = release.bump_fallback(target.read_text(encoding="utf-8"), version)
            if previous is None:
                return False, f"no {release.FALLBACK_CONST} in {target.name}"
            target.write_text(updated, encoding="utf-8", newline="\n")
            for step in (
                ("commit", "-am", f"Release {version}"),
                ("push", "-u", "origin", branch),
            ):
                result = _run(["git", "-C", str(path), *step])
                if result.returncode != 0:
                    return False, f"`git {' '.join(step)}`: {(result.stderr or '').strip()}"
        finally:
            _run(["git", "-C", str(devkit), "worktree", "remove", "--force", str(path)])
            # The branch survives the worktree by design -- it is on the remote now,
            # and the local ref is what `gh pr create --head` resolves.
    return True, branch


def wait_for_checks(devkit: Path, number: int) -> None:
    """Block until every check on `number` has settled.

    One blocking call rather than a poll loop, per the CI-waiting rule -- and without
    `--fail-fast`, which is the flag that looks right here and is not: this gate is
    *expected* to go red, and failing fast would return before the other jobs report,
    leaving `gate_verdict` to judge a partial rollup.

    Its exit code is deliberately ignored. A red gate is a normal outcome here and the
    verdict is read from the rollup afterwards; the only thing being waited on is that
    nothing is still running.
    """
    deadline = time.monotonic() + CHECKS_APPEAR_TIMEOUT
    while True:
        result = _run(["gh", "pr", "checks", str(number), "--watch"], cwd=devkit, capture=False)
        # `gh` exits 1 for "some checks failed" (settled -- done waiting) and also for
        # "no checks reported on this branch" (not settled -- the gate has not been
        # queued yet). Only the second is worth retrying, and only briefly.
        if result.returncode == 0 or time.monotonic() > deadline:
            return
        # A non-empty rollup is not proof the wait is over: the losing side of the
        # enqueue race sees the checks appear *as* it gives up, so the rollup that
        # ends the retry is the one that is still running. Returning on it hands
        # `gate_verdict` a PENDING gate, which is a WAIT, which stops the release --
        # that is what stranded #256. Only a settled rollup ends the wait.
        if pr_rollup_settled(devkit, number):
            return
        time.sleep(CHECKS_APPEAR_INTERVAL)


def pr_rollup_settled(devkit: Path, number: int) -> bool:
    """True when `number` reports checks and none of them is still running."""
    rollup = pr_rollup(devkit, number)
    return bool(rollup) and gate_state(rollup).state != "PENDING"


def pr_rollup(devkit: Path, number: int) -> list[dict]:
    data = _gh_json(["pr", "view", str(number), "--json", "statusCheckRollup"], devkit)
    if not isinstance(data, dict):
        return []
    rollup = data.get("statusCheckRollup")
    return (
        [entry for entry in rollup if isinstance(entry, dict)] if isinstance(rollup, list) else []
    )


def failed_run_id(devkit: Path, rollup: Sequence[dict], job: str = GATE_TEST_JOB) -> int:
    """The Actions run id behind the failing `job`, read from its details URL."""
    for check in rollup:
        if str(check.get("name") or "") != job:
            continue
        match = re.search(r"/runs/(\d+)", str(check.get("detailsUrl") or ""))
        if match:
            return int(match.group(1))
    return 0


def download_failing_tests(devkit: Path, run_id: int) -> list[str]:
    """The tests named in the run's `test-failures` artifact; [] when unreadable.

    An unreadable artifact is not treated as "no failures" by the caller --
    `gate_verdict` refuses on an empty list, because "the suite named nothing" and
    "nobody could look" are the same evidence from here and only one of them is safe.
    """
    if not run_id:
        return []
    with tempfile.TemporaryDirectory(prefix="devkit-gate-") as tmp:
        result = _run(
            ["gh", "run", "download", str(run_id), "-n", GATE_TEST_ARTIFACT, "-D", tmp],
            cwd=devkit,
        )
        if result.returncode != 0:
            return []
        texts = [
            path.read_text(encoding="utf-8", errors="replace") for path in Path(tmp).rglob("*.log")
        ]
    return failing_test_names("\n".join(texts))


def newest_workflow_run(devkit: Path, workflow: str = RELEASE_WORKFLOW) -> int:
    data = _gh_json(
        ["run", "list", "--workflow", workflow, "--limit", "1", "--json", "databaseId"], devkit
    )
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return int(data[0].get("databaseId") or 0)
    return 0


def dispatch_tag_workflow(devkit: Path, version: str) -> tuple[int, str]:
    """Dispatch `release.yml phase=tag` and return `(run_id, error)`.

    The tag is pushed **by the workflow**, never from here. `git_policy` refuses a
    hand-pushed release tag outright, and the reason is the point rather than the
    obstacle: the workflow stages the tag locally, runs lint and the full suite against
    that exact commit, and pushes only then. A tag is what every consumer pins, so it
    is the one ref that must name a commit whose suite passed *as tagged*.
    """
    before = newest_workflow_run(devkit)
    dispatch = _run(
        [
            "gh",
            "workflow",
            "run",
            RELEASE_WORKFLOW,
            "-f",
            f"version={version}",
            "-f",
            "phase=tag",
        ],
        cwd=devkit,
    )
    if dispatch.returncode != 0:
        return 0, (dispatch.stderr or dispatch.stdout).strip()
    deadline = time.monotonic() + RUN_DISCOVERY_TIMEOUT
    while time.monotonic() < deadline:
        current = newest_workflow_run(devkit)
        if current and current != before:
            return current, ""
        time.sleep(RUN_DISCOVERY_INTERVAL)
    return 0, f"{RELEASE_WORKFLOW} was dispatched but no new run appeared to watch"


# --- the pipeline ------------------------------------------------------------


def _say(message: str) -> None:
    print(f"release-pipeline: {message}", flush=True)


def _stop(message: str) -> int:
    print(f"release-pipeline: STOPPED -- {message}", file=sys.stderr)
    return 1


def run_pipeline(
    devkit: Path,
    version: str,
    adopt: bool,
    workspace: Path | None,
    projects: Sequence[str] = (),
) -> int:
    """Execute the release. 0 done, 1 refused, 2 a step failed."""
    branch = release.branch_for(version)

    if main_fallback(devkit) == version:
        # Resumable: the prepare PR has already merged, so the bump is on main and only
        # the tag is missing. Re-running the task after a network failure at step 7
        # should finish the release, not refuse it as half-done.
        _say(f"{release.FALLBACK_CONST} already names {version} on main -- skipping to the tag")
    else:
        number = open_release_pr(devkit, branch)
        if number:
            _say(f"reusing the open prepare PR #{number} for {branch}")
        else:
            _say(f"preparing {branch}")
            ok, detail = prepare(devkit, version)
            if not ok:
                print(f"release-pipeline: prepare failed: {detail}", file=sys.stderr)
                return 2
            created = _run(
                [
                    "gh",
                    "pr",
                    "create",
                    "--base",
                    "main",
                    "--head",
                    branch,
                    "--title",
                    f"Release {version}",
                    "--body",
                    PR_BODY.format(version=version, expected=EXPECTED_RED_TEST),
                ],
                cwd=devkit,
            )
            if created.returncode != 0:
                print(
                    f"release-pipeline: pushed {branch} but the PR failed: "
                    f"{(created.stderr or created.stdout).strip()}",
                    file=sys.stderr,
                )
                return 2
            number = pr_number_from_url(created.stdout) or open_release_pr(devkit, branch)
            if not number:
                return _stop(f"opened a PR for {branch} but could not read its number")
            _say(f"opened PR #{number}")

        _say(f"waiting for the gate on #{number} (one blocking call; this takes minutes)")
        wait_for_checks(devkit, number)
        rollup = pr_rollup(devkit, number)
        gate = gate_state(rollup)
        failing = (
            download_failing_tests(devkit, failed_run_id(devkit, rollup))
            if gate.state == "FAILED"
            else []
        )
        verdict, reason = gate_verdict(gate, failing)
        _say(f"gate {gate.state}: {reason}")
        if verdict != VERDICT_PROCEED:
            return _stop(f"not merging #{number} -- {reason}")

        merged = _run(["gh", "pr", "merge", str(number), "--squash", "--delete-branch"], cwd=devkit)
        # `--delete-branch` exits 1 when only the *local* branch delete fails, which is
        # routine here: the branch was pushed from a worktree that no longer exists. The
        # PR's own state is the authority on whether the merge happened.
        state = _gh_json(["pr", "view", str(number), "--json", "state"], devkit)
        if not (isinstance(state, dict) and state.get("state") == "MERGED"):
            print(
                f"release-pipeline: merge of #{number} failed: "
                f"{(merged.stderr or merged.stdout).strip()}",
                file=sys.stderr,
            )
            return 2
        _say(f"merged #{number}")

    _say(f"dispatching {RELEASE_WORKFLOW} phase=tag -- it tests the tagged commit before pushing")
    run_id, error = dispatch_tag_workflow(devkit, version)
    if error:
        print(f"release-pipeline: {error}", file=sys.stderr)
        return 2
    watched = _run(["gh", "run", "watch", str(run_id), "--exit-status"], cwd=devkit, capture=False)
    if watched.returncode != 0:
        return _stop(
            f"the tag workflow failed -- {version} was NOT pushed. "
            f"`gh run view {run_id} --log-failed` has the reason."
        )

    _run(["git", "-C", str(devkit), "fetch", "--tags", "--quiet", "origin"])
    if version not in existing_tags(devkit):
        return _stop(f"{RELEASE_WORKFLOW} reported success but {version} is not here after a fetch")
    _say(f"{version} is tagged.")

    if not adopt:
        _say("skipping adoption (--no-adopt); consumers stay on the previous release")
        return 0
    scope = adoption_scope(projects)
    _say(f"opening an adoption PR in {', '.join(projects) if projects else 'every consumer'}")
    command = [
        # Not `sys.executable`: under the scheduled pass that is `pythonw.exe`, and
        # Windows ignores `CREATE_NO_WINDOW` for a GUI-subsystem child -- leaving the
        # upgrade pass console-less, which is what makes every `git` beneath it open a
        # window of its own. See `sweep.console_python`.
        sweep.console_python(),
        str(SCRIPTS_DIR / "upgrade-project.py"),
        scope,
        "--yes",
        "--devkit",
        str(devkit),
    ]
    if workspace is not None:
        command += ["--workspace", str(workspace)]
    adopted = _run(command, cwd=devkit, capture=False)
    if adopted.returncode != 0:
        # Not a release failure: the release is cut and tagged. Adoption is a separate
        # per-consumer act with its own artifact, and `logs/upgrade.log` says which.
        return _stop(
            f"{version} is released, but the adoption pass exited {adopted.returncode} "
            f"-- see logs/upgrade.log, then re-run `upgrade-project.py {scope} --yes`"
        )
    _say(f"{version} released and up for adoption everywhere.")
    return 0


PR_BODY = """Bumps `FALLBACK_DEVKIT_REF` to `{version}` so a project generated before the
tag exists still pins something resolvable.

Opened by `scripts/release-pipeline.py`. **`{expected}` is expected to fail on this PR**
— it compares the constant against `git describe --tags`, and `{version}` is not tagged
until this merges. The pipeline verifies that it is the *only* failure before merging.
"""


def build_parser() -> argparse.ArgumentParser:
    """The CLI, built separately so a test can parse a command line without running it.

    `upgrade-project.py` quotes this script's invocation at whoever reads its artifact,
    and a remedy printed in one file and implemented in another is the pairing that
    goes stale silently -- nothing runs the sentence.
    `test_the_remedy_upgrade_project_prints_is_this_scripts_cli` runs it here.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "level",
        nargs="?",
        default="patch",
        help="patch, minor, major, or an explicit vX.Y.Z",
    )
    parser.add_argument("--devkit", type=Path, default=REPO_ROOT, help=argparse.SUPPRESS)
    parser.add_argument(
        "--workspace", type=Path, default=None, help="workspace file for the adoption pass"
    )
    parser.add_argument(
        "--projects",
        default="",
        help=(
            "comma-delimited consumer names to open adoption PRs in, as the "
            "`Devkit: Cut Release` checklist emits them. Omitted means every consumer"
        ),
    )
    parser.add_argument(
        "--no-adopt",
        dest="adopt",
        action="store_false",
        help="cut the release but do not open the consumers' adoption PRs",
    )
    parser.add_argument(
        "--if-needed",
        action="store_true",
        help=(
            "do nothing, successfully, unless main carries a vendored or published-channel "
            "change no tag delivers. The scheduled pass runs with this; a click does not"
        ),
    )
    apply_mode = parser.add_mutually_exclusive_group()
    apply_mode.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    apply_mode.add_argument("--yes", dest="dry_run", action="store_false")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)

    upgrade = upgrade_module()
    if upgrade.picked_nothing(args.projects):
        # Escaping the consumer checklist is backing out of the click, and the click is
        # the whole release -- so nothing is cut. `--no-adopt` is the spelling for
        # "release, but adopt nowhere"; an escaped picker is not a request for it.
        print(
            "release-pipeline: nothing was picked -- no release was cut. Tick the "
            "consumers to adopt in, or pass --no-adopt to release without adopting.",
            file=sys.stderr,
        )
        return 1
    projects = upgrade.project_selection(args.projects)

    if _run(["gh", "--version"]).returncode != 0:
        print(
            "release-pipeline: `gh` is not on PATH; this needs it for every step", file=sys.stderr
        )
        return 2

    _run(["git", "-C", str(args.devkit), "fetch", "--tags", "--quiet", "origin"])
    tags = existing_tags(args.devkit)
    version, refusal = next_version(tags, args.level)
    if refusal:
        print(f"release-pipeline: {refusal}", file=sys.stderr)
        return 2

    if args.if_needed:
        current = newest_release(tags)
        owed = release_needed(args.devkit, current) if current else []
        if current and not owed:
            # Exit 0 and say so. A scheduled job that declined to act and a scheduled
            # job that never fired look identical in `schtasks`, so the quiet night has
            # to be a sentence in the artifact rather than an absence of one.
            print(
                f"release-pipeline: nothing to release -- every change on main since "
                f"{current} is devkit's own (docs, tests, generator). A consumer can "
                f"already reach everything {current} delivers."
            )
            return 0
        reason = (
            "no release tag exists yet, so nothing is deliverable"
            if not current
            else f"{len(owed)} change(s) {current} cannot deliver: {', '.join(owed)}"
        )
        print(f"release-pipeline: releasing because {reason}")

    print(f"release-pipeline: {version} ({args.level})")
    for index, step in enumerate(plan_steps(version, args.adopt, projects), 1):
        print(f"  {index}. {step}")
    if args.dry_run:
        print("\nDry run -- nothing was changed. Re-run with --yes to apply.")
        return 0
    print()
    return run_pipeline(args.devkit, version, args.adopt, args.workspace, projects)


if __name__ == "__main__":
    sys.exit(main())
