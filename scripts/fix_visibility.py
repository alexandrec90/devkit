#!/usr/bin/env python3
"""A registered project whose repo is private and runs Actions is a person's step.

On GitHub's free plan only a public repo's Actions minutes are free and unlimited. A
private one spends a monthly allowance shared by every private repo on the account, and
once it is gone GitHub refuses each of their jobs (`fix_plan.NO_BILLING`):
productivity-tracker and social-scraper, both created `--private` by `new-project.py`,
did that on 2026-11-01. Repos are created public now (`devkit_project.CREATE_VISIBILITY`);
this catches one made private some other way, or turned private later, before its
minutes run out rather than after.

Making a repo public publishes its whole history, so the step is its owner's, not a
fixer's: the finding names it after `harness_triage.PERSON_STEP` and awaits them. A later
pass that reads the repo public or archived, or finds its workflows gone, retires it.

Tested in `tests/test_fix_visibility.py`.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fix_findings
import fix_loop
import sweep

KIND = "private-repo-runs-ci"
STEP = (
    "make it public (check its history holds no secret first; `gh repo edit "
    "--visibility public --accept-visibility-change-consequences`) or archive it"
)
WORKFLOWS = Path(".github") / "workflows"
# Checkouts asked at once: each is one `gh repo view`, and the pass has a deadline.
WORKERS = 8


def runs_ci(checkout: Path) -> bool:
    """Whether the checkout carries a GitHub Actions workflow."""
    folder = checkout / WORKFLOWS
    return folder.is_dir() and any(p.suffix in (".yml", ".yaml") for p in folder.iterdir())


def is_private(gh: Callable[..., object]) -> bool | None:
    """Whether the repo `gh` is bound to is private (or internal) and not archived; None
    when GitHub could not say, which retires nothing and files nothing."""
    done = gh("repo", "view", "--json", "visibility,isArchived")
    if getattr(done, "returncode", 1) != 0:
        return None
    try:
        data = json.loads(getattr(done, "stdout", "") or "")
    except ValueError:
        return None
    if not isinstance(data, dict) or "visibility" not in data:
        return None
    return data["visibility"] != "PUBLIC" and not data.get("isArchived")


def detail(project: str) -> str:
    """The finding's line, stable across passes: no count, no date."""
    return (
        f"{project} is private and runs GitHub Actions, whose minutes are free only for a "
        f"public repo -- {fix_findings.triage.PERSON_STEP} {STEP}"
    )


def check(
    ctx: fix_loop.Context,
    journal: fix_findings.Journal,
    gh_for: Callable[[Path], Callable[..., object]] = sweep.gh_for,
) -> list[str]:
    """File `KIND` for each checkout that runs CI from a private repo, retire the open
    ones no longer so: lines for the record."""
    answers = ask(ctx, gh_for)
    private = sorted(name for name, answer in answers.items() if answer)
    journal.add(*(fix_findings.Finding(KIND, name, detail(name)) for name in private))
    lines = [f"{name} -- private and runs Actions: filed for its owner" for name in private]
    keep = set(private) | {name for name, answer in answers.items() if answer is None}
    return lines + retire(ctx, keep)


def ask(
    ctx: fix_loop.Context, gh_for: Callable[[Path], Callable[..., object]]
) -> dict[str, bool | None]:
    """`is_private` for each checkout that runs CI, asked at once."""
    checkouts = [name for name in ctx.projects if runs_ci(ctx.root / name)]
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        found = pool.map(lambda name: is_private(gh_for(ctx.root / name)), checkouts)
        return dict(zip(checkouts, found, strict=True))


def retire(ctx: fix_loop.Context, keep: set[str]) -> list[str]:
    """Resolve each open `KIND` finding about a project not in `keep`."""
    marker = f"{KIND}: "
    stale = [
        item
        for item in fix_findings.triage.open_items(
            fix_findings.triage.load(ctx.devkit_dir), (fix_findings.FINDING,)
        )
        if item.fields.get("detail", "").startswith(marker) and item.project not in keep
    ]
    note = "the repo is public or archived, or runs no workflow, as of this pass"
    if stale and ctx.writes:
        fix_findings.triage.resolve([item.id for item in stale], note, root=ctx.devkit_dir)
    verb = "retired" if ctx.writes else "would retire"
    return [f"{verb} [{item.id}] -- {note}" for item in stale]
