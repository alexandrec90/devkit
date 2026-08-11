#!/usr/bin/env python3
"""Adopt a devkit release in a consuming project, as one reviewable change.

A devkit upgrade moves four things that describe one upstream revision: the
vendored files, the `DEVKIT_VERSION` stamp, the `rev:` in the project's
`.pre-commit-config.yaml`, and the `ref:` in its PR gate. `sync-devkit.py --pull`
now moves all four atomically or refuses; this script is what puts the result on
its own branch and into its own PR.

**It is deliberately not part of shipping.** A harness upgrade is dozens of files
of upstream churn, and folding it into `/ship` or `sweep --ship` would mix it into
whatever change was actually being shipped -- which is how a consumer ended up with
an unfinished upgrade buried in 364 uncommitted files, discovered only when its own
commit gate refused. An upgrade is its own operation with its own diff.

Refuses a dirty target for the same reason: a branch cut under uncommitted work
carries that work along, and the commit here would have to guess which files were
part of the upgrade.

**It only ever moves a project forward.** A consumer that vendored a release this
checkout has no tag for -- the normal state of the hours around a release, when the
adoption lands before the tag does -- is refused rather than pulled back to the
older one; see `unreleased_adoption`.

`--all` upgrades every checkout in the workspace that has actually vendored devkit,
one PR each -- the release is one upstream revision, so adopting it everywhere is
one operation. Each project is still upgraded on its own terms: a refusal in one
does not stop the others, and the exit code reports the worst outcome.

Every refusal and failure is written to `logs/upgrade.log` in the devkit checkout,
overwritten per run and emptied when a run is clean. A `--all` interleaves several
checkouts' output in one terminal, which is the shape of output that scrolls away
before it is read.

Pure and stdlib-only; every decision is an importable function tested in
`tests/test_upgrade_project.py`.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sweep
import task_branch as tb

REPO_ROOT = Path(__file__).resolve().parents[1]
# devkit's own `scripts/`, resolved from this file rather than from `REPO_ROOT`:
# that one is the artifact root, and the tests point it at a temporary directory.
SCRIPTS_DIR = Path(__file__).resolve().parent
# Box-aware (see `sweep.default_workspace`).
DEFAULT_WORKSPACE = sweep.default_workspace(REPO_ROOT)
SYNC_SCRIPT = "scripts/sync-devkit.py"

# Where this run's refusals and failures are written, relative to the devkit checkout
# (`logs/` is gitignored). An upgrade run spans several checkouts and its output is
# interleaved; the terminal gets a status line and this path, per the failure-artifact
# rule in `.claude/rules/engineering.md`.
ARTIFACT = Path("logs") / "upgrade.log"

# How many times `--pull` may be re-run before the disagreement is the bug -- see
# `pull_to_fixpoint`. Two is the honest ceiling; the third only ever proves it.
MAX_PULL_PASSES = 3

# The per-project files an upgrade moves besides the MANIFEST itself. Shown in the
# dry run so the plan names them; the commit stages with `add -A`, which is exact
# rather than lax because `refusal()` guarantees a clean tree beforehand.
UPGRADE_PATHS: tuple[str, ...] = (
    "DEVKIT_VERSION",
    "DEVKIT_FILES.json",
    ".pre-commit-config.yaml",
    ".github/workflows/pr-gate.yml",
)


# Why `--all` passes over a checkout. Skips are not failures: a workspace holds
# things that are not consumers, and one of them is devkit itself.
SKIP_SOURCE = "is the devkit checkout this run pulls from"
SKIP_UNADOPTED = "has no DEVKIT_VERSION -- it has never vendored devkit"

# What `refusal()` says when devkit has nothing to adopt. A constant because
# `main()` reports it once for the whole run rather than once per project.
NO_TAG = "devkit has no release tags -- there is nothing to adopt. Cut one first (see RELEASING.md)"


def project_selection(value: str | None) -> list[str]:
    """Comma-delimited checkout selection returned by the VS Code multi-pick."""
    if not value:
        return []
    return list(dict.fromkeys(name.strip() for name in value.split(",") if name.strip()))


@dataclass(frozen=True)
class Candidate:
    """One checkout `--all` looked at, and the three facts that decide its fate.

    `common_dir` is git's ref store for the checkout: two *linked worktrees* of one
    repo (`carameli` and `carameli-b`) share it, two clones of one remote do not.
    It is the only reliable way to tell those apart, and telling them apart is a
    correctness requirement here -- see `select_all`.
    """

    name: str
    adopts: bool = True
    is_source: bool = False
    common_dir: str = ""


def select_all(candidates: list[Candidate]) -> list[tuple[str, str]]:
    """`(name, skip reason)` for each checkout, in workspace order; "" means upgrade.

    Worktree siblings are deduplicated because upgrading both would not merely be
    redundant, it would fail: they share a ref store, so the second `checkout -b
    claude/devkit-upgrade-<mmdd>` hits a branch that already exists, and both PRs
    would target the same repo with the same change.

    First in workspace order claims the repo. That is arbitrary when the claimant
    turns out to be un-upgradable (dirty, or parked on a task branch), so the skip
    message points at naming the sibling explicitly -- which bypasses this entirely.
    """
    claimed: dict[str, str] = {}
    decided: list[tuple[str, str]] = []
    for candidate in candidates:
        if candidate.is_source:
            decided.append((candidate.name, SKIP_SOURCE))
            continue
        if not candidate.adopts:
            decided.append((candidate.name, SKIP_UNADOPTED))
            continue
        # No key means git would not say; treat the checkout as its own store
        # rather than merging every unknown into one bucket.
        key = candidate.common_dir or f"?{candidate.name}"
        first = claimed.get(key)
        if first:
            decided.append(
                (
                    candidate.name,
                    f"shares a repo with {first}; upgrade it there, or name this one explicitly",
                )
            )
            continue
        claimed[key] = candidate.name
        decided.append((candidate.name, ""))
    return decided


def candidates_for(root: Path, names: list[str], devkit: Path) -> list[Candidate]:
    """Inspect each checkout just enough to feed `select_all`.

    Ordered cheapest first, and the order matters: the ref-store lookup shells out
    to git, so it never runs for devkit itself or for a directory that was never a
    consumer.
    """
    built: list[Candidate] = []
    for name in names:
        path = root / name
        if _same_path(path, devkit):
            built.append(Candidate(name, is_source=True))
        elif not (path / "DEVKIT_VERSION").is_file():
            built.append(Candidate(name, adopts=False))
        else:
            built.append(Candidate(name, common_dir=sweep.common_dir(sweep.git_for(path), path)))
    return built


def _same_path(left: Path, right: Path) -> bool:
    """Whether two paths name the same directory, symlinks and `..` resolved."""
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return False


# The topic every upgrade branch is named for. Held as a constant because the name
# is written by `branch_name` and read back by `is_upgrade_branch`, which is how a
# checkout parked mid-upgrade is told apart from one holding unrelated work.
UPGRADE_SLUG = tb.slugify("devkit upgrade")


def branch_name(today: _dt.date | None = None) -> str:
    """The branch an upgrade lands on: `claude/devkit-upgrade-<mmdd>`."""
    return tb.branch_name(UPGRADE_SLUG, set(), today)


def is_upgrade_branch(branch: str) -> bool:
    """True for a branch this script cut, on any day.

    Deliberately not `branch == branch_name()`: the `-<mmdd>` stamp means an upgrade
    parked yesterday would not match today's name, and yesterday's unfinished upgrade
    is exactly the one worth recognising.
    """
    return branch.startswith(f"{tb.BRANCH_PREFIX}{UPGRADE_SLUG}-")


def commit_message(tag: str, files: int | str) -> str:
    """Subject for the upgrade commit. Names the release, because for this one
    change the version *is* the description -- unlike a swept commit, nothing here
    is a guess about content.

    `files` takes a string so the printed *plan* can say `<n>`: the count is only
    known after the pull, and printing `0` there described every real run wrongly.
    """
    return f"Adopt devkit {tag} ({files} vendored file(s))"


def pr_body(tag: str, previous: str, changed: list[str]) -> str:
    """PR body: what moved, and the one thing a reviewer has to check."""
    lines = [
        f"Adopts devkit **{tag}** (was `{previous}`), via `{SYNC_SCRIPT} --pull`.",
        "",
        "The four things that describe the vendored revision move together, so the "
        "commit-time drift gate and the PR gate now measure against the same tag:",
        "",
        "- vendored files from the `MANIFEST`",
        "- `DEVKIT_VERSION`",
        "- `.pre-commit-config.yaml` → `rev:`",
        "- `.github/workflows/pr-gate.yml` → the harness checkout `ref:`",
        "",
        "Review this as an upstream adoption, not as authored work: the file "
        "contents come from devkit and belong upstream if they are wrong.",
    ]
    if changed:
        lines += ["", f"Changed paths ({len(changed)}):", ""]
        lines += [f"- `{path}`" for path in changed[: sweep.PR_BODY_FILE_LIMIT]]
        if len(changed) > sweep.PR_BODY_FILE_LIMIT:
            lines.append(f"- …and {len(changed) - sweep.PR_BODY_FILE_LIMIT} more")
    return "\n".join(lines)


def refusal(state: sweep.State, tag: str | None) -> str:
    """Why this project cannot be upgraded right now, or "" when it can.

    Ordered by what the operator has to do about it, cheapest first.
    """
    if not state.is_git:
        return "not a git checkout"
    if not tag:
        return NO_TAG
    if state.dirty:
        return (
            f"{state.dirty} uncommitted file(s). An upgrade is its own change; "
            f"commit or ship the work in progress first"
        )
    if is_upgrade_branch(state.branch):
        # Not "unrelated work" -- it is this operation, half done. Sending the
        # operator home is a dead end: today's run would cut a differently dated
        # branch, so following that advice strands a branch that already holds the
        # adoption (pushed, in the case that produced this message) and opens a
        # second one beside it.
        return (
            f"parked on {state.branch}, an unfinished upgrade this script cut. "
            f"Finish that one -- ship the branch and open its PR -- or delete it if "
            f"it was abandoned; re-running from the home branch would leave it "
            f"stranded and cut a second upgrade branch alongside it"
        )
    if sweep.is_task_branch(state.branch):
        return (
            f"already on the task branch {state.branch}. Upgrade from the home "
            f"branch so the adoption is not mixed into unrelated work"
        )
    return ""


def plan(state: sweep.State, tag: str | None, today: _dt.date | None = None) -> sweep.Plan:
    """The git steps for one project's upgrade, or a refusal.

    The `--pull` itself is not a git step, so it is not in `steps`: it runs between
    the branch and the commit, and the commit is only meaningful if it succeeded.
    """
    reason = refusal(state, tag)
    if reason:
        return sweep.Plan(refusal=reason)
    return sweep.Plan(steps=(("checkout", "-b", branch_name(today)),), anchor=state.branch)


def changed_paths(git) -> list[str]:
    """Everything the pull touched.

    `refusal()` guarantees the tree was clean before the pull ran, so every dirty
    path afterwards came from it. That precondition is what lets the commit below
    stage with `add -A` instead of guessing at a path list -- and it is why the
    clean-tree refusal is a correctness requirement, not politeness.
    """
    result = git("status", "--porcelain")
    return list(sweep.parse_porcelain(result.stdout if result.returncode == 0 else ""))


def _abandon(git, name: str, home: str, why: str, code: int) -> int:
    """Undo the branch this run cut, so an upgrade that did nothing leaves nothing.

    Never forces: `branch -d` refuses a branch carrying commits, and a refusal here
    means the run did more than it thought, which is a state for a human rather
    than one to clean up automatically.

    Names the project, like every other line this prints. Under `--all` these are
    interleaved with other checkouts' output, and an unattributed "already current"
    is a sentence the reader has to guess the subject of.
    """
    branch = branch_name()
    if git("checkout", home).returncode != 0:
        print(
            f"upgrade: {name} -- {why}, but could not return to {home}; still on {branch}",
            file=sys.stderr,
        )
        return 2
    if git("branch", "-d", branch).returncode != 0:
        print(
            f"upgrade: {name} -- {why}, but {branch} would not delete; it is not empty",
            file=sys.stderr,
        )
        return 2
    print(f"upgrade: {name} -- {why}; no branch left behind.")
    return code


def commit_for(devkit: Path, rev: str) -> str:
    """The full commit SHA `rev` names in the devkit checkout, or "" when it names none.

    Looked up once per run, because every project in an `--all` adopts the same
    release. "" is the honest answer for a rev git cannot resolve, and `is_current`
    treats it as "cannot tell" rather than as "not current".
    """
    result = subprocess.run(
        ["git", "-C", str(devkit), "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def names_commit(stamp: str, commit: str) -> bool:
    """True when `stamp` abbreviates (or equals) the full SHA `commit`.

    A prefix test rather than an equality one because `DEVKIT_VERSION` holds
    `git rev-parse --short`'s output, whose width varies with the repository.

    Anything that is not a plausible SHA matches nothing -- the `<rev>-dirty` an
    `--allow-dirty` pull stamps, or the literal `unknown` -- which is the right
    answer twice over: a provisional pull corresponds to no release, so it is never
    current and always has something to adopt.
    """
    stamp = stamp.strip().lower()
    if len(stamp) < 7 or not all(char in "0123456789abcdef" for char in stamp):
        return False
    return commit.strip().lower().startswith(stamp)


def is_current(project: Path, tag_commit: str) -> bool:
    """True when the project's stamp already names the release's commit.

    **The comparison has to go through the SHA.** `DEVKIT_VERSION` records the
    upstream *commit* by contract -- `sync-devkit.py --pull` writes `git_head(src)`
    there and the vendored `test_harness_version_records_a_commit` asserts a hex
    value -- so comparing that file against a tag *name* compares two things that can
    never be equal. This did exactly that, which made the predicate false for every
    project forever: each scheduled run cut a branch, built a source worktree and ran
    a full pull on projects already sitting on the release, discovered afterwards
    that the tree was clean, and abandoned. The plan it printed first said an upgrade
    was happening; the line after it said "already current".

    `stale_pin` in `sync-devkit.py` documents the same trap from the other side, and
    solves it the other way -- by reading the tag out of the receipt. Either proof
    works; this one is used here because `--all` has the devkit checkout in hand and
    a stamp predates the receipt ever recording a tag.

    Checked *before* anything is cut or copied, so proving a project is up to date
    costs one file read and leaves the repo untouched. That is the property that
    makes this safe to run on a schedule.
    """
    if not tag_commit:
        return False
    try:
        stamp = (project / "DEVKIT_VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return names_commit(stamp, tag_commit)


def vendored_release(project: Path) -> str:
    """The release `project`'s receipt says its files came from; "" when unrecorded.

    Read through `sync-devkit.read_receipt_tag` rather than reparsed here. The receipt
    is that script's format, it already answers this question, and a second reader
    that disagreed even slightly would make "which release is this project on" a
    question with two answers -- the exact failure `is_current` was written to end.
    Hyphenated filename, hence the loader; `SCRIPTS_DIR` rather than `REPO_ROOT`
    because that one is a knob the tests move.
    """
    loader_dir = SCRIPTS_DIR / "precommit"
    if str(loader_dir) not in sys.path:
        sys.path.insert(0, str(loader_dir))
    # Resolved by the insert above; `scripts/precommit/` is not an importable package.
    from _loader import load_by_path

    sync = load_by_path("_sync_devkit", SCRIPTS_DIR / "sync-devkit.py")
    return sync.read_receipt_tag(project)


def unreleased_adoption(project: Path, tags: list[str]) -> str:
    """Why this project must not be upgraded *to* `tags[0]`, or "".

    A project whose receipt names a release this devkit checkout has no tag for is
    holding something this run cannot produce, and every step after the branch would
    then run backwards: an older tree pulled over a newer one, a commit titled
    `Adopt devkit <older>`, and a PR body describing the reversal as an adoption.

    This is not a hypothetical -- it is the ordinary state of the hours around a
    release. A consumer adopts off the release branch before the tag is cut, or the
    tag exists upstream and this checkout has not fetched it, and until then the
    stamp names a commit git here cannot resolve. `is_current` answers "no" to that,
    correctly, and "not current" was being read as "stale" for want of this check.

    "" when the receipt records no tag (a pull from before it carried one, or an
    `--allow-untagged` one) and when `tags` is empty (a devkit this could not query).
    Cannot tell is not ahead -- the line `sync-devkit.stale_pin` draws, for the same
    reason: a check that fired on every project it could not read would be ignored.
    """
    vendored = vendored_release(project)
    if not tags or not vendored or vendored in tags:
        return ""
    return (
        f"vendored devkit {vendored}, which this checkout has no tag for; the newest "
        f"here is {tags[0]}. Adopting {tags[0]} would move it backwards. Fetch devkit's "
        f"tags, or cut {vendored} if it was never released (see RELEASING.md)"
    )


@contextlib.contextmanager
def source_at_tag(devkit: Path, tag: str):
    """A clean checkout of `devkit` at `tag`, as a temporary worktree.

    The devkit checkout itself is normally on a branch with uncommitted work, and
    `sync-devkit.py --pull` rightly refuses such a source -- its files are at no
    upstream revision while the stamp would claim one. A throwaway worktree at the
    tag is the source a consumer actually wants: exactly the released tree.
    """
    with tempfile.TemporaryDirectory(prefix="devkit-") as tmp:
        path = Path(tmp) / tag.replace("/", "-")
        add = subprocess.run(
            ["git", "-C", str(devkit), "worktree", "add", "--detach", str(path), tag],
            capture_output=True,
            text=True,
            check=False,
        )
        if add.returncode != 0:
            raise RuntimeError((add.stderr or add.stdout).strip())
        try:
            yield path
        finally:
            subprocess.run(
                ["git", "-C", str(devkit), "worktree", "remove", "--force", str(path)],
                capture_output=True,
                text=True,
                check=False,
            )


def run_pull(project: Path, devkit: Path) -> subprocess.CompletedProcess[str]:
    """`sync-devkit.py --pull` inside `project`, sourced from `devkit`.

    Runs the *project's own* vendored copy: it is the one whose MANIFEST describes
    what that project has, and an older copy upgrading itself is the normal case.
    """
    return subprocess.run(
        [sys.executable, SYNC_SCRIPT, "--pull", "--src", str(devkit)],
        cwd=str(project),
        capture_output=True,
        text=True,
        check=False,
    )


def sync_script_bytes(project: Path) -> bytes:
    """The project's vendored copy of the puller, or b"" when it has none."""
    try:
        return (project / SYNC_SCRIPT).read_bytes()
    except OSError:
        return b""


