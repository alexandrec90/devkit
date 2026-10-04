#!/usr/bin/env python3
"""Uncommitted drift on a checkout's default branch: carry a sibling relock, report the rest.

ibkr_trader's `main` held an uncommitted `uv.lock` from 2026-10-03 20:53 on. data-lake
had dropped praw at 18:00 (`54b781d`); an interactive session then ran `uv run` in the
ibkr_trader checkout, and a bare `uv run` relocks against the editable sibling as it is
on disk. Worktree reconcile reported `[needs-branch] -- 1 uncommitted file(s) on main`
every 15 minutes and nothing acted on it, because nothing may touch work on a default
branch it cannot account for.

Two shapes of drift it can account for, both **the lockfile alone** (`assess`). One is
already on origin byte for byte -- what ibkr_trader's was by 2026-10-04, once #82 landed
the same relock and the checkout was merely kept from fast-forwarding -- and is
restored. The other is **relocked against a newer sibling path dependency**: derived
data, not anyone's work, recording a change the sibling already merged. `relock_reason`
and `assess` hold it to all of:

- `uv.lock` is the only path `git status` reports, and the committed lock is origin's;
- the sibling the `[tool.uv.sources]` path names is clean in `pyproject.toml` and its
  HEAD is on its origin, so CI can check that commit out;
- every package both locks hold is at the same version -- nothing was upgraded -- and
  the sibling's entry is the one that changed, now naming what its `pyproject.toml`
  declares, which the committed lock's entry did not;
- `uv lock --check` agrees the lock is what uv resolves today, without writing it.

Then it is carried: a fresh tree on `agent/auto/relock-<sibling>-<blob>` off origin,
the lock copied in, the CI pin of the sibling moved to the commit it was locked against
when the repo pins one (`worktree_env.pinned_ref`; ibkr_trader's PR gate does, and a lock
without the pin fails `uv sync --locked`), an intent the next pass ships, and the
checkout's lock restored. Anything else is reported in the record and left exactly as it
is.

Tested in `tests/test_fix_drift.py`.
"""

from __future__ import annotations

import datetime as _dt
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fix_cycle
import fix_reports
import fix_trees
import ship_intent
import sweep
import task_branch as tb
import worktree_env

LOCKFILE = "uv.lock"
WORKFLOWS = Path(".github") / "workflows"
CHECK_TIMEOUT = 120
_SHA = re.compile(r"^[0-9a-f]{40}$")
_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


# --- reading the two locks ---------------------------------------------------------------


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", str(name)).lower()


def packages(text: str) -> list[dict]:
    """The lock's `[[package]]` tables; empty when it does not parse."""
    try:
        listed = tomllib.loads(text).get("package", [])
    except (tomllib.TOMLDecodeError, AttributeError):
        return []
    return [p for p in listed if isinstance(p, dict)] if isinstance(listed, list) else []


def versions(listed: list[dict], skip: str = "") -> dict[str, set[str]]:
    """`name -> versions locked`, leaving out the package named `skip`."""
    found: dict[str, set[str]] = {}
    for package in listed:
        name = _norm(package.get("name", ""))
        if name and name != skip:
            found.setdefault(name, set()).add(str(package.get("version", "")))
    return found


def sibling_entry(listed: list[dict], rel: str) -> dict:
    """The package whose source is the path `rel` (`../data-lake`); {} when none is."""
    want = rel.replace("\\", "/").rstrip("/")
    for package in listed:
        source = package.get("source")
        if not isinstance(source, dict):
            continue
        for key in ("editable", "directory", "path"):
            if str(source.get(key, "")).replace("\\", "/").rstrip("/") == want:
                return package
    return {}


def locked_requirements(entry: dict) -> set[str]:
    """What a lock says a path package requires: its requires-dist and every dev group."""
    found = entry.get("metadata")
    metadata: dict = found if isinstance(found, dict) else {}
    rows = list(metadata.get("requires-dist", []) or [])
    groups = metadata.get("requires-dev", {}) or {}
    for group in groups.values() if isinstance(groups, dict) else []:
        rows += list(group or [])
    return {_norm(row.get("name", "")) for row in rows if isinstance(row, dict)} - {""}


