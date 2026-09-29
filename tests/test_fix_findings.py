"""`scripts/fix_findings.py`: the one sink for what the fix pass could not turn green."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fix_findings
import harness_triage as triage


def finding(**fields) -> fix_findings.Finding:
    base = {"kind": "push-failed", "project": "carameli", "detail": "agent/x: rejected"}
    base.update(fields)
    return fix_findings.Finding(**base)


def test_the_kind_leads_the_detail_so_one_kind_recurring_is_one_ledger_group(tmp_path):
    fix_findings.record_all([finding()], [], tmp_path)
    [item] = triage.open_items(triage.load(tmp_path))
    assert item.event == fix_findings.FINDING
    assert item.detail == "push-failed: agent/x: rejected"
    assert item.project == "carameli"
    assert item.signature == fix_findings.signature(finding()), "computed the ledger's own way"


def test_a_finding_already_open_is_not_filed_again(tmp_path):
    """A pass every half hour would otherwise file one stuck push 48 times a day."""
    assert fix_findings.record_all([finding(), finding()], [], tmp_path) == [finding()]
    items = triage.load(tmp_path)
    assert fix_findings.record_all([finding(), finding(detail="other")], items, tmp_path) == [
        finding(detail="other")
    ]
    assert len(triage.open_items(triage.load(tmp_path))) == 2


def test_a_resolved_finding_that_recurs_is_filed_again(tmp_path):
    fix_findings.record_all([finding()], [], tmp_path)
    [item] = triage.open_items(triage.load(tmp_path))
    triage.resolve([item.id], "fixed", root=tmp_path)
    assert fix_findings.record_all([finding()], triage.load(tmp_path), tmp_path) == [finding()]


def test_optional_fields_are_written_only_when_set():
    assert dict(finding().fields()) == {
        "project": "carameli",
        "detail": "push-failed: agent/x: rejected",
    }
    full = dict(finding(key="k", evidence="e", command="c", agent="codex").fields())
    assert (full["key"], full["evidence"], full["command"], full["agent"]) == (
        "k",
        "e",
        "c",
        "codex",
    )


def test_at_repoints_the_evidence_only_when_there_is_some():
    one = finding(evidence="u")
    assert one.at("") is one
    assert one.at("C:/t").evidence == "C:/t"


def test_a_step_that_raises_is_a_finding_with_its_traceback_kept(tmp_path):
    journal = fix_findings.Journal(tmp_path)

    def broken():
        raise KeyError("head")

    assert journal.step("collect", broken, default="fallback") == "fallback"
    assert journal.crashed == ["collect"]
    [found] = journal.findings
    assert found.kind == "pass-step-crashed" and found.project == "devkit"
    assert "'collect' raised KeyError" in found.detail
    assert "Traceback" in Path(found.evidence).read_text(encoding="utf-8")
    assert journal.step("fine", lambda a, b=0: a + b, 1, b=2) == 3


def test_an_interrupt_is_not_swallowed_as_a_finding(tmp_path):
    """Named families, not `Exception`: stopping the pass must still stop it."""
    journal = fix_findings.Journal(tmp_path)

    def interrupted():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        journal.step("x", interrupted)


def test_the_pass_adds_its_own_error_classes(tmp_path):
    class ProjectError(Exception):
        pass

    def raises():
        raise ProjectError("unknown checkout")

    narrow = fix_findings.Journal(tmp_path)
    with pytest.raises(ProjectError):
        narrow.step("x", raises)
    wide = fix_findings.Journal(tmp_path, errors=(*fix_findings.STEP_ERRORS, ProjectError))
    assert wide.step("x", raises) is None and wide.crashed == ["x"]


def test_escalation_reads_back_open_then_resolved_by_the_problem_key(tmp_path):
    assert fix_findings.escalation("p", []) == fix_findings.NOT_ESCALATED
    fix_findings.record_all([finding(key="p")], [], tmp_path)
    items = triage.load(tmp_path)
    assert fix_findings.escalation("p", items).open
    assert fix_findings.escalation("q", items) == fix_findings.NOT_ESCALATED
    [item] = triage.open_items(items)
    triage.resolve([item.id], "fixed in the harness", root=tmp_path)
    state = fix_findings.escalation("p", triage.load(tmp_path))
    assert not state.open and state.resolved_at


def test_after_compares_iso_stamps_and_everything_follows_nothing():
    assert fix_findings.after("", "2026-01-01T00:00:00+00:00")
    assert fix_findings.after("2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00")
    assert not fix_findings.after("2026-01-02T00:00:00+00:00", "2026-01-01T00:00:00+00:00")
    assert fix_findings.after("a", "b"), "unparseable falls back to text order"


def test_the_triage_backlog_lists_both_new_event_names():
    """`harness_triage.TRIAGE_EVENTS` is the backlog; an event outside it is forensics."""
    assert {fix_findings.FINDING, fix_findings.FRICTION} <= set(triage.TRIAGE_EVENTS)


def test_evidence_is_kept_by_content_and_an_unwritable_place_is_no_evidence(tmp_path):
    first = fix_findings.evidence_file("Traceback: x", tmp_path, "step-collect")
    assert Path(first).read_text(encoding="utf-8") == "Traceback: x"
    assert fix_findings.evidence_file("Traceback: x", tmp_path, "step-collect") == first
    (tmp_path / "file").write_text("", encoding="utf-8")
    assert fix_findings.evidence_file("x", tmp_path / "file", "s") == ""


def test_a_rewritten_artifact_is_cited_by_a_copy_that_outlives_it(tmp_path):
    """55655d1a: a finding that cited `logs/installers.log` cited a file the next run
    rewrote. The copy is what it names; an artifact that cannot be read is cited as it
    is, and so is one whose copy cannot be written."""
    artifact = tmp_path / "installers.log"
    artifact.write_text("install-x.py: failed\n", encoding="utf-8")
    kept = fix_findings.kept(artifact, tmp_path, "installers")
    artifact.write_text("current\n", encoding="utf-8")
    assert Path(kept).read_text(encoding="utf-8") == "install-x.py: failed\n"
    assert fix_findings.kept(tmp_path / "gone.log", tmp_path, "x") == str(tmp_path / "gone.log")
    (tmp_path / "file").write_text("", encoding="utf-8")
    assert fix_findings.kept(artifact, tmp_path / "file", "x") == str(artifact)


def test_fresh_keeps_order_and_drops_duplicates_within_a_batch():
    a, b = finding(detail="a"), finding(detail="b")
    assert fix_findings.fresh([a, b, a], []) == [a, b]


def test_file_adds_to_a_journal_and_is_a_no_op_without_one(tmp_path):
    journal = fix_findings.Journal(tmp_path)
    fix_findings.file(journal, "merge-failed", "carameli", "#5: conflict", "u")
    assert journal.findings == [
        fix_findings.Finding("merge-failed", "carameli", "#5: conflict", evidence="u")
    ]
    fix_findings.file(None, "merge-failed", "carameli", "#5: conflict")


# The details as filed: 936fd498 and 91fa12f2 (two ships), e1463857 (a regate, where only
# git's hint survived), each quoting its own tree.
OWNERSHIP_DETAILS = [
    "roguelike worktree-serialized-squishing-breeze: push: C:/Users/alexa/vs-code/roguelike/"
    ".claude/worktrees/serialized-squishing-breeze/.git' is owned by: BUILTIN/Administrators "
    "(S-1-5-32-544) but the current user is: DESKTOP-B9FC8VP/alexa (S-1-5-21",
    "roguelike worktree-synchronous-marinating-hellman: push: rs/alexa/vs-code/roguelike/"
    ".claude/worktrees/synchronous-marinating-hellman/.git' is owned by: BUILTIN/Administrators "
    "(S-1-5-32-544) but the current user is: DESKTOP-B9FC8VP/alexa (S-1-5-21-30",
    "roguelike main -- FAILED to re-run the gate: git config --global --add safe.directory "
    "C:/Users/alexa/vs-code/roguelike",
]


def test_one_ownership_refusal_in_many_trees_is_one_ledger_group(tmp_path):
    """Each elevated tree filed a group of its own while one fix, pending, covered all."""
    journal = fix_findings.Journal(tmp_path)
    for detail in OWNERSHIP_DETAILS:
        fix_findings.file(journal, "ship-failed", "roguelike", detail)
    assert {f.detail for f in journal.findings} == {f"roguelike: {fix_findings.OWNERSHIP}"}
    assert len(fix_findings.record_all(journal.findings, [], tmp_path)) == 1
    kept = [Path(f.evidence).read_text(encoding="utf-8") for f in journal.findings]
    assert kept == OWNERSHIP_DETAILS, "the tree each one named is kept as its evidence"


def test_a_folded_detail_keeps_the_evidence_it_was_given(tmp_path):
    journal = fix_findings.Journal(tmp_path)
    fix_findings.file(journal, "ship-failed", "roguelike", OWNERSHIP_DETAILS[0], "C:/tree")
    assert journal.findings[0].evidence == "C:/tree"


def test_any_other_detail_is_filed_as_written():
    assert fix_findings.by_cause("carameli", "agent/x: rejected") == "agent/x: rejected"


def test_a_finding_that_names_a_branch_is_filed_settled_against_it(tmp_path):
    """4806bd8d: a complaint the corrected session fixed on its own branch is resolved
    pending that branch's merge; `fix_verify` reopens it if the branch never lands."""
    complaint = finding(kind="user-frustration", detail="why did you", settles_with="agent/y-0926")
    written = fix_findings.record_all([complaint, finding()], [], tmp_path)
    assert written == [complaint, finding()]
    items = triage.load(tmp_path)
    [still_open] = triage.open_items(items)
    assert still_open.detail == "push-failed: agent/x: rejected"
    [settled] = [i for i in items if i.event == triage.RESOLVED_EVENT]
    assert settled.fields["pr"] == "agent/y-0926"
    assert "went on to ship agent/y-0926" in settled.fields["note"]
    assert "settles_with" not in dict(complaint.fields()), "not a ledger field"


def test_settle_resolves_only_an_open_row_a_branch_names(tmp_path):
    complaint = finding(kind="user-frustration", detail="why did you")
    fix_findings.record_all([complaint, finding()], [], tmp_path)
    assert fix_findings.settle([complaint, finding()], tmp_path) == [], "no branch named"
    shipped = fix_findings.Finding(**{**vars(complaint), "settles_with": "agent/y-0926"})
    elsewhere = finding(detail="never filed", settles_with="agent/z-0926")
    assert len(fix_findings.settle([shipped, elsewhere], tmp_path)) == 1
    [still_open] = triage.open_items(triage.load(tmp_path))
    assert still_open.detail == "push-failed: agent/x: rejected"
