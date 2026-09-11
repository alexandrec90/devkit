#!/usr/bin/env python3
"""The Windows Terminal profile devkit's agent tabs are launched under.

**The defect this fixes.** A tab opened as `wt new-tab -d <worktree> pwsh.exe -Command
claude` carries no profile of its own. With no `-p`, Windows Terminal builds the tab from
whatever the *default* profile is and overrides exactly three fields -- the command line,
the starting directory and the title -- so an agent session is drawn as an ordinary shell
and is indistinguishable from one at a glance. Worse, every gesture that asks the terminal
for "another one of these" answers with the default profile in the default profile's own
directory: the `+` button always does (it never reads the focused tab, by design), and
Duplicate Tab replays the *profile's* command line, which is a bare shell, not the agent.
An agent tab is therefore a dead end -- you cannot get a second shell beside the session
without navigating back to the worktree by hand.

A named profile is what carries that identity. It gives the tab its own icon, scheme and
tab colour, so a paid session is visibly not a shell; it puts `Agent` in the `+` dropdown;
and it makes Duplicate Tab open a plain shell **in the same worktree**, which is the thing
that was actually wanted. What the profile deliberately does *not* do is run an agent: its
command line is a bare `pwsh`, and the agent arrives as the per-tab override the launchers
already pass, so duplicating a tab never spends a session.

**Inheriting the default profile is not only a cosmetic problem**, which is what the
workstation this was written on turned out to demonstrate: its default profile carries
`"elevate": true`. A tab built from that profile is an *elevated* session, and Windows
Terminal cannot host an elevated session as a tab in an unelevated window -- so every
agent spawn broke out into a separate elevated window with a UAC prompt, rather than
landing in the window `-w 0` names. The reported symptom was "the task opens my default
terminal instead of a tab", and the cause was that the tab had no profile of its own to
open under. `-p` is what stops an agent session inheriting whatever the operator happens
to have made their default -- elevation included.

**Why the launchers still ask before passing `-p`.** A `-p` naming a profile that is not
installed is not fatal -- Windows Terminal runs the overridden command line anyway (probed
on WT 1.24, the tab ran) -- but it is a warning at the one moment nobody is watching for
one, and the answer is a two-line lookup. A machine that never ran the installer, or an
operator who deleted the profile, gets exactly today's behaviour instead.

The definition is installed by `scripts/install-wt-profile.py`; this module owns what is
installed and how to recognise it, so the installer and the two launchers cannot disagree.
Every function here is pure except `read_settings`, and it is total: a missing, unreadable
or malformed settings file is `{}`, never an exception, because its callers are on the
path that opens an agent.

Tested in `tests/test_wt_profile.py`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devkit_jsonc

# What `-p` names. Windows Terminal matches `-p` by profile NAME, while the installer
# recognises its own work by GUID -- so renaming the profile in the Settings UI turns the
# `-p` off (the launchers fall back) rather than aiming it at a profile that moved.
PROFILE_NAME = "Agent"

# Stable, and generated rather than random so a re-install updates the profile it wrote
# last time instead of appending a second one:
#   uuid.uuid5(uuid.NAMESPACE_URL, "https://devkit.invalid/windows-terminal/agent")
PROFILE_GUID = "{13e769c4-94bb-57d3-95dc-b6df240af99c}"

# The fields devkit owns. Anything the operator adds to the profile by hand (a font, an
# opacity, a `startingDirectory`) is left alone by `merged` below -- this is the set that
# is restored when it drifts, not a declaration that the profile may hold nothing else.
#
# `commandline` is a bare shell on purpose: see the docstring. `-NoLogo` matches what the
# launchers pass so a duplicated tab does not suddenly grow a banner the agent tab lacked.
PROFILE: dict[str, Any] = {
    "guid": PROFILE_GUID,
    "name": PROFILE_NAME,
    "commandline": "pwsh.exe -NoLogo",
    "icon": "\U0001f916",  # robot; WT renders an emoji icon without a file on disk
    "colorScheme": "One Half Dark",
    "tabColor": "#8A63D2",
}

# Every place Windows Terminal keeps that file, most specific first. The Store build is
# what `winget`/the Store installs and is what this workstation runs; the third is an
# unpackaged (portable or scoop) install, which keeps its settings outside `Packages`.
SETTINGS_RELATIVE = (
    Path("Packages/Microsoft.WindowsTerminal_8wekyb3d8bbwe/LocalState/settings.json"),
    Path("Packages/Microsoft.WindowsTerminalPreview_8wekyb3d8bbwe/LocalState/settings.json"),
    Path("Microsoft/Windows Terminal/settings.json"),
)


def settings_candidates(local_app_data: str | os.PathLike[str] | None = None) -> list[Path]:
    """Every path Windows Terminal might keep `settings.json` at, in preference order."""
    root = Path(local_app_data or os.environ.get("LOCALAPPDATA", ""))
    return [root / relative for relative in SETTINGS_RELATIVE]


def settings_path(local_app_data: str | os.PathLike[str] | None = None) -> Path | None:
    """The settings file this machine actually has, or None when Windows Terminal is not
    installed -- which is every non-Windows machine and is not an error anywhere."""
    for candidate in settings_candidates(local_app_data):
        if candidate.is_file():
            return candidate
    return None


def read_settings(path: Path | None) -> dict[str, Any]:
    """Parse `settings.json`, answering `{}` for every way that can fail.

    JSONC rather than JSON because the file is documented as accepting comments and a
    hand-edited one usually has them; `devkit_jsonc` is the parser the rest of devkit
    reads workspace files with.
    """
    if path is None:
        return {}
    try:
        parsed = devkit_jsonc.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def profiles_list(settings: dict[str, Any]) -> list[dict[str, Any]]:
    """The `profiles.list` array, tolerating both shapes the schema allows.

    `profiles` may be the object holding `defaults` and `list`, or -- in an older
    hand-written file -- the list itself.
    """
    profiles = settings.get("profiles")
    entries: list[Any] = []
    if isinstance(profiles, list):
        entries = profiles
    elif isinstance(profiles, dict):
        nested = profiles.get("list")
        entries = nested if isinstance(nested, list) else []
    return [entry for entry in entries if isinstance(entry, dict)]


def installed(settings: dict[str, Any]) -> dict[str, Any] | None:
    """devkit's profile as this machine currently holds it, found by GUID."""
    for entry in profiles_list(settings):
        if entry.get("guid") == PROFILE_GUID:
            return entry
    return None


