"""`scripts/fix_drift.py`: a default branch's uncommitted lockfile, carried or restored.

Real git throughout, in throwaway repositories: the property is what happens to a
checkout's tracked files, which a faked git could only assert about itself. `uv` is the
one thing faked (`lock_checks`) -- the suite does not resolve packages.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fix_cycle
import fix_drift as fd
import fix_reports
import ship_intent

ROOT_PKG = '[[package]]\nname = "ibkr-trader"\nversion = "0.1.0"\nsource = { editable = "." }\n'


def lock(*, praw: bool, httpx: str = "0.27.0") -> str:
    """A uv.lock with the root, the sibling `data-lake`, and its dependencies."""
    names = ["httpx", "praw"] if praw else ["httpx"]
    deps = ", ".join(f'{{ name = "{n}" }}' for n in names)
    text = 'version = 1\nrevision = 3\nrequires-python = ">=3.12"\n\n' + ROOT_PKG + "\n"
    text += (
        '[[package]]\nname = "data-lake"\nversion = "0.1.0"\n'
        'source = { editable = "../data-lake" }\n'
        f"dependencies = [{deps}]\n\n[package.metadata]\nrequires-dist = [{deps}]\n\n"
    )
    text += f'[[package]]\nname = "httpx"\nversion = "{httpx}"\nsource = {{ registry = "x" }}\n\n'
    if praw:
        text += '[[package]]\nname = "praw"\nversion = "7.7.1"\nsource = { registry = "x" }\n\n'
    return text


SIBLING_NOW = '[project]\nname = "data-lake"\ndependencies = ["httpx>=0.27"]\n'
SIBLING_THEN = '[project]\nname = "data-lake"\ndependencies = ["httpx>=0.27", "praw>=7.7"]\n'
PIN_OLD = "a" * 40
PYPROJECT = '[tool.uv.sources]\ndata-lake = { path = "../data-lake", editable = true }\n'


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return done.stdout.strip()


def _repo(tmp: Path, name: str, files: dict[str, str]) -> tuple[Path, Path]:
    """An origin with one commit of `files`, and a checkout of it on `main`."""
    origin, author, checkout = tmp / f"{name}.git", tmp / f"{name}-author", tmp / "ws" / name
    _git(tmp, "init", "--bare", "-b", "main", str(origin))
    _git(tmp, "clone", str(origin), str(author))
    for key, value in (("user.email", "t@t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(author, "config", key, value)
    _git(author, "config", "core.hooksPath", str(tmp / "no-hooks"))
    _git(author, "config", "core.autocrlf", "false")
    for path, text in files.items():
        (author / path).parent.mkdir(parents=True, exist_ok=True)
        (author / path).write_text(text, encoding="utf-8", newline="\n")
    _git(author, "add", ".")
    _git(author, "commit", "-m", "one")
    _git(author, "push", "origin", "main")
    _git(tmp, "-c", "core.autocrlf=false", "clone", str(origin), str(checkout))
    _git(checkout, "config", "core.hooksPath", str(tmp / "no-hooks"))
    _git(checkout, "config", "core.autocrlf", "false")
    _git(checkout, "remote", "set-head", "origin", "main")
    return author, checkout


@pytest.fixture
def world(tmp_path, monkeypatch):
    """ibkr_trader on `main` with a lock committed against data-lake-with-praw, its PR
    gate pinning the sibling, and data-lake since moved on to drop praw."""
    gate = (
        "jobs:\n  t:\n    steps:\n      - uses: actions/checkout@v7\n        with:\n"
        f"          repository: o/data-lake\n          ref: {PIN_OLD}\n"
    )
    _, checkout = _repo(
        tmp_path,
        "ibkr_trader",
        {
            "pyproject.toml": PYPROJECT,
            "uv.lock": lock(praw=True),
            ".github/workflows/pr-gate.yml": gate,
            # As every generated project ignores it: where the carried tree is cut.
            ".gitignore": ".claude/worktrees/\n",
        },
    )
    _, sibling = _repo(tmp_path, "data-lake", {"pyproject.toml": SIBLING_NOW})
    monkeypatch.setattr(fd, "lock_checks", lambda checkout: "")
    return checkout, sibling


def relock(checkout: Path, text: str) -> None:
    (checkout / fd.LOCKFILE).write_text(text, encoding="utf-8", newline="\n")


# --- the pure half ------------------------------------------------------------------------


def test_a_relock_against_the_moved_sibling_is_recognised_as_nothing_else():
    head, work = lock(praw=True), lock(praw=False)
    assert fd.relock_reason(head, work, "../data-lake", SIBLING_NOW) == ""
    assert fd.sibling_entry(fd.packages(work), "../data-lake")["name"] == "data-lake"
    assert fd.versions(fd.packages(head), skip="data-lake") == {
        "ibkr-trader": {"0.1.0"},
        "httpx": {"0.27.0"},
        "praw": {"7.7.1"},
    }


def test_an_upgrade_riding_on_a_relock_is_not_one():
    work = lock(praw=False, httpx="0.28.1")
    why = fd.relock_reason(lock(praw=True), work, "../data-lake", SIBLING_NOW)
    assert why.startswith("it changes the version of httpx")


def test_a_lock_that_does_not_match_the_sibling_or_already_did_is_not_a_relock():
    head = lock(praw=True)
    stale = fd.relock_reason(head, lock(praw=False), "../data-lake", SIBLING_THEN)
    assert stale == "its data-lake entry does not match ../data-lake/pyproject.toml"
    same = fd.relock_reason(
        head, head.replace("revision = 3", "revision = 3 "), "../data-lake", SIBLING_THEN
    )
    assert same.startswith("the committed lock already matches")
    assert fd.relock_reason("", lock(praw=False), "../data-lake", SIBLING_NOW).startswith(
        "uv.lock does not lock"
    )


def test_what_a_sibling_declares_and_what_a_lock_requires_of_it_are_compared_by_name():
    pyproject = (
        '[project]\ndependencies = ["HTTPX>=0.27"]\n'
        "[project.optional-dependencies]\narchive = [\"pyarrow>=17; python_version>'3'\"]\n"
        '[dependency-groups]\ndev = ["ruff==0.6", {include-group = "x"}]\n'
    )
    assert fd.declared_requirements(pyproject) == {"httpx", "pyarrow", "ruff"}
    entry = {
        "metadata": {
            "requires-dist": [{"name": "httpx"}, {"name": "pyarrow", "marker": "extra"}],
            "requires-dev": {"dev": [{"name": "ruff"}]},
        }
    }
    assert fd.locked_requirements(entry) == {"httpx", "pyarrow", "ruff"}


def test_move_pin_replaces_the_old_commit_in_every_workflow(tmp_path):
    workflows = tmp_path / fd.WORKFLOWS
    workflows.mkdir(parents=True)
    (workflows / "a.yml").write_text(f"ref: {PIN_OLD}\n", encoding="utf-8")
    (workflows / "b.yaml").write_text("ref: main\n", encoding="utf-8")
    assert fd.move_pin(tmp_path, PIN_OLD, "b" * 40) == [".github/workflows/a.yml"]
    assert (workflows / "a.yml").read_text(encoding="utf-8") == f"ref: {'b' * 40}\n"


# --- a real checkout ------------------------------------------------------------------------


def test_a_clean_default_branch_or_a_task_branch_is_not_drift(world):
    checkout, _ = world
    assert fd.dirty_default(checkout) == ("", [])
    _git(checkout, "checkout", "-b", "agent/x")
    relock(checkout, lock(praw=False))
    assert fd.dirty_default(checkout) == ("", []), "a task branch's edits are its own"


def test_anything_but_the_lockfile_alone_is_left_and_said(world):
    checkout, _ = world
    relock(checkout, lock(praw=False))
    (checkout / "notes.md").write_text("mine\n", encoding="utf-8")
    line = fd.tend_one("ibkr_trader", checkout, fix_cycle.DISPATCH)
    assert line == "ibkr_trader main -- left as is: 2 uncommitted file(s), not uv.lock alone"
    assert (checkout / "notes.md").is_file() and "praw" not in (checkout / "uv.lock").read_text(
        encoding="utf-8"
    ), "nothing touched"


def test_a_relock_against_the_sibling_is_carried_to_its_own_branch_and_main_restored(world):
    checkout, sibling = world
    relock(checkout, lock(praw=False))
    base, porcelain = fd.dirty_default(checkout)
    verdict = fd.assess(checkout, base, porcelain)
    assert verdict == fd.Verdict(
        fd.CARRY, rel="../data-lake", sha=_git(sibling, "rev-parse", "HEAD")
    )
    plan = fd.tend_one("ibkr_trader", checkout, fix_cycle.PLAN)
    assert plan.startswith("ibkr_trader main -- would carry uv.lock relocked against ../data-lake")
    assert fd.dirty_default(checkout)[1] == [" M uv.lock"], "a plan touches nothing"

    line = fd.tend(checkout.parent / "w.code-workspace", ["ibkr_trader"], fix_cycle.DISPATCH)[0]
    assert "carried to agent/auto/relock-data-lake-" in line and line.endswith(
        "main's uv.lock restored"
    )
    assert fd.dirty_default(checkout) == ("", []), "main is clean again"
    tree = next((checkout / ".claude" / "worktrees").iterdir())
    assert "praw" not in (tree / fd.LOCKFILE).read_text(encoding="utf-8")
    gate = (tree / ".github" / "workflows" / "pr-gate.yml").read_text(encoding="utf-8")
    assert verdict.sha in gate and PIN_OLD not in gate, "the CI pin follows the lock"
    assert (tree / fix_reports.ORIGIN_FILE).is_file(), "the pass's own PR merges itself"
    message = (tree / ship_intent.INTENT_FILE).read_text(encoding="utf-8")
    assert message.startswith(f"Relock uv.lock against data-lake {verdict.sha[:9]}")
    assert "pr-gate.yml" in message


def test_the_same_relock_again_is_restored_not_carried_twice(world):
    checkout, _ = world
    relock(checkout, lock(praw=False))
    workspace = checkout.parent / "w.code-workspace"
    fd.tend(workspace, ["ibkr_trader"], fix_cycle.DISPATCH)
    relock(checkout, lock(praw=False))
    line = fd.tend(workspace, ["ibkr_trader"], fix_cycle.DISPATCH)[0]
    assert "already carried on agent/auto/relock-data-lake-" in line
    assert len(list((checkout / ".claude" / "worktrees").iterdir())) == 1
    assert fd.dirty_default(checkout) == ("", [])


def test_a_lock_already_on_origin_is_restored_so_the_checkout_can_sync(world, tmp_path):
    """ibkr_trader on 2026-10-04: #82 landed the very bytes `main` held uncommitted, and
    that local edit is what kept reconcile from fast-forwarding the checkout."""
    checkout, _ = world
    author = tmp_path / "ibkr_trader-author"
    relock(author, lock(praw=False))
    _git(author, "commit", "-am", "relock")
    _git(author, "push", "origin", "main")
    _git(checkout, "fetch", "origin")
    relock(checkout, lock(praw=False))
    base, porcelain = fd.dirty_default(checkout)
    assert fd.assess(checkout, base, porcelain) == fd.Verdict(fd.LANDED)
    assert fd.tend_one("ibkr_trader", checkout, fix_cycle.PLAN).startswith(
        "ibkr_trader main -- would restore uv.lock"
    )
    line = fd.tend_one("ibkr_trader", checkout, fix_cycle.DISPATCH)
    assert line.endswith("main's uv.lock restored") and fd.dirty_default(checkout) == ("", [])
    _git(checkout, "merge", "--ff-only", "origin/main")


def test_a_lock_relocked_while_main_is_behind_origin_is_left(world, tmp_path):
    checkout, _ = world
    author = tmp_path / "ibkr_trader-author"
    relock(author, lock(praw=True, httpx="0.27.2"))
    _git(author, "commit", "-am", "bump")
    _git(author, "push", "origin", "main")
    _git(checkout, "fetch", "origin")
    relock(checkout, lock(praw=False))
    assert fd.tend_one("ibkr_trader", checkout, fix_cycle.DISPATCH).endswith(
        "left as is: the committed uv.lock is not origin/main's"
    )


def test_a_sibling_with_local_edits_or_unpushed_commits_is_not_locked_against(world):
    checkout, sibling = world
    relock(checkout, lock(praw=False))
    (sibling / "pyproject.toml").write_text(SIBLING_NOW + "# edit\n", encoding="utf-8")
    assert fd.sibling_head(sibling) == ("", "data-lake/pyproject.toml has uncommitted changes")
    _git(sibling, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "local")
    sha, why = fd.sibling_head(sibling)
    assert sha == "" and "is not on origin/main" in why
    assert "left as is" in fd.tend_one("ibkr_trader", checkout, fix_cycle.DISPATCH)


def test_uv_refusing_the_lock_leaves_it(world, monkeypatch):
    checkout, _ = world
    relock(checkout, lock(praw=False))
    monkeypatch.setattr(fd, "lock_checks", lambda c: "uv lock --check refused it: stale")
    assert fd.tend_one("ibkr_trader", checkout, fix_cycle.DISPATCH).endswith(
        "left as is: uv lock --check refused it: stale"
    )


def test_a_lock_that_changes_between_the_read_and_the_restore_is_left(world):
    checkout, _ = world
    relock(checkout, lock(praw=False))
    blob = fd.lock_blob(checkout)
    relock(checkout, lock(praw=False, httpx="9.9.9"))
    assert (
        fd.restore(checkout, "main", blob) == "uv.lock changed meanwhile, so main's was left as is"
    )


def test_lock_checks_runs_uv_lock_check_and_reports_a_refusal(monkeypatch, tmp_path):
    calls: list = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs["cwd"]))
        return subprocess.CompletedProcess(argv, 1, "", "error: The lockfile needs updating\n")

    monkeypatch.setattr(fd.shutil, "which", lambda name: "uv")
    assert fd.lock_checks(tmp_path, runner) == (
        "uv lock --check refused it: error: The lockfile needs updating"
    )
    assert calls == [(["uv", "lock", "--check"], tmp_path)]
    monkeypatch.setattr(fd.shutil, "which", lambda name: None)
    assert fd.lock_checks(tmp_path, runner).startswith("uv is not on PATH")


def test_intent_names_the_sibling_commit_and_the_moved_pin(world):
    checkout, _ = world
    text = fd.intent(checkout, "data-lake", "c" * 40, [".github/workflows/pr-gate.yml"])
    assert text.splitlines()[0] == f"Relock uv.lock against data-lake {'c' * 9}"
    assert "moves to the same commit in .github/workflows/pr-gate.yml" in text


def test_carry_reports_a_tree_it_could_not_cut(world, monkeypatch):
    checkout, sibling = world
    relock(checkout, lock(praw=False))
    monkeypatch.setattr(fd.fix_trees, "cut_fresh_tree", lambda c, b, base: (None, b))
    verdict = fd.Verdict(fd.CARRY, rel="../data-lake", sha=_git(sibling, "rev-parse", "HEAD"))
    assert fd.carry(checkout, "main", verdict).startswith("FAILED to cut agent/auto/relock-")
    assert fd.dirty_default(checkout)[1] == [" M uv.lock"], "left exactly as it was"
