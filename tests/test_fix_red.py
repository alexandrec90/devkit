"""`scripts/fix_red.py`: step 3 of the fix pass -- what is red, and re-running a gate."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fix_cycle
import fix_red


def _regate_world(monkeypatch, code: int, err: str = "") -> list:
    asked: list = []

    def gh(*args):
        asked.append(args)
        return subprocess.CompletedProcess(args, code, "", err)

    monkeypatch.setattr(fix_red.sweep, "gh_for", lambda _p: gh)
    monkeypatch.setattr(fix_red.sweep, "git_for", lambda _p: lambda *a: None)
    monkeypatch.setattr(fix_red.tb, "detect_default_branch", lambda _git, fallback="main": "master")
    return asked


def test_regate_dispatches_the_gate_on_the_default_branch(monkeypatch, tmp_path):
    """devkit main at a merge the auto-merge workflow made: `GITHUB_TOKEN` raises no push
    event, so no gate ran at the tip and every pass held every project PR behind it."""
    asked = _regate_world(monkeypatch, 0)
    ok, line = fix_red.regate(tmp_path)
    assert ok and line.startswith("master -- ")
    assert asked == [("workflow", "run", fix_red.gate_evidence.GATE_WORKFLOW, "--ref", "master")]


def test_a_regate_gh_refuses_is_said_with_its_last_line(monkeypatch, tmp_path):
    _regate_world(monkeypatch, 1, "warn\nHTTP 422: Workflow does not have 'workflow_dispatch'\n")
    ok, line = fix_red.regate(tmp_path)
    assert not ok and line.endswith("HTTP 422: Workflow does not have 'workflow_dispatch'")
    _regate_world(monkeypatch, 1)
    assert fix_red.regate(tmp_path) == (False, "master -- FAILED to re-run the gate: ?")


def test_regate_unread_re_runs_each_and_reports_only_the_dispatched(monkeypatch, tmp_path):
    answers = {"devkit": (True, "main -- gate re-run"), "carameli": (False, "main -- FAILED x")}
    monkeypatch.setattr(fix_red, "regate", lambda project_dir: answers[project_dir.name])
    lines, rerun = fix_red.regate_unread(tmp_path, ["devkit", "carameli"], fix_cycle.DISPATCH)
    assert lines == ["devkit main -- gate re-run", "carameli main -- FAILED x"]
    assert rerun == {"devkit"}


def test_regate_unread_outside_dispatch_only_says_what_it_would_do(monkeypatch, tmp_path):
    monkeypatch.setattr(fix_red, "regate", lambda _d: pytest.fail("plan re-gated"))
    lines, rerun = fix_red.regate_unread(tmp_path, ["devkit"], fix_cycle.PLAN)
    assert lines == ["devkit -- would re-run the gate: no verdict at the tip"]
    assert rerun == set()
    assert fix_red.regate_unread(tmp_path, [], fix_cycle.DISPATCH) == ([], set())
