"""`scripts/job_claims.py`: exactly one PC takes a job, with GitHub as the only arbiter.

The claims' whole subject is what git does at a push, so every test of one drives real
git against a bare repository standing in for GitHub, rather than a fake that would only
repeat what this file believes about it.
"""

from __future__ import annotations

import datetime as _dt
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from support import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import fix_ledger
import host_memory
import job_claims as jc
import sweep

NOW = 1_800_000_000.0
HOUR = 3600.0


def run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def clone(root: Path, name: str, remote: Path) -> sweep.Git:
    """A machine's checkout: all a claim needs of one is a repo with `origin` set."""
    path = root / name
    path.mkdir()
    run_git(path, "init", "-q")
    run_git(path, "remote", "add", "origin", remote.as_posix())
    return sweep.git_for(path)


@pytest.fixture
def pcs(tmp_path):
    """Two machines and the remote they share."""
    remote = tmp_path / "remote.git"
    run_git(tmp_path, "init", "-q", "--bare", str(remote))
    return clone(tmp_path, "pc-a", remote), clone(tmp_path, "pc-b", remote), remote


def on_remote(remote: Path, ref: str) -> str:
    return run_git(remote, "rev-parse", "--verify", "--quiet", ref).stdout.strip()


# --- exactly one wins ---------------------------------------------------------------------


def test_pushing_the_same_sha_twice_succeeds_twice_which_is_why_a_claim_differs_per_machine(pcs):
    """The reasoning, held to git. Two PCs making an identical commit would each be told
    "up to date" by the second push, with the lease never consulted, and both would
    believe they had won."""
    a, _, remote = pcs
    ref = jc.claim_ref("same")
    commit = jc.make_commit(a, "claim", NOW, nonce="fixed")
    for _attempt in range(2):
        done = a("push", "--no-verify", f"--force-with-lease={ref}:", "origin", f"{commit}:{ref}")
        assert done.returncode == 0, "and so the lease would decide nothing"
    assert on_remote(remote, ref) == commit


def test_two_machines_in_one_second_make_two_commits(pcs):
    """Host and clock can both match: a renamed clone, a test, two identical boxes."""
    a, b, _ = pcs
    assert jc.make_commit(a, "claim x", NOW) != jc.make_commit(b, "claim x", NOW)
    assert jc.make_commit(a, "claim x", NOW, "n") == jc.make_commit(b, "claim x", NOW, "n"), (
        "the nonce is the difference: without it these two are the identical commit"
    )


def test_the_first_push_wins_and_the_second_is_told_who_holds_it(pcs):
    a, b, remote = pcs
    assert jc.claim(a, "job", jc.RUN, "pc-a", NOW).won
    second = jc.claim(b, "job", jc.RUN, "pc-b", NOW + 1)
    assert second.outcome == jc.LOST and second.holder == "pc-a"
    assert on_remote(remote, jc.claim_ref("job"))


def test_two_machines_racing_never_both_win(pcs):
    a, b, _ = pcs
    for number in range(6):
        with ThreadPoolExecutor(2) as pool:
            jobs = [
                pool.submit(jc.claim, git, f"race-{number}", jc.RUN, host, NOW)
                for git, host in ((a, "pc-a"), (b, "pc-b"))
            ]
            attempts = [job.result() for job in jobs]
        won = [attempt for attempt in attempts if attempt.won]
        assert len(won) == 1, attempts
        assert all(a.outcome in (jc.WON, jc.LOST, jc.FAILED) for a in attempts)


def test_a_claim_carries_no_files_and_names_its_holder(pcs):
    a, _, remote = pcs
    jc.claim(a, "job", jc.AGENT, "pc-a", NOW)
    ref = jc.claim_ref("job")
    assert run_git(remote, "rev-parse", f"{ref}^{{tree}}").stdout.strip() == jc.EMPTY_TREE
    assert jc.holder(a, ref).host == "pc-a"
    assert jc.holder(a, ref).at == NOW


# --- a failed push is not a win -------------------------------------------------------------


def test_a_network_failure_is_neither_won_nor_lost(tmp_path):
    """It must not be read as a win: a `run` task would then run on a PC that does not hold
    the claim, while the PC that does runs it too."""
    git = clone(tmp_path, "pc-a", tmp_path / "nowhere.git")
    attempt = jc.claim(git, "job", jc.RUN, "pc-a", NOW)
    assert attempt.outcome == jc.FAILED and not attempt.won
    assert attempt.said


def test_a_refused_ref_that_then_cannot_be_read_is_failed_not_won(pcs):
    """Refused, then unreadable: it was released in between, or the network went."""
    a, _, _ = pcs

    def git(*args, env=None):
        if args[0] == "fetch":
            return subprocess.CompletedProcess(args, 1, "", "no network")
        if args[0] == "push":
            return subprocess.CompletedProcess(args, 1, "", " ! [rejected] x (stale info)")
        return a(*args, env=env)

    attempt = jc.claim(git, "job", jc.RUN, "pc-a", NOW)
    assert attempt.outcome == jc.FAILED


