#!/usr/bin/env python3
"""Stand the coding-agent harness down, in three groups, and put it back exactly.

The harness is three separate costs wearing one name, and until now only one of them
could be switched off. `DEVKIT_HOOKS_OFF` (see `scripts/hooks/harness_config.py`)
quietened four hooks; nothing touched the branch tier, the instruction files every
session pays for on every turn, or the scheduled jobs that keep opening PRs in the
background. An operator who wanted a bare agent had to remember all three by hand, which
is not a thing anyone does twice -- so this owns the whole set, and `--status` is the
answer to "what is actually off right now".

| group | what it stands down | how |
| --- | --- | --- |
| `hooks` | every hook, the branch tier included | `DEVKIT_HOOKS_OFF=1` |
| `instructions` | `CLAUDE.md` and `.claude/rules/*.md`, every tier | move aside, reversibly |
| `jobs` | the scheduled jobs that deliver agent branches | `schtasks /Change /DISABLE` |

**Skills are deliberately not a group.** They cost nothing until a session invokes one by
name, which is the opposite of the always-loaded tier this exists to switch off, and a
bare session that has lost `/ship` has lost the thing it needs *most* once the hooks that
used to do that work are gone.

## Why the instruction group moves files

There is no flag for it. `claude --bare` skips CLAUDE.md discovery and is unusable here:
it also refuses OAuth, so on a subscription login it answers every prompt with "Not
logged in", and setting `CLAUDE_CODE_SIMPLE=1` by hand does the same. `--safe-mode`
disables skills, MCP servers and slash commands too, which is several groups more than
anybody asked for. So the only lever on what gets injected is whether the file is on disk
when the session starts. `harness_state.py` owns the moving, and the skip-worktree bit
that keeps a moved file out of `git add -A`.

## What reads a stood-down tier, and what to expect of it

An instruction file that has moved is still an instruction file, so the two gates that
assert *about* the tier read through `harness_state.instruction_sources` and see the
stashed copies: `tests/test_doc_claims.py` and `scripts/instruction-budget.py`. They stay
honest with the group off.

`sync-devkit.py --check` does not, and cannot be made to without a devkit-only import in
a vendored file. With the instructions group off it reports the vendored rules as drift,
because on disk they are missing. That is not a false alarm about anything the operator
did not do, but it is noise, so `--off` says so at the time. CI is unaffected: a clean
clone has never had a ledger.

Windows-only for the `jobs` group (`schtasks`), silent about it elsewhere -- the same
contract as `schedule_health.py`. The other two groups are cross-platform.

Read-only by default: `--status` is what runs with no verb, `--off`/`--on` are what act.

Tested in `tests/test_harness_switch.py`.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devkit_project
import harness_state
from harness_state import Ledger

REPO_ROOT = Path(__file__).resolve().parents[1]

# The three groups, in the order `--status` prints them and `--off` applies them: cheapest
# and most reversible first, so a run interrupted half way has stood down a prefix of the
# list rather than an arbitrary subset.
GROUPS = ("hooks", "instructions", "jobs")

# The USER settings file, not each project's. `harness_config.hooks_off` reads the
# environment, and this is the one file whose `env` block reaches every Claude session on
# the machine -- including one started in an ephemeral box, whose project settings came
# from a checkout and whose cwd is in neither the workspace nor a registered project.
CLAUDE_USER_SETTINGS = Path.home() / ".claude" / "settings.json"
HOOKS_OFF_ENV = "DEVKIT_HOOKS_OFF"
HOOKS_OFF_VALUE = "1"

# Codex reads no settings file of ours, so its hooks only see the variable if the machine
# has it. `setx` is the persistent user environment on Windows; elsewhere the shell
# profile is the operator's own business and this reports rather than edits.
WINDOWS = os.name == "nt"

# The scheduled tasks that exist to move agent branches along, and only those. The others
# this machine runs -- `devkit-docker-prune`, `devkit-docker-stop-idle`,
# `devkit-global-tools`, `devkit-tray`, `devkit-rc-servers` -- are machine maintenance that
# has nothing to do with whether an agent is running, so switching them off here would be
# turning off the vacuum cleaner because you stopped cooking.
BRANCH_DELIVERY_JOBS = (
    "devkit-worktree-reconcile",
    "devkit-upgrade-projects",
    "devkit-release",
)

# What reads as "off" to a human must not switch the harness off, the same asymmetry
# `harness_config._OFF_VALUES` and `git_policy.SKIP_ENV_VAR` document: someone writing
# `DEVKIT_HOOKS_OFF=0` means "leave the hooks running".
_READS_AS_ON = frozenset({"", "0", "false", "no", "off"})

EXIT_OK = 0
EXIT_USAGE = 2


def job_change_argv(name: str, enable: bool) -> list[str]:
    """`schtasks` for one job. Disabled rather than deleted: the registration carries the
    trigger, the windowless interpreter and the time limit its installer chose, and
    re-creating it from memory is how those get lost."""
    return ["schtasks", "/Change", "/TN", name, "/ENABLE" if enable else "/DISABLE"]


def settings_with_env(payload: object, name: str, value: str | None) -> dict:
    """`payload` with `env[name]` set to `value`, or removed when `value` is None.

    Comments do not survive (`json.dumps`), which is the same trade `project_settings.py`
    makes on the same file for the same reason: one owner for the shape beats two parsers
    that disagree about it.
    """
    data = dict(payload) if isinstance(payload, dict) else {}
    # Bound before the isinstance rather than fetched twice: `data.get` returns
    # `Any | None` on each call, so mypy cannot carry the narrowing across two of them.
    block = data.get("env")
    env = dict(block) if isinstance(block, dict) else {}
    if value is None:
        env.pop(name, None)
    else:
        env[name] = value
    if env:
        data["env"] = env
    else:
        data.pop("env", None)
    return data


def read_settings(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def hooks_are_off(path: Path = CLAUDE_USER_SETTINGS) -> bool:
    """Whether the user settings file carries the switch.

    Read by `agent-box.py`, which exports the same variable into the terminal it opens so
    a Codex session -- which reads no settings file of ours -- stands down too.
    """
    payload = read_settings(path)
    env = payload.get("env") if isinstance(payload, dict) else None
    value = env.get(HOOKS_OFF_ENV, "") if isinstance(env, dict) else ""
    return str(value).strip().lower() not in _READS_AS_ON


def default_roots(workspace: Path) -> list[Path]:
    """The tiers a session loads, outermost first.

    The user tier and the workspace root come first because they are the two that reach a
    session running in an ephemeral box, where the checkout tier is the box's own copy.
    """
    roots = [Path.home() / ".claude", workspace.parent]
    text = workspace.read_text(encoding="utf-8") if workspace.is_file() else ""
    roots += [workspace.parent / name for name in devkit_project.known_projects(text)]
    return [root for root in roots if root.is_dir()]


def switch_hooks(off: bool, path: Path = CLAUDE_USER_SETTINGS, runner=subprocess.run) -> list[str]:
    """Write or clear `DEVKIT_HOOKS_OFF` in the user settings, and in the user env."""
    payload = settings_with_env(
        read_settings(path), HOOKS_OFF_ENV, HOOKS_OFF_VALUE if off else None
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")
    lines = [f"  {HOOKS_OFF_ENV}{'=' + HOOKS_OFF_VALUE if off else ' removed'} in {path}"]
    if not WINDOWS:
        lines.append("  (not Windows: set the same variable in your shell profile for Codex)")
        return lines
    if off:
        runner(["setx", HOOKS_OFF_ENV, HOOKS_OFF_VALUE], capture_output=True)
    else:
        runner(
            ["reg", "delete", "HKCU\\Environment", "/v", HOOKS_OFF_ENV, "/f"], capture_output=True
        )
    lines.append("  user environment updated too, so Codex hooks see it in a new terminal")
    return lines


def switch_jobs(off: bool, names: Sequence[str] = BRANCH_DELIVERY_JOBS, runner=subprocess.run):
    """Disable or re-enable the branch-delivery scheduled tasks."""
    if not WINDOWS:
        return ["  (not Windows: no scheduled tasks to switch)"], []
    lines: list[str] = []
    changed: list[str] = []
    for name in names:
        done = runner(job_change_argv(name, not off), capture_output=True, text=True)
        if done.returncode == 0:
            changed.append(name)
            lines.append(f"  {'disabled' if off else 'enabled'}: {name}")
        else:
            lines.append(f"  not registered here, skipped: {name}")
    return lines, changed


def switch_instructions(
    off: bool, ledger: Ledger, workspace: Path, runner=subprocess.run
) -> list[str]:
    """Move the instruction tier aside, or put it back. See `harness_state`."""
    if not off:
        return harness_state.restore_root(ledger, None, runner)
    lines: list[str] = []
    for root in default_roots(workspace):
        lines += harness_state.switch_root(root, ledger, runner)
    lines.append(
        "  note: `sync-devkit.py --check` will report the vendored rules as drift while "
        "this group is off -- they are held, not lost."
    )
    return lines


def status_lines(ledger: Ledger, workspace: Path) -> list[str]:
    """What is off right now, group by group, read from the world and not from intent."""
    held = len(ledger.instructions)
    roots = sorted({Path(f.root).name for f in ledger.instructions})
    instructions = f"  ({held} files held from: {', '.join(roots)})" if held else ""
    jobs = f"  ({', '.join(ledger.jobs)})" if ledger.jobs else ""
    live = sum(len(harness_state.instruction_files(root)) for root in default_roots(workspace))
    return [
        f"hooks:        {'OFF' if hooks_are_off() else 'on'}  ({CLAUDE_USER_SETTINGS})",
        f"instructions: {'OFF' if held else 'on'}{instructions}",
        f"jobs:         {'OFF' if ledger.jobs else 'on'}{jobs}",
        f"\n{live} instruction files are currently loadable across the registered roots.",
    ]


def apply(groups: Iterable[str], off: bool, workspace: Path, runner=subprocess.run) -> list[str]:
    """Run the requested groups and return the whole report."""
    ledger = Ledger.load()
    wanted = set(groups)
    lines: list[str] = []
    for group in (name for name in GROUPS if name in wanted):
        lines.append(f"{group}:")
        if group == "hooks":
            lines += switch_hooks(off, runner=runner)
            ledger.hooks = off
        elif group == "instructions":
            lines += switch_instructions(off, ledger, workspace, runner)
        else:
            job_lines, changed = switch_jobs(off, runner=runner)
            lines += job_lines
            ledger.jobs = tuple(changed) if off else ()
    ledger.save()
    return lines or ["nothing selected"]


def selected_groups(raw: str) -> list[str] | None:
    """The `--group` value as a list, or None when it names something that is not a group."""
    names = [name.strip() for name in raw.split(",") if name.strip()]
    if "all" in names or not names:
        return list(GROUPS)
    return names if all(name in GROUPS for name in names) else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--off", action="store_true", help="stand the selected groups down")
    action.add_argument("--on", action="store_true", help="put the selected groups back")
    # Accepted rather than inferred from "no verb", so the workspace task's picker can
    # offer three literal values instead of one option that is the absence of the others.
    action.add_argument("--status", action="store_true", help="report and change nothing")
    parser.add_argument(
        "--group",
        default="all",
        help=f"comma-separated: {', '.join(GROUPS)} (default: all of them)",
    )
    parser.add_argument("--workspace", type=Path, default=None)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    import worktree

    workspace = (args.workspace or worktree.DEFAULT_WORKSPACE).resolve()

    # A dismissed quick-pick leaves the literal `${input:harnessGroups}` in the argument:
    # VS Code aborts a run itself only for its own input types, never for a `command` one.
    # Treated as a cancel here, which is where it has to be -- `log-wrap.py` passes its
    # tail through untouched, so a check in the wrapper would prove nothing about this.
    if "${input:" in args.group:
        print("harness-switch: no groups picked; nothing done")
        return EXIT_OK

    groups = selected_groups(args.group)
    if groups is None:
        print(f"harness-switch: unknown group(s) in '{args.group}'", file=sys.stderr)
        return EXIT_USAGE

    if args.off or args.on:
        print("\n".join(apply(groups, args.off, workspace)) + "\n")
    print("\n".join(status_lines(Ledger.load(), workspace)))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
