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

`--all` upgrades every checkout in the workspace that has actually vendored devkit,
one PR each -- the release is one upstream revision, so adopting it everywhere is
one operation. Each project is still upgraded on its own terms: a refusal in one
does not stop the others, and the exit code reports the worst outcome.

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
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sweep
import task_branch as tb

REPO_ROOT = Path(__file__).resolve().parents[1]
# Box-aware (see `sweep.default_workspace`).
DEFAULT_WORKSPACE = sweep.default_workspace(REPO_ROOT)
SYNC_SCRIPT = "scripts/sync-devkit.py"

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


def branch_name(today: _dt.date | None = None) -> str:
    """The branch an upgrade lands on: `claude/devkit-upgrade-<mmdd>`."""
    return tb.branch_name(tb.slugify("devkit upgrade"), set(), today)


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


def upgrade_one(name: str, project: Path, tag: str, source: Path | None = None) -> int:
    """Adopt `tag` in one checkout: 0 done or nothing to do, 1 refused, 2 failed.

    `source` is a clean devkit worktree at `tag` to pull from. **None means dry
    run** -- the plan is printed and nothing is touched, which is also why the
    caller materialises the worktree rather than this function: one release is one
    worktree however many projects adopt it.

    The exit codes are the sweep convention (1 needs a human decision, 2 something
    broke mid-flight) so `main` can take the worst across a `--all` run.
    """
    state = sweep.inspect(name, project, fetch=False)
    upgrade = plan(state, tag, None)
    if upgrade.refusal:
        print(f"upgrade: {name} -- {upgrade.refusal}", file=sys.stderr)
        return 1

    previous = (project / "DEVKIT_VERSION").read_text(encoding="utf-8").strip()
    print(f"upgrade: {name} {previous} -> {tag}")
    for step in upgrade.steps:
        print(f"  1. git -C {name} {' '.join(step)}")
    print(f"  2. {SYNC_SCRIPT} --pull --src <devkit worktree at {tag}>")
    print(f"  3. git -C {name} add {' '.join(UPGRADE_PATHS)} + the MANIFEST paths")
    print(f"  4. git -C {name} commit -m {commit_message(tag, '<n>')!r}")
    print("  5. git push -u origin, then gh pr create")
    if source is None:
        return 0

    git = sweep.git_for(project)
    applied = sweep.apply_plan(name, project, upgrade, git=git)
    if not applied.ok:
        print(f"upgrade: FAILED at `{applied.failed}`\n{applied.error}", file=sys.stderr)
        return 2

    pulled = run_pull(project, source)
    print(pulled.stdout.rstrip())
    if pulled.returncode != 0:
        print(pulled.stderr.rstrip(), file=sys.stderr)
        return _abandon(git, name, upgrade.anchor, "the pull refused", code=2)

    changed = changed_paths(git)
    if not changed:
        # The already-current case, and the one that has to be *free*: this is meant
        # to be run on a schedule to prove nothing is stale, so a no-op run must
        # leave no trace. Cutting a branch and walking away would litter one empty
        # `claude/devkit-upgrade-<mmdd>` per check, and `--sync` would then have to
        # reap them.
        return _abandon(git, name, upgrade.anchor, "already current", code=0)

    for step in (
        # Safe only because the tree was clean before the pull -- see `changed_paths`.
        ("add", "-A"),
        ("commit", "-m", commit_message(tag, len(changed))),
        ("push", "-u", "origin", branch_name()),
    ):
        result = git(*step)
        if result.returncode != 0:
            print(f"upgrade: FAILED at `git {' '.join(step)}`", file=sys.stderr)
            print((result.stderr or result.stdout).rstrip(), file=sys.stderr)
            return 2

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
        print(f"upgrade: pushed, but the PR failed: {error}", file=sys.stderr)
        return 2
    print(f"upgrade: PR {'opened' if created else 'already open'}: {url}")
    return 0


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

    if not args.workspace.is_file():
        print(f"upgrade: no workspace file at {args.workspace}", file=sys.stderr)
        return 2
    names = sweep.parse_workspace(args.workspace.read_text(encoding="utf-8"))
    unknown = [name for name in requested if name not in names]
    if unknown:
        print(
            f"upgrade: {', '.join(unknown)} not in {args.workspace.name}. "
            f"Known checkouts: {', '.join(names)}",
            file=sys.stderr,
        )
        return 2

    root = args.workspace.parent
    tag = latest_tag(args.devkit)
    if not tag:
        # Reported once for the run, not once per project: with --all it is the same
        # fact about devkit every time, and repeating it reads as four problems.
        print(f"upgrade: {NO_TAG}", file=sys.stderr)
        return 1

    # One lookup for the whole run: every project adopts the same release, and this is
    # what the per-project stamps are measured against.
    tag_commit = commit_for(args.devkit, tag)

    scope = names if args.every else requested
    selected = select_all(candidates_for(root, scope, args.devkit))

    todo: list[str] = []
    for name, skip in selected:
        if skip:
            # An explicitly named checkout that cannot be a target is an operator
            # error, not a skip: they asked for something this cannot do.
            if not args.every:
                print(f"upgrade: {name} {skip}", file=sys.stderr)
                return 2
            print(f"upgrade: {name} -- skipped, it {skip}")
        # Before inspecting or refusing anything: an up-to-date project is the common
        # case on a scheduled run, and proving it must not depend on the project being
        # clean, on the right branch, or on anything else this could refuse over.
        elif is_current(root / name, tag_commit):
            print(f"upgrade: {name} is already on devkit {tag}.")
        else:
            todo.append(name)

    if not todo:
        return 0

    if args.dry_run:
        # The refusals still count. A dry run is how a scheduled check asks "could
        # this be adopted right now", and answering 0 while a project is parked on a
        # task branch reports "all clear" for the one state that is not.
        codes = [upgrade_one(name, root / name, tag) for name in todo]
        print("\nDry run -- nothing was changed. Re-run with --yes to apply.")
        return max(codes)

    # One worktree for the whole run: every project adopts the same revision, and
    # it is materialised only once something is actually going to be pulled.
    try:
        with source_at_tag(args.devkit, tag) as source:
            codes = [upgrade_one(name, root / name, tag, source) for name in todo]
    except RuntimeError as exc:
        print(f"upgrade: could not check devkit out at {tag}: {exc}", file=sys.stderr)
        return 2
    return max(codes)


def latest_tag(devkit: Path) -> str | None:
    """devkit's newest release tag, or None when it has none.

    Deliberately *not* `describe --exact-match HEAD`: a consumer adopts the latest
    release, and the devkit checkout is normally sitting on a working branch. Keying
    off HEAD made this refuse with "HEAD is not tagged" almost every time it ran,
    which is noise rather than signal -- and this is meant to be safe to run on a
    schedule to prove nothing is stale.
    """
    result = subprocess.run(
        ["git", "-C", str(devkit), "tag", "--list", "--sort=-v:refname"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    tags = [line.strip() for line in result.stdout.splitlines() if line.strip()]
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
