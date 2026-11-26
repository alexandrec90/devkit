"""The halves of retiring a project that the registry edit leaves, and their reverse.

`plug-projects.py --unplug NAME` and `--plug NAME` drive this; it has no command line of
its own. Unplugging is the registry and nothing else, so on its own a retired project's
collector kept running, its claimed scheduled tasks kept firing (and, unclaimed once the
project left the registry, were filed as `scheduled-job-unowned`), its compose stack
kept its containers, and Dependabot kept opening PRs on its repo. After the registry
step, this runs the rest, in an order that fails safe:

| step | retire | restore |
| --- | --- | --- |
| collector | `collectors.py stop-here` | back to what it was: `run-here`, or `release` |
| claimed tasks | exported to XML, then deleted | registered again from that XML |
| compose stack | `docker compose down` -- **never `-v`** | `up -d` of what was running |
| GitHub repo | `gh repo archive` | unarchived by `plug-projects.py`, before it clones |

**The repo is archived last**, so a run that failed earlier leaves it writable for the
fix. Archiving is what stops Dependabot and every other automation on the repo: an
archived repo is read-only, and `fix_dependabot` already skips an unregistered project,
so nothing is left to open a PR or send a fixer at one. It is fully reversible.
**Volumes are never removed**: a static checkout's volumes hold a dev database that costs
hours to rebuild, the reason `docker-maint.py` may not pass `-v` either. Images stay too,
so a restore rebuilds nothing.

**What retire stopped is recorded on this machine** (`RECORD`, under devkit's ignored
`logs/`), because the collector assignment, the tasks and the running containers are all
per machine. That record is what lets a restore put back exactly what was there -- a
collector that was hands-off goes back to hands-off rather than starting -- and a second
retire keeps the first record's answers, which the first retire has since changed. The
registry and the repo are global, so on a second PC that ran the project, `--unplug` it
again there: the registry and archive steps are skipped as already done.

Pure planning (`retire_plan`, `restore_plan`, `merged_record`) over gathered `Facts`;
`execute` runs the steps through an injected runner. Tested in
`tests/test_project_retire.py`.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))
import collector_build
import collectors_config as config
import devkit_project
import devkit_schtasks
import harness_config
import sweep

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"

# Machine-local by construction, beside the collector assignment it records: `logs/` of
# devkit's static checkout (`collectors_config.home`), which git ignores.
RECORD = Path("logs/retired-projects.json")
TASKS_DIR = Path("logs/retired-projects")

WINDOWS = os.name == "nt"

COMPOSE_DOWN_TIMEOUT = 300.0
COMPOSE_UP_TIMEOUT = 1800.0
QUICK_TIMEOUT = 60.0

# `sweep.run_bounded`'s shape: `run(argv, timeout, cwd=...)`.
Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class Facts:
    """Everything a plan reads, gathered before anything acts."""

    name: str
    checkout: Path
    on_disk: bool
    collector: config.Collector | None = None
    # This machine's collector assignment: `run`, `stop`, or None for hands-off.
    assigned: str | None = None
    has_compose: bool = False
    # Running compose services; None when docker could not say.
    running: tuple[str, ...] | None = ()
    # Claimed scheduled tasks present on this machine.
    tasks: tuple[str, ...] = ()
    record: dict | None = None
    # `owner/name` on GitHub, and whether it is archived; None when gh could not say.
    repo: str = ""
    archived: bool | None = False


@dataclass(frozen=True)
class Step:
    """One command, fully described before anything runs."""

    label: str
    argv: tuple[str, ...]
    cwd: Path | None = None
    timeout: float | None = None
    # Where the command's stdout is kept (a task's exported XML), UTF-16 as schtasks reads.
    save_to: Path | None = None


@dataclass
class Plan:
    steps: list[Step] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# --- the record ---------------------------------------------------------------------


def load_records(path: Path) -> dict[str, dict]:
    """`{project: record}`. Never raises; unreadable is "nothing retired here"."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in raw.items() if isinstance(v, dict)} if isinstance(raw, dict) else {}


