#!/usr/bin/env python3
"""One line of workspace health, for a SessionStart hook.

Answers the questions that went unasked for a whole day of parallel work and cost
an afternoon to unpick: **is there work stranded in any checkout**, **is any
checkout behind on devkit**, and **is the branch policy being enforced still the
one in this checkout**. All three were already computable; nothing was asking.

The third is the quietest of them. The global hooks are a *copy*, so a stale one
still fires and simply enforces an older policy -- there is no failure to notice,
which is why it needs a line here rather than a check someone remembers to run.

Design constraints, all from the fact that this runs at the top of every session:

- **Never blocks.** Always exits 0. A status line that can fail a session start is
  a status line that gets removed the first time it is wrong.
- **No network.** `--no-fetch`, so ahead/behind may be stale -- but "3 checkouts
  have uncommitted work" does not need a fetch to be true, and a hook that costs
  seconds gets disabled.
- **Silent when healthy.** Prints nothing when there is nothing to say, so the one
  time it does speak is worth reading.
- **Absent workspace is silence, not an error.** The registry is a workstation-local
  file; on CI, a fresh clone, or anyone else's machine there is simply nothing to
  report.

Tested in `tests/test_workspace_status.py`.
"""

from __future__ import annotations

import json
import sys
import time as _time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import schedule_health
import sweep
import worktree

REPO_ROOT = Path(__file__).resolve().parents[1]
# Box-aware (see `sweep.default_workspace`). This one had the quietest version of the
# bug: a session started inside a box resolved a registry that does not exist, and this
# hook's whole design is to stay silent when it cannot tell -- so it reported nothing,
# in exactly the checkouts an agent works in.
DEFAULT_WORKSPACE = sweep.default_workspace(REPO_ROOT)
# devkit's PERMANENT checkout, which is this one unless the session is in a box. The
# adoption and retired-hook checks below exempt the source of those files by comparing
# paths against it -- and from a box the comparison missed, so this line reported devkit
# itself as "never vendored devkit" at the top of every agent session.
SOURCE_ROOT = sweep.source_checkout(REPO_ROOT)
# Where `install-git-policy.py` puts the runtime the global hooks actually execute.
POLICY_TARGET = Path.home() / ".devkit" / "git-hooks"
# The workspace-root settings file that wires the cross-checkout edit guard. Not inside
# any repo -- see `guard_line` for why that is exactly why it needs reporting.
ROOT_SETTINGS = Path(".claude") / "settings.json"
GUARD_SCRIPT = "worktree-guard.py"

# `install-git-policy.py` is hyphenated and so cannot be imported by name. Going
# through the shared loader keeps the file list and the comparison in one place --
# duplicating them here would put a second copy inside the very hook whose job is
# to notice copies going stale. Guarded because nothing in this file may break a
# session start: an unavailable loader just means this half stays silent.
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent / "precommit"))
    # Resolved by the sys.path insert above; `scripts/precommit/` is not an
    # importable package.
    from _loader import load_by_path
except ImportError:  # pragma: no cover - the repo always ships _loader.py
    # mypy DOES resolve the import above, so it knows the real signature and reads
    # this fallback as a type error rather than the guard it is. The ignore has to
    # be here, on the assignment -- an `import-not-found` ignore on the import
    # itself is what used to be here, and it was dead.
    load_by_path = None  # type: ignore[assignment]


def stranded_line(results: list[sweep.Result]) -> str:
    """The stranded-work half; "" when every checkout is clean.

    Names the checkouts rather than counting them: "3 checkouts need action" makes
    you go run something else to find out which, and at the top of a session the
    whole point is to not send you on an errand.
    """
    actionable = [r for r in results if r.verdict in sweep.ACTIONABLE]
    if not actionable:
        return ""
    parts = [f"{r.state.name} ({r.verdict})" for r in actionable]
    return f"stranded work: {', '.join(parts)}"


def behind_line(behind: dict[str, str], latest: str) -> str:
    """The devkit-freshness half; "" when nothing is knowably behind."""
    if not behind:
        return ""
    parts = [f"{name} on {tag}" for name, tag in sorted(behind.items())]
    return f"devkit {latest} available: {', '.join(parts)}"


