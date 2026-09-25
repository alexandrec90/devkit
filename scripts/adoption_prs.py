#!/usr/bin/env python3
"""How an adoption PR is named, found, and -- when a newer release replaces it -- closed.

Cut out of `scripts/upgrade-project.py`, which is three times its `file_lines` ceiling
and twice its `definitions` one with a split deferred on two earlier records; the
superseded rule was the third body of work to land there and the first that another
script also needs. `fix-prs.py` has to tell an adoption PR from any other -- an agent
sent at one the next sweep will close spends a session on a tree nobody wants -- and a
hyphenated module cannot be imported, so the seam that both read moved to a name both
can. `upgrade-project.py` imports every name back and its callers did not move.

**Only the newest release's adoption stands.** A red v0.11.20 adoption beside the
v0.11.21 one is not a second chance at the older release: merging it lands a vendored
copy the very next pass replaces. `close_superseded` closes it with the successor named,
so the trail reads from either PR; the box behind it is `worktree.py reconcile`'s to reap,
exactly as a merged one's is, and nothing is deleted here.

Stdlib plus this repo's modules. Tested in `tests/test_adoption_prs.py`, with the sweep's
own use of these names still exercised through `tests/test_upgrade_project.py`.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sweep
import task_branch as tb


# The topic every upgrade branch is named for; `upgrade_slug` appends the release.
UPGRADE_SLUG = "devkit upgrade"


def upgrade_slug(tag: str) -> str:
    """The topic `worktree.plan_new` names this upgrade's branch and box from.

    Carries the tag, and that is a correctness requirement rather than a label: a
    branch name whose PR merged is *permanently retired* by the branch policy, and
    `plan_new` disambiguates only against refs that still exist -- a squash-merged,
    branch-deleted PR leaves none. Named by date alone, the morning release's merged
    adoption therefore blocked every commit of the afternoon's (v0.9.0 -> v0.9.1, in
    three consumers at once). One release is one operation, so the release is the
    name.

    The name is also what makes a rerun recognisable: `open_adoption_pr` matches the
    branch stem below, so the second run of a release finds the first run's PR instead
    of opening its own.
    """
    return f"{UPGRADE_SLUG} {tag}"


def upgrade_branch_stem(tag: str) -> str:
    """The prefix every branch this script cuts for `tag` starts with.

    `worktree.plan_new` appends `-<mmdd>` and, for a same-day rerun, `-<n>` -- so the
    stem is as much of the name as is fixed by the release. Built from `tb` rather
    than spelled out, because the two halves are the box tier's to decide: a rename
    there that this file restated would silently stop matching.

    Under `tb.AUTOMATION_PREFIX`, because nobody asked for this branch. It is the same
    vendoring commit in every consumer, cut nightly by a scheduled job, and it was
    crowding out the change a reviewer had actually asked to see -- twenty-eight of
    `preview-task.py`'s twenty-nine rows, on the day that menu was first printed. The
    namespace is what lets that menu drop them without guessing from a slug.
    """
    return f"{tb.AUTOMATION_PREFIX}{tb.slugify(upgrade_slug(tag))}-"


def upgrade_branch_stems(tag: str) -> tuple[str, ...]:
    """Every stem an open adoption PR for `tag` might be on: what this cuts, and what it
    cut before the automation namespace existed.

    The legacy spelling is not tidiness -- dropping it would reintroduce the exact
    duplicate this file already collected three PRs from. `open_adoption_pr` is the only
    thing standing between an in-flight adoption and a second one, and on the first run
    after this change every adoption in flight is on the old name. It costs one extra
    `startswith` per open PR and stops mattering once those merge; delete it when no
    consumer has an open PR under `tb.BRANCH_PREFIX` for an upgrade, which is a fact
    about the fleet rather than about this file.
    """
    return (
        upgrade_branch_stem(tag),
        f"{tb.BRANCH_PREFIX}{tb.slugify(upgrade_slug(tag))}-",
    )


def open_adoption_pr(project: Path, tag: str) -> str:
    """`#<n> <url>` for an open PR already adopting `tag` in `project`; "" when none.

    **The currency test alone is not enough to stop a duplicate.**
    `is_current_on_remote` reads `DEVKIT_VERSION` off `origin/<default>`, which only
    changes when an adoption *merges* -- so between opening a PR and merging it, every
    run judges the project out of date and cuts another box, another branch and another
    PR for the same release. carameli collected three for v0.10.2 (#170, #174, #175) in
    sixteen hours that way, because the first one's gate was red and it sat open: a
    scheduled run at 03:00, a manual rerun, and one more the following morning.

    Matching is by branch stem rather than by title, because the title is prose this
    script owns today and could reword tomorrow, while the branch name is the identity
    the box registry and `reconcile` already key on.

    **Fails open.** No `gh`, no auth, a repo with no remote: all answer "" and the run
    proceeds exactly as it did before this existed. A duplicate PR is a nuisance; a
    scheduled upgrade that stops running because the CLI is missing is a silent one.
    """
    listed = sweep.gh_for(project)(
        "pr", "list", "--state", "open", "--limit", "100", "--json", "number,headRefName,url"
    )
    if listed.returncode != 0:
        return ""
    try:
        rows = json.loads(listed.stdout or "[]")
    except json.JSONDecodeError:
        return ""
    stems = upgrade_branch_stems(tag)
    for row in rows if isinstance(rows, list) else []:
        if str(row.get("headRefName", "")).startswith(stems):
            return f"#{row.get('number')} {row.get('url', '')}".strip()
    return ""


def adoption_prefixes() -> tuple[str, str]:
    """Every stem an adoption branch may start with, for *any* release.

    `upgrade_branch_stems(tag)` with the tag cut off: what lets a reader tell an
    adoption PR from any other without knowing which release it adopts. Shared with
    `fix-prs.py`, which must not send an agent at one the next pass will close.
    """
    slug = tb.slugify(UPGRADE_SLUG)
    return (f"{tb.AUTOMATION_PREFIX}{slug}-", f"{tb.BRANCH_PREFIX}{slug}-")


def superseded_adoptions(rows: list[dict], tag: str) -> list[dict]:
    """The open adoption PRs for any release other than `tag`, in the order listed."""
    stems = upgrade_branch_stems(tag)
    return [
        row
        for row in rows
        if isinstance(row, dict)
        and str(row.get("headRefName", "")).startswith(adoption_prefixes())
        and not str(row.get("headRefName", "")).startswith(stems)
    ]


def close_superseded(project: Path, tag: str) -> list[str]:
    """Close every open adoption PR for an older release, and say which. Fails open.

    A red adoption for v0.11.20 sitting beside the v0.11.21 one is not a second chance
    at the older release: merging it would land a vendored copy the very next pass
    replaces, and an agent sent to fix it spends a session on a tree nobody wants. Only
    the newest release's adoption stands; the rest are closed with the successor named,
    so the trail reads from either PR. The box behind a closed one is `worktree.py
    reconcile`'s to reap, exactly as a merged one's is -- nothing is deleted here.

    Fails open the way `open_adoption_pr` does: no `gh`, no auth, a repo with no
    remote, all answer "nothing closed" and the upgrade goes on as before.
    """
    gh = sweep.gh_for(project)
    listed = gh(
        "pr", "list", "--state", "open", "--limit", "100", "--json", "number,headRefName,url"
    )
    if listed.returncode != 0:
        return []
    try:
        rows = json.loads(listed.stdout or "[]")
    except json.JSONDecodeError:
        return []
    closed = []
    for row in superseded_adoptions(rows if isinstance(rows, list) else [], tag):
        number = str(row.get("number", ""))
        note = (
            f"Superseded: devkit {tag} is the newest release, and its adoption replaces "
            f"this one. Closed by `upgrade-project.py`; nothing here needs fixing."
        )
        if gh("pr", "close", number, "--comment", note).returncode == 0:
            closed.append(f"#{number}")
    return closed


# --- green adoptions, the one thing the pass merges ---------------------------------------


GREEN = frozenset({"SUCCESS", "SKIPPED", "NEUTRAL"})


def _gate_green(row: dict) -> bool:
    rollup = row.get("statusCheckRollup") or []
    if not isinstance(rollup, list) or not rollup:
        return False
    verdicts = {str(node.get("conclusion") or node.get("state") or "").upper() for node in rollup}
    return verdicts <= GREEN and str(row.get("mergeable", "")).upper() != "CONFLICTING"


def green_adoptions(rows: Iterable[dict], prefixes: tuple[str, ...], label: str) -> list[dict]:
    """Open adoption PRs whose gate passed and that carry the label: mergeable unattended.

    An adoption is upstream churn whose green gate is the whole review, and letting
    those land is what keeps a release's fan-out from piling up in the queue. Nothing
    else is merged by the pass; every other green PR waits for a person.
    """
    return [
        row
        for row in rows
        if isinstance(row, dict)
        and not row.get("isDraft")
        and str(row.get("headRefName", "")).startswith(prefixes)
        and label
        in {
            str(entry.get("name", "")) if isinstance(entry, dict) else str(entry)
            for entry in row.get("labels", []) or []
        }
        and _gate_green(row)
    ]
