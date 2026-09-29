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

Only templates with no `{{ }}` substitution are listed: a rendered copy of one that has
them differs per project, and nothing records the values it was rendered with.
`tests/test_template_refresh.py` holds `REFRESHED` to that.

`python scripts/template_refresh.py <project>` does the same by hand.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import sweep

REPO_ROOT = Path(__file__).resolve().parents[1]

# Project path -> the template it was rendered from, relative to the devkit checkout.
REFRESHED: dict[str, str] = {
    "scripts/run-tests.py": "templates/core/scripts/run-tests.py.tmpl",
}

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def _lf(text: str) -> str:
    return text.replace("\r\n", "\n")


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
    if text == current:
        return f"{target}: already devkit's current template"
    if text not in past_versions(devkit, template, runner):
        return f"{target}: the project's own, left as it is"
    body = current.replace("\n", "\r\n") if b"\r\n" in held else current
    path.write_bytes(body.encode("utf-8"))
    return f"{target}: an untouched earlier template, refreshed to devkit's current one"


def refresh(
    project: Path, devkit: Path = REPO_ROOT, runner: Runner = sweep.run_windowless
) -> list[str]:
    """`refresh_one` for every entry of `REFRESHED`."""
    return [refresh_one(project, target, devkit, runner) for target in REFRESHED]


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
