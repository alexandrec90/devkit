#!/usr/bin/env python3
"""Install what merged, within the half hour, whether or not anyone is at the PC.

A project whose default branch carries something that has to be *installed* on this PC
to take effect -- a helper copied out of the checkout, a service registered -- says how
in `scripts/deploy.py` (`CONTRACT`). A merge installs nothing by itself, and the local
end-to-end tier that used to do it waits for an idle PC and for a checkout on its
default branch: tvshell's Kodi-crash fix once sat merged and uninstalled for a day of TV
watching, because the PC was "in use" every fire and the checkout was on a person's
branch.

So `e2e-pass.py run --due` calls `deploy_all` **before** its idle check, on every fire:

- **Only origin's tip, never the checkout.** The contract runs in a detached tree cut at
  `origin/<default>` (`path_for`), so a person's branch or dirty checkout neither blocks
  it nor is installed by it. The prefix is this module's own, not `disposable_tree`'s,
  whose sweep belongs to the medium tier running beside this one.
- **Once per tip** (`is_due`). The verdict is `logs/deploy/<project>/result.json`, the
  script's output beside it; a red one is tried again after `RETRY`, sooner if the tip
  moves. A red deploy is not filed here: the e2e tier's own run of the same project
  reads the installed state and is red for it, which is the fix pass's channel.
- **Optional.** A project with no `scripts/deploy.py` on origin is not asked for one.

The contract runs under the machine's console Python with `DEVKIT_DEPLOY=1`, cwd the
tree, and must be safe while someone is using the PC: this is its whole point.

Tested in `tests/test_deploy_merged.py`.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sys
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent))
import disposable_tree
import e2e_results
import sweep
import task_branch as tb

REPO_ROOT = Path(__file__).resolve().parents[1]

CONTRACT = "scripts/deploy.py"
ENV_FLAG = "DEVKIT_DEPLOY"
RUN_ENV = {ENV_FLAG: "1", "PYTHONUTF8": "1"}
PREFIX = "deploy-"
RESULTS_DIR = Path("logs") / "deploy"
TIMEOUT = 30 * 60.0
RETRY = _dt.timedelta(hours=1)

PASSED = "passed"
FAILED = "failed"

Git = sweep.Git
Runner = Callable[..., object]


@dataclass(frozen=True)
class Deployed:
    project: str
    sha: str
    status: str
    finished: str
    reason: str = ""


def result_path(project: str, root: Path = REPO_ROOT) -> Path:
    return root / RESULTS_DIR / project / "result.json"


def read(project: str, root: Path = REPO_ROOT) -> Deployed | None:
    try:
        data = json.loads(result_path(project, root).read_text(encoding="utf-8"))
        return Deployed(**data)
    except (OSError, ValueError, TypeError):
        return None


def write(result: Deployed, output: str, root: Path = REPO_ROOT) -> Path:
    path = result_path(result.project, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(result), indent=2) + "\n", encoding="utf-8")
    (path.parent / "output.log").write_text(output, encoding="utf-8")
    return path


def is_due(last: Deployed | None, tip: str, now: _dt.datetime) -> bool:
    """Whether `tip` is owed a deploy: never tried there, or red there an hour ago. A
    tip that could not be read is owed nothing."""
    if not tip:
        return False
    if last is None or last.sha != tip:
        return True
    if last.status != FAILED:
        return False
    try:
        return now - _dt.datetime.fromisoformat(last.finished) >= RETRY
    except (ValueError, TypeError):
        return True


def path_for(checkout: Path, sha: str) -> Path:
    return disposable_tree.aw.default_root(checkout) / f"{PREFIX}{sha[:12]}"


def clear_leftovers(checkout: Path, runner: Runner) -> None:
    """Remove what a killed run of this module left. Only this job cuts the prefix, and
    the scheduler never runs two of it at once (`IgnoreNew`)."""
    root = disposable_tree.aw.default_root(checkout)
    for tree in sorted(root.glob(f"{PREFIX}*")) if root.is_dir() else []:
        disposable_tree.destroy(checkout, tree, runner)


def run_contract(checkout: Path, tip: str, runner: Runner) -> tuple[int, str]:
    """`(exit code, output)` of `CONTRACT` in a tree at `tip`, the tree gone afterwards."""
    clear_leftovers(checkout, runner)
    tree = path_for(checkout, tip)
    tree.parent.mkdir(parents=True, exist_ok=True)
    add = ("worktree", "add", "--quiet", "--force", "--force", "--detach", str(tree), tip)
    cut = runner(["git", "-C", str(checkout), *add], disposable_tree.CUT_TIMEOUT)
    if not e2e_results.ok(cut):
        return 1, f"could not cut a tree at {tip[:12]}: {e2e_results.said(cut)}"
    try:
        env = {**os.environ, **RUN_ENV}
        argv = [sweep.console_python(), str(tree / CONTRACT)]
        done = runner(argv, TIMEOUT, cwd=str(tree), env=env)
    finally:
        left = disposable_tree.destroy(checkout, tree, runner)
    output = f"{getattr(done, 'stdout', '') or ''}\n{getattr(done, 'stderr', '') or ''}"
    if left:
        output += f"\n{left}"
    return int(getattr(done, "returncode", 1)), output


def deploy_project(
    name: str,
    checkout: Path,
    *,
    root: Path = REPO_ROOT,
    git_for: Callable[[Path], Git] = sweep.git_for,
    runner: Runner = sweep.run_bounded,
    now: Callable[[], _dt.datetime] = _dt.datetime.now,
) -> Deployed | None:
    """Deploy origin's tip of `name` when it is owed one; None when nothing ran."""
    git = git_for(checkout)
    if not e2e_results.ok(git("fetch", "--quiet", "origin")):
        return None
    base = tb.detect_default_branch(git, fallback="main")
    tip = e2e_results.origin_tip(git, base)
    if not tip or not e2e_results.ok(git("cat-file", "-e", f"origin/{base}:{CONTRACT}")):
        return None
    if not is_due(read(name, root), tip, now()):
        return None
    code, output = run_contract(checkout, tip, runner)
    why = "timed out" if code == sweep.TIMED_OUT else ("" if code == 0 else f"exit {code}")
    stamp = now().isoformat(timespec="seconds")
    result = Deployed(name, tip, PASSED if code == 0 else FAILED, stamp, why)
    write(result, output, root)
    return result


def line_for(result: Deployed) -> str:
    said = f" ({result.reason})" if result.reason else ""
    where = (RESULTS_DIR / result.project).as_posix()
    return f"{result.project}: deploy {result.status} at {result.sha[:12]}{said}; see {where}"


class Seams(Protocol):
    """What `deploy_all` reads off `e2e-pass.Seams`, a frozen dataclass: read-only."""

    @property
    def root(self) -> Path: ...
    @property
    def git_for(self) -> Callable[[Path], Git]: ...
    @property
    def runner(self) -> Runner: ...
    @property
    def now(self) -> Callable[[], _dt.datetime]: ...


def deploy_all(workspace: Path, names: Iterable[str], seams: Seams) -> list[Deployed]:
    """Every project in `names` owed a deploy, deployed; what ran, each line printed."""
    ran: list[Deployed] = []
    for name in names:
        checkout = workspace.parent / name
        if not checkout.is_dir():
            continue
        result = deploy_project(
            name,
            checkout,
            root=seams.root,
            git_for=seams.git_for,
            runner=seams.runner,
            now=seams.now,
        )
        if result is not None:
            print(line_for(result), flush=True)
            ran.append(result)
    return ran
