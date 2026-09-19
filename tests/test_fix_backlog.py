"""`scripts/fix_backlog.py`: the harness-defect ledger as one failure for the pass."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fix_backlog
import fix_plan


def test_the_backlog_becomes_one_failure_with_its_groups_as_evidence(monkeypatch, tmp_path):
    devkit = tmp_path / "devkit"
    shard = fix_backlog.triage.ledger_file(devkit)
    shard.parent.mkdir(parents=True)
    lines = [
        "2026-09-18T06:02:17+00:00\tevent=scheduled-job-failed\tagent=claude\thost=h"
        "\tproject=devkit\tdetail=unattended task 'Scheduled: Devkit Release' failed",
        "2026-09-18T13:58:38+00:00\tevent=agent-report\tagent=claude\thost=h"
        "\tproject=carameli\tversion=7572259\tmessage=lint cannot pass on the pinned Node",
        "2026-09-18T14:58:38+00:00\tevent=agent-report\tagent=claude\thost=h"
        "\tproject=carameli\tversion=7572259\tmessage=lint cannot pass on the pinned Node",
    ]
    shard.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        fix_backlog.tb, "detect_default_branch", lambda git, fallback="main": "main"
    )
    backlog = fix_backlog.ledger_failure(devkit, tmp_path / "ev")
    assert backlog is not None
    assert (backlog.kind, backlog.project, backlog.base) == (fix_plan.LEDGER, "devkit", "main")
    assert len(backlog.signature) == 2, "two groups, not three lines"
    assert backlog.signature[0].startswith("agent-report carameli [")
    assert backlog.signature[0].endswith(" x2")
    assert len(backlog.sha) == fix_plan.KEY_DIGEST
    log = Path(backlog.evidence) / "harness-triage.log"
    assert "Scheduled: Devkit Release" in log.read_text(encoding="utf-8")
    assert fix_plan.describe(backlog).startswith("2 open group(s) on the harness-defect ledger")


def test_a_retired_group_changes_the_key_and_an_empty_ledger_is_no_failure(monkeypatch, tmp_path):
    """The dispatch ledger keys on the sha, so a resolution -- or a new item -- is a new
    dispatch, and the same backlog twice is not."""
    devkit = tmp_path / "devkit"
    shard = fix_backlog.triage.ledger_file(devkit)
    shard.parent.mkdir(parents=True)
    line = "2026-09-18T06:02:17+00:00\tevent=scheduled-job-failed\tagent=claude\thost=h\tproject=devkit\tdetail=x\n"
    shard.write_text(line, encoding="utf-8")
    monkeypatch.setattr(
        fix_backlog.tb, "detect_default_branch", lambda git, fallback="main": "main"
    )
    first = fix_backlog.ledger_failure(devkit, tmp_path / "ev")
    again = fix_backlog.ledger_failure(devkit, tmp_path / "ev")
    assert first is not None and again is not None and first.sha == again.sha
    fix_backlog.triage.resolve(
        [first.signature[0].split("[")[1].split("]")[0]], "fixed", root=devkit
    )
    assert fix_backlog.ledger_failure(devkit, tmp_path / "ev") is None
    (tmp_path / "empty").mkdir()
    assert fix_backlog.ledger_failure(tmp_path / "empty", tmp_path / "ev") is None
