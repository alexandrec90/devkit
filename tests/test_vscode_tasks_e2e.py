"""The workspace's tasks, clicked in a real VS Code: the tier no reading of the file reaches.

`tests/test_workspace_tasks_live.py` runs every picker's command and every cancel guard
on this machine, and can still not see the part that kept going wrong: what VS Code and
its two input extensions *do* with the block. The order inputs are asked in; whether a
chained picker receives the answer to the one before it; whether Escape on a command
input runs the task with the literal or with an answer from an earlier click; whether a
picker's command id resolves at all. Each of those was once written down wrong in
`.claude/rules/vscode-tasks.md` -- one of them for two releases -- and found by a person.

So this opens the installed VS Code on a rendered copy of `workspace.jsonc` and clicks:

- **an isolated profile** -- its own `--user-data-dir` and `--extensions-dir`, holding
  copies of the two input extensions from this machine's install -- so nothing the
  person has open is touched and nothing they remember is read;
- **every task's command replaced by `vscode_e2e/receipt.py`**, so each run writes the
  argv VS Code resolved and nothing the task names runs. The pickers are untouched: they
  run their real commands, which is what a chain needs;
- **a driver extension** (`vscode_e2e/driver/run.js`) that runs each task twice, once
  accepting the first row of every pick and once pressing Escape.

What a run must show: accepted, every task starts with no `${input:...}` left in its
arguments; escaped, a task either does not start or starts carrying a literal -- never
fully resolved with answers nobody gave this click. The scripts' own handling of that
literal is the live tier's (`test_escaping_any_command_picker_runs_nothing_and_exits_clean`).

Opens a window, which is why `scripts/e2e-pass.py` runs the tier only once the PC is
idle. Marked `local`, so CI and every default run deselect it; on the workstation, no VS
Code installed is a failure.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from support import LIVE_WORKSPACE, REPO_ROOT, devkit_jsonc, needs_live_workspace

HERE = Path(__file__).resolve().parent / "vscode_e2e"
sys.path.insert(0, str(HERE))
import receipt  # noqa: E402 - lives beside the driver, which is where the render points

DRIVER = HERE / "driver"
RECEIPT = HERE / "receipt.py"
CANONICAL = REPO_ROOT / "workspace.jsonc"
EXTENSIONS_HOME = Path.home() / ".vscode" / "extensions"

# Isolated-profile settings: no trust prompt to block a task, nothing phoning home,
# nothing restored from a previous run of this same profile.
SETTINGS = {
    "security.workspace.trust.enabled": False,
    "update.mode": "none",
    "extensions.autoUpdate": False,
    "extensions.autoCheckUpdates": False,
    "telemetry.telemetryLevel": "off",
    "workbench.startupEditor": "none",
    "window.restoreWindows": "none",
    "task.saveBeforeRun": "never",
    "task.problemMatchers.neverPrompt": True,
    "terminal.integrated.enablePersistentSessions": False,
    "git.enabled": False,
}

# One step of the driver: long enough that a pick it just accepted has made way for the
# next, short enough that forty tasks finish inside the night.
STEP_MS = 1200
# How long a started task gets to finish once its process is up.
SETTLE_MS = 3000
# The slowest picker chain on the machine, plus a fresh profile's first terminal, with
# room; the live tier holds each picker to its own, much smaller, budget.
TASK_TIMEOUT_MS = 120_000
# An escaped task that has not started by now is one Escape cancelled: the slowest picker
# draws, is escaped, and a literal-carrying task's terminal starts, inside this.
ESCAPE_TIMEOUT_MS = 30_000
LAUNCH_TIMEOUT_S = 3600


def code_executable() -> Path | None:
    """`Code.exe` beside the `code` launcher on PATH: the editor itself, not its CLI shim,
    because `--extensionTestsPath` is an argument to the electron app."""
    launcher = shutil.which("code")
    if launcher is None:
        return None
    for candidate in (Path(launcher).resolve().parent.parent / "Code.exe",):
        if candidate.is_file():
            return candidate
    return None


def picker_extensions(home: Path = EXTENSIONS_HOME) -> dict[str, Path]:
    """The newest installed copy of each extension `workspace.jsonc` recommends."""
    wanted = devkit_jsonc.loads(CANONICAL.read_text(encoding="utf-8"))["extensions"][
        "recommendations"
    ]
    found: dict[str, Path] = {}
    for ident in wanted:
        copies = sorted(home.glob(f"{ident}-*"), key=lambda p: p.name)
        if copies:
            found[ident] = copies[-1]
    return found


def render(text: str, receipts: Path, beside: Path = LIVE_WORKSPACE) -> dict:
    """The canonical workspace as the isolated VS Code opens it.

    Folders are absolute and keep their names -- `${workspaceFolder:devkit}` resolves by
    name, and this tree's directory is named for the worktree -- with devkit pointing at
    this tree. Every task runs `receipt.py` in front of its own command line.
    """
    document = devkit_jsonc.loads(text)
    folders = []
    for entry in document.get("folders", []):
        name = Path(str(entry["path"])).name
        where = REPO_ROOT if name == "devkit" else beside.parent / name
        if where.is_dir():
            folders.append({"name": name, "path": str(where)})
    for task in document["tasks"]["tasks"]:
        own = [str(task.get("command", "")), *(str(arg) for arg in task.get("args", []))]
        task["command"] = "python"
        task["args"] = [str(RECEIPT), str(receipts), task["label"], "--", *own]
        task.pop("presentation", None)
    return {"folders": folders, "tasks": document["tasks"], "settings": SETTINGS}


# Comma-separated task labels to drive instead of all of them: a fixer reproducing one
# task's red, without sitting through forty.
ONLY_ENV = "DEVKIT_VSCODE_E2E_ONLY"


def plan_for(tasks: list[dict], only: str = "") -> list[dict]:
    """Each task accepted; each task that asks anything escaped too."""
    wanted = {label.strip() for label in only.split(",") if label.strip()}
    tasks = [task for task in tasks if not wanted or task["label"] in wanted]
    plan = [{"label": task["label"], "mode": "accept"} for task in tasks]
    asking = [t for t in tasks if "${input:" in json.dumps(t.get("args", []))]
    return plan + [{"label": task["label"], "mode": "escape"} for task in asking]


def receipts_of(receipts: Path, label: str) -> list[list[str]]:
    path = receipts / f"{receipt.slug(label)}.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line)["argv"] for line in path.read_text(encoding="utf-8").splitlines()]


def judge(results: list[dict], receipts: Path) -> list[str]:
    """Every way a run broke the contract in the module docstring, one line each."""
    problems: list[str] = []
    # Each started run appended one receipt, in plan order: the n-th start is line n.
    used: dict[str, int] = {}
    for result in results:
        label, mode = result["label"], result["mode"]
        mine: list[list[str]] = []
        if result.get("started"):
            at = used.get(label, 0)
            mine = receipts_of(receipts, label)[at : at + 1]
            used[label] = at + 1
        if result.get("error"):
            problems.append(f"{label} [{mode}]: {result['error']}")
            continue
        if mode == "accept":
            if not result.get("started") or not mine:
                problems.append(
                    f"{label} [accept]: never started after {result.get('seconds', 0):.0f}s "
                    f"({result.get('steps', 0)} picks answered)"
                )
            elif any("${input:" in arg for arg in mine[0]):
                left = [arg for arg in mine[0] if "${input:" in arg]
                problems.append(f"{label} [accept]: started with unresolved {left}")
        elif result.get("started") and mine and not any("${input:" in a for a in mine[0]):
            problems.append(
                f"{label} [escape]: started fully resolved after Escape -- an answer from "
                f"an earlier click, not this one: {mine[0]}"
            )
    return problems


NEVER_STARTED = " [accept]: never started"


def never_started(problems: list[str]) -> list[str]:
    """The labels `judge` says an accepted run never started, in its order."""
    return [line.split(NEVER_STARTED)[0] for line in problems if NEVER_STARTED in line]


def second_look(problems: list[str], again: list[dict], found: list[str]) -> list[str]:
    """`problems` with each never-started line a fresh window re-ran replaced by what that
    window `found`. 50917bb2: a task with no input at all "never started after 120s (76
    picks answered)" in a full run, and the same label passed in 134 s alone. Which of
    the editor and the task broke is what a fresh window answers."""
    redone = tuple(f"{result['label']}{NEVER_STARTED}" for result in again)
    return [line for line in problems if not line.startswith(redone)] + found


# --- the pure half, which CI runs ---------------------------------------------------------


def test_the_render_keeps_every_task_and_runs_the_receipt_in_front_of_it(tmp_path):
    (tmp_path / "carameli").mkdir()
    text = json.dumps(
        {
            "folders": [{"path": "carameli"}, {"path": "devkit"}, {"path": "gone"}],
            "tasks": {
                "version": "2.0.0",
                "tasks": [
                    {
                        "label": "A: B",
                        "type": "process",
                        "command": "python",
                        "args": ["x.py", "${input:p}"],
                        "presentation": {"panel": "new"},
                    }
                ],
                "inputs": [],
            },
        }
    )
    rendered = render(text, tmp_path / "r", beside=tmp_path / "w.code-workspace")
    assert rendered["folders"] == [
        {"name": "carameli", "path": str(tmp_path / "carameli")},
        {"name": "devkit", "path": str(REPO_ROOT)},
    ]
    [task] = rendered["tasks"]["tasks"]
    assert task["command"] == "python"
    assert task["args"] == [
        str(RECEIPT),
        str(tmp_path / "r"),
        "A: B",
        "--",
        "python",
        "x.py",
        "${input:p}",
    ]
    assert "presentation" not in task
    assert rendered["settings"]["security.workspace.trust.enabled"] is False


def test_the_plan_escapes_only_the_tasks_that_ask():
    tasks = [{"label": "a", "args": ["${input:x}"]}, {"label": "b", "args": []}]
    assert plan_for(tasks) == [
        {"label": "a", "mode": "accept"},
        {"label": "b", "mode": "accept"},
        {"label": "a", "mode": "escape"},
    ]
    assert plan_for(tasks, " b ,") == [{"label": "b", "mode": "accept"}]


def _receipt(receipts: Path, label: str, *runs: list[str]) -> None:
    receipts.mkdir(exist_ok=True)
    lines = "".join(json.dumps({"label": label, "argv": argv}) + "\n" for argv in runs)
    (receipts / f"{receipt.slug(label)}.jsonl").write_text(lines, encoding="utf-8")


def test_the_judge_names_each_broken_contract(tmp_path):
    receipts = tmp_path / "r"
    _receipt(receipts, "ok", ["python", "a.py", "carameli"], ["python", "a.py", "${input:p}"])
    _receipt(receipts, "stale", ["python", "a.py", "carameli"], ["python", "a.py", "carameli"])
    _receipt(receipts, "literal", ["python", "a.py", "${input:p}"])
    results = [
        {"label": "ok", "mode": "accept", "started": True},
        {"label": "stale", "mode": "accept", "started": True},
        {"label": "literal", "mode": "accept", "started": True},
        {"label": "never", "mode": "accept", "started": False, "seconds": 120, "steps": 9},
        {"label": "gone", "mode": "accept", "error": "not found by fetchTasks"},
        {"label": "ok", "mode": "escape", "started": True},
        {"label": "stale", "mode": "escape", "started": True},
        {"label": "never", "mode": "escape", "started": False},
    ]
    assert judge(results, receipts) == [
        "literal [accept]: started with unresolved ['${input:p}']",
        "never [accept]: never started after 120s (9 picks answered)",
        "gone [accept]: not found by fetchTasks",
        "stale [escape]: started fully resolved after Escape -- an answer from an earlier "
        "click, not this one: ['python', 'a.py', 'carameli']",
    ]


def test_a_second_look_clears_a_wedged_window_and_keeps_a_broken_task():
    first = [
        "wedged [accept]: never started after 120s (76 picks answered)",
        "broken [accept]: never started after 120s (9 picks answered)",
        "stale [escape]: started fully resolved after Escape",
        "unseen [accept]: never started after 120s (1 picks answered)",
    ]
    assert never_started(first) == ["wedged", "broken", "unseen"]
    again = [
        {"label": "wedged", "mode": "accept", "started": True},
        {"label": "broken", "mode": "accept", "started": False},
    ]
    found = ["broken [accept]: never started after 120s (98 picks answered)"]
    assert second_look(first, again, found) == [
        "stale [escape]: started fully resolved after Escape",
        "unseen [accept]: never started after 120s (1 picks answered)",
        "broken [accept]: never started after 120s (98 picks answered)",
    ], "a label the second window was not given stays red"


def test_the_receipt_appends_what_it_was_handed(tmp_path):
    assert receipt.main([str(tmp_path), "A: B", "--", "python", "x.py", "v"]) == 0
    assert receipt.main([str(tmp_path), "A: B", "--", "python", "x.py", "w"]) == 0
    assert receipts_of(tmp_path, "A: B") == [["python", "x.py", "v"], ["python", "x.py", "w"]]
    assert receipt.main([str(tmp_path), "A: B", "x"]) == 2


# --- the live half: the editor itself -----------------------------------------------------


@needs_live_workspace
def test_every_task_resolves_its_inputs_and_honours_escape_in_real_vscode(tmp_path):
    code = code_executable()
    # A failure, not a skip: this tier runs only on the workstation the workspace is for,
    # and a skip there is the scheduled run passing on nothing (`scripts/e2e.py`).
    assert code is not None, "VS Code is not installed here (no Code.exe beside `code` on PATH)"
    extensions = picker_extensions()
    wanted = devkit_jsonc.loads(CANONICAL.read_text(encoding="utf-8"))["extensions"]
    absent = sorted(set(wanted["recommendations"]) - set(extensions))
    assert not absent, f"the picker extensions are not installed here: {absent}"
    extensions_dir = tmp_path / "extensions"
    for source in extensions.values():
        shutil.copytree(source, extensions_dir / source.name)
    results, problems = click(code, extensions_dir, tmp_path / "run", os.environ.get(ONLY_ENV, ""))
    if suspects := never_started(problems):
        wanted = ",".join(suspects)
        again, found = click(code, extensions_dir, tmp_path / "second-look", wanted)
        problems = second_look(problems, again, found)
    assert results, "the plan drove no task"
    assert not problems, "\n".join(problems)


def click(code: Path, extensions_dir: Path, where: Path, only: str) -> tuple[list[dict], list[str]]:
    """One window on a profile of its own in `where`: `(its results, what `judge` found)`."""
    receipts = where / "receipts"
    receipts.mkdir(parents=True)
    user = where / "user-data" / "User"
    user.mkdir(parents=True)
    (user / "settings.json").write_text(json.dumps(SETTINGS), encoding="utf-8")
    rendered = render(CANONICAL.read_text(encoding="utf-8"), receipts)
    workspace = where / "e2e.code-workspace"
    workspace.write_text(json.dumps(rendered, indent=2), encoding="utf-8")
    out = where / "report.json"
    tasks = plan_for(rendered["tasks"]["tasks"], only)
    plan = {
        "out": str(out),
        "tasks": tasks,
        "step_ms": STEP_MS,
        "settle_ms": SETTLE_MS,
        "task_timeout_ms": TASK_TIMEOUT_MS,
        "escape_timeout_ms": ESCAPE_TIMEOUT_MS,
    }
    (where / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "ELECTRON_RUN_AS_NODE"}
    env["DEVKIT_VSCODE_E2E_PLAN"] = str(where / "plan.json")
    done = subprocess.run(
        [
            str(code),
            str(workspace),
            f"--user-data-dir={where / 'user-data'}",
            f"--extensions-dir={extensions_dir}",
            f"--extensionDevelopmentPath={DRIVER}",
            f"--extensionTestsPath={DRIVER / 'run.js'}",
            "--skip-welcome",
            "--skip-release-notes",
            "--disable-workspace-trust",
        ],
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=LAUNCH_TIMEOUT_S,
        check=False,
    )
    assert out.is_file(), (
        f"the driver never reported (exit {done.returncode}): {done.stderr[-800:]}"
    )
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["missingCommands"] == [], "VS Code no longer has the commands the driver sends"
    assert done.returncode == 0, f"VS Code exited {done.returncode}: {done.stderr[-800:]}"
    assert len(report["results"]) == len(tasks), "the driver stopped part-way"
    return report["results"], judge(report["results"], receipts)