def test_a_commit_that_could_not_be_made_is_failed():
    def git(*args, env=None):
        return subprocess.CompletedProcess(args, 128, "", "no")

    assert jc.claim(git, "job", jc.RUN, "pc-a", NOW).outcome == jc.FAILED
    assert not jc.mark_done(git, jc.done_ref("job"), "done", NOW)


# --- the pre-push hooks ---------------------------------------------------------------------


def test_a_claim_passes_a_failing_pre_push_hook_that_would_stop_a_branch(pcs, tmp_path):
    """A root commit reads to `pre-commit`'s pre-push stage as publishing everything, so a
    machine whose hook is that stage would spend minutes of gate on an empty tree, and a
    failure there would be read as a lost claim. The control is what makes it a test."""
    a, _, _remote = pcs
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "pre-push").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    a("config", "core.hooksPath", hooks.as_posix())
    commit = jc.make_commit(a, "x", NOW)
    assert a("push", "origin", f"{commit}:refs/heads/branch").returncode != 0, "the control"
    assert jc.claim(a, "job", jc.RUN, "pc-a", NOW).won


def test_the_hook_bypass_cannot_reach_a_branch_or_a_tag(pcs):
    a, _, _ = pcs
    for ref in ("refs/heads/main", "refs/tags/v1.0.0"):
        with pytest.raises(ValueError):
            jc._push(a, "abc", ref, "")
    with pytest.raises(ValueError):
        jc.prune(a, ["refs/heads/main"])


# --- a claim outlives its holder only for a TTL -----------------------------------------------


def test_only_a_win_is_a_win():
    assert jc.Attempt(jc.WON).won
    assert not jc.Attempt(jc.LOST, holder="pc-b").won
    assert not jc.Attempt(jc.FAILED, said="no network").won


def test_a_claim_expires_at_its_own_kinds_ttl_and_no_sooner():
    held = jc.Ref(jc.claim_ref("job"), "1", NOW, "claim fix by pc-a")
    ttl = jc.ttl_of(jc.FIX).total_seconds()
    assert not jc.expired(held, jc.FIX, NOW + ttl - 1)
    assert jc.expired(held, jc.FIX, NOW + ttl)


def test_a_claim_within_its_ttl_stands(pcs):
    a, b, _ = pcs
    jc.claim(a, "job", jc.RUN, "pc-a", NOW)
    just_inside = NOW + jc.ttl_of(jc.RUN).total_seconds() - 1
    assert jc.claim(b, "job", jc.RUN, "pc-b", just_inside).outcome == jc.LOST


def test_a_stale_claim_is_broken_by_exactly_one_machine(pcs, tmp_path):
    a, b, remote = pcs
    c = clone(tmp_path, "pc-c", remote)
    jc.claim(a, "job", jc.RUN, "pc-a", NOW)
    late = NOW + jc.ttl_of(jc.RUN).total_seconds() + 1
    first = jc.claim(b, "job", jc.RUN, "pc-b", late)
    assert first.won and first.broke == "pc-a"
    second = jc.claim(c, "job", jc.RUN, "pc-c", late)
    assert second.outcome == jc.LOST and second.holder == "pc-b"


def test_a_breaker_whose_lease_went_stale_loses_rather_than_overwrites(pcs):
    """Two machines read one expired claim and both push: only the first lease is true."""
    a, b, remote = pcs
    jc.claim(a, "job", jc.RUN, "pc-a", NOW)
    ref = jc.claim_ref("job")
    stale = jc.holder(a, ref)
    assert jc._push(a, jc.make_commit(a, "claim by pc-a", NOW + 5), ref, stale.sha)[0] == jc.WON
    late = jc.make_commit(b, "claim by pc-b", NOW + 6)
    assert jc._push(b, late, ref, stale.sha)[0] == jc.LOST
    assert on_remote(remote, ref) != late


def test_a_holder_asking_again_keeps_its_claim_with_a_fresh_stamp(pcs):
    a, _, _ = pcs
    jc.claim(a, "job", jc.RUN, "pc-a", NOW)
    again = jc.claim(a, "job", jc.RUN, "pc-a", NOW + 100)
    assert again.won and not again.broke
    assert jc.holder(a, jc.claim_ref("job")).at == NOW + 100


def test_each_kind_of_job_has_a_ttl_and_a_fixers_is_when_the_ledger_looks_again():
    for kind in (jc.RUN, jc.AGENT, jc.ELEVATED, jc.TIDY, jc.FIX, jc.HARNESS):
        assert jc.ttl_of(kind) == jc.TTL[kind]
    assert jc.ttl_of("unknown") == jc.DEFAULT_TTL
    assert jc.TTL[jc.FIX] == fix_ledger.RESEND_AFTER


