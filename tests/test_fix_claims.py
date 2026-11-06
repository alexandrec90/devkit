"""`scripts/fix_claims.py`: two PCs on `devkit.fixPass: dispatch` send one fixer, not two.

Real git against bare repositories, as in `tests/test_job_claims.py`; `send_all`'s wiring
of this is asserted through the pass in `tests/test_fix_pass.py`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from support import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import fix_claims
import fix_cycle
import fix_ledger
import fix_plan
import fix_stall
import job_claims as jc

HOST_ENV = fix_claims.fix_findings.harness_events.HOST_ENV


def run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def machine(root: Path, remotes: Path) -> Path:
    """A workspace root holding a checkout of each project."""
    root.mkdir()
    for project in ("devkit", "carameli"):
        path = root / project
        path.mkdir()
        run_git(path, "init", "-q")
        run_git(path, "remote", "add", "origin", (remotes / f"{project}.git").as_posix())
    return root


@pytest.fixture
def pcs(tmp_path, monkeypatch):
    remotes = tmp_path / "remotes"
    remotes.mkdir()
    for project in ("devkit", "carameli"):
        run_git(remotes, "init", "-q", "--bare", f"{project}.git")
    monkeypatch.setattr(fix_claims.job_claims, "wait_turn", lambda *a, **k: 0.0)
    return machine(tmp_path / "a", remotes), machine(tmp_path / "b", remotes), remotes


@pytest.fixture(autouse=True)
def host_restored(monkeypatch):
    """`turn` names the machine through `$DEVKIT_HOST`; this puts it back."""
    monkeypatch.setenv(HOST_ENV, "pc-a")


def turn(root: Path, host: str, writes: bool = True, busy: str = "") -> fix_claims.Turn:
    os.environ[HOST_ENV] = host
    return fix_claims.Turn(root, writes, None, busy)


def red(sha: str = "abc", project: str = "carameli") -> fix_plan.Failure:
    return fix_plan.Failure(
        fix_plan.PR, project, 5, "t", "u", head="agent/x", base="main", sha=sha, signature=("s",)
    )


def session(sha: str = "abc") -> fix_plan.Decision:
    return fix_plan.Decision(fix_plan.DISPATCH, "n", (red(sha),))


def harness() -> fix_plan.Decision:
    return fix_plan.Decision(fix_plan.UPSTREAM, "n", (red(), red(project="devkit")))


def test_two_pcs_at_the_same_red_send_one_fixer(pcs):
    a, b, _ = pcs
    assert turn(a, "pc-a").held(session()) == ""
    why = turn(b, "pc-b").held(session())
    assert why.startswith(fix_claims.CLAIMED_BY) and "pc-a" in why


def test_the_same_red_at_a_new_commit_is_a_new_claim(pcs):
    """As it is a new key in the ledger: a fixer that pushed and left it red."""
    a, b, _ = pcs
    assert turn(a, "pc-a").held(session("one")) == ""
    assert turn(b, "pc-b").held(session("two")) == ""


def test_a_claim_lives_in_the_project_it_is_about(pcs):
    a, _, remotes = pcs
    turn(a, "pc-a").held(session())
    claim_id, _kind = fix_claims.claim_of(session())
    assert claim_id.startswith("fix-")
    found = run_git(remotes / "carameli.git", "for-each-ref", jc.CLAIM).stdout
    assert claim_id in found


def test_the_harness_session_is_one_claim_in_devkit_whatever_its_failures(pcs):
    a, b, remotes = pcs
    assert fix_claims.claim_of(harness()) == (fix_claims.HARNESS_CLAIM, jc.HARNESS)
    assert fix_claims.repo_of(harness()) == fix_cycle.DEVKIT
    assert turn(a, "pc-a").held(harness()) == ""
    assert turn(b, "pc-b", busy="").held(harness()).startswith(fix_claims.CLAIMED_BY)
    found = run_git(remotes / "devkit.git", "for-each-ref", jc.CLAIM).stdout
    assert jc.claim_ref(fix_claims.HARNESS_CLAIM) in found


def test_the_harness_claim_goes_back_once_that_machines_session_has_finished(pcs):
    """Or the other PC would sit out the TTL for a session that ended hours ago."""
    a, b, _ = pcs
    assert turn(a, "pc-a").held(harness()) == ""
    turn(a, "pc-a", busy="tree").held(session("one"))
    assert turn(b, "pc-b").held(harness()).startswith(fix_claims.CLAIMED_BY), "still working"
    turn(a, "pc-a", busy="").held(session("two"))
    assert turn(b, "pc-b").held(harness()) == ""


def test_a_machine_sending_again_is_not_held_by_its_own_claim(pcs):
    """Its ledger decided that, and it is the one that knows its session died."""
    a, _, _ = pcs
    assert turn(a, "pc-a").held(session()) == ""
    assert turn(a, "pc-a").held(session()) == ""


def test_a_send_that_failed_hands_its_claim_back(pcs):
    a, b, _ = pcs
    first = turn(a, "pc-a")
    first.held(session())
    first.failed(session())
    assert turn(b, "pc-b").held(session()) == ""


def test_a_plan_pass_claims_nothing(pcs):
    a, _, remotes = pcs
    planning = turn(a, "pc-a", writes=False)
    assert planning.held(session()) == ""
    planning.failed(session())
    assert run_git(remotes / "carameli.git", "for-each-ref", jc.CLAIM).stdout == ""


def test_a_claim_that_could_not_be_made_fails_open_and_says_so(tmp_path, monkeypatch):
    """Every fixer on every project stopping for a push problem is far worse than a rare
    duplicate, so the send goes, and its record line carries why it went unclaimed."""
    monkeypatch.setattr(fix_claims.job_claims, "wait_turn", lambda *a, **k: 0.0)
    root = machine(tmp_path / "a", tmp_path / "nowhere")
    sending = turn(root, "pc-a")
    assert sending.held(session()) == ""
    assert "claim failed open" in sending.note(session())
    assert sending.note(session("other")) == ""


def test_a_won_claim_leaves_the_record_line_alone(pcs):
    a, _, _ = pcs
    sending = turn(a, "pc-a")
    sending.held(session())
    assert sending.note(session()) == ""


def test_nothing_to_send_costs_no_call_to_github(tmp_path, monkeypatch):
    """A pass with nothing to send on a bad network held `send` for a five-minute fetch."""
    called = []
    monkeypatch.setattr(fix_claims.job_claims, "release", lambda *a: called.append(a))
    turn(tmp_path, "pc-a")
    assert called == []


def test_the_fetch_for_the_harness_claim_is_bounded_and_respects_the_deadline(pcs, monkeypatch):
    a, _, _ = pcs
    seen = []

    def release(*_args):
        seen.append(fix_claims.sweep._until - fix_claims.time.monotonic())
        return False

    monkeypatch.setattr(fix_claims.job_claims, "release", release)
    os.environ[HOST_ENV] = "pc-a"
    fix_claims.Turn(a, True, None, "", lambda: 5.0).held(session())
    assert seen and 0 < seen[0] <= 5.0
    seen.clear()
    fix_claims.Turn(a, True, None, "", lambda: 900.0).held(session())
    assert 0 < seen[0] <= fix_claims.RELEASE_SECONDS


def test_the_claims_push_respects_the_deadline(pcs, monkeypatch):
    a, _, _ = pcs
    seen = []

    def claim(*_args):
        seen.append(fix_claims.sweep._until - fix_claims.time.monotonic())
        return jc.Attempt(jc.WON)

    monkeypatch.setattr(fix_claims.job_claims, "claim", claim)
    os.environ[HOST_ENV] = "pc-a"
    fix_claims.Turn(a, True, None, "tree", lambda: 5.0).held(session())
    assert seen and 0 < seen[0] <= 5.0


def test_a_wait_on_another_pcs_claim_is_no_stale_wait():
    """It ends on its own at the TTL."""
    assert fix_stall.tracked(f"{fix_claims.CLAIMED_BY} pc-a, which sent it")


def test_the_wait_for_a_turn_is_taken_once_a_pass_and_stops_at_the_deadline(pcs, monkeypatch):
    a, _, _ = pcs
    waits: list[tuple] = []
    monkeypatch.setattr(
        fix_claims.job_claims, "wait_turn", lambda free, limit: waits.append((free, limit))
    )
    sending = fix_claims.Turn(a, True, 4096, "", lambda: 30.0)
    sending.held(session("one"))
    sending.held(session("two"))
    assert waits == [(4096, 30.0)]


def test_the_digest_is_of_the_key_the_ledger_records(pcs):
    claim_id, kind = fix_claims.claim_of(session())
    assert kind == jc.FIX and len(claim_id) == len("fix-") + fix_ledger.KEY_DIGEST
    assert claim_id != fix_claims.claim_of(session("other"))[0]
