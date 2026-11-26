#!/usr/bin/env python3
"""Retire a project from this machine and the registry, or restore one, in one command.

    retire-project.py retire NAME [--yes] [--force] [--dry-run]
    retire-project.py restore NAME [--yes] [--dry-run]
    retire-project.py list                 # what this machine has retired, and how

`plug-projects.py --unplug` is the registry half and nothing else: it touches nothing on
disk, so a retired project's collector kept running, its claimed scheduled tasks kept
firing (and, unclaimed once the project left the registry, were filed as
`scheduled-job-unowned`), and its compose stack kept its containers. This script runs
that unplug and then the three halves it leaves, in an order that fails safe:

| step | retire | restore |
| --- | --- | --- |
| registry | `plug-projects.py --unplug` (its gates and hazards) | `--plug`, cloning if absent |
| collector | `collectors.py stop-here` | back to what it was: `run-here`, or `release` |
| claimed tasks | exported to XML, then deleted | registered again from that XML |
| compose stack | `docker compose down` -- **never `-v`** | `up -d` of what was running |

**The registry goes first** because it is the step with gates: a live box, unpushed work
or the wrong branch refuses there, before anything has been stopped. **Volumes are never
removed**: a static checkout's volumes hold a dev database that costs hours to rebuild,
the reason `docker-maint.py` may not pass `-v` either. Images stay too, so a restore
rebuilds nothing.

**What retire stopped is recorded on this machine** (`RECORD`, under devkit's ignored
`logs/`), because the collector assignment, the tasks and the running containers are all
per machine. That record is what lets a restore put back exactly what was there -- a
collector that was hands-off goes back to hands-off rather than starting -- and a second
retire keeps the first record's answers, which the first retire has since changed. The
registry is global, so on a second PC that ran the project, `retire` again there: the
registry step is skipped as already done and the local halves run.

Pure planning (`retire_plan`, `restore_plan`, `merged_record`) over gathered `Facts`;
`execute` runs the steps through an injected runner. Tested in
`tests/test_retire_project.py`.
"""

from __future__ import annotations

import argparse
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
ARTIFACT = REPO_ROOT / "logs" / "retire-project.log"

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
    registered: bool
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


# --- planning -----------------------------------------------------------------------


def _py(script: str, *args: str) -> tuple[str, ...]:
    return (sys.executable, str(SCRIPTS / script), *args)


def refusal(facts: Facts) -> str:
    """Why `facts.name` cannot be retired or restored at all; "" when it can."""
    if facts.name == sweep.source_checkout(REPO_ROOT).name:
        return "devkit is the checkout every other one is driven from -- it cannot be retired"
    if facts.name in devkit_project.NOT_PROJECTS:
        return f"{facts.name} is a reference checkout, not a project"
    return ""


def retire_plan(facts: Facts, base: Path, *, force: bool = False) -> Plan:
    """The steps a retire of `facts.name` takes on this machine, registry first."""
    plan = Plan()
    if facts.registered:
        unplug = _py("plug-projects.py", "--unplug", facts.name, "--yes")
        plan.steps.append(Step("unplug from the registry", unplug + (("--force",) if force else ())))
    else:
        plan.notes.append("not in the registry -- that half is already done")
    if facts.collector is not None and facts.assigned != config.STOP:
        plan.steps.append(
            Step("stop its collector here", _py("collectors.py", "stop-here", facts.name))
        )
    for task in facts.tasks:
        saved = task_file(base, facts.name, task)
        plan.steps.append(
            Step(f"export task {task}", tuple(devkit_schtasks.query_xml_argv(task)),
                 timeout=QUICK_TIMEOUT, save_to=saved)
        )
        plan.steps.append(
            Step(f"delete task {task}", tuple(devkit_schtasks.delete_argv(task)), timeout=QUICK_TIMEOUT)
        )
    _plan_compose_down(facts, plan)
    return plan


def _plan_compose_down(facts: Facts, plan: Plan) -> None:
    if not facts.has_compose:
        return
    if facts.running is None:
        plan.notes.append(
            "docker did not answer -- its containers were not checked; start Docker and "
            "retire again to take them down"
        )
        return
    plan.steps.append(
        Step("remove its containers (volumes kept)", ("docker", "compose", "down"),
             cwd=facts.checkout, timeout=COMPOSE_DOWN_TIMEOUT)
    )


