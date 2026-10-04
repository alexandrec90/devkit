"""`scripts/fix_issues.py`: a tracker issue closed once its workflow is green at the tip."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fix_cycle
import fix_issues

TIP = "f" * 40
GREEN = {"status": "completed", "conclusion": "success", "headSha": TIP, "url": "https://run/7"}
RED = {"status": "completed", "conclusion": "failure", "headSha": TIP, "url": "https://run/6"}
ISSUE = {"number": 12, "title": f"Nightly{fix_issues.gate_evidence.ISSUE_SUFFIX}", "body": ""}


def world(monkeypatch, runs: list[dict], close_code: int = 0):
    asked: list = []

    def gh(*args):
        asked.append(args)
        if args[:2] == ("issue", "close"):
            return subprocess.CompletedProcess(
                args, close_code, "", "" if close_code == 0 else "HTTP 403: nope\n"
            )
        raise AssertionError(args)

    monkeypatch.setattr(fix_issues.gate_evidence, "nightly_issues", lambda _gh: [ISSUE])
    monkeypatch.setattr(
        fix_issues.gate_evidence,
        "scheduled_at_tip",
        lambda project_dir, workflow: ("main", "nightly.yml", runs, TIP),
    )
    return gh, asked


def test_an_issue_whose_workflow_is_green_at_the_tip_is_closed_as_completed(monkeypatch, tmp_path):
    gh, asked = world(monkeypatch, [GREEN, RED])
    lines = fix_issues.sweep_project("ibkr_trader", tmp_path, fix_cycle.DISPATCH, gh)
    assert lines == ["ibkr_trader #12 (Nightly) -- closed: green on origin/main"]
    (close,) = asked
    assert close[:5] == ("issue", "close", "12", "--reason", "completed")
    assert "https://run/7" in close[-1] and "fffffffff" in close[-1]


def test_an_issue_still_red_at_the_tip_is_left_open(monkeypatch, tmp_path):
    gh, asked = world(monkeypatch, [RED, GREEN])
    assert fix_issues.sweep_project("ibkr_trader", tmp_path, fix_cycle.DISPATCH, gh) == []
    assert asked == []


def test_a_plan_only_says_it_would_close(monkeypatch, tmp_path):
    gh, asked = world(monkeypatch, [GREEN])
    lines = fix_issues.sweep_project("ibkr_trader", tmp_path, fix_cycle.PLAN, gh)
    assert lines == ["ibkr_trader #12 (Nightly) -- would close: green on origin/main at the tip"]
    assert asked == []


def test_a_close_gh_refuses_is_a_failed_line_for_the_pass_to_file(monkeypatch, tmp_path):
    gh, _ = world(monkeypatch, [GREEN], close_code=1)
    lines = fix_issues.sweep_project("ibkr_trader", tmp_path, fix_cycle.DISPATCH, gh)
    assert lines == ["ibkr_trader #12 (Nightly) -- FAILED to close: HTTP 403: nope"]


def test_close_and_its_comment(monkeypatch):
    calls: list = []

    def gh(*args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1, "", "")

    assert fix_issues.close(gh, 3, "c") == "?"
    assert calls == [("issue", "close", "3", "--reason", "completed", "--comment", "c")]
    comment = fix_issues.close_comment("Nightly", "main", GREEN)
    assert comment.startswith("Closed by the fix pass: the Nightly workflow passed on origin/main")


def test_sweep_green_reads_every_registered_checkout_and_skips_a_missing_one(monkeypatch, tmp_path):
    (tmp_path / "ibkr_trader").mkdir()
    gh, _ = world(monkeypatch, [GREEN])
    lines = fix_issues.sweep_green(
        tmp_path / "w.code-workspace", ["ibkr_trader", "gone"], fix_cycle.PLAN, lambda d: gh
    )
    assert [line.split(" ", 1)[0] for line in lines] == ["ibkr_trader"]