def save_records(path: Path, records: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(sorted(records.items())), indent=2) + "\n", encoding="utf-8")


def merged_record(facts: Facts, now: str) -> dict:
    """What this retire records: the machine as it stands, unless a retire already did.

    A second retire sees the collector at `stop` and the stack down -- the first retire's
    doing -- so the first record's collector answer is kept, and its services and tasks
    are kept alongside anything new that has appeared since.
    """
    running = list(facts.running or ())
    fresh = {
        "collector": facts.assigned,
        "services": sorted(running),
        "tasks": sorted(facts.tasks),
        "retired_at": now,
    }
    old = facts.record
    if not old:
        return fresh
    return {
        "collector": old.get("collector"),
        "services": sorted(set(old.get("services", [])) | set(running)),
        "tasks": sorted(set(old.get("tasks", [])) | set(facts.tasks)),
        "retired_at": old.get("retired_at", now),
    }


def task_file(base: Path, project: str, task: str) -> Path:
    """Where a retired task's XML is kept: one file per task, named from the task."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", task).strip("-") or "task"
    return base / TASKS_DIR / project / f"{slug}.xml"


def is_retired(name: str, base: Path | None = None) -> bool:
    """Has this machine a record of retiring `name`?"""
    return name in load_records((base or config.home(REPO_ROOT)) / RECORD)


# --- planning -----------------------------------------------------------------------


def _py(script: str, *args: str) -> tuple[str, ...]:
    return (sys.executable, str(SCRIPTS / script), *args)


def refusal(name: str) -> str:
    """Why `name` cannot be retired or restored at all; "" when it can."""
    if name == sweep.source_checkout(REPO_ROOT).name:
        return "devkit is the checkout every other one is driven from -- it cannot be retired"
    if name in devkit_project.NOT_PROJECTS:
        return f"{name} is a reference checkout, not a project"
    return ""


def archive_argv(repo: str, *, undo: bool = False) -> tuple[str, ...]:
    """`gh repo archive` (or `unarchive`), with no prompt to answer."""
    return ("gh", "repo", "unarchive" if undo else "archive", repo, "--yes")


def retire_plan(facts: Facts, base: Path) -> Plan:
    """The steps a retire of `facts.name` takes after its registry edit, archive last."""
    plan = Plan()
    if facts.collector is not None and facts.assigned != config.STOP:
        plan.steps.append(
            Step("stop its collector here", _py("collectors.py", "stop-here", facts.name))
        )
    for task in facts.tasks:
        saved = task_file(base, facts.name, task)
        plan.steps.append(
            Step(
                f"export task {task}",
                tuple(devkit_schtasks.query_xml_argv(task)),
                timeout=QUICK_TIMEOUT,
                save_to=saved,
            )
        )
        plan.steps.append(
            Step(
                f"delete task {task}",
                tuple(devkit_schtasks.delete_argv(task)),
                timeout=QUICK_TIMEOUT,
            )
        )
    _plan_compose_down(facts, plan)
    _plan_archive(facts, plan)
    return plan


def _plan_compose_down(facts: Facts, plan: Plan) -> None:
    if not facts.has_compose:
        return
    if facts.running is None:
        plan.notes.append(
            "docker did not answer -- its containers were not checked; start Docker and "
            "unplug it again to take them down"
        )
        return
    plan.steps.append(
        Step(
            "remove its containers (volumes kept)",
            ("docker", "compose", "down"),
            cwd=facts.checkout,
            timeout=COMPOSE_DOWN_TIMEOUT,
        )
    )


def _plan_archive(facts: Facts, plan: Plan) -> None:
    if not facts.repo:
        plan.notes.append("no GitHub repo to archive")
    elif facts.archived is None:
        plan.notes.append(
            f"gh could not say whether {facts.repo} is archived -- check `gh auth status` "
            "and unplug it again to archive it"
        )
    elif facts.archived:
        plan.notes.append(f"{facts.repo} is already archived")
    else:
        plan.steps.append(
            Step(
                f"archive {facts.repo} on GitHub (stops Dependabot)",
                archive_argv(facts.repo),
                timeout=QUICK_TIMEOUT,
            )
        )


def restore_plan(facts: Facts, base: Path) -> Plan:
    """The steps a restore of `facts.name` takes once it is plugged (and cloned) again:
    what the record says this machine had running."""
    plan = Plan()
    record = facts.record
    if record is None:
        plan.notes.append(
            "this machine has no record of retiring it -- nothing local is started; "
            "`collectors.py run-here` and `docker compose up -d` do it by hand"
        )
        return plan
    for task in record.get("tasks", []):
        saved = task_file(base, facts.name, task)
        plan.steps.append(
            Step(
                f"register task {task}",
                tuple(devkit_schtasks.register_argv(task, saved)),
                timeout=QUICK_TIMEOUT,
            )
        )
    services = _restart_services(facts, record)
    if services:
        plan.steps.append(
            Step(
                "start what was running",
                ("docker", "compose", "up", "-d", *services),
                cwd=facts.checkout,
                timeout=COMPOSE_UP_TIMEOUT,
            )
        )
    _plan_collector_back(facts, record, plan)
    return plan


def _restart_services(facts: Facts, record: dict) -> list[str]:
    """The recorded services minus the collector's own, which `run-here` starts on its
    own terms (lease, redeploy, settle) rather than a bare `up`."""
    own = facts.collector.service if facts.collector is not None else ""
    return [s for s in record.get("services", []) if s and s != own]


def _plan_collector_back(facts: Facts, record: dict, plan: Plan) -> None:
    if facts.collector is None:
        return
    was = record.get("collector")
    if was == config.RUN:
        plan.steps.append(
            Step("start its collector here", _py("collectors.py", "run-here", facts.name))
        )
    elif was is None and facts.assigned == config.STOP:
        plan.steps.append(
            Step("hand its collector back (hands off)", _py("collectors.py", "release", facts.name))
        )


# --- gathering ----------------------------------------------------------------------


def running_services(checkout: Path, run: Runner) -> tuple[str, ...] | None:
    """The compose services running in `checkout`; None when docker could not say."""
    argv = ["docker", "compose", "ps", "--services", "--status", "running"]
    result = run(argv, QUICK_TIMEOUT, cwd=str(checkout))
    if result.returncode != 0:
        return None
    return tuple(sorted({line.strip() for line in result.stdout.splitlines() if line.strip()}))


def claimed_tasks(checkout: Path) -> tuple[str, ...]:
    """The scheduled tasks the project's `.devkit.toml` claims (`project_tasks`)."""
    if not (checkout / harness_config.MANIFEST_NAME).is_file():
        return ()
    return tuple(harness_config.load(checkout).schedule.tasks)