def pull_to_fixpoint(
    project: Path,
    source: Path,
    passes: int = MAX_PULL_PASSES,
    pull: Callable[[Path, Path], subprocess.CompletedProcess[str]] | None = None,
) -> tuple[list[subprocess.CompletedProcess[str]], str]:
    """`--pull` until the puller stops changing. `(the runs, "" or a divergence)`.

    **One pull cannot adopt a release that changed the MANIFEST**, and the shortfall
    is invisible until commit time. `scripts/sync-devkit.py` is itself a MANIFEST
    entry, so the copy executing a pull is the copy that pull replaces: the list being
    copied is the *old* release's, and any path the new release added to it is not in
    it. v0.7.0 added seven entries (the Codex adapter pair, the CI contract test,
    `dependabot-automerge.yml`, ...) and retired three more; the pull reported success
    having moved 17 files and left those ten behind.

    Nothing downstream absorbed that. `git commit` runs the `devkit-drift` pre-commit
    hook, which compares against the *new* MANIFEST out of pre-commit's own clone of
    the pinned rev -- so it failed the commit, in all three consumers at once, each
    left parked on the upgrade branch holding a half-adopted release.

    So: pull, and if the pull replaced the puller, pull again with the copy now in
    place. The second pass runs the release's own MANIFEST and is where a normal
    upgrade settles. A third that still moves it means devkit and this project
    disagree about what the manifest is -- a bug to report, not a loop to widen.
    """
    runner = pull or run_pull
    runs: list[subprocess.CompletedProcess[str]] = []
    for _ in range(passes):
        before = sync_script_bytes(project)
        result = runner(project, source)
        runs.append(result)
        # A refused pull is the caller's to report; re-running it would only refuse
        # again, and the second refusal is not new information.
        if result.returncode != 0 or sync_script_bytes(project) == before:
            return runs, ""
    return runs, (
        f"{SYNC_SCRIPT} still changed after {passes} pulls -- devkit and this "
        f"project do not agree on what the MANIFEST is. Adopt by hand and report it."
    )