def policy_line(
    source_root: Path = REPO_ROOT, target: Path = POLICY_TARGET, latest: str = ""
) -> str:
    """The branch-policy half; "" when there is nothing to say.

    The global hooks are a *copy* of `scripts/git_policy.py`, so they go stale
    invisibly: the hooks keep firing, they just enforce an older policy. Nothing
    else in the workspace would ever mention it, which is how a runtime came to be
    missing its escape hatch for two days while the source had it -- the only
    symptom was an env var that appeared to do nothing.

    Two different questions, because they have different answers. *Modified* means
    the installed bytes no longer match the receipt, which should never happen and
    is worth saying loudly. *Behind* means it was installed from an older release,
    which is ordinary and just wants a re-run.

    Neither is a comparison against the working tree, so editing `git_policy.py` on
    a branch stays silent -- the check would otherwise warn continuously while the
    policy is being worked on, and a line that always warns is one nobody reads.

    Spawn-free, per this file's contract: the receipt carries hashes so verifying
    it costs a read, and `latest` is resolved by the caller from the ref store.
    Silent when nothing is installed (a fresh clone, CI, anyone else's machine)
    and silent on any error.
    """
    if load_by_path is None or not target.is_dir():
        return ""
    try:
        installer = load_by_path(
            "_install_git_policy", source_root / "scripts" / "install-git-policy.py"
        )
        receipt = installer.read_receipt(target)
        drifted = installer.compare_install(target, receipt)
        behind = installer.behind_ref(receipt, latest)
    except Exception:
        return ""
    parts = []
    if drifted:
        parts.append(", ".join(f"{d.name} {d.reason}" for d in drifted))
    if behind:
        parts.append(f"installed from {behind}, {latest} available")
    if not parts:
        return ""
    return (
        f"branch policy: {'; '.join(parts)} "
        f"(fix: python devkit/scripts/install-git-policy.py --yes)"
    )


# What makes a checkout a devkit *project* rather than a directory sitting next to one.
# Ordered by how early each fails: without `DEVKIT_VERSION` nothing has ever been
# vendored at all, so the rest cannot be true either and reporting both would be noise.
#
# The pre-commit config earns its place because it is the only thing that *runs* the
# gate. A repo can hold every vendored hook and still commit anything, and that failure
# is invisible -- there is no error, checks simply never fire.
ADOPTION_MARKERS: tuple[tuple[str, str], ...] = (
    ("DEVKIT_VERSION", "never vendored devkit"),
    (".pre-commit-config.yaml", "no pre-commit gate"),
)


def adoption_line(root: Path, names: list[str], source: Path = SOURCE_ROOT) -> str:
    """Registered checkouts that are not shaped like devkit projects; "" when all are.

    The gap this closes. `upgrade-project.py` classifies an unadopted checkout as
    `SKIP_UNADOPTED` and moves on, which is the right call -- it adopts releases, it
    does not onboard -- but the skip is rendered exactly like a routine one and scrolls
    past with them. So ibkr_trader sat outside the harness indefinitely: no vendored
    hooks, no commit gate, no `lint-fix.py`, and 29 lint findings in a directory no CI
    scope covered. Nothing was broken, which is precisely why nobody looked.

    A permanent condition needs a standing report rather than a line in a log nobody
    re-reads, and this hook is the only thing that speaks at the start of every session.

    Reports the first missing marker per checkout, not all of them: "never vendored
    devkit" already implies the rest, and one fix per checkout is what the reader can
    act on. `source` is devkit itself, which is the origin of these files rather than an
    adopter of them -- the same exemption `upgrade-project.py` makes for it.
    """
    faults = adoption_faults(root, names, source)
    if not faults:
        return ""
    named = ", ".join(f"{name} ({reason})" for name, reason in faults)
    return (
        f"not devkit projects: {named} "
        f"(fix: python devkit/scripts/sync-devkit.py --pull, from that checkout)"
    )


def adoption_faults(
    root: Path, names: list[str], source: Path = SOURCE_ROOT
) -> list[tuple[str, str]]:
    """`[(checkout, why)]` for those missing a marker -- the data behind `adoption_line`.

    Separate from the rendering so the architectural test in
    `tests/test_workspace_status.py` can assert on the *set* of offenders rather than
    grep a sentence. A test that matches on formatting breaks when the wording changes,
    which teaches everyone to relax the test.
    """
    try:
        source_resolved = source.resolve()
    except OSError:
        source_resolved = source
    faults: list[tuple[str, str]] = []
    for name in names:
        path = root / name
        try:
            if not path.is_dir() or path.resolve() == source_resolved:
                continue
        except OSError:
            continue
        for marker, reason in ADOPTION_MARKERS:
            if not (path / marker).is_file():
                faults.append((name, reason))
                break
    return faults