def present_tasks(names: Sequence[str], run: Runner) -> tuple[str, ...]:
    """Which of `names` this machine's scheduler holds; none off Windows."""
    if not WINDOWS:
        return ()
    found = []
    for name in names:
        result = run(devkit_schtasks.query_xml_argv(name), QUICK_TIMEOUT)
        if result.returncode == 0 and devkit_schtasks.parse_task(result.stdout) is not None:
            found.append(name)
    return tuple(found)


def repo_archived(repo: str, run: Runner) -> tuple[str, bool | None]:
    """`(repo, archived)`: `("", False)` when GitHub has no such repo, and `archived` None
    when gh could not say at all -- not knowing is not "not archived"."""
    result = run(["gh", "repo", "view", repo, "--json", "isArchived"], QUICK_TIMEOUT)
    if result.returncode != 0:
        said = (result.stderr or "").lower()
        return ("", False) if "could not resolve" in said else (repo, None)
    try:
        return repo, bool(json.loads(result.stdout or "{}").get("isArchived"))
    except (ValueError, AttributeError):
        return repo, None


def gather(
    name: str, checkout: Path, base: Path, run: Runner, *, owner: str = "", local: bool = True
) -> Facts:
    """Read the collector setting, this machine's assignment and record, and -- when
    `local` -- the checkout's running services, its claimed tasks and its repo."""
    collectors, _notes = config.parse_setting(devkit_project.canonical_text())
    collector = next((c for c in collectors if c.project == name), None)
    on_disk = checkout.is_dir()
    has_compose = on_disk and bool(collector_build.compose_files(checkout))
    repo, archived = repo_archived(f"{owner}/{name}", run) if local and owner else ("", False)
    return Facts(
        name=name,
        checkout=checkout,
        on_disk=on_disk,
        collector=collector,
        assigned=config.load_assignment(base / config.ASSIGNMENT).get(name),
        has_compose=has_compose,
        running=running_services(checkout, run) if local and has_compose else (),
        tasks=present_tasks(claimed_tasks(checkout), run) if local and on_disk else (),
        record=load_records(base / RECORD).get(name),
        repo=repo,
        archived=archived,
    )