def commit_with_hook_retry(git, message: str) -> tuple[subprocess.CompletedProcess[str], bool]:
    """`git commit`, re-staged and retried once when the commit hooks rewrote the tree.

    Half of pre-commit's hooks are *fixers*: they reformat, they mirror a directory,
    they regenerate a manifest. A fixer that changes something fails the commit with
    "files were modified by this hook", and the convention is that you stage the
    result and commit again -- carameli's `sync-codex` hook remirrored `.claude/skills/`
    and stopped an upgrade that had nothing wrong with it.

    An upgrade meets this more than most changes do, because it lands dozens of
    upstream files at once through every fixer the project runs.

    **Retried only when the tree actually changed during the failed attempt.** That is
    what separates a hook that fixed something from a gate that refused something: a
    lint error the formatter cannot fix leaves the tree exactly as it was, and
    committing over it again would just fail twice and say so twice.
    """
    before = _status(git)
    first = git("commit", "-m", message)
    if first.returncode == 0 or _status(git) == before:
        return first, False
    git("add", "-A")
    return git("commit", "-m", message), True


def _status(git) -> str:
    """Raw `status --porcelain`, **status codes and all**.

    Deliberately not `changed_paths`, which drops the codes: a fixer hook rewrites
    files that were already staged, so the path list is identical afterwards and only
    the code moves (`M ` -> `MM`). Comparing the parsed paths would report "nothing
    changed" for the one event this exists to detect.
    """
    result = git("status", "--porcelain")
    return result.stdout if result.returncode == 0 else ""