def retired_hooks_line(root: Path, names: list[str], source: Path = SOURCE_ROOT) -> str:
    """Checkouts whose settings still wire a hook devkit has retired; "" when none do.

    The failure this exists for is loud in the worst way: `sync-devkit.py --pull`
    *deletes* a retired script, so a surviving `hooks` entry naming it makes the
    harness spawn a missing file on every prompt or edit in that project. `--pull`
    unwires it (`prune_settings`), which handles every project that has upgraded --
    and this line is for the ones that have not, plus any settings file the pull could
    not parse and therefore, correctly, refused to rewrite.

    Reads the retired list from `sync-devkit.py` rather than repeating it, because a
    second copy is exactly the drift this whole file exists to notice.
    """
    if load_by_path is None:
        return ""
    try:
        sync = load_by_path("_sync_devkit", source / "scripts" / "sync-devkit.py")
        # Repo-relative paths, and only the ones that could BE a hook. Matching on the
        # basename made `README.md` a retired hook here (RETIRED_PATHS carries
        # `.claude/skills/state-tools/README.md`) and named every checkout whose
        # settings mention a README -- which is a markdownlint hook, wired and live.
        retired = sync.retired_hook_paths()
        settings_rel = sync.SETTINGS_FILE
    except Exception:
        return ""

    offenders: list[str] = []
    for name in names:
        path = root / name / settings_rel
        try:
            text = path.read_text(encoding="utf-8") if path.is_file() else ""
        except OSError:
            continue
        probe = text.replace("\\\\", "/").replace("\\", "/")
        hits = sorted({rel.rsplit("/", 1)[-1] for rel in retired if rel in probe})
        if hits:
            offenders.append(f"{name} ({', '.join(hits)})")
    if not offenders:
        return ""
    return (
        f"settings wire retired hooks: {', '.join(offenders)} "
        f"(fix: python devkit/scripts/sync-devkit.py --pull, from that checkout)"
    )


def boxes_line(rows: list[dict]) -> str:
    """The ephemeral-box half; "" when there are none.

    The static tier is reported by `stranded_line` because a checkout that holds work is
    a thing someone has to act on. A box is the same problem wearing a different shape:
    it holds a port slot out of a fixed ceiling and, if its project has a stack, a set of
    containers and volumes. Nothing else says a box exists — they are deliberately absent
    from the workspace file, so the sweep cannot see them and no picker lists them.

    Three states, because they want different actions, and only one of them wants a
    command from whoever reads this line. A box that still holds work wants shipping. A
    box whose work has left wants reaping, and is pure leaked resource until it is. A box
    that is pushed and waiting on its PR wants **nothing**: `reconcile` reaps it when the
    PR merges, and the line used to file it under `reapable` and then append `reap --all
    --yes` as the fix -- standing advice, printed at every session start, to destroy the
    checkout an open review still points at. `worktree.AWAITS_A_MERGE` is the same
    constant `reap` refuses on, so the advice here and the tool's behaviour cannot drift
    apart again.

    The fix suffix is therefore printed only when something is actually reapable.
    """
    if not rows:
        return ""
    reapable = [row["box"] for row in rows if row["reapable"]]
    awaiting = [
        row["box"]
        for row in rows
        if not row["reapable"] and row["verdict"] in worktree.AWAITS_A_MERGE
    ]
    holding = [
        row["box"]
        for row in rows
        if not row["reapable"] and row["verdict"] not in worktree.AWAITS_A_MERGE
    ]
    parts = []
    if holding:
        parts.append(f"{len(holding)} holding work ({', '.join(sorted(holding))})")
    if awaiting:
        parts.append(f"{len(awaiting)} awaiting a PR merge ({', '.join(sorted(awaiting))})")
    if reapable:
        parts.append(f"{len(reapable)} reapable ({', '.join(sorted(reapable))})")
    line = f"{len(rows)} ephemeral box(es): {'; '.join(parts)}"
    if reapable:
        line += " (fix: python devkit/scripts/worktree.py reap --all --yes)"
    return line