def declared_requirements(pyproject: str) -> set[str]:
    """What a `pyproject.toml` declares: dependencies, every extra and every group."""
    try:
        data = tomllib.loads(pyproject)
    except tomllib.TOMLDecodeError:
        return set()
    project = data.get("project", {}) if isinstance(data.get("project"), dict) else {}
    specs = list(project.get("dependencies", []) or [])
    for extra in (project.get("optional-dependencies", {}) or {}).values():
        specs += list(extra or [])
    for group in (data.get("dependency-groups", {}) or {}).values():
        specs += [spec for spec in group or [] if isinstance(spec, str)]
    return {
        _norm(m.group(1)) for spec in specs if isinstance(spec, str) and (m := _NAME.match(spec))
    }


def relock_reason(head: str, work: str, rel: str, sibling_pyproject: str) -> str:
    """Why `work` is not `head` relocked against the sibling at `rel` and nothing else;
    empty when it is exactly that. Pure: both locks and the sibling's pyproject as text."""
    old, new = packages(head), packages(work)
    old_entry, new_entry = sibling_entry(old, rel), sibling_entry(new, rel)
    if not old or not new or not new_entry:
        return f"{LOCKFILE} does not lock the path dependency {rel}"
    name = _norm(new_entry.get("name", ""))
    before, after = versions(old, name), versions(new, name)
    moved = sorted(n for n in before.keys() & after.keys() if before[n] != after[n])
    if moved:
        return f"it changes the version of {', '.join(moved[:3])}: an upgrade, not a relock"
    declared = declared_requirements(sibling_pyproject)
    if locked_requirements(new_entry) != declared:
        return f"its {name} entry does not match {rel}/pyproject.toml"
    if locked_requirements(old_entry) == declared:
        return f"the committed lock already matches {rel}; the change is something else"
    return ""


# --- the checkout and its sibling ---------------------------------------------------------


def lock_blob(checkout: Path) -> str:
    """The blob id git would commit the working lock as -- through `hash-object`, so the
    checkout's own line-ending filter applies -- or "" when git cannot say."""
    return _out(sweep.git_for(checkout)("hash-object", LOCKFILE)).strip()


def _out(done: object) -> str:
    return str(getattr(done, "stdout", "") or "") if getattr(done, "returncode", 1) == 0 else ""


def sibling_head(sibling: Path) -> tuple[str, str]:
    """`(sha, why not)`: the sibling's HEAD when its pyproject is clean and origin has it."""
    git = sweep.git_for(sibling)
    if _out(git("status", "--porcelain", "--", "pyproject.toml")).strip():
        return "", f"{sibling.name}/pyproject.toml has uncommitted changes"
    sha = _out(git("rev-parse", "HEAD")).strip()
    base = tb.detect_default_branch(git, fallback="main")
    if not sha or git("merge-base", "--is-ancestor", sha, f"origin/{base}").returncode != 0:
        return "", f"{sibling.name}'s HEAD is not on origin/{base}, so CI could not check it out"
    return sha, ""