def verify_pull(project: Path, source: Path) -> subprocess.CompletedProcess[str]:
    """`--check` after the pull: the comparison `git commit` is about to make anyway.

    The `devkit-drift` pre-commit hook performs exactly this check, one step later and
    with no way to explain itself -- the operator sees `commit -m 'Adopt devkit ...'`
    fail with a hook id. Running it here turns that into an upgrade refusal, at the
    point where the tree that caused it is still the subject of the sentence.
    """
    return subprocess.run(
        [sys.executable, SYNC_SCRIPT, "--check", "--src", str(source)],
        cwd=str(project),
        capture_output=True,
        text=True,
        check=False,
    )


@dataclass(frozen=True)
class Outcome:
    """One checkout's exit code, and the text `logs/upgrade.log` records for it.

    `upgrade_one` used to return a bare code, which served `max()` and nothing else:
    every detail went to stderr, where a multi-project run interleaves it and the
    scrollback then loses it. The artifact needs those same sentences, so they are
    carried out rather than only printed.
    """

    name: str
    code: int
    detail: str = ""


def upgrade_one(name: str, project: Path, tag: str, source: Path | None = None) -> Outcome:
    """Adopt `tag` in one checkout: 0 done or nothing to do, 1 refused, 2 failed.

    `source` is a clean devkit worktree at `tag` to pull from. **None means dry
    run** -- the plan is printed and nothing is touched, which is also why the
    caller materialises the worktree rather than this function: one release is one
    worktree however many projects adopt it.

    The exit codes are the sweep convention (1 needs a human decision, 2 something
    broke mid-flight) so `main` can take the worst across a `--all` run.
    """

    def failed(code: int, *lines: str) -> Outcome:
        """Report to stderr and to the artifact in one act, so they cannot diverge."""
        text = "\n".join(line.rstrip() for line in lines if line.strip())
        print(text, file=sys.stderr)
        return Outcome(name, code, text)

    state = sweep.inspect(name, project, fetch=False)
    upgrade = plan(state, tag, None)
    if upgrade.refusal:
        return failed(1, f"upgrade: {name} -- {upgrade.refusal}")

    previous = (project / "DEVKIT_VERSION").read_text(encoding="utf-8").strip()
    print(f"upgrade: {name} {previous} -> {tag}")
    for step in upgrade.steps:
        print(f"  1. git -C {name} {' '.join(step)}")
    print(f"  2. {SYNC_SCRIPT} --pull --src <devkit worktree at {tag}>")
    print(f"  3. git -C {name} add {' '.join(UPGRADE_PATHS)} + the MANIFEST paths")
    print(f"  4. git -C {name} commit -m {commit_message(tag, '<n>')!r}")
    print("  5. git push -u origin, then gh pr create")
    if source is None:
        return Outcome(name, 0)

    git = sweep.git_for(project)
    applied = sweep.apply_plan(name, project, upgrade, git=git)
    if not applied.ok:
        return failed(2, f"upgrade: {name} -- FAILED at `{applied.failed}`", applied.error)

    runs, divergence = pull_to_fixpoint(project, source)
    print("\n".join(run.stdout.rstrip() for run in runs).rstrip())
    pulled = runs[-1]
    if pulled.returncode != 0:
        refusal = failed(2, f"upgrade: {name} -- the pull refused", pulled.stderr)
        # `_abandon` decides the code -- a branch it could not unwind is worse news
        # than the refusal that led there -- but the refusal is what the reader needs.
        code = _abandon(git, name, upgrade.anchor, "the pull refused", code=2)
        return Outcome(name, code, refusal.detail)
    if divergence:
        # Deliberately not abandoned: the tree holds a partial adoption, and dropping
        # the branch out from under it would hide the evidence in a dirty checkout.
        return failed(2, f"upgrade: {name} -- {divergence}")

    checked = verify_pull(project, source)
    if checked.returncode != 0:
        return failed(
            2,
            f"upgrade: {name} -- the pull left drift that the commit gate will reject",
            checked.stdout,
            checked.stderr,
        )

    changed = changed_paths(git)
    if not changed:
        # The already-current case, and the one that has to be *free*: this is meant
        # to be run on a schedule to prove nothing is stale, so a no-op run must
        # leave no trace. Cutting a branch and walking away would litter one empty
        # `claude/devkit-upgrade-<mmdd>` per check, and `--sync` would then have to
        # reap them.
        return Outcome(name, _abandon(git, name, upgrade.anchor, "already current", code=0))

    # Safe only because the tree was clean before the pull -- see `changed_paths`.
    staged = git("add", "-A")
    if staged.returncode != 0:
        return failed(
            2, f"upgrade: {name} -- FAILED at `git add -A`", staged.stderr or staged.stdout
        )

    committed, retried = commit_with_hook_retry(git, commit_message(tag, len(changed)))
    if committed.returncode != 0:
        return failed(
            2,
            f"upgrade: {name} -- FAILED at `git commit` "
            f"({'the hooks rewrote the tree and it still failed' if retried else 'a gate refused it'})",
            committed.stderr or committed.stdout,
        )

    pushed = git("push", "-u", "origin", branch_name())
    if pushed.returncode != 0:
        return failed(
            2,
            f"upgrade: {name} -- FAILED at `git push`",
            pushed.stderr or pushed.stdout,
        )

    url, created, error = sweep.ensure_pr(
        sweep.gh_for(project),
        sweep.Plan(
            pr_title=commit_message(tag, len(changed)),
            pr_body=pr_body(tag, previous, changed),
            pr_head=branch_name(),
            pr_base=state.default_branch,
        ),
    )
    if error:
        return failed(2, f"upgrade: {name} -- pushed, but the PR failed: {error}")
    print(f"upgrade: PR {'opened' if created else 'already open'}: {url}")
    return Outcome(name, 0)