# --- release, done, prune -----------------------------------------------------------------------


def test_only_the_holder_can_release(pcs):
    a, b, remote = pcs
    jc.claim(a, "job", jc.RUN, "pc-a", NOW)
    assert not jc.release(b, "job", "pc-b")
    assert on_remote(remote, jc.claim_ref("job"))
    assert jc.release(a, "job", "pc-a")
    assert not on_remote(remote, jc.claim_ref("job"))
    assert jc.claim(b, "job", jc.RUN, "pc-b", NOW).won


def test_release_of_a_claim_that_was_never_made_says_so(pcs):
    assert not jc.release(pcs[0], "nothing", "pc-a")


def test_done_is_recorded_once_and_a_second_recording_is_still_done(pcs):
    a, b, remote = pcs
    ref = jc.done_ref("abc")
    assert jc.mark_done(a, ref, "done p/x by pc-a: passed", NOW)
    assert jc.mark_done(b, ref, "done p/x by pc-b: passed", NOW + 1)
    assert run_git(remote, "log", "-1", "--format=%s", ref).stdout.strip().endswith("pc-a: passed")


def test_a_done_push_the_server_rejected_is_not_done(pcs):
    """A `[remote rejected]` reads like a lost race, and the ref is not there: said to be
    done, the claim would go back and another PC would run the job again."""
    a, _, remote = pcs
    hook = remote / "hooks" / "update"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text("#!/bin/sh\necho fatal >&2\nexit 1\n", encoding="utf-8", newline="\n")
    run_git(remote, "config", "core.hooksPath", "hooks")
    assert not jc.mark_done(a, jc.done_ref("abc"), "done p/x by pc-a: passed", NOW)
    assert not on_remote(remote, jc.done_ref("abc"))


def test_a_ref_is_asked_of_the_server_not_the_last_fetch(pcs):
    a, b, _ = pcs
    assert jc.on_server(b, jc.done_ref("abc")) == ""
    jc.mark_done(a, jc.done_ref("abc"), "done p/x by pc-a: passed", NOW)
    assert jc.on_server(b, jc.done_ref("abc"))


def test_a_server_that_cannot_be_asked_is_neither_yes_nor_no(tmp_path):
    git = clone(tmp_path, "pc-a", tmp_path / "nowhere.git")
    assert jc.on_server(git, jc.done_ref("abc")) is None


def test_a_stamp_moves_a_ref_only_from_what_it_was_read_as(pcs):
    a, _, _ = pcs
    ref = jc.handed_ref("abc")
    assert jc.stamp(a, ref, "handed p/x by pc-a: attempt 1", "", NOW)
    first = jc.on_server(a, ref)
    assert jc.stamp(a, ref, "offered p/x by pc-a: attempt 1", first, NOW + 1)
    assert not jc.stamp(a, ref, "handed p/x by pc-b: attempt 2", first, NOW + 2), "stale"


def test_a_heartbeat_is_said_at_most_daily_and_reports_when_it_was_last_seen(pcs):
    a, _, remote = pcs
    ref = jc.host_ref("pc-a")
    assert jc.heartbeat(a, [], "pc-a", NOW) is None, "a host never seen has no last time"
    first = on_remote(remote, ref)
    assert first
    fresh = [jc.Ref(ref, first, NOW, "host pc-a seen")]
    assert jc.heartbeat(a, fresh, "pc-a", NOW + 3600) is None
    assert on_remote(remote, ref) == first
    assert jc.heartbeat(a, fresh, "pc-a", NOW + 2 * 86_400) == NOW
    assert on_remote(remote, ref) != first


def test_a_host_silent_for_thirty_days_stops_counting():
    refs = [
        jc.Ref(jc.host_ref("near"), "1", NOW - 86_400),
        jc.Ref(jc.host_ref("far"), "2", NOW - jc.HOST_UNSEEN_AFTER.total_seconds()),
        jc.Ref(jc.done_ref("abc"), "3", NOW),
    ]
    assert jc.live_hosts(refs, NOW) == {"near"}


def test_refs_are_keyed_by_what_follows_their_prefix():
    refs = [jc.Ref(jc.handed_ref("abc/pc-a"), "1", NOW), jc.Ref(jc.done_ref("x"), "2", NOW)]
    assert set(jc.keys_under(refs, jc.HANDED)) == {"abc/pc-a"}


def test_done_per_host_is_its_own_ref():
    assert jc.done_ref("abc") == "refs/devkit/done/abc"
    assert jc.done_ref("abc", "pc-a") == "refs/devkit/done/abc/pc-a"


