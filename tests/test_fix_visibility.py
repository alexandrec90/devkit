"""`scripts/fix_visibility.py`: a private repo that runs Actions awaits its owner."""

from __future__ import annotations

import datetime as _dt
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fix_cycle
import fix_findings
import fix_loop
import fix_visibility

NOW = _dt.datetime(2026, 11, 1, 23, 0, tzinfo=_dt.UTC)


def _ctx(tmp_path: Path, projects: list[str], mode: str = fix_cycle.DISPATCH) -> fix_loop.Context:
    devkit = tmp_path / "devkit"
    devkit.mkdir(exist_ok=True)
    return fix_loop.Context(
        tmp_path, projects, devkit, tmp_path / "l.json", tmp_path / "h", mode, NOW
    )


def _checkout(root: Path, name: str, workflow: bool = True) -> None:
    folder = root / name / ".github" / "workflows"
    folder.mkdir(parents=True)
    if workflow:
        (folder / "pr-gate.yml").write_text("on: pull_request\n", encoding="utf-8")


def _gh(answers: dict[str, object]):
    """`gh_for` over `answers`: a dict is the repo's JSON, an int a failed exit code."""

    def bound(path: Path):
        def gh(*args):
            answer = answers[path.name]
            if isinstance(answer, int):
                return subprocess.CompletedProcess(args, answer, "", "HTTP 502")
            return subprocess.CompletedProcess(args, 0, json.dumps(answer), "")

        return gh

    return bound


PRIVATE = {"visibility": "PRIVATE", "isArchived": False}
PUBLIC = {"visibility": "PUBLIC", "isArchived": False}


def test_runs_ci_needs_a_workflow_file(tmp_path):
    _checkout(tmp_path, "a")
    _checkout(tmp_path, "b", workflow=False)
    assert fix_visibility.runs_ci(tmp_path / "a")
    assert not fix_visibility.runs_ci(tmp_path / "b"), "an empty workflows folder runs nothing"
    assert not fix_visibility.runs_ci(tmp_path / "absent")


def test_is_private_reads_visibility_and_archive_and_unknown_is_none():
    def gh_of(code, out):
        return lambda *a: subprocess.CompletedProcess(a, code, out, "")

    assert fix_visibility.is_private(gh_of(0, json.dumps(PRIVATE))) is True
    assert fix_visibility.is_private(gh_of(0, json.dumps({"visibility": "INTERNAL"}))) is True
    assert fix_visibility.is_private(gh_of(0, json.dumps(PUBLIC))) is False
    archived = {"visibility": "PRIVATE", "isArchived": True}
    assert fix_visibility.is_private(gh_of(0, json.dumps(archived))) is False, "runs nothing"
    assert fix_visibility.is_private(gh_of(1, "")) is None
    assert fix_visibility.is_private(gh_of(0, "not json")) is None
    assert fix_visibility.is_private(gh_of(0, "[]")) is None


def test_ask_reads_only_checkouts_that_run_ci_and_retire_keeps_what_it_is_told(tmp_path):
    _checkout(tmp_path, "a")
    _checkout(tmp_path, "b", workflow=False)
    ctx = _ctx(tmp_path, ["a", "b", "gone"])
    assert fix_visibility.ask(ctx, _gh({"a": PRIVATE})) == {"a": True}
    journal = fix_findings.Journal(ctx.devkit_dir)
    journal.add(fix_findings.Finding(fix_visibility.KIND, "a", fix_visibility.detail("a")))
    fix_loop.record(ctx, journal)
    assert fix_visibility.retire(ctx, {"a"}) == []
    [line] = fix_visibility.retire(ctx, set())
    assert line.startswith("retired [")


def test_a_private_repo_running_ci_is_filed_awaiting_its_owner(tmp_path):
    """productivity-tracker and social-scraper, 2026-11-01: private, out of minutes, and
    every gate refused before anything said they were private."""
    for name in ("social-scraper", "carameli", "notes"):
        _checkout(tmp_path, name, workflow=name != "notes")
    ctx = _ctx(tmp_path, ["social-scraper", "carameli", "notes"])
    journal = fix_findings.Journal(ctx.devkit_dir)
    gh = _gh({"social-scraper": PRIVATE, "carameli": PUBLIC, "notes": PRIVATE})
    lines = fix_visibility.check(ctx, journal, gh)
    assert lines == ["social-scraper -- private and runs Actions: filed for its owner"]
    [found] = journal.findings
    assert (found.kind, found.project) == (fix_visibility.KIND, "social-scraper")
    assert found.detail == fix_visibility.detail("social-scraper")
    fix_loop.record(ctx, journal)
    items = fix_findings.triage.open_items(fix_findings.triage.load(ctx.devkit_dir))
    assert list(fix_findings.triage.awaiting_person(items).values()) == [fix_visibility.STEP]


def test_a_repo_made_public_is_retired_and_one_github_could_not_read_is_kept(tmp_path):
    for name in ("a", "b"):
        _checkout(tmp_path, name)
    ctx = _ctx(tmp_path, ["a", "b"])
    journal = fix_findings.Journal(ctx.devkit_dir)
    fix_visibility.check(ctx, journal, _gh({"a": PRIVATE, "b": PRIVATE}))
    fix_loop.record(ctx, journal)
    ids = {
        item.project: item.id
        for item in fix_findings.triage.open_items(fix_findings.triage.load(ctx.devkit_dir))
    }
    later = _gh({"a": PUBLIC, "b": 1})
    rehearsal = _ctx(tmp_path, ["a", "b"], fix_cycle.PLAN)
    assert fix_visibility.check(rehearsal, fix_findings.Journal(ctx.devkit_dir), later) == [
        f"would retire [{ids['a']}] -- the repo is public or archived, or runs no workflow, "
        "as of this pass"
    ]
    [line] = fix_visibility.check(ctx, fix_findings.Journal(ctx.devkit_dir), later)
    assert line.startswith(f"retired [{ids['a']}]")
    [left] = fix_findings.triage.open_items(fix_findings.triage.load(ctx.devkit_dir))
    assert left.project == "b", "an unreadable answer proves nothing"
