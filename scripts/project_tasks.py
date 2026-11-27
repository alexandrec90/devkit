#!/usr/bin/env python3
"""Which project owns which Windows scheduled task, and which hand-made task no one does.

The fix pass judged two kinds of task: devkit's own `devkit-` jobs and the scheduled
collectors, whose task is named after their project. Any other task on the machine was
invisible to it, however it failed. `SeaweedFS data-lake pool` is data-lake's -- its
installer is `data-lake/scripts/storage-node.py` -- and was disabled for three weeks with
its last run stopped mid-flight, and nothing filed it, because nothing knew whose it was.

**A project claims its tasks in its `.devkit.toml`:**

    [schedule]
    tasks = ["SeaweedFS data-lake pool"]

`declared` reads that from every registered project, and `fix_loop.job_findings` judges
those tasks with devkit's own and files a failure against the claiming project, whose
fixer then works in that project's checkout. A project registered later is read the same
way on the next pass; nothing here lists projects.

**And a task nobody claims is a finding, not a blind spot.** `unowned` names every task in
the scheduler's root folder whose `Author` is blank -- what `Register-ScheduledTask` or the
Task Scheduler UI leaves on a task a person made -- that no project claims and that is not
devkit's. Vendors' tasks carry their author (`NVIDIA Corporation`, `Microsoft
Corporation`), devkit's carry `devkit`, and Windows' own live in subfolders, so none of
them is asked about. Its fixer gives the task an owner: claims it in the project whose
code it runs, or moves its script into one first.

    project_tasks.py            # every claimed task and its project, then the unowned

Tested in `tests/test_project_tasks.py`.
"""

from __future__ import annotations

import csv
import io
import subprocess
import sys
import tomllib
from collections.abc import Callable, Iterable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))
import harness_config
import schedule_health


def committed_manifest(checkout: Path) -> str | None:
    """The `.devkit.toml` on `checkout`'s `origin/HEAD`, as text; None when unreadable.

    The working tree is the person's checkout, and it lags its default branch whenever
    the after-merge sync leaves it alone: host-scripts' claim of "Dell Idle Blank" merged
    at 16:08 on 2026-11-26, the checkout stayed behind on three uncommitted files, and the
    16:11 pass refiled the task as unowned (002f2388). A claim counts once it is merged."""
    for ref in ("origin/HEAD", "origin/main", "origin/master"):
        try:
            done = subprocess.run(
                ["git", "show", f"{ref}:{harness_config.MANIFEST_NAME}"],
                cwd=checkout,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
                check=False,
                creationflags=schedule_health.NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if done.returncode == 0:
            return done.stdout
    return None


def _tasks_in(text: str) -> tuple[str, ...]:
    """The `[schedule] tasks` in manifest `text`; none when it does not parse."""
    try:
        return harness_config.from_dict(tomllib.loads(text)).schedule.tasks
    except tomllib.TOMLDecodeError:  # `from_dict` itself never raises
        return ()


def declared(
    root: Path,
    projects: Iterable[str],
    committed: Callable[[Path], str | None] = committed_manifest,
) -> dict[str, str]:
    """`{task: project}` for each task a registered project's `.devkit.toml` claims.

    A claim in either the working tree or the committed default branch counts, so a
    checkout behind its merged claim does not file the task as unowned.
    A task two projects claim stays with the first in `projects` -- the workspace's
    order -- so a duplicate never moves a finding between fixers from pass to pass."""
    owners: dict[str, str] = {}
    for project in projects:
        checkout = root / project
        if not checkout.is_dir():
            continue
        tasks: list[str] = []
        if (checkout / harness_config.MANIFEST_NAME).is_file():
            tasks.extend(harness_config.load(checkout).schedule.tasks)
        merged = committed(checkout)
        if merged is not None:
            tasks.extend(_tasks_in(merged))
        for task in tasks:
            owners.setdefault(task, project)
    return owners


def hand_made(stdout: str) -> list[str]:
    """The root-folder tasks in `schtasks /Query /FO CSV /V` output with no `Author`."""
    names: set[str] = set()
    for row in csv.DictReader(io.StringIO(stdout)):
        raw = row.get("TaskName") or ""
        name = raw.lstrip("\\")
        if raw == "TaskName" or not name or "\\" in name:
            continue  # a repeated header, or a task in a subfolder: Windows' own
        # `schtasks` prints a blank author as `N/A`, as it does every empty field.
        if (row.get("Author") or "").strip() in ("", schedule_health.NEVER):
            names.add(name)
    return sorted(names)


def unowned(stdout: str, claimed: Iterable[str]) -> list[str]:
    """The hand-made tasks in `stdout` that are not devkit's and that no project claims."""
    taken = frozenset(claimed)
    return [
        name
        for name in hand_made(stdout)
        if not name.startswith(schedule_health.PREFIX) and name not in taken
    ]


def unowned_on_machine(claimed: Iterable[str]) -> list[str]:
    """`unowned` asked of this machine's scheduler; [] when it cannot be asked."""
    stdout = schedule_health.scheduler_csv()
    return [] if stdout is None else unowned(stdout, claimed)


def main(argv: list[str] | None = None) -> int:
    del argv
    import devkit_project  # the registry reader; heavy, and only the CLI needs it
    import project_registry

    root = project_registry.projects_root(Path(__file__).resolve().parents[1])
    if not root.is_dir():
        print(f"no projects root at {root}", file=sys.stderr)
        return 2
    projects = devkit_project.known_projects()
    owners = declared(root, projects)
    for task, project in sorted(owners.items()):
        print(f"{project}: {task}")
    for task in unowned_on_machine(owners):
        print(f"UNOWNED: {task}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