def scheduler_line(source: Path = SOURCE_ROOT, now: float = 0.0, stale_hours: float = 2.0) -> str:
    """Reports an unattended pass that has stopped running; "" while it is running.

    Everything automatic in this workspace hangs off one Windows scheduled task, and a
    scheduled task that stops looks *exactly* like one that is working: no error, no
    output, nothing red. It happened -- the task sat disabled for five days while every
    box it manages leaked its port slot and its volumes, and every checkout drifted
    behind its remote until a session opened on a tree that predated a merged PR. The
    only reason anyone found out was that the drift eventually blocked a person.

    So this asks the question by the answer that matters. Not "is a task registered"
    -- `schtasks` would say yes for a task that is disabled, pointed at a moved
    checkout, or dying on startup every fire -- but **when did a pass last finish**,
    which `worktree.write_reconcile_log` stamps on every run, success or failure. One
    `stat` covers a disabled task, a deleted one, a crashed interpreter and a broken
    path alike, with no spawn and nothing platform-specific.

    Read against `SOURCE_ROOT`, never `REPO_ROOT`: the scheduled command names the
    permanent checkout's `worktree.py`, so that is the only `logs/` the pass ever
    writes to. From a box, `REPO_ROOT` is the box, and this would report the workspace
    as broken in exactly the sessions an agent runs in.

    Silent when the log has never existed. That is a workstation that has not installed
    the task -- a fresh clone, CI, anyone else's machine -- and a standing line
    demanding a Windows-only convenience be installed is one you learn to skim, which
    is the failure this line is trying to fix rather than repeat.
    """
    log = source / worktree.RECONCILE_LOG
    try:
        age = (now or _time.time()) - log.stat().st_mtime
    except OSError:
        return ""
    if age < stale_hours * 3600:
        return ""
    return (
        f"unattended pass last ran {_age(age)} ago -- boxes are not being reaped and "
        f"checkouts are not being synced "
        f"(fix: python devkit/scripts/install-reconcile-task.py --status)"
    )


