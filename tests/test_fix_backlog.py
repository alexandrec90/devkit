"""`scripts/fix_backlog.py`: the harness-defect ledger as one failure for the pass."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fix_backlog
import fix_ledger
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
    assert len(backlog.sha) == fix_ledger.KEY_DIGEST
    log = Path(backlog.evidence) / "harness-triage.log"
    assert "Scheduled: Devkit Release" in log.read_text(encoding="utf-8")
    assert fix_plan.describe(backlog).startswith("2 open group(s) on the harness-defect ledger")


def test_a_recurrence_in_an_open_group_sends_no_second_session(monkeypatch, tmp_path):
    """Every scheduled job now files each failed run, and a 15-minute job failing all
    day is ~96 rows in one group. Keyed on every open id, each row changed the sha and
    sent a devkit session at a defect one already had (2026-10-03). Keyed on the groups,
    only a new group -- or a retired one -- is a new dispatch."""
    devkit = tmp_path / "devkit"
    shard = fix_backlog.triage.ledger_file(devkit)
    shard.parent.mkdir(parents=True)
    row = "\tevent=scheduled-job-failed\tagent=claude\thost=h\tproject=devkit\tmessage=job failed"
    shard.write_text("2026-10-03T10:00:00+00:00" + row + "\n", encoding="utf-8")
    monkeypatch.setattr(
        fix_backlog.tb, "detect_default_branch", lambda git, fallback="main": "main"
    )
    first = fix_backlog.ledger_failure(devkit, tmp_path / "ev")
    with shard.open("a", encoding="utf-8") as handle:
        handle.write("2026-10-03T10:15:00+00:00" + row + "\n")
    again = fix_backlog.ledger_failure(devkit, tmp_path / "ev")
    assert first is not None and again is not None
    assert again.signature[0].endswith(" x2"), "the recurrence is still shown"
    assert again.sha == first.sha, "and sends nobody"
    other = "2026-10-03T10:30:00+00:00\tevent=agent-report\tagent=claude\thost=h\tproject=devkit\tmessage=m"
    with shard.open("a", encoding="utf-8") as handle:
        handle.write(other + "\n")
    newer = fix_backlog.ledger_failure(devkit, tmp_path / "ev")
    assert newer is not None and newer.sha != first.sha, "a new group is a new dispatch"


def test_a_retired_group_changes_the_key_and_an_empty_ledger_is_no_failure(monkeypatch, tmp_path):
    """The dispatch ledger keys on the sha, so a resolution -- or a new group -- is a new
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


def test_a_group_whose_fix_is_in_flight_is_shown_but_sends_no_session(monkeypatch, tmp_path):
    """d677ea57: #410's isolation-guard fix sat unmerged while the detector kept filing,
    and every new row changed the backlog's sha -- a devkit session each time, sent to
    re-prove a group whose fix was one merge away. Such a group is listed as pending and
    left out of the dispatch; alone, it is no failure at all."""
    devkit = tmp_path / "devkit"
    shard = fix_backlog.triage.ledger_file(devkit)
    shard.parent.mkdir(parents=True)
    row = (
        "\tevent=session-friction\tagent=claude\thost=h\tproject=devkit\tdetail=isolation-guard: x"
    )
    first = "2026-09-26T21:00:00+00:00" + row
    ref = fix_backlog.triage.item_id(first)
    resolved = f"2026-09-26T21:10:00+00:00\tevent=triage-resolved\tref={ref}\tpr=410\tnote=n"
    again = "2026-09-26T23:00:00+00:00" + row
    shard.write_text("\n".join((first, resolved, again)) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        fix_backlog.tb, "detect_default_branch", lambda git, fallback="main": "main"
    )
    assert fix_backlog.ledger_failure(devkit, tmp_path / "ev", {ref: "410"}) is None
    other = "2026-09-26T23:30:00+00:00\tevent=agent-report\tagent=claude\thost=h\tproject=devkit\tmessage=m"
    with shard.open("a", encoding="utf-8") as handle:
        handle.write(other + "\n")
    backlog = fix_backlog.ledger_failure(devkit, tmp_path / "ev", {ref: "410"})
    assert backlog is not None and len(backlog.signature) == 1
    assert backlog.signature[0].startswith("agent-report devkit [")
    log = (Path(backlog.evidence) / "harness-triage.log").read_text(encoding="utf-8")
    assert "PENDING on 410" in log
    assert len(fix_backlog.ledger_failure(devkit, tmp_path / "ev").signature) == 2, "landed"