def artifact_body(tag: str, dry_run: bool, outcomes: list[Outcome]) -> str:
    """The full text of `logs/upgrade.log` -- empty when nothing needs a human.

    Empty on a clean run rather than absent, so a stale artifact can never send the
    next reader after a failure that is already fixed. The header carries the command
    that re-runs the thing that failed, because the artifact is read by whoever finds
    it and not only by whoever started the run.
    """
    actionable = [outcome for outcome in outcomes if outcome.code != 0]
    if not actionable:
        return ""
    names = " ".join(outcome.name for outcome in actionable)
    lines = [
        "# source: devkit scripts/upgrade-project.py",
        f"# run: adopt devkit {tag} ({'dry run' if dry_run else 'apply'})",
        f"# retry: python scripts/upgrade-project.py {','.join(o.name for o in actionable)} --yes",
        f"# unresolved: {names}",
        "",
    ]
    for outcome in actionable:
        lines.append(f"=== {outcome.name} (exit {outcome.code}) ===")
        lines.append(outcome.detail.strip() or "(no detail recorded)")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_artifact(root: Path, body: str) -> Path | None:
    """Persist the report under `root`. Best-effort: an unwritable `logs/` is not
    itself a reason to fail an upgrade that otherwise worked."""
    path = root / ARTIFACT
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    except OSError:
        return None
    return path


