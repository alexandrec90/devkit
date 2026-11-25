"""`scripts/collector_lease.py`: one machine runs a `failover` collector, and it can change."""

from __future__ import annotations

import subprocess

import pytest
from support import load_script

lease = load_script("scripts/collector_lease.py")
config, jc = lease.config, lease.job_claims
sweep = jc.sweep

NOW = 1_800_000_000.0
C = config.Collector("sports", "collector", failover=True)
RUN = {"sports": config.RUN}


def run_git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def pc(tmp_path):
    """`pc(name, at)` is one pass on that machine; `pc.remote` is where they meet."""
    remote = tmp_path / "remote.git"
    run_git(tmp_path, "init", "-q", "--bare", str(remote))

    def one(name, at, assignment=RUN, free=32000, collector=C):
        path = tmp_path / name
        if not path.exists():
            path.mkdir()
            run_git(path, "init", "-q")
            run_git(path, "remote", "add", "origin", remote.as_posix())
        said: list[str] = []
        slept: list[float] = []
        acted = lease.resolve(
            [collector],
            assignment,
            path / "claims.json",
            lease.Claimant(
                sweep.git_for(path),
                name,
                said.append,
                now=lambda: at,
                free_mb=lambda: free,
                sleep=slept.append,
            ),
        )
        one.said, one.slept = said, slept
        return acted.get("sports")

    one.remote = remote
    return one


def test_exactly_one_machine_runs_it(pc):
    assert pc("a", NOW) == config.RUN
    assert pc("b", NOW + 5) == config.STOP
    assert "a runs it" in pc.said[0]


def test_a_holder_that_renews_is_never_taken_over(pc):
    for step in range(8):
        at = NOW + step * 900
        assert pc("a", at) == config.RUN
        assert pc("b", at + 5) == config.STOP


def test_a_silent_holder_is_taken_over_and_stands_by_when_it_returns(pc):
    assert pc("a", NOW) == config.RUN
    assert pc("b", NOW + 3599) == config.STOP
    assert pc("b", NOW + 3601) == config.RUN
    assert "taken over from a" in pc.said[0]
    assert pc("a", NOW + 3700) == config.STOP


def test_a_holder_cut_off_from_github_stops_before_the_claim_can_be_broken(pc):
    assert pc("a", NOW) == config.RUN
    pc.remote.rename(pc.remote.with_name("gone.git"))
    assert pc("a", NOW + lease.GRACE - 1) == config.RUN
    assert pc("a", NOW + lease.GRACE) == config.STOP
    # A pass every fifteen minutes must land between the grace and the TTL.
    assert lease.GRACE + 900 < jc.ttl_of(jc.COLLECTOR).total_seconds()


def test_a_machine_that_never_held_it_does_not_run_it_offline(pc):
    pc.remote.rename(pc.remote.with_name("gone.git"))
    assert pc("a", NOW) == config.STOP


def test_a_short_machine_waits_longest_yet_runs_it_when_nobody_else_does(pc):
    assert pc("a", NOW, free=100) == config.RUN
    assert pc.slept == [jc.MAX_DELAY_SECONDS]
    assert pc("a", NOW + 900, free=100) == config.RUN
    assert pc.slept == [], "a renewal waits for nobody"


def test_stop_here_gives_the_claim_back_so_another_takes_it_at_once(pc):
    assert pc("a", NOW) == config.RUN
    assert pc("a", NOW + 60, assignment={"sports": config.STOP}) == config.STOP
    assert pc("b", NOW + 70) == config.RUN


def test_a_collector_without_failover_is_untouched(pc):
    plain = config.Collector("sports", "collector")
    assert pc("a", NOW, collector=plain) == config.RUN
    assert pc("b", NOW, collector=plain) == config.RUN


def test_the_tray_sees_stop_on_a_standby_and_run_on_the_holder():
    standby, holder = {"sports": {"held": False}}, {"sports": {"held": True}}
    assert lease.effective([C], RUN, standby) == {"sports": config.STOP}
    assert lease.effective([C], RUN, holder) == RUN
    assert lease.effective([C], RUN, {}) == RUN


def test_each_collector_has_a_claim_of_its_own():
    assert lease.claim_id("sports") != lease.claim_id("news")
    # Machines on different devkits must agree on it, or two run the collector at once.
    assert lease.claim_id("sports") == "collector-sports"


def test_a_saved_record_reads_back_and_its_folder_is_made(tmp_path):
    path = tmp_path / "logs" / "claims.json"
    record = {"sports": {lease.HELD: True, lease.HOLDER: "a", lease.AT: NOW}}
    lease.save(path, record)
    assert lease.load(path) == record


def test_a_record_that_does_not_parse_is_empty(tmp_path):
    path = tmp_path / "x.json"
    path.write_text("[", encoding="utf-8")
    assert lease.load(path) == {}
    path.write_text("[]", encoding="utf-8")
    assert lease.load(path) == {}


@pytest.mark.parametrize(
    ("entry", "why"),
    [
        ({"service": "s", "failover": "yes"}, "`failover` must be"),
        ({"command": ["x"], "minutes": 5, "failover": True}, "is for a `service`"),
    ],
)
def test_a_bad_failover_is_refused(entry, why):
    assert why in config._entry("sports", entry)[1]