def restore_plan(facts: Facts, base: Path) -> Plan:
    """The steps a restore of `facts.name` takes: registry (and clone), then what the
    record says this machine had running."""
    plan = Plan()
    if not facts.registered or not facts.on_disk:
        plan.steps.append(
            Step("plug into the registry", _py("plug-projects.py", "--plug", facts.name, "--yes"))
        )
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
            Step(f"register task {task}", tuple(devkit_schtasks.register_argv(task, saved)),
                 timeout=QUICK_TIMEOUT)
        )
    services = _restart_services(facts, record)
    if services:
        plan.steps.append(
            Step("start what was running", ("docker", "compose", "up", "-d", *services),
                 cwd=facts.checkout, timeout=COMPOSE_UP_TIMEOUT)
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


def gather(name: str, base: Path, run: Runner, *, local: bool = True) -> Facts:
    """Read the registry, the collector setting, this machine's assignment and record,
    and -- when `local` -- the checkout's running services and claimed tasks."""
    workspace_root = sweep.default_workspace(base).parent
    checkout = workspace_root / name
    canonical = devkit_project.canonical_text()
    collectors, _notes = config.parse_setting(canonical)
    collector = next((c for c in collectors if c.project == name), None)
    on_disk = checkout.is_dir()
    has_compose = on_disk and bool(collector_build.compose_files(checkout))
    return Facts(
        name=name,
        checkout=checkout,
        registered=name in devkit_project.known_projects(canonical),
        on_disk=on_disk,
        collector=collector,
        assigned=config.load_assignment(base / config.ASSIGNMENT).get(name),
        has_compose=has_compose,
        running=running_services(checkout, run) if local and has_compose else (),
        tasks=present_tasks(claimed_tasks(checkout), run) if local and on_disk else (),
        record=load_records(base / RECORD).get(name),
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
            return f"{step.label}: exit {result.returncode}" + (f" -- {detail[-1]}" if detail else "")
        if step.save_to is not None:
            save_xml(result.stdout, step.save_to)
    return ""


def write_artifact(lines: Sequence[str]) -> None:
    """This run's failures, per `.claude/rules/engineering.md`; emptied by a clean run."""
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(lines)
    ARTIFACT.write_text(body + "\n" if body else "", encoding="utf-8", newline="\n")


def confirmed(plan: Plan, args: argparse.Namespace, ask=input) -> bool:
    """Print the plan; whether to go on. `--dry-run` never does, `--yes` always does."""
    print(f"  {args.verb} {args.name} will:")
    for step in plan.steps:
        print(f"    {step.label}")
    for note in plan.notes:
        print(f"  NOTE    {note}")
    if not plan.steps or args.dry_run:
        return False
    if args.yes:
        return True
    try:
        return ask("  apply? [y/N] ").strip().lower() in {"y", "yes"}
    except EOFError:
        return False


def finish_record(args: argparse.Namespace, facts: Facts, base: Path, failed: str) -> None:
    """Keep the record a retire made; drop the one a restore has put back."""
    records = load_records(base / RECORD)
    if args.verb == "retire" or failed or args.name not in records:
        return
    records.pop(args.name)
    save_records(base / RECORD, records)
    for task in (facts.record or {}).get("tasks", []):
        task_file(base, args.name, task).unlink(missing_ok=True)


def act(args: argparse.Namespace, base: Path, run: Runner) -> int:
    """`retire` or `restore` one project: plan, confirm, record, run."""
    facts = gather(args.name, base, run, local=args.verb == "retire")
    why = refusal(facts)
    if why or not (facts.registered or facts.on_disk or facts.record):
        why = why or f"{args.name} is not registered, not on this PC and not retired here"
        print(f"ERROR   {why}", file=sys.stderr)
        write_artifact([why])
        return 2
    if args.verb == "retire":
        plan = retire_plan(facts, base, force=args.force)
    else:
        plan = restore_plan(facts, base)
    if not confirmed(plan, args):
        if not plan.steps and not args.dry_run:
            # A restore with nothing left to start is already restored.
            finish_record(args, facts, base, "")
        print("  nothing changed")
        write_artifact([])
        return 0
    if args.verb == "retire":
        records = load_records(base / RECORD)
        records[args.name] = merged_record(facts, _dt.datetime.now().isoformat(timespec="seconds"))
        save_records(base / RECORD, records)
    failed = execute(plan.steps, run)
    finish_record(args, facts, base, failed)
    if failed:
        print(f"ERROR   {failed}", file=sys.stderr)
        print("        fix that and run the same command again; done steps are skipped", file=sys.stderr)
    write_artifact([failed] if failed else [])
    return 1 if failed else 0


def list_records(base: Path) -> int:
    records = load_records(base / RECORD)
    if not records:
        print("  nothing is retired on this machine")
    for name, record in records.items():
        services = ", ".join(record.get("services", [])) or "none"
        tasks = ", ".join(record.get("tasks", [])) or "none"
        print(
            f"  {name}: retired {record.get('retired_at', '?')}; collector was "
            f"{record.get('collector') or 'hands-off'}; running: {services}; tasks: {tasks}"
        )
    write_artifact([])
    return 0


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="verb", required=True)
    sub.add_parser("list", help="what this machine has retired, and what it stopped")
    for verb, text in (("retire", "unplug and stop it here"), ("restore", "plug and start it back")):
        cmd = sub.add_parser(verb, help=text)
        cmd.add_argument("name")
        cmd.add_argument("--yes", action="store_true", help="skip the confirmation")
        cmd.add_argument("--dry-run", action="store_true", help="print the plan and stop")
        if verb == "retire":
            cmd.add_argument(
                "--force", action="store_true", help="unplug over live boxes or unpushed work"
            )
        else:
            cmd.set_defaults(force=False)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, run: Runner = sweep.run_bounded) -> int:
    args = parse_args(argv)
    base = config.home(REPO_ROOT)
    if args.verb == "list":
        return list_records(base)
    try:
        return act(args, base, run)
    except OSError as exc:
        print(f"ERROR   {exc}", file=sys.stderr)
        write_artifact([str(exc)])
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
