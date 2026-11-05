#!/usr/bin/env python3
"""Bring a consumer's untouched copy of a devkit template up to the current template.

`templates/` is a one-shot copy (`scripts/CLAUDE.md`): `sync-devkit.py --pull` never
looks at a template again, so a fix made to one after a project was generated stays in
devkit. #391 made `run-tests.py.tmpl` run the tests for what changed by default, a
sweep retired six full-suite groups on the strength of it, and none of the six
consumers had it (114da279): three of them held the template byte for byte as it stood
at `e9eb759`, which runs the whole suite bare.

A copy that is byte-identical to some earlier version of its template is one the
project never made its own, so devkit's current version is what the project would
have been generated with today. Only such a copy is replaced; a project that edited
its runner (carameli's, which runs in a container, or ibkr_trader's, which delegates to
its own artifact runner) is left alone and named, because what it chose instead is its
own. `upgrade-project.py` runs `refresh` in every adoption box, after the pull and
before the commit, so the refresh reaches the project through its own gate.

A rendered copy differs per project and nothing records the values it was rendered with,
so a template with a `{{ }}` is listed only when every substitution sits on a line
`RENDERED_LINES` names: that line is left out of the comparison, and the refreshed copy
keeps the project's own. `lint-all.py.tmpl` gained that when its secrets pass reached five
projects no other way (05000136); those whose copy differs anywhere else keep their own.
`tests/test_template_refresh.py` holds `REFRESHED` to that.

**A refresh that rewrote a file re-reconciles the untested-symbol ratchet.** The pull's
own reconcile ran before the refresh, so the functions the newer template added --
devkit's code, tested in devkit -- read to the project's vendored gate as debt the
project wrote: roguelike's v0.11.34 adoption went red naming `run-tests.py`'s
`changed_paths`, `default_branch`, `tests_for` and `with_basetemp`. The reconcile is
`sync-devkit.reconcile_untested_baseline`, with the refreshed paths counted among the
files devkit wrote, so they are not held back as the project's own uncommitted edits.


`python scripts/template_refresh.py <project>` does the same by hand.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import sweep

REPO_ROOT = Path(__file__).resolve().parents[1]

# Project path -> the template it was rendered from, relative to the devkit checkout.
REFRESHED: dict[str, str] = {
    "scripts/run-tests.py": "templates/core/scripts/run-tests.py.tmpl",
    "scripts/lint-all.py": "templates/core/scripts/lint-all.py.tmpl",
    ".vscode/tasks.json": "templates/core/dot-vscode/tasks.json.tmpl",
}
# The line of a project's copy that holds what it was rendered with, by target.
RENDERED_LINES: dict[str, re.Pattern[str]] = {
    "scripts/lint-all.py": re.compile(r"^MYPY_SCOPE = .*$", re.M),
}

Runner = Callable[..., "subprocess.CompletedProcess[str]"]
# `(project, paths devkit wrote)` -> `(dropped, recorded)`, or None when not run.
Reconciler = Callable[[Path, tuple[str, ...]], "tuple[int, int] | None"]

REFRESHED_LINE = "an untouched earlier template, refreshed to devkit's current one"


def _lf(text: str) -> str:
    return text.replace("\r\n", "\n")


def without_rendered(target: str, text: str) -> str:
    """`text` less the line `target` was rendered on: what two copies are compared by."""
    line = RENDERED_LINES.get(target)
    return line.sub("", text) if line else text


def rendered_as(target: str, template: str, held: str) -> str:
    """`template` carrying the line `held` was rendered with, where `target` has one."""
    line = RENDERED_LINES.get(target)
    found = line.search(held) if line else None
    return line.sub(lambda _: found.group(), template) if line and found else template


def past_versions(devkit: Path, template: str, runner: Runner = sweep.run_windowless) -> set[str]:
    """Every committed text of `template` in `devkit`'s history, with LF line endings.

    Empty where git cannot answer: no repository, or a template with no history.
    """

    def git(*args: str) -> str:
        try:
            done = runner(
                ["git", *args],
                cwd=devkit,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except OSError:
            return ""
        return done.stdout if done.returncode == 0 else ""

    shas = git("log", "--format=%H", "--", template).split()
    return {_lf(text) for text in (git("show", f"{sha}:{template}") for sha in shas) if text}


def refresh_one(
    project: Path, target: str, devkit: Path, runner: Runner = sweep.run_windowless
) -> str:
    """Replace `project`'s `target` with its current template when it is an untouched
    earlier one. Returns what was found, as one line."""
    path = project / target
    template = REFRESHED[target]
    try:
        held = path.read_bytes()
        current = _lf((devkit / template).read_text(encoding="utf-8"))
    except OSError:
        return f"{target}: not there, or no {template} to compare it with"
    text = _lf(held.decode("utf-8", errors="replace"))
    compared = without_rendered(target, text)
    if compared == without_rendered(target, current):
        return f"{target}: already devkit's current template"
    past = {
        without_rendered(target, version) for version in past_versions(devkit, template, runner)
    }
    body = rendered_as(target, current, text)
    # A copy with no rendered line to carry over would be written with its `{{ }}` bare.
    if compared not in past or "{{" in body:
        return f"{target}: the project's own, left as it is"
    body = body.replace("\n", "\r\n") if b"\r\n" in held else body
    path.write_bytes(body.encode("utf-8"))
    return f"{target}: {REFRESHED_LINE}"


def reconcile_untested(project: Path, rewritten: tuple[str, ...]) -> tuple[int, int] | None:
    """devkit's `sync-devkit.reconcile_untested_baseline` on `project`, with `rewritten`
    counted among the paths devkit wrote alongside the MANIFEST."""
    loader_dir = Path(__file__).resolve().parent / "precommit"
    if str(loader_dir) not in sys.path:
        sys.path.insert(0, str(loader_dir))
    # Resolved by the insert above; `scripts/precommit/` is not an importable package.
    from _loader import load_by_path

    sync = load_by_path("_sync_devkit", loader_dir.parent / "sync-devkit.py")
    return sync.reconcile_untested_baseline(project, (*sync.MANIFEST, *rewritten))


def ratchet_line(counts: tuple[int, int] | None) -> str:
    """What the post-refresh reconcile did, as one line."""
    if counts is None:
        return "untested-symbol ratchet: not reconciled -- no baseline, or no scanner to say"
    dropped, recorded = counts
    return (
        f"untested-symbol ratchet: dropped {dropped} line(s) now covered, "
        f"recorded {recorded} gap(s) the refreshed template added"
    )


def refresh(
    project: Path,
    devkit: Path = REPO_ROOT,
    runner: Runner = sweep.run_windowless,
    reconcile: Reconciler = reconcile_untested,
) -> list[str]:
    """`refresh_one` for every entry of `REFRESHED`, then the ratchet reconciled when any
    file was rewritten."""
    lines = [refresh_one(project, target, devkit, runner) for target in REFRESHED]
    done = zip(REFRESHED, lines, strict=True)
    rewritten = tuple(target for target, line in done if line.endswith(REFRESHED_LINE))
    if rewritten:
        lines.append(ratchet_line(reconcile(project, rewritten)))
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("project", type=Path, help="the consumer checkout to refresh")
    parser.add_argument("--devkit", type=Path, default=REPO_ROOT, help="devkit checkout")
    args = parser.parse_args(argv)
    for line in refresh(args.project, args.devkit):
        print(f"template-refresh: {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