def _age(seconds: float) -> str:
    """`93600` -> `1d 2h`. Coarse on purpose: this line is about days, not minutes."""
    hours = int(seconds // 3600)
    return f"{hours}h" if hours < 24 else f"{hours // 24}d {hours % 24}h"


def guard_line(root: Path, settings: Path = ROOT_SETTINGS) -> str:
    """Reports a workspace root that does not run the cross-checkout edit guard.

    `worktree-guard.py` is wired in every repo that vendors devkit's settings, which is
    the case it matters least in — a session inside a checkout is already the case the
    guard stays silent for. The session it exists for is the one opened at the workspace
    *root*, and that root is not inside any repository, so its `.claude/settings.json` is
    a workstation file no test in this repo can hold in place.

    Absent-is-silent everywhere else in this file, and it is the wrong default here: an
    unwired guard has no symptom at all. The edits land on home branches, and the sweep
    reports the resulting `needs-branch` backlog days later as though a human had left it
    there. So a root that has no settings file, or one that never names the guard, is
    reported -- but only on a machine that has a multi-root workspace to begin with,
    which `main` has already established by the time this is called.
    """
    path = root / settings
    try:
        if path.is_file() and GUARD_SCRIPT in path.read_text(encoding="utf-8"):
            return ""
    except OSError:
        return ""
    return (
        f"cross-checkout edit guard not wired at the workspace root: an edit from a "
        f"root session lands on a checkout's home branch with no task branch under it "
        f"(fix: add a PreToolUse hook running devkit/scripts/{GUARD_SCRIPT} "
        f"to {settings.as_posix()})"
    )


def schedule_lines() -> list[str]:
    """One line per unhealthy scheduled job; [] when they are all fine.

    **The point of surfacing it here rather than in a command you run.** A scheduled
    job's failure mode is silence, so the check for it must not itself be something
    someone has to remember -- that is the same defect one level up. `reconcile` was
    disabled for five days and 471 runs; the cost was 26 leaked boxes and 5 GB, and
    nothing anywhere was red. This is the line that would have said so on the next
    session start.

    Never raises: `report` swallows its own failures and answers [] off Windows.
    """
    try:
        return [f"schedule: {line}" for line in schedule_health.report()]
    except Exception:
        return []


def scheduler_fallback(scheduler: str, schedule: list[str]) -> str:
    """Keep the log warning unless schtasks already explains reconcile's failure."""
    if any("devkit-worktree-reconcile:" in line for line in schedule):
        return ""
    return scheduler


def render(
    results: list[sweep.Result],
    behind: dict[str, str],
    latest: str,
    policy: str = "",
    adoption: str = "",
    boxes: str = "",
    guard: str = "",
    retired: str = "",
    scheduler: str = "",
    schedule: list[str] | None = None,
) -> str:
    """The whole message, or "" when there is nothing worth saying."""
    halves = (
        # First: it is the reason several of the lines below are non-empty. A reader
        # who fixes the stranded checkouts by hand and leaves the pass stopped will be
        # reading the same list tomorrow.
        *(schedule or []),
        scheduler,
        stranded_line(results),
        behind_line(behind, latest),
        policy,
        adoption,
        boxes,
        guard,
        retired,
    )
    lines = [line for line in halves if line]
    if not lines:
        return ""
    return "\n".join(f"[workspace] {line}" for line in lines)


def projects_behind(root: Path, names: list[str], latest: str) -> dict[str, str]:
    """`{checkout: its vendored tag}` for those not on `latest`.

    Only reports a checkout whose vendored tag is *recorded*. An unrecorded one is
    "cannot tell" -- see `sync-devkit.stale_pin` -- and guessing would put every
    not-yet-upgraded project in this line forever until someone stopped reading it.
    """
    behind: dict[str, str] = {}
    for name in names:
        receipt = root / name / "DEVKIT_FILES.json"
        if not receipt.is_file():
            continue
        try:
            raw = json.loads(receipt.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        tag = raw.get("devkit_tag") if isinstance(raw, dict) else None
        if isinstance(tag, str) and tag and tag != latest:
            behind[name] = tag
    return behind


def latest_devkit_tag(devkit: Path) -> str:
    """devkit's newest release tag, or "" -- read from refs, without running git.

    Reading `.git/refs/tags` and `packed-refs` directly keeps this hook stdlib-only
    and spawn-free at session start. A missing or unreadable ref store is "" rather
    than an error, which the caller treats as "nothing to say".
    """
    tags: list[str] = []
    loose = devkit / ".git" / "refs" / "tags"
    if loose.is_dir():
        tags.extend(p.name for p in loose.iterdir() if p.is_file())
    packed = devkit / ".git" / "packed-refs"
    if packed.is_file():
        try:
            for line in packed.read_text(encoding="utf-8").splitlines():
                marker = " refs/tags/"
                if marker in line and not line.startswith("#"):
                    tags.append(line.split(marker, 1)[1].strip().removesuffix("^{}"))
        except OSError:
            pass
    return max(set(tags), key=_version_key, default="")


def _version_key(tag: str) -> tuple[int, ...]:
    """Sort `vMAJOR.MINOR.PATCH` numerically; anything else sorts lowest."""
    try:
        return tuple(int(part) for part in tag.lstrip("v").split("."))
    except ValueError:
        return (-1,)


def box_survey(workspace: Path) -> list[dict]:
    """`worktree.survey`, downgraded to silence on any failure.

    Never fetches, for this file's no-network contract, and never raises: the survey
    spawns git per box, and one box left in a state git dislikes must not cost the whole
    status line.
    """
    try:
        return worktree.survey(workspace, fetch=False)
    except Exception:
        return []


def main(argv: list[str] | None = None) -> int:
    workspace = DEFAULT_WORKSPACE
    if not workspace.is_file():
        return 0
    try:
        names = sweep.parse_workspace(workspace.read_text(encoding="utf-8"))
        if not names:
            return 0
        root = workspace.parent
        results = sweep.sweep(root, names, fetch=False)
        latest = latest_devkit_tag(root / "devkit")
        behind = projects_behind(root, names, latest) if latest else {}
        schedule = schedule_lines()
        scheduler = scheduler_fallback(scheduler_line(), schedule)
        message = render(
            results,
            behind,
            latest,
            policy_line(latest=latest),
            adoption_line(root, names),
            boxes_line(box_survey(workspace)),
            guard_line(root),
            retired_hooks_line(root, names),
            scheduler,
            schedule,
        )
    except Exception as exc:
        print(f"[workspace] status unavailable ({type(exc).__name__})", file=sys.stderr)
        return 0
    if message:
        print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
