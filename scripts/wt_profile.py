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
agent spawn broke out into a separate window with a UAC prompt in front of it. The
reported symptom was "the task opens my default terminal instead of a tab", and the
cause was that the tab had no profile of its own to open under. `-p` is what stops an
agent session inheriting whatever the operator happens to have made their default --
elevation included.

**The separate window outlived that fix, and this is the half to read.** `-p` stops the
*tab* from elevating; it cannot make an unelevated tab join an *elevated window*,
because `-w` only ever sees windows at its own elevation. On a machine whose default
profile elevates, every window the operator opened by hand is an elevated one while VS
Code -- and so every task it runs, and every `wt.exe` those spawn -- is not. `-w 0` then
finds nothing it is allowed to join and opens a window of its own: the same reported
symptom a second time, now without the UAC prompt to explain it. **Nothing in this file
can fix that**, because the two sides have to match -- run the editor elevated, or drop
`"elevate"` from the default profile. What devkit does instead is see it coming and say
so: `launch_note` is the line a launcher prints when it is about to open a tab it knows
will land in a window of its own, so a stray window reads as an elevation mismatch
rather than as a task that ignored `-w 0`.

**Why the launchers still ask before passing `-p`.** A `-p` naming a profile that is not
installed is not fatal -- Windows Terminal runs the overridden command line anyway (probed
on WT 1.24, the tab ran) -- but it is a warning at the one moment nobody is watching for
one, and the answer is a two-line lookup. A machine that never ran the installer, or an
operator who deleted the profile, gets exactly today's behaviour instead.

The definition is installed by `scripts/install-wt-profile.py`; this module owns what is
installed and how to recognise it, so the installer and the two launchers cannot disagree.
Every function here is pure except `read_settings`, `is_elevated` and the two `launch_*`
wrappers that call them, and all three are total: a missing, unreadable or malformed
settings file is `{}` and an elevation this cannot determine is "not elevated", never an
exception, because its callers are on the path that opens an agent.

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
# What an unelevated launcher prints when the windows on screen are elevated ones. Three
# lines: what is true, what will therefore happen, and the two ways to make it stop --
# because an operator reading this is watching a window they did not ask for appear, and
# a note that only named the cause would leave them with nothing to do about it.
ELEVATION_NOTE = (
    '\n  note: Windows Terminal opens elevated windows here ("elevate" on the default'
    "\n  profile) and this process is not elevated, so the tab gets a window of its own --"
    "\n  -w 0 cannot cross that split. Run VS Code elevated, or unelevate the profile."
)

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


def without(settings: dict[str, Any]) -> dict[str, Any]:
    """`settings` with devkit's profile dropped -- a new object, input untouched.

    The inverse of `merged`, and matched to it: the profile is found by GUID, so a
    same-named profile the operator wrote themselves is left where it is. Only devkit's
    own entry goes, and nothing else in the file is touched -- `profiles.defaults`, the
    key bindings and the colour schemes are all somebody else's.
    """
    updated = dict(settings)
    profiles = updated.get("profiles")
    entries = [
        dict(entry) for entry in profiles_list(settings) if entry.get("guid") != PROFILE_GUID
    ]
    if isinstance(profiles, dict):
        updated["profiles"] = {**profiles, "list": entries}
    else:
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


def default_elevates(settings: dict[str, Any]) -> bool:
    """True when a window the operator opens by hand on this machine is an ELEVATED one.

    The `+` button and every ordinary launch build their window from `defaultProfile`,
    so that profile's `elevate` is the elevation the windows already on screen have --
    which is the only thing that decides whether a tab can join one. Read through
    `profiles.defaults` too, because a field set there applies to every profile that
    does not override it, and an operator who elevates everything writes it once.

    `defaultProfile` is a GUID in every file Windows Terminal writes, but the schema
    also accepts a profile NAME and hand-edited files use one; an unresolvable value
    answers False rather than guessing, since a machine with no such profile is one
    where nothing about elevation can be claimed.
    """
    profiles = settings.get("profiles")
    inherited = profiles.get("defaults") if isinstance(profiles, dict) else None
    fallback = inherited.get("elevate") if isinstance(inherited, dict) else None
    wanted = settings.get("defaultProfile")
    for entry in profiles_list(settings) if wanted else ():
        if wanted in (entry.get("guid"), entry.get("name")):
            return bool(entry.get("elevate", fallback))
    return False


def is_elevated() -> bool:
    """Whether THIS process holds an elevated token. False everywhere but Windows.

    `sys.platform` rather than `os.name` for the reason `preview-ui-host.py:_kernel32`
    writes out at length: it is the only spelling of "not Windows" that narrows for
    mypy, which CI runs on Linux where `ctypes.windll` does not exist. The call itself
    cannot fail in any documented way, and is guarded anyway -- every caller is opening
    a paid session, and none of them should lose it to a note about window placement.
    """
    # Off Windows there is no elevation split for a tab to be refused by.
    if sys.platform != "win32":
        return False
    import ctypes

    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def window_note(settings: dict[str, Any], elevated: bool) -> str:
    """The note a launcher prints, or `""` when the tab will land where the operator is.

    A trailing string rather than a line of its own because both launchers already
    print one line before opening a tab, and this belongs to that line: the operator
    reads "opening claude in ..." and the reason it is about to appear somewhere else
    in the same breath. Pure, and takes the elevation rather than reading it, so the
    mismatch is a table in the tests instead of a machine state they would have to fake.
    """
    if elevated or not default_elevates(settings):
        return ""
    return ELEVATION_NOTE


def launch_note(local_app_data: str | os.PathLike[str] | None = None) -> str:
    """`window_note` for this machine and this process. The other thing a launcher calls."""
    return window_note(read_settings(settings_path(local_app_data)), is_elevated())
