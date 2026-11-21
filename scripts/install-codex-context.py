#!/usr/bin/env python3
"""Write the two machine-local lines Codex needs to read a devkit repo's instruction tier.

`codex_context.py` reports them -- `project_doc_fallback_filenames` in
`<CODEX_HOME>/config.toml`, and the rule bridge in `<CODEX_HOME>/AGENTS.md` -- and README
says both are in place. Reporting was all anything did: this machine's daily
`workspace-status.py` named both missing every day from 2026-10-04 (ledger bdd4dfd3,
4dd922ff), and a report nobody acts on is the same as none. This is the writer, so
`installers.py` puts them back on its next maintenance pass instead.

**Missing only, never a value.** A `project_doc_fallback_filenames` already set, to
anything, is the operator's and is kept; an `AGENTS.md` that already names
`.claude/rules` already carries a bridge in the operator's words. `--uninstall` removes
only what this wrote, found by its own exact text.

**Exit codes**, matching every other installer: 0 current, 1 needs installing, 2 left
alone -- no `CODEX_HOME` directory (no Codex to configure; `codex_context` is silent there
for the same reason).

No `TASK_NAME`/`GROUP`: this registers no scheduled job, like `install-agent-env.py`.
Stdlib only, every decision an importable function, tested in
`tests/test_install_codex_context.py`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import codex_context

FALLBACK_LINE = f'{codex_context.FALLBACK_SETTING} = ["CLAUDE.md"]'
BRIDGE = (
    "<!-- devkit: rule bridge, written by scripts/install-codex-context.py -->\n"
    "## Project rules\n"
    "\n"
    f"Codex does not discover `{codex_context.BRIDGE_MARKER}/`. In a repository that has "
    "one, read each rule file's frontmatter before editing: take every rule with no "
    "`paths:` in full, and each scoped rule whose `paths` match the files being edited. "
    "The repository's `CLAUDE.md` stays the index -- do not copy rule bodies here.\n"
    "<!-- devkit: end rule bridge -->\n"
)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def with_fallback(text: str) -> str | None:
    """`config.toml` with the fallback setting added, or None when it is already set.

    Above the first table header: a key written after `[tui]` would belong to `[tui]`."""
    if codex_context.FALLBACK_SETTING in text:
        return None
    lines = text.splitlines(keepends=True)
    at = next((i for i, line in enumerate(lines) if line.lstrip().startswith("[")), len(lines))
    head = "".join(lines[:at])
    if head and not head.endswith("\n"):
        head += "\n"
    return head + FALLBACK_LINE + "\n" + "".join(lines[at:])


def without_fallback(text: str) -> str | None:
    """`config.toml` with this installer's exact line taken out, or None when it is not there."""
    lines = text.splitlines(keepends=True)
    kept = [line for line in lines if line.rstrip("\r\n") != FALLBACK_LINE]
    return None if len(kept) == len(lines) else "".join(kept)


def with_bridge(text: str) -> str | None:
    """`AGENTS.md` with the bridge appended, or None when it already names the rules."""
    if codex_context.BRIDGE_MARKER in text:
        return None
    if not text:
        return BRIDGE
    return text.rstrip("\n") + "\n\n" + BRIDGE


def without_bridge(text: str) -> str | None:
    """`AGENTS.md` with this installer's exact bridge taken out, or None when it is not there."""
    if BRIDGE not in text:
        return None
    return text.replace("\n\n" + BRIDGE, "\n").replace(BRIDGE, "")


def plan(home: Path, uninstall: bool) -> list[tuple[Path, str]]:
    """`(file, new text)` for each file the verb would change."""
    edits = (
        (home / codex_context.CONFIG_NAME, without_fallback if uninstall else with_fallback),
        (home / codex_context.AGENTS_NAME, without_bridge if uninstall else with_bridge),
    )
    changes = []
    for path, edit in edits:
        updated = edit(_read(path))
        if updated is not None:
            changes.append((path, updated))
    return changes


def run_check(home: Path) -> int:
    """`--check`: 0 current, 1 a line missing, 2 no Codex here."""
    if not home.is_dir():
        print(f"install-codex-context: no {home} -- no Codex to configure here")
        return 2
    missing = [path.name for path, _text in plan(home, uninstall=False)]
    if not missing:
        print(f"install-codex-context: up to date ({home})")
        return 0
    print(
        f"install-codex-context: {', '.join(missing)} in {home} lack what Codex needs to "
        "read a repo's CLAUDE.md and .claude/rules/. "
        "Re-run: python scripts/install-codex-context.py --yes",
        file=sys.stderr,
    )
    return 1


def build_parser() -> argparse.ArgumentParser:
    """The CLI, shaped like every installer's: the verbs in one exclusive group, and
    `--yes` / `--dry-run` in another so `--uninstall --yes` can be spelled."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--home",
        type=Path,
        default=None,
        help="the Codex home (default: CODEX_HOME, else ~/.codex)",
    )
    mode = parser.add_mutually_exclusive_group()
    apply_mode = parser.add_mutually_exclusive_group()
    apply_mode.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="print the plan without changing anything (default)",
    )
    apply_mode.add_argument(
        "--yes",
        dest="dry_run",
        action="store_false",
        help="apply: add what is missing, or confirm an --uninstall",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="report whether both lines are in place; exit 1 when one is missing",
    )
    mode.add_argument(
        "--uninstall",
        action="store_true",
        help="remove what this installer wrote (dry run unless --yes)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    home: Path = args.home or codex_context.codex_home()
    if args.check:
        return run_check(home)
    if not home.is_dir():
        print(f"install-codex-context: no {home} -- left alone", file=sys.stderr)
        return 2
    changes = plan(home, args.uninstall)
    verb = "remove from" if args.uninstall else "add to"
    if not changes:
        print(f"install-codex-context: nothing to {verb} {home}")
        return 0
    print(f"Would {verb}: {', '.join(str(path) for path, _text in changes)}")
    if args.dry_run:
        print("\nDry run -- nothing changed. Re-run with --yes.")
        return 0
    try:
        for path, text in changes:
            path.write_text(text, encoding="utf-8", newline="\n")
    except OSError as error:
        print(f"\ninstall-codex-context: REFUSED -- {error}", file=sys.stderr)
        return 2
    print(f"\ninstall-codex-context: wrote {len(changes)} file(s) in {home}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
