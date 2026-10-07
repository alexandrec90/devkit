"""`scripts/fix_red.py`: step 3 of the fix pass -- what is red, and re-running a gate."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fix_cycle
import fix_red


def _regate_world(monkeypatch, code: int, err: str = "", answers: list | None = None) -> list:
    """`answers`, when given, is `(code, err)` per call in turn; else every call is `code`."""
    asked: list = []

    def gh(*args):
        asked.append(args)
        answer = answers.pop(0) if answers else (code, err)
        return subprocess.CompletedProcess(args, answer[0], "", answer[1])

    monkeypatch.setattr(fix_red.sweep, "gh_for", lambda _p: gh)
    monkeypatch.setattr(fix_red.time, "sleep", lambda _s: None)
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


_GATEWAY = "HTTP 504: We couldn't respond to your request in time. Sorry about that. (https://api.github.com/x)"


def test_a_regate_github_times_out_on_is_retried(monkeypatch, tmp_path):
    """bc1ec6d1: one `HTTP 504` from the dispatch API was filed as a harness defect, and a
    fixer sent at devkit for a GitHub hiccup the next attempt would have cleared."""
    asked = _regate_world(monkeypatch, 0, answers=[(1, _GATEWAY), (0, "")])
    ok, line = fix_red.regate(tmp_path)
    assert ok and line == "master -- no verdict at the tip; gate re-run"
    assert len(asked) == 2


def test_a_regate_github_keeps_failing_on_is_deferred_not_filed(monkeypatch, tmp_path):
    """Still a server error after every retry: the next pass re-runs it, since the tip is
    still unread, so the line carries no `FAILED` for `fix-pass.py` to file."""
    slept: list = []
    asked = _regate_world(monkeypatch, 1, _GATEWAY)
    monkeypatch.setattr(fix_red.time, "sleep", slept.append)
    ok, line = fix_red.regate(tmp_path)
    assert not ok and "FAILED" not in line
    assert line.startswith("master -- gate re-run deferred: GitHub answered HTTP 504")
    assert len(asked) == len(fix_red.REGATE_RETRY_SECONDS) + 1
    assert slept == list(fix_red.REGATE_RETRY_SECONDS)


def test_a_refusal_is_not_retried(monkeypatch, tmp_path):
    asked = _regate_world(monkeypatch, 1, "HTTP 422: Workflow does not have 'workflow_dispatch'")
    ok, line = fix_red.regate(tmp_path)
    assert not ok and "FAILED" in line
    assert len(asked) == 1


@pytest.mark.parametrize(
    ("said", "transient"),
    [
        (_GATEWAY, True),
        ("HTTP 502: Bad Gateway", True),
        ("HTTP 503: No server is currently available", True),
        ('Post "https://api.github.com/x": net/http: TLS handshake timeout', True),
        ("error connecting to api.github.com", True),
        # 4d942641: how a push words GitHub's 500.
        ("remote: Internal Server Error", True),
        ("fatal: unable to access 'https://github.com/o/r.git/': Could not resolve host", True),
        ("! [remote rejected] main -> main (protected branch hook declined)", False),
        ("HTTP 422: Workflow does not have 'workflow_dispatch'", False),
        ("HTTP 404: Not Found", False),
        ("HTTP 5000 widgets", False),
        ("", False),
    ],
)
def test_transient_names_only_a_server_or_network_failure(said, transient):
    assert fix_red.transient(said) is transient


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
    # Counted as re-run all the same: a rehearsal that held every project fixer behind
    # "devkit's gate could not be read" showed holds the dispatch pass never makes.
    assert rerun == {"devkit"}
    assert fix_red.regate_unread(tmp_path, [], fix_cycle.DISPATCH) == ([], set())
