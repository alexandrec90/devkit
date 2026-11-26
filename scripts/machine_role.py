#!/usr/bin/env python3
"""Which kind of PC this is, for who takes the heavy jobs: `workstation` or `worker`.

    machine_role.py                    # what this machine is, and what that means
    machine_role.py --set worker       # the PC nobody uses: heavy work comes here first
    machine_role.py --set workstation  # the PC a person works at: heavy work only as fallback
    machine_role.py --set unset        # back to ordering by free memory alone

Every PC fires the same scheduled jobs at the same minute, and `job_claims` used to order
them by free memory alone (`job_claims.delay_seconds`). That is a race a busy desk wins
whenever it has memory free for a moment, so the PC a person works at kept picking up
fixers, test runs and queue sessions while an idle laptop watched. A role says it outright.

- **`worker`** claims a heavy job the moment it sees one, as every PC does today.
- **`workstation`** leaves a heavy job for `job_claims.WORKER_FIRST` from when it first
  saw it (`job_claims.Sightings`), and claims it only if it is still unclaimed then --
  but only while some worker's heartbeat is fresh (`job_claims.live_workers`): a worker
  says it is alive from its after-merge pass, so one that is off, asleep or offline
  costs nothing once its beat ages out. One alive but short of memory or disk never
  claims, so the job falls back to the workstation and is late rather than lost.
- **unset** (`""`, the default in devkit's `workspace.jsonc`) is today's behaviour.

What is heavy is the caller's to say, and listed in `scripts/CLAUDE.md`: the medium tier,
the local end-to-end tier, a fixer session, and a queue `run` or `agent` task. Everything
cheap -- the fix trigger, ship, an elevated request, the reconcile -- ignores the role.

**The setting is this machine's, in its live workspace file**, beside `devkit.fixPass`:
`"devkit.machineRole"` under `settings`. It is one of `devkit_project.MACHINE_SETTINGS`,
so a render carries the live value over and an adopt never records it. `--set` edits the
live file in place, every comment kept; that is a machine switch, not the hand edit of
the task block `.claude/rules/vscode-tasks.md` forbids.

Tested in `tests/test_machine_role.py`.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devkit_jsonc
import sweep

REPO_ROOT = Path(__file__).resolve().parents[1]

SETTING = "devkit.machineRole"
WORKSTATION = "workstation"
WORKER = "worker"
UNSET = ""
ROLES = (WORKSTATION, WORKER)
# What `--set` takes for "no role": an empty string is not a word a person can type.
UNSET_WORD = "unset"

# The member as written, so `with_role` can replace its value in place.
_MEMBER = re.compile(r'("devkit\.machineRole"\s*:\s*)"[^"\n]*"')
_SETTINGS = re.compile(r'"settings"\s*:\s*\{')

EXIT_OK = 0
EXIT_USAGE = 2

MEANING = {
    WORKER: "takes heavy jobs (medium tier, local e2e, fixer sessions, queue run/agent) at once",
    WORKSTATION: "leaves heavy jobs to a worker, and takes one only when none has claimed it",
    UNSET: "no role: PCs are ordered by free memory alone, as before roles existed",
}


def role_of(text: str) -> str:
    """The role a workspace file's text gives this machine; `UNSET` for anything absent,
    unparsable or misspelt, which is today's behaviour rather than a guess at either."""
    try:
        payload = devkit_jsonc.loads(text)
    except (json.JSONDecodeError, TypeError):
        return UNSET
    settings = payload.get("settings") if isinstance(payload, dict) else None
    value = settings.get(SETTING) if isinstance(settings, dict) else None
    return value if value in ROLES else UNSET


def role_here(workspace: Path | None = None) -> str:
    """This machine's role, off its live workspace file; `UNSET` when it cannot be read."""
    path = workspace or sweep.default_workspace(REPO_ROOT)
    try:
        return role_of(path.read_text(encoding="utf-8"))
    except OSError:
        return UNSET


def role_beside(devkit_root: Path) -> str:
    """The role the workspace file beside the devkit checkout at `devkit_root` gives, so
    a pass handed a root in a test reads that root's file and never the live one."""
    return role_here(sweep.default_workspace(devkit_root))


def with_role(text: str, role: str) -> str | None:
    """`text` with `SETTING` set to `role` (`UNSET` writes `""`), every comment kept: the
    member's value replaced where it stands, or the member added at the top of
    `settings`. None when the text has no `settings` object to put it in, or the edit
    would not read back as `role`."""
    if role not in (*ROLES, UNSET):
        return None
    value = json.dumps(role)
    # Searched with the comments blanked, which keeps every offset, so a comment that
    # quotes the setting is never the place edited.
    scan = devkit_jsonc.blank_comments(text)
    if member := _MEMBER.search(scan):
        edited = f"{text[: member.end(1)]}{value}{text[member.end() :]}"
    else:
        opened = _SETTINGS.search(scan)
        if opened is None:
            return None
        at = opened.end()
        edited = f'{text[:at]}\n\t\t"{SETTING}": {value},{text[at:]}'
    return edited if role_of(edited) == role else None


def describe(role: str, workspace: Path) -> str:
    """The line `machine_role.py` prints for `role`."""
    shown = role or UNSET_WORD
    return f"machine role: {shown} -- {MEANING[role]}\n  ({SETTING} in {workspace})"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--set",
        dest="role",
        choices=(*ROLES, UNSET_WORD),
        help="write this machine's role into its live workspace file",
    )
    parser.add_argument("--workspace", type=Path, default=None)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    workspace = args.workspace or sweep.default_workspace(REPO_ROOT)
    if args.role is not None:
        wanted = UNSET if args.role == UNSET_WORD else args.role
        try:
            text = workspace.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"machine_role: cannot read {workspace}: {exc}", file=sys.stderr)
            return EXIT_USAGE
        edited = with_role(text, wanted)
        if edited is None:
            print(
                f"machine_role: no `settings` object to write into in {workspace}", file=sys.stderr
            )
            return EXIT_USAGE
        if edited != text:
            workspace.write_text(edited, encoding="utf-8", newline="")
    print(describe(role_here(workspace), workspace))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