def is_current(settings: dict[str, Any]) -> bool:
    """True when every field devkit owns is present with the value it owns.

    A superset passes: the operator's own additions to the profile are not drift.
    """
    entry = installed(settings)
    return entry is not None and all(entry.get(key) == value for key, value in PROFILE.items())


def merged(settings: dict[str, Any]) -> dict[str, Any]:
    """`settings` with devkit's profile added or repaired -- a new object, input untouched.

    Repair is a field-wise update rather than a replacement, so an operator who set a font
    or an opacity on the profile keeps it while the fields devkit owns are put back.
    """
    updated = dict(settings)
    profiles = updated.get("profiles")
    entries = [dict(entry) for entry in profiles_list(settings)]
    for entry in entries:
        if entry.get("guid") == PROFILE_GUID:
            entry.update(PROFILE)
            break
    else:
        entries.append(dict(PROFILE))
    if isinstance(profiles, dict):
        updated["profiles"] = {**profiles, "list": entries}
    else:
        # A bare list, or no `profiles` key at all: write the shape the schema documents.
        updated["profiles"] = {"list": entries}
    return updated


def profile_name(settings: dict[str, Any]) -> str:
    """`"Agent"` when a profile of that NAME is registered, `""` otherwise.

    By name because that is what Windows Terminal resolves `-p` against, and because a
    profile the operator renamed is one they no longer want the launchers aiming at.
    The `-p` spelling itself belongs to the two argv builders, which already write
    `--title` and `-d` themselves; this answers only whether there is one to pass.
    """
    return next(
        (PROFILE_NAME for entry in profiles_list(settings) if entry.get("name") == PROFILE_NAME),
        "",
    )


def launch_name(local_app_data: str | os.PathLike[str] | None = None) -> str:
    """The profile a tab about to be opened should use, or `""`. What a launcher calls."""
    return profile_name(read_settings(settings_path(local_app_data)))