def test_what_the_last_fetch_brought_reads_back(pcs):
    a, b, _ = pcs
    jc.mark_done(a, jc.done_ref("abc"), "done p/x by pc-a: passed", NOW)
    jc.mark_done(a, jc.done_ref("def", "pc-a"), "done p/y by pc-a: passed", NOW)
    jc.claim(a, "job", jc.RUN, "pc-a", NOW)
    assert b("fetch", "--quiet", "origin", f"+{jc.REFS}*:{jc.REFS}*").returncode == 0
    refs = jc.read_refs(b)
    assert jc.done_ids(refs) == {"abc", "def/pc-a"}
    held = [ref for ref in refs if ref.name == jc.claim_ref("job")]
    assert held[0].host == "pc-a" and held[0].at == NOW


def test_a_line_of_the_wrong_shape_is_skipped():
    good = "refs/devkit/claim/a\0abc\x00123\0claim run by pc at now"
    assert [ref.name for ref in jc.parse_refs(f"junk\n{good}\nx\0y\0z\0w\n")] == [
        "refs/devkit/claim/a"
    ]


def test_old_claims_are_pruned_and_nothing_else(pcs):
    a, _, remote = pcs
    old, fresh = jc.claim_ref("old"), jc.claim_ref("new")
    refs = [
        jc.Ref(old, "1", NOW - jc.PRUNE_AFTER.total_seconds() - 1),
        jc.Ref(fresh, "2", NOW - 10),
        jc.Ref(jc.done_ref("x"), "3", NOW - 10 * jc.PRUNE_AFTER.total_seconds()),
    ]
    assert jc.prunable(refs, NOW) == [old]
    assert jc.prune(a, [])
    jc.claim(a, "old", jc.RUN, "pc-a", NOW)
    assert jc.prune(a, [old])
    assert not on_remote(remote, old)


# --- who may claim, and how soon ---------------------------------------------------------------


FLOOR = host_memory.SESSION_MB + host_memory.FLOOR_MB


def test_a_machine_under_the_hold_does_not_claim_at_all():
    machine = jc.Machine("pc", free_mb=FLOOR - 1)
    assert jc.refusal(jc.Needs(), machine).startswith("held for memory")
    assert jc.refusal(jc.Needs(memory=False), machine) == ""
    assert jc.refusal(jc.Needs(), jc.Machine("pc", free_mb=FLOOR)) == ""


def test_a_probe_that_failed_holds_nothing_back():
    """The stance `host_memory` documents: a broken probe must not stop every job."""
    assert jc.refusal(jc.Needs(), jc.Machine("pc", free_mb=None)) == ""


@pytest.mark.parametrize(
    ("needs", "machine", "word"),
    [
        (jc.Needs(docker=True), jc.Machine("pc"), "docker"),
        (jc.Needs(checkout="carameli"), jc.Machine("pc"), "carameli"),
        (jc.Needs(idle_seconds=600.0), jc.Machine("pc", idle_seconds=5.0), "idle"),
    ],
)
def test_a_machine_without_a_capability_does_not_claim(needs, machine, word):
    assert word in jc.refusal(needs, machine)


def test_a_machine_with_each_capability_may_claim():
    machine = jc.Machine("pc", docker=True, checkouts=frozenset({"carameli"}), idle_seconds=900.0)
    needs = jc.Needs(docker=True, checkout="carameli", idle_seconds=600.0)
    assert jc.refusal(needs, machine) == ""


def test_more_free_memory_waits_less():
    """Which is the whole of how work drifts to the emptiest machine."""
    delays = [jc.delay_seconds(mb) for mb in (FLOOR, 6000, 9000, 12000, jc.PROMPT_MB)]
    assert delays == sorted(delays, reverse=True)
    assert delays[0] == jc.MAX_DELAY_SECONDS and delays[-1] == 0.0
    assert jc.delay_seconds(jc.PROMPT_MB * 2) == 0.0


def test_a_failed_probe_waits_half_and_the_hold_waits_longest():
    assert jc.delay_seconds(None) == jc.MAX_DELAY_SECONDS / 2
    assert jc.delay_seconds(10) == jc.MAX_DELAY_SECONDS


def test_the_longest_wait_is_well_inside_the_shortest_cycle():
    assert jc.MAX_DELAY_SECONDS < _dt.timedelta(minutes=5).total_seconds() / 2


def test_the_wait_sleeps_once_up_to_its_limit():
    slept: list[float] = []
    assert jc.wait_turn(None, slept.append) == jc.MAX_DELAY_SECONDS / 2
    assert jc.wait_turn(None, slept.append, limit=3.0) == 3.0
    assert jc.wait_turn(None, slept.append, limit=-1.0) == 0.0
    assert jc.wait_turn(jc.PROMPT_MB, slept.append) == 0.0
    assert slept == [jc.MAX_DELAY_SECONDS / 2, 3.0]
