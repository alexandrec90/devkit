"""`scripts/friction_pending.py`: a row an open PR's detector stops is that PR's to settle."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import friction_pending as fp
import harness_triage as triage

# A PR's detector that retires every row, through a sibling it imports from the default
# branch -- which is how a real one reaches `fix_findings` and `session_transcripts`.
RETIRES_ALL = (
    "import fix_findings\n\n"
    "def outdated(items):\n"
    "    return [(i.id, fix_findings.FRICTION + ' gone') for i in items]\n"
)


def item(raw: str) -> triage.Item:
    return triage.Item("2026-09-27T03:58:26+00:00", "session-friction", {}, raw)


def done(stdout: str = "", code: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["x"], code, stdout, "")


def test_only_open_prs_that_change_the_detector_are_asked_with_their_copy_of_it():
    rows = [
        {"number": 420, "headRefName": "worktree-hazy", "files": [{"path": fp.DETECTOR}]},
        {"number": 422, "headRefName": "agent/other", "files": [{"path": "scripts/fix_send.py"}]},
        {"number": 423, "headRefName": "agent/gone", "files": [{"path": fp.DETECTOR}]},
    ]
    calls: list[tuple[str, ...]] = []

    def gh(*args):
        assert args[:2] == ("pr", "list") and "open" in args
        return done(json.dumps(rows))

    def git(*args):
        calls.append(args)
        if args[0] == "show":
            return done("source 420") if "worktree-hazy" in args[1] else done("", 128)
        return done()

    assert fp.detector_fixes(gh, git) == [fp.Fix("420", "worktree-hazy", "source 420")]
    assert ("fetch", "--quiet", "origin", "worktree-hazy") in calls
    assert ("show", f"origin/worktree-hazy:{fp.DETECTOR}") in calls
    assert not any("agent/other" in " ".join(c) for c in calls)


def test_a_gh_that_cannot_answer_names_no_fix():
    assert fp.detector_fixes(lambda *a: done("not json"), lambda *a: done()) == []
    assert fp.detector_fixes(lambda *a: done("", 1), lambda *a: done()) == []


def test_a_prs_detector_is_asked_beside_the_default_branchs_modules_and_leaves_no_trace():
    path_before = list(sys.path)
    rows = [item("a"), item("b")]
    found = fp.outdated_on(fp.Fix("420", "b", RETIRES_ALL), rows)
    assert found == [(rows[0].id, "session-friction gone"), (rows[1].id, "session-friction gone")]
    assert sys.path == path_before and "session_friction_pr420" not in sys.modules


def test_a_prs_detector_that_cannot_run_alone_answers_nothing():
    """It needs a sibling the PR also changed: its rows stay open for a session."""
    broken = "from session_transcripts import only_on_the_branch\n"
    assert fp.outdated_on(fp.Fix("9", "b", broken), [item("a")]) == []
    raising = "def outdated(items):\n    raise KeyError('x')\n"
    assert fp.outdated_on(fp.Fix("9", "b", raising), [item("a")]) == []
    assert "session_friction_pr9" not in sys.modules


def test_the_first_pr_to_stop_a_row_settles_it_and_the_note_names_the_pr():
    rows = [item("a")]
    fixes = [fp.Fix("420", "worktree-hazy", RETIRES_ALL), fp.Fix("423", "agent/y", RETIRES_ALL)]
    [(ref, note, pr)] = fp.pending(rows, fixes)
    assert (ref, pr) == (rows[0].id, "420")
    assert note.startswith("not merged yet: #420 (worktree-hazy) fixes the detector")
    assert fp.pending(rows, []) == []
