#!/usr/bin/env python3
"""Register the `Agent` Windows Terminal profile every devkit agent tab is opened under.

`scripts/wt_profile.py` owns *what* the profile is and why one is needed at all; this
registers it on a machine and answers whether the registration is still current, which is
the question `installers.py` asks every installer here.

**Why an installer and not a line in the launcher.** The profile is machine state, not
repo state -- it lives in Windows Terminal's own `settings.json`, one file per user, read
by every terminal on the desktop rather than by this checkout. That is the same category
as the global Git policy next door, and it wants the same three properties: idempotent
(`--yes` twice registers one profile, because the GUID is derived rather than random),
self-healing (a field the operator overwrote is put back on the next maintenance pass),
and answerable without changing anything (`--check`).

**Exit codes**, matching every other installer: 0 current, 1 needs installing, 2 left
alone. Two is *not* a failure -- it is the answer on every machine with no Windows
Terminal, which is every CI runner and every non-Windows checkout, and reporting that as
drift would make the whole pass meaningless where it does not apply.

`settings.json` is rewritten as JSON, so hand-written comments in it do not survive a
`--yes`. The plan says so before it writes, and the previous file is copied beside itself
with a timestamp first. Windows Terminal reloads the file as it is written, so the profile
is live in already-open windows without restarting anything.

Stdlib only, every decision is an importable function, tested in
`tests/test_install_wt_profile.py`.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import shutil
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devkit_jsonc
import wt_profile

# No `TASK_NAME`/`GROUP`: this registers no scheduled job. `installers.py` runs it on
# every maintenance pass, which is what keeps the profile current without a job of its
# own -- see `tests/test_installer_contract.py`, which holds the count of installers that
# schedule nothing.

BACKUP_STAMP = "%Y%m%d-%H%M%S"


def backup_path(path: Path, now: _dt.datetime | None = None) -> Path:
    """Where the pre-write copy of `settings.json` goes.

    Beside the original rather than under `logs/`: the operator who wants it back is in
    Windows Terminal's settings folder, not in this checkout. The suffix keeps it out of
    the `.json` Windows Terminal itself would try to read.
    """
    stamp = (now or _dt.datetime.now()).strftime(BACKUP_STAMP)
    return path.with_name(f"{path.name}.devkit-{stamp}.bak")


def has_comments(text: str) -> bool:
    """True when the settings file holds JSONC comments a JSON rewrite would drop."""
    return devkit_jsonc.blank_comments(text) != text


def render_plan(path: Path, settings: dict[str, Any], comments: bool) -> str:
    """What `--yes` would do, in the terms the operator would have to undo it in."""
    entry = wt_profile.installed(settings)
    verb = "repair" if entry else "add"
    lines = [
        f"Windows Terminal profile install ({path}):",
        f"  {verb} profile {wt_profile.PROFILE_NAME} {wt_profile.PROFILE_GUID}",
    ]
    lines += [f"    {key} = {value!r}" for key, value in wt_profile.PROFILE.items()]
    lines.append(f"  copy the current file to {backup_path(path).name} first")
    if comments:
        lines.append(
            "  WARNING: this settings.json has comments in it; a rewrite drops them "
            "(the backup above keeps them)"
        )
    return "\n".join(lines)


def write_settings(path: Path, settings: dict[str, Any], now: _dt.datetime | None = None) -> Path:
    """Back up `settings.json` and write `settings` over it. Returns the backup path.

    Four-space JSON with a trailing newline, which is the shape Windows Terminal writes
    the file in itself, so a `--yes` on an untouched file is a one-hunk diff rather than a
    whole-file reformat.
    """
    backup = backup_path(path, now)
    shutil.copy2(path, backup)
    path.write_text(json.dumps(settings, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
    return backup


def run_check(path: Path | None) -> int:
    """`--check`: 0 current, 1 missing or drifted, 2 no Windows Terminal on this machine."""
    if path is None:
        print(
            "install-wt-profile: no Windows Terminal settings.json on this machine -- "
            "nothing to register",
            file=sys.stderr,
        )
        return 2
    settings = wt_profile.read_settings(path)
    if wt_profile.is_current(settings):
        print(f"install-wt-profile: up to date ({wt_profile.PROFILE_NAME} in {path})")
        return 0
    state = "has drifted from" if wt_profile.installed(settings) else "is missing from"
    print(
        f"install-wt-profile: profile {wt_profile.PROFILE_NAME} {state} {path}. "
        "Agent tabs open under the default profile until this is registered. "
        "Re-run: python scripts/install-wt-profile.py --yes",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    # The plan prints the profile field by field, and one of those fields is an emoji
    # icon; a Windows console is cp1252 and raised UnicodeEncodeError on it rather than
    # printing the plan. Same fix, and the same reason, as `resume-sessions.py`.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--settings",
        type=Path,
        default=None,
        help="Windows Terminal settings.json (default: this machine's, if it has one)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="print the plan without changing anything (default)",
    )
    mode.add_argument(
        "--yes",
        dest="dry_run",
        action="store_false",
        help="register the profile in Windows Terminal's settings.json",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help=(
            "report whether the profile is registered and current; exit 1 when it is "
            "missing or has drifted, 2 when Windows Terminal is not installed here"
        ),
    )
    args = parser.parse_args(argv)
    path = args.settings or wt_profile.settings_path()
    if args.check:
        return run_check(path)
    if path is None or not path.is_file():
        print(
            "install-wt-profile: no Windows Terminal settings.json to register in "
            f"({path or 'searched ' + str(len(wt_profile.SETTINGS_RELATIVE)) + ' locations'})",
            file=sys.stderr,
        )
        return 2

    try:
        text = path.read_text(encoding="utf-8")
        settings = wt_profile.read_settings(path)
        print(render_plan(path, settings, has_comments(text)))
        if args.dry_run:
            print("\nDry run -- nothing changed. Re-run with --yes to register.")
            return 0
        if wt_profile.is_current(settings):
            print(f"\ninstall-wt-profile: already current -- {path} not rewritten")
            return 0
        backup = write_settings(path, wt_profile.merged(settings))
    except OSError as error:
        print(f"\ninstall-wt-profile: REFUSED -- {error}", file=sys.stderr)
        return 2
    print(f"\ninstall-wt-profile: registered {wt_profile.PROFILE_NAME}; backup at {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