# --- carrying it out ----------------------------------------------------------------


def save_xml(text: str, target: Path) -> None:
    """Keep an exported task document as `schtasks /Create /XML` will read it back.

    The pipe hands back a UTF-16 declaration over 8-bit text with CRCRLF line endings
    (`devkit_schtasks.parse_task`), and the file has to be in the encoding it declares
    (`devkit_schtasks.write_task_file`)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text.replace("\r\r\n", "\n").replace("\r\n", "\n"), encoding="utf-16")


def execute(steps: Sequence[Step], run: Runner, out=print) -> str:
    """Run `steps` in order and stop at the first failure: "" when all ran, else why."""
    for step in steps:
        out(f"  {step.label}")
        cwd = None if step.cwd is None else str(step.cwd)
        result = run(list(step.argv), step.timeout, cwd=cwd)
        for line in (result.stdout or "").splitlines() if step.save_to is None else []:
            out(f"    {line}")
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            return f"{step.label}: exit {result.returncode}" + (
                f" -- {detail[-1]}" if detail else ""
            )
        if step.save_to is not None:
            save_xml(result.stdout, step.save_to)
    return ""


def forget(base: Path, name: str, record: dict | None) -> None:
    """Drop the record of a retire a restore has put back, and its kept task files."""
    records = load_records(base / RECORD)
    if name not in records:
        return
    records.pop(name)
    save_records(base / RECORD, records)
    for task in (record or {}).get("tasks", []):
        task_file(base, name, task).unlink(missing_ok=True)


def _notes(plan: Plan, out) -> None:
    for note in plan.notes:
        out(f"  NOTE    {note}")


def retire(
    name: str,
    checkout: Path,
    owner: str,
    run: Runner = sweep.run_bounded,
    out=print,
    base: Path | None = None,
) -> str:
    """Stop `name` on this machine and archive its repo: "" when done, else why not.

    The record is written before anything runs, so a run that fails halfway still knows
    what to put back."""
    if why := refusal(name):
        return why
    base = base or config.home(REPO_ROOT)
    facts = gather(name, checkout, base, run, owner=owner)
    plan = retire_plan(facts, base)
    _notes(plan, out)
    records = load_records(base / RECORD)
    records[name] = merged_record(facts, _dt.datetime.now().isoformat(timespec="seconds"))
    save_records(base / RECORD, records)
    return execute(plan.steps, run, out)


def restore(
    name: str,
    checkout: Path,
    run: Runner = sweep.run_bounded,
    out=print,
    base: Path | None = None,
) -> str:
    """Put back what retiring `name` stopped here: "" when done, else why not. The
    record is kept until every step has run, so a failed restore can be run again."""
    if why := refusal(name):
        return why
    base = base or config.home(REPO_ROOT)
    facts = gather(name, checkout, base, run, local=False)
    plan = restore_plan(facts, base)
    _notes(plan, out)
    failed = execute(plan.steps, run, out)
    if not failed:
        forget(base, name, facts.record)
    return failed
