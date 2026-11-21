#!/usr/bin/env python3
"""Put the agent-shell environment in the user tier, so a session started anywhere has it.

`project_settings.AGENT_ENV` reaches a session through its project's
`.claude/settings.json`, which the settings pass fills in on every `--pull`. That covers a
session Claude Code started *in* a project, and nothing else. A session started at the
workspace root -- the folder holding every checkout, which is no project and which
nothing in devkit writes -- loads only the user tier, and then `cd`s into a checkout
where its Git Bash still rewrites `git show origin/main:.claude/x` into
`origin\\main;.claude\\x`, the "ambiguous argument" `MSYS2_ARG_CONV_EXCL` exists to stop
(5769209d). The user tier is the one settings file every session loads, wherever it
started, so that is where this puts the same keys.

**Missing keys only, never a value.** A key already in the user `env` is the operator's
decision and is kept whatever it says -- the same rule the settings pass keeps for a
project's file, and the same function (`project_settings.with_agent_env`). `--uninstall`
is the mirror: it removes only a key still holding devkit's own value.

**Exit codes**, matching every other installer: 0 current, 1 needs installing, 2 left
alone -- no `~/.claude` here (no Claude Code to configure), or a settings file this will
not rewrite because it cannot read it as a JSON object. Refusing is the point of 2:
Claude Code keeps permissions and the hooks switch in the same file, and a rewrite from a
failed parse would drop them all.

No `TASK_NAME`/`GROUP`: this registers no scheduled job. `installers.py` runs it on every
maintenance pass, which is what puts a key `AGENT_ENV` gains on this machine without a
person -- see `tests/test_installer_contract.py`, which holds the count of installers that
schedule nothing.

Stdlib only, every decision is an importable function, tested in
`tests/test_install_agent_env.py`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import project_settings

USER_SETTINGS = Path.home() / ".claude" / "settings.json"
AGENT_ENV = project_settings.AGENT_ENV


def read_settings(path: Path) -> dict | None:
    """The user settings as a dict, `{}` when the file does not exist yet, None when it
    cannot be read as a JSON object -- a shape this refuses to rewrite."""
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("env", {}), dict):
        return None
    return payload


def missing_keys(settings: dict) -> list[str]:
    """The `AGENT_ENV` keys the user `env` does not set, in `AGENT_ENV` order."""
    return project_settings.with_agent_env(settings)[1]


def without_agent_env(settings: dict) -> tuple[dict, list[str]]:
    """`(settings, keys removed)`: every `AGENT_ENV` key still holding devkit's value
    taken out, and an `env` left empty dropped. A key the operator changed is theirs."""
    env = settings.get("env", {})
    ours = [key for key, value in AGENT_ENV.items() if env.get(key) == value]
    if not ours:
        return settings, []
    kept = {key: value for key, value in env.items() if key not in ours}
    rest = {key: value for key, value in settings.items() if key != "env"}
    return ({**rest, "env": kept} if kept else rest), ours


def write_settings(path: Path, settings: object) -> None:
    """Two-space JSON with a trailing newline: the shape Claude Code writes the file in,
    and the one `harness-switch.py` writes it in when it sets `DEVKIT_HOOKS_OFF`."""
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8", newline="\n")


def run_check(path: Path) -> int:
    """`--check`: 0 current, 1 a key missing, 2 no Claude Code here or an unreadable file."""
    if not path.parent.is_dir():
        print(f"install-agent-env: no {path.parent} -- no Claude Code to configure here")
        return 2
    settings = read_settings(path)
    if settings is None:
        print(f"install-agent-env: {path} is not a JSON object -- left alone", file=sys.stderr)
        return 2
    missing = missing_keys(settings)
    if not missing:
        print(f"install-agent-env: up to date ({len(AGENT_ENV)} keys in {path})")
        return 0
    print(
        f"install-agent-env: {', '.join(missing)} missing from {path}. A session started "
        "outside a project runs without them. Re-run: python scripts/install-agent-env.py --yes",
        file=sys.stderr,
    )
    return 1


def build_parser() -> argparse.ArgumentParser:
    """The CLI, shaped like every installer's: the verbs in one exclusive group, and
    `--yes` / `--dry-run` in another so `--uninstall --yes` can be spelled."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--settings",
        type=Path,
        default=USER_SETTINGS,
        help=f"the user settings file (default: {USER_SETTINGS})",
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
        help="apply: add the missing keys, or confirm an --uninstall",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="report whether every agent-env key is set; exit 1 when one is missing",
    )
    mode.add_argument(
        "--uninstall",
        action="store_true",
        help="remove the keys still holding devkit's value (dry run unless --yes)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    path: Path = args.settings
    if args.check:
        return run_check(path)
    settings = read_settings(path) if path.parent.is_dir() else None
    if settings is None:
        print(f"install-agent-env: nothing to edit at {path} -- left alone", file=sys.stderr)
        return 2
    updated: object
    if args.uninstall:
        updated, keys = without_agent_env(settings)
        verb, done = "remove", "removed"
    else:
        updated, keys = project_settings.with_agent_env(settings)
        verb, done = "add", "added"
    if not keys:
        print(f"install-agent-env: nothing to {verb} in {path}")
        return 0
    print(f"Would {verb} in {path}: {', '.join(keys)}")
    if args.dry_run:
        print("\nDry run -- nothing changed. Re-run with --yes.")
        return 0
    try:
        write_settings(path, updated)
    except OSError as error:
        print(f"\ninstall-agent-env: REFUSED -- {error}", file=sys.stderr)
        return 2
    print(f"\ninstall-agent-env: {done} {len(keys)} key(s) in {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