def _finish(tag: str, dry_run: bool, outcomes: list[Outcome]) -> int:
    """Write the artifact, then return the worst code across the run.

    Every exit from `main` goes through here, including the ones that never reach a
    project: "devkit has no tags" and "that checkout is not in the workspace" are the
    two failures most likely to be read hours later out of a task terminal.
    """
    body = artifact_body(tag, dry_run, outcomes)
    path = write_artifact(REPO_ROOT, body)
    if body and path is not None:
        print(f"upgrade: details in {ARTIFACT.as_posix()}", file=sys.stderr)
    return max((outcome.code for outcome in outcomes), default=0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # Optional, and paired with --all for the same reason --dry-run is paired with
    # --yes: the VS Code picker has to emit one real token on the "every project"
    # branch too, and an empty string would reach argparse as a stray positional.
    parser.add_argument(
        "project",
        nargs="?",
        help="checkout name(s) to upgrade, comma-delimited, as listed in the workspace",
    )
    parser.add_argument(
        "--all",
        dest="every",
        action="store_true",
        help=(
            "upgrade every checkout that has vendored devkit, one PR each. Skips "
            "devkit itself, anything that never adopted, and the second worktree of "
            "a repo already upgraded in this run"
        ),
    )
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    parser.add_argument(
        "--devkit", type=Path, default=REPO_ROOT, help="devkit checkout to pull from"
    )
    apply_mode = parser.add_mutually_exclusive_group()
    apply_mode.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    apply_mode.add_argument("--yes", dest="dry_run", action="store_false")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    requested = project_selection(args.project)
    if args.every and requested:
        parser.error(f"--all upgrades every project; drop {args.project} or drop --all")
    if not args.every and not requested:
        parser.error("name one or more checkouts to upgrade, or pass --all for every adopter")

    def stopped(code: int, message: str, tag: str = "(none resolved)") -> int:
        """A run-level failure: reported once, and to the artifact like any other."""
        print(message, file=sys.stderr)
        return _finish(tag, args.dry_run, [Outcome("(run)", code, message)])

    if not args.workspace.is_file():
        return stopped(2, f"upgrade: no workspace file at {args.workspace}")
    names = sweep.parse_workspace(args.workspace.read_text(encoding="utf-8"))
    unknown = [name for name in requested if name not in names]
    if unknown:
        return stopped(
            2,
            f"upgrade: {', '.join(unknown)} not in {args.workspace.name}. "
            f"Known checkouts: {', '.join(names)}",
        )

    root = args.workspace.parent
    tag = latest_tag(args.devkit)
    if not tag:
        # Reported once for the run, not once per project: with --all it is the same
        # fact about devkit every time, and repeating it reads as four problems.
        return stopped(1, f"upgrade: {NO_TAG}")

    # One lookup for the whole run: every project adopts the same release, and this is
    # what the per-project stamps are measured against.
    tag_commit = commit_for(args.devkit, tag)
    # The whole tag set, for the projects that are *ahead* of it rather than behind.
    tags = release_tags(args.devkit)

    scope = names if args.every else requested
    selected = select_all(candidates_for(root, scope, args.devkit))

    # Refusals decided before any project is touched. Carried rather than printed
    # only, because `_finish` owes them to the artifact and to the exit code.
    preflight: list[Outcome] = []
    todo: list[str] = []
    for name, skip in selected:
        if skip:
            # An explicitly named checkout that cannot be a target is an operator
            # error, not a skip: they asked for something this cannot do.
            if not args.every:
                return stopped(2, f"upgrade: {name} {skip}", tag)
            print(f"upgrade: {name} -- skipped, it {skip}")
        # Before inspecting or refusing anything: an up-to-date project is the common
        # case on a scheduled run, and proving it must not depend on the project being
        # clean, on the right branch, or on anything else this could refuse over.
        elif is_current(root / name, tag_commit):
            print(f"upgrade: {name} is already on devkit {tag}.")
        elif backwards := unreleased_adoption(root / name, tags):
            # A refusal (1) rather than a skip: the project is on a release this
            # checkout cannot serve, and a human has to reconcile the two. Reported
            # here so the branch refusal below can never describe it instead -- a
            # project ahead of the release is usually parked on the upgrade branch
            # that put it there, and that is the less true of the two facts.
            message = f"upgrade: {name} -- {backwards}"
            print(message, file=sys.stderr)
            preflight.append(Outcome(name, 1, message))
        else:
            todo.append(name)

    if not todo:
        # Still written -- as an empty file when nothing needs a human, clearing
        # whatever the last run left.
        return _finish(tag, args.dry_run, preflight)

    if args.dry_run:
        # The refusals still count. A dry run is how a scheduled check asks "could
        # this be adopted right now", and answering 0 while a project is parked on a
        # task branch reports "all clear" for the one state that is not.
        outcomes = [upgrade_one(name, root / name, tag) for name in todo]
        print("\nDry run -- nothing was changed. Re-run with --yes to apply.")
        return _finish(tag, args.dry_run, preflight + outcomes)

    # One worktree for the whole run: every project adopts the same revision, and
    # it is materialised only once something is actually going to be pulled.
    try:
        with source_at_tag(args.devkit, tag) as source:
            outcomes = [upgrade_one(name, root / name, tag, source) for name in todo]
    except RuntimeError as exc:
        return stopped(2, f"upgrade: could not check devkit out at {tag}: {exc}", tag)
    return _finish(tag, args.dry_run, preflight + outcomes)


def release_tags(devkit: Path) -> list[str]:
    """Every devkit release tag, newest first; [] when there are none to read.

    Split out from `latest_tag` because the *set* answers a second question the pick
    cannot: whether the release a project already vendored exists in this checkout at
    all. See `unreleased_adoption`.
    """
    result = subprocess.run(
        ["git", "-C", str(devkit), "tag", "--list", "--sort=-v:refname"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def latest_tag(devkit: Path) -> str | None:
    """devkit's newest release tag, or None when it has none.

    Deliberately *not* `describe --exact-match HEAD`: a consumer adopts the latest
    release, and the devkit checkout is normally sitting on a working branch. Keying
    off HEAD made this refuse with "HEAD is not tagged" almost every time it ran,
    which is noise rather than signal -- and this is meant to be safe to run on a
    schedule to prove nothing is stale.
    """
    tags = release_tags(devkit)
    return tags[0] if tags else None


def _devkit_tag(devkit: Path) -> str | None:
    """The tag on the devkit checkout's HEAD -- the release being adopted."""
    result = subprocess.run(
        ["git", "-C", str(devkit), "describe", "--tags", "--exact-match", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or None if result.returncode == 0 else None


if __name__ == "__main__":
    sys.exit(main())