def lock_checks(checkout: Path, runner=sweep.run_windowless) -> str:
    """Why `uv lock --check` refuses the checkout's lock as it stands; empty when it
    accepts it. It resolves and compares, and writes nothing."""
    uv = shutil.which("uv")
    if not uv:
        return "uv is not on PATH to confirm the relock"
    try:
        done = runner(
            [uv, "lock", "--check"],
            cwd=checkout,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CHECK_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"uv lock --check could not run: {exc}"
    if done.returncode == 0:
        return ""
    said = (done.stderr or done.stdout or "").strip().splitlines()
    return f"uv lock --check refused it: {said[-1] if said else done.returncode}"


def move_pin(tree: Path, old: str, new: str) -> list[str]:
    """Replace the pinned sibling commit `old` with `new` in every workflow; the files."""
    moved = []
    for workflow in sorted((tree / WORKFLOWS).glob("*.y*ml")):
        text = workflow.read_text(encoding="utf-8")
        if old in text:
            workflow.write_text(text.replace(old, new), encoding="utf-8")
            moved.append(workflow.relative_to(tree).as_posix())
    return moved


# --- one checkout -------------------------------------------------------------------------


def dirty_default(checkout: Path) -> tuple[str, list[str]]:
    """`(default branch, porcelain lines)` when the checkout sits on its default branch
    with anything uncommitted; `("", [])` otherwise."""
    git = sweep.git_for(checkout)
    base = tb.detect_default_branch(git, fallback="main")
    if _out(git("symbolic-ref", "--short", "HEAD")).strip() != base:
        return "", []
    status = git("status", "--porcelain")
    lines = [line for line in _out(status).splitlines() if line.strip()]
    return (base, lines) if lines else ("", [])


# What `assess` makes of a default branch's drift.
LEAVE = "leave"  # anything but the two below: reported, never touched
LANDED = "landed"  # the lock is origin's byte for byte: restore it so the checkout syncs
CARRY = "carry"  # the lock relocked against a newer sibling: onto its own PR


@dataclass(frozen=True)
class Verdict:
    action: str
    why: str = ""
    rel: str = ""  # CARRY: the sibling's path source
    sha: str = ""  # CARRY: the sibling commit the lock matches


def assess(checkout: Path, base: str, lines: list[str]) -> Verdict:
    """What may be done about the uncommitted state of a checkout on its default branch.

    The lockfile alone, and either already on origin -- a fixer relocked and landed the
    same bytes, and the checkout is merely behind (ibkr_trader on 2026-10-04: #82 landed
    the very lock its `main` held, which kept reconcile from fast-forwarding it) -- or a
    relock against a sibling that `relock_reason` and `uv lock --check` both accept.
    """
    if lines != [f" M {LOCKFILE}"]:
        return Verdict(LEAVE, f"{len(lines)} uncommitted file(s), not {LOCKFILE} alone")
    git = sweep.git_for(checkout)
    blob = lock_blob(checkout)
    if blob and _out(git("rev-parse", f"origin/{base}:{LOCKFILE}")).strip() == blob:
        return Verdict(LANDED)
    head = _out(git("show", f"HEAD:{LOCKFILE}"))
    if head != _out(git("show", f"origin/{base}:{LOCKFILE}")):
        return Verdict(LEAVE, f"the committed {LOCKFILE} is not origin/{base}'s")
    work = (checkout / LOCKFILE).read_text(encoding="utf-8", errors="replace")
    reasons = []
    for rel in worktree_env.path_sources(checkout):
        sibling = (checkout / rel).resolve()
        pyproject = sibling / "pyproject.toml"
        text = pyproject.read_text(encoding="utf-8") if pyproject.is_file() else ""
        if why := relock_reason(head, work, rel, text):
            reasons.append(why)
            continue
        sha, why = sibling_head(sibling)
        why = why or lock_checks(checkout)
        return Verdict(LEAVE, why) if why else Verdict(CARRY, rel=rel, sha=sha)
    return Verdict(LEAVE, "; ".join(reasons) or "no [tool.uv.sources] path dependency relocked it")


def restore(checkout: Path, base: str, blob: str) -> str:
    """Put the committed lock back, unless it changed since `blob` was read; what happened."""
    if lock_blob(checkout) != blob:
        return f"{LOCKFILE} changed meanwhile, so {base}'s was left as is"
    restored = sweep.git_for(checkout)("checkout", "--", LOCKFILE)
    if getattr(restored, "returncode", 1) != 0:
        said = (getattr(restored, "stderr", "") or "git refused").strip()
        return f"FAILED to restore {base}'s {LOCKFILE}: {said}"
    return f"{base}'s {LOCKFILE} restored"


def carry(checkout: Path, base: str, verdict: Verdict) -> str:
    """Move the relock off the default branch onto its own tree and intent; what happened.

    The branch is named by the lock's blob, so the same relock is carried once: a later
    `uv run` that relocks the same bytes finds its branch and only restores.
    """
    blob = lock_blob(checkout)
    name = Path(verdict.rel).name
    branch = f"{tb.AUTOMATION_PREFIX}relock-{tb.slugify(name, max_len=24)}-{blob[:8]}"
    git = sweep.git_for(checkout)
    if any(
        git("rev-parse", "--verify", "--quiet", ref).returncode == 0
        for ref in (f"refs/heads/{branch}", f"refs/remotes/origin/{branch}")
    ):
        return f"already carried on {branch}; {restore(checkout, base, blob)}"
    tree, branch = fix_trees.cut_fresh_tree(checkout, branch, base)
    if tree is None:
        return f"FAILED to cut {branch} off origin/{base}; left as is"
    shutil.copyfile(checkout / LOCKFILE, tree / LOCKFILE)
    pin = worktree_env.pinned_ref(checkout, name)
    pins = move_pin(tree, pin, verdict.sha) if _SHA.match(pin) and pin != verdict.sha else []
    for path, text in (
        (fix_reports.ORIGIN_FILE, "fix-pass\n"),
        (ship_intent.INTENT_FILE, intent(checkout, name, verdict.sha, pins)),
    ):
        (tree / path).parent.mkdir(parents=True, exist_ok=True)
        (tree / path).write_text(text, encoding="utf-8")
    return f"carried to {branch} in {tree}, to ship next pass; {restore(checkout, base, blob)}"


def intent(checkout: Path, name: str, sha: str, pins: list[str]) -> str:
    """The commit message the carried relock ships with."""
    stamp = _dt.datetime.fromtimestamp((checkout / LOCKFILE).stat().st_mtime)
    pinned = (
        f" The CI pin of {name} moves to the same commit in {', '.join(pins)}: `uv sync "
        "--locked` checks this lock against the commit the gate checks out."
        if pins
        else ""
    )
    return (
        f"Relock {LOCKFILE} against {name} {sha[:9]}\n\n"
        f"{name} changed its own dependencies, and a `uv` run in the {checkout.name} checkout "
        f"relocked against it on disk ({stamp:%Y-%m-%d %H:%M}), leaving {LOCKFILE} "
        "uncommitted on the default branch. The fix pass checked that the lock changes only "
        "the sibling's entry and what it brings or drops -- no package that stays moved "
        f"version -- that {name}'s entry matches its pyproject.toml at {sha[:9]}, and that "
        f"`uv lock --check` accepts it, then carried it here.{pinned}\n"
    )


def tend_one(project: str, checkout: Path, mode: str) -> str:
    """The record line for one checkout, "" when its default branch is clean; in dispatch
    mode a landed lock is restored and a sibling relock carried."""
    base, porcelain = dirty_default(checkout)
    if not base:
        return ""
    verdict = assess(checkout, base, porcelain)
    where = f"{project} {base}"
    if verdict.action == LEAVE:
        return f"{where} -- left as is: {verdict.why}"
    if verdict.action == LANDED:
        what = f"{LOCKFILE} is origin/{base}'s already, byte for byte"
        if mode != fix_cycle.DISPATCH:
            return f"{where} -- would restore {LOCKFILE}: {what}"
        return f"{where} -- {what}; {restore(checkout, base, lock_blob(checkout))}"
    what = f"{LOCKFILE} relocked against {verdict.rel} {verdict.sha[:9]}"
    if mode != fix_cycle.DISPATCH:
        return f"{where} -- would carry {what}"
    return f"{where} -- {what}: {carry(checkout, base, verdict)}"


def tend(workspace: Path, projects: list[str], mode: str) -> list[str]:
    """Every registered checkout with uncommitted work on its default branch, a line each."""
    lines = []
    for project in projects:
        checkout = workspace.parent / project
        if (checkout / ".git").exists() and (line := tend_one(project, checkout, mode)):
            lines.append(line)
    return lines
