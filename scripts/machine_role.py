#!/usr/bin/env python3
"""Which kind of PC this is, for who takes the heavy jobs: `workstation` or `worker`.

    machine_role.py                    # what this machine is, and what that means
    machine_role.py --set worker       # the PC nobody uses: heavy work comes here first
    machine_role.py --set workstation  # the PC a person works at: heavy work only as fallback
    machine_role.py --set unset        # back to ordering by free memory alone
    machine_role.py --migrate          # copy this PC's settings out of its workspace file

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
- **unset** (`""`, the default) is today's behaviour.

What is heavy is the caller's to say, and listed in `scripts/CLAUDE.md`: the medium tier,
the local end-to-end tier, a fixer session, and a queue `run` or `agent` task. Everything
cheap -- the fix trigger, ship, an elevated request, the reconcile -- ignores the role.

**The setting is this machine's, in `<projects-root>/.devkit-machine.json`**, beside
`devkit.fixPass` (`project_registry.MACHINE_KEYS`): `"devkit.machineRole"`, read through
`project_registry.setting` and nowhere else. `--set` writes the machine file;
`--migrate` copies all three machine settings the retired live workspace file still
holds and the machine file lacks (`project_registry.migrate_machine_settings`), which
the daily `devkit-workspace-status` pass also does on every PC -- so a PC that had not
migrated keeps its role from that pass on.

Tested in `tests/test_machine_role.py`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import project_registry
import projects_root

REPO_ROOT = Path(__file__).resolve().parents[1]

SETTING = "devkit.machineRole"
WORKSTATION = "workstation"
WORKER = "worker"
UNSET = ""
ROLES = (WORKSTATION, WORKER)
# What `--set` takes for "no role": an empty string is not a word a person can type.
UNSET_WORD = "unset"

EXIT_OK = 0
EXIT_USAGE = 2

MEANING = {
    WORKER: "takes heavy jobs (medium tier, local e2e, fixer sessions, queue run/agent) at once",
    WORKSTATION: "leaves heavy jobs to a worker, and takes one only when none has claimed it",
    UNSET: "no role: PCs are ordered by free memory alone, as before roles existed",
}


def role_value(value: object) -> str:
    """The role a stored value names; `UNSET` for anything absent or misspelt, which is
    today's behaviour rather than a guess at either."""
    return value if isinstance(value, str) and value in ROLES else UNSET


def role_here(root: Path | None = None) -> str:
    """This machine's role, off the machine file at the projects root `root` (default:
    the one this checkout resolves). `UNSET` when it cannot be read, a malformed machine
    file included."""
    where = projects_root.projects_root(REPO_ROOT) if root is None else root
    try:
        return role_value(project_registry.setting(SETTING, root=where))
    except (project_registry.RegistryError, OSError):
        return UNSET


def role_beside(devkit_root: Path) -> str:
    """The role the machine beside the devkit checkout at `devkit_root` has, so a pass
    handed a root in a test reads that root's files and never the live ones."""
    return role_here(projects_root.projects_root(devkit_root))


def migrate_line(workspace: Path) -> str:
    """Run the one-shot migration for the machine whose live file is `workspace`, as a
    daily pass's line: "" when there was nothing to copy, and never raises -- a pass
    that reports on the machine must not die on the machine file."""
    machine = workspace.parent / project_registry.MACHINE_FILE_NAME
    try:
        copied = project_registry.migrate_machine_settings(workspace=workspace)
    except (project_registry.RegistryError, OSError) as exc:
        return f"machine settings not migrated -- {exc}"
    return f"moved {', '.join(copied)} into {machine}" if copied else ""


def migrate_and_say(workspace: Path, prefix: str = "") -> None:
    """`migrate_line`, printed when it has something to say: one call for a daily pass."""
    if line := migrate_line(workspace):
        print(f"{prefix}{line}")


def describe(role: str, where: Path) -> str:
    """The line `machine_role.py` prints for `role`."""
    shown = role or UNSET_WORD
    return f"machine role: {shown} -- {MEANING[role]}\n  ({SETTING} in {where})"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--set",
        dest="role",
        choices=(*ROLES, UNSET_WORD),
        help=f"write this machine's role into {project_registry.MACHINE_FILE_NAME}",
    )
    parser.add_argument(
        "--migrate",
        action="store_true",
        help="copy the machine settings the live workspace file holds into the machine file",
    )
    parser.add_argument(
        "--root", type=Path, default=None, help="the projects root the machine file is in"
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="deprecated: names the root; also the live file `--migrate` reads",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    root = projects_root.from_flags(args.root, args.workspace, REPO_ROOT)
    machine = root / project_registry.MACHINE_FILE_NAME
    try:
        if args.migrate:
            copied = project_registry.migrate_machine_settings(
                root=root, workspace=args.workspace
            )
            print(f"machine_role: copied {', '.join(copied) or 'nothing'} into {machine}")
        if args.role is not None:
            wanted = UNSET if args.role == UNSET_WORD else args.role
            project_registry.set_machine_setting(SETTING, wanted, root=root)
    except (project_registry.RegistryError, OSError) as exc:
        print(f"machine_role: cannot write {machine}: {exc}", file=sys.stderr)
        return EXIT_USAGE
    print(describe(role_here(root), machine))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
