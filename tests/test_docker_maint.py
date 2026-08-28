"""Tests for `scripts/docker-maint.py`'s stack modes and its argument plumbing.

The daemon modes (`restart-engine`, `fix`, `prune`) are still not *executed* here:
they kill Docker Desktop and compact a WSL2 VHDX, so there is nothing to assert that
does not involve doing it. Their **decisions** are a different matter and are covered,
because `prune_verdict` was factored out to be pure precisely so the choice to stop a
machine's containers at 4am could be tested without stopping any. What IS tested is
everything the `up`/`down` hoist added, plus the two invariants that would be
expensive to get wrong:

  - arguments reach the delegate (before this, `find_delegate`'s spawn passed none, so
    a hoisted "Docker: Start Stack" would have dropped `--build` in every project that
    ships its own `docker-up.py` — a stack that comes up healthy running a stale image);
  - `down` never grows `-v`, because this runs from a one-click task over a project
    picker and named volumes hold real dev databases.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import timedelta

import pytest
from support import load_script

docker_maint = load_script("scripts/docker-maint.py")


@pytest.fixture
def stack(tmp_path, monkeypatch):
    """A cwd that looks like a project with a compose stack."""
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def commands(monkeypatch):
    """Capture what `run()` would have executed instead of executing it."""
    seen: list[list[str]] = []
    monkeypatch.setattr(docker_maint, "run", lambda cmd, **kw: seen.append(list(cmd)) or 0)
    return seen


# --- argument parsing -------------------------------------------------------


def test_split_args_extracts_the_mode_and_forwards_the_rest():
    assert docker_maint.split_args(["up", "--build"]) == ("up", ["--build"], False)


def test_split_args_consumes_generic_from_anywhere():
    """It is devkit's own flag, not the delegate's, so it must not be forwarded."""
    mode, forwarded, generic_only = docker_maint.split_args(["--generic", "down", "--timeout", "5"])
    assert (mode, forwarded, generic_only) == ("down", ["--timeout", "5"], True)


def test_split_args_rejects_an_unknown_mode():
    assert docker_maint.split_args(["upp"])[0] is None
    assert docker_maint.split_args([])[0] is None


def test_an_unknown_mode_is_a_usage_error(capsys):
    assert docker_maint.main(["upp"]) == 2
    assert "usage: docker-maint.py" in capsys.readouterr().err


def test_split_args_is_pure():
    original = ["up", "--build"]
    docker_maint.split_args(original)
    assert original == ["up", "--build"]


# --- delegation -------------------------------------------------------------


def test_a_projects_own_script_wins_and_receives_the_arguments(stack, monkeypatch):
    """The regression this hoist would otherwise have shipped.

    carameli's `docker-up.py --build` became a workspace task; if the fixed `--build`
    stops arriving, the task still exits 0 and still brings a stack up — just not the
    one the user's edits are in.
    """
    script = stack / "scripts" / "docker-up.py"
    script.parent.mkdir()
    script.write_text("")
    spawned: dict = {}

    def fake_run(cmd, **kwargs):
        spawned["cmd"] = list(cmd)
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(docker_maint.subprocess, "run", fake_run)

    assert docker_maint.main(["up", "--build"]) == 0
    assert spawned["cmd"] == [sys.executable, str(script), "--build"]


def test_generic_skips_a_delegate_that_exists(stack, commands):
    (stack / "scripts").mkdir()
    (stack / "scripts" / "docker-down.py").write_text("")
    docker_maint.main(["down", "--generic"])
    assert commands == [["docker", "compose", "down"]]


def test_the_delegates_exit_code_is_the_answer(stack, monkeypatch):
    script = stack / "scripts" / "docker-down.py"
    script.parent.mkdir()
    script.write_text("")
    monkeypatch.setattr(
        docker_maint.subprocess, "run", lambda cmd, **kw: type("R", (), {"returncode": 3})()
    )
    assert docker_maint.main(["down"]) == 3


def test_every_mode_has_a_delegate_entry_and_a_generic_fallback():
    """`find_delegate` indexes DELEGATES by mode and `GENERIC` is indexed the same way.

    A mode listed in MODES but absent from either dict is a KeyError at click time,
    which reads as a crash rather than as the missing wiring it is.
    """
    assert set(docker_maint.MODES) == set(docker_maint.DELEGATES) == set(docker_maint.GENERIC)


# --- the generic stack fallbacks --------------------------------------------


def test_generic_up_forwards_what_it_was_given(stack, commands):
    docker_maint.main(["up", "--build"])
    assert commands == [["docker", "compose", "up", "-d", "--build"]]


def test_generic_down_stops_containers_only(stack, commands):
    docker_maint.main(["down"])
    assert commands == [["docker", "compose", "down"]]


def test_generic_down_never_destroys_volumes(stack, commands):
    """Not a style preference. A named volume here is a dev database, and this runs
    from a picker where the wrong entry is one keystroke away."""
    docker_maint.main(["down"])
    assert not ({"-v", "--volumes"} & set(commands[0]))


def test_a_repo_with_no_compose_file_is_named_rather_than_handed_to_compose(
    tmp_path, monkeypatch, capsys
):
    """devkit and a `bare` preset have no stack, and the task is one picker over every
    checkout — so landing on one is ordinary. Exit 2 (a usage answer), never 0."""
    monkeypatch.chdir(tmp_path)
    calls: list = []
    monkeypatch.setattr(docker_maint, "run", lambda cmd, **kw: calls.append(cmd) or 0)

    for mode in ("up", "down"):
        assert docker_maint.main([mode]) == 2
    assert calls == [], "compose was invoked for a repo that has no compose file"
    assert "no compose file" in capsys.readouterr().err


@pytest.mark.parametrize(
    "name", ["docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"]
)
def test_compose_file_recognises_every_supported_spelling(tmp_path, name):
    (tmp_path / name).write_text("")
    assert docker_maint.compose_file(tmp_path) == tmp_path / name


def test_compose_file_returns_none_when_there_is_no_stack(tmp_path):
    assert docker_maint.compose_file(tmp_path) is None


# --- the unattended prune guard ------------------------------------------------
# `prune` has two halves with very different blast radii. `docker system prune` frees
# space inside the VM and returns nothing to Windows -- the VHDX does not shrink when
# its contents do. `Optimize-VHD` is what returns it, and it needs `wsl --shutdown`,
# which stops every running container. `--idle-only` is that distinction made operable
# for the scheduled caller; these pin it, since the daemon modes themselves cannot be
# exercised without actually doing it.


def _ps(monkeypatch, stdout: str = "", returncode: int = 0, boom: bool = False):
    """Stand in for `docker ps -q`."""

    def fake_run(argv, **_kw):
        if boom:
            raise OSError("docker is not on PATH")
        return subprocess.CompletedProcess(argv, returncode, stdout, "")

    monkeypatch.setattr(docker_maint.subprocess, "run", fake_run)


def test_running_containers_counts_the_ids(monkeypatch):
    _ps(monkeypatch, "abc123\ndef456\n")
    assert docker_maint.running_containers() == 2


def test_running_containers_ignores_blank_lines(monkeypatch):
    _ps(monkeypatch, "\n\nabc123\n\n")
    assert docker_maint.running_containers() == 1


def test_an_idle_engine_reports_zero(monkeypatch):
    _ps(monkeypatch, "")
    assert docker_maint.running_containers() == 0


@pytest.mark.parametrize("kwargs", [{"returncode": 1}, {"boom": True}])
def test_an_unreachable_engine_is_not_reported_as_idle(monkeypatch, kwargs):
    """-1 rather than 0, so `--idle-only` fails toward *not* pruning. Guessing zero
    would license `wsl --shutdown` against a machine that might be mid-run."""
    _ps(monkeypatch, **kwargs)
    assert docker_maint.running_containers() == -1


def test_idle_only_does_nothing_while_containers_are_up(monkeypatch, capsys):
    """The scheduled case, on a machine with disk to spare. Stopping twelve containers
    at 4am to reclaim disk nobody needs is not a trade anything should make unattended.

    `free_gb` is stubbed rather than left to read the real volume: the skip is now
    conditional on free space, so a developer whose own disk happened to be under the
    floor would watch this assert the opposite of what it is named for."""
    monkeypatch.setattr(docker_maint, "running_containers", lambda: 12)
    monkeypatch.setattr(docker_maint, "free_gb", lambda *_a, **_kw: 400.0)
    monkeypatch.setattr(
        docker_maint, "docker_info_ok", lambda *_a, **_kw: pytest.fail("touched the engine")
    )
    assert docker_maint.generic_prune(idle_only=True) == 0
    printed = capsys.readouterr().out
    assert "SKIPPED" in printed and "12 containers up" in printed


def test_idle_only_compacts_anyway_when_the_disk_is_under_the_floor(monkeypatch, capsys):
    """The reversal, end to end: the same twelve containers, a disk low enough that
    `reconcile` is deleting open-PR boxes, and the prune goes ahead. This is the test
    that fails if the escalation is reverted."""
    monkeypatch.setattr(docker_maint, "running_containers", lambda: 12)
    monkeypatch.setattr(docker_maint, "free_gb", lambda *_a, **_kw: 12.0)
    monkeypatch.setattr(docker_maint, "docker_info_ok", lambda *_a, **_kw: False)
    monkeypatch.setattr(docker_maint, "start_docker", lambda: None)
    monkeypatch.setattr(docker_maint, "poll_engine", lambda **_kw: False)
    assert docker_maint.generic_prune(idle_only=True) == 1
    printed = capsys.readouterr().out
    assert "SKIPPED" not in printed
    assert "Proceeding" in printed and "12.0 GB free" in printed


def test_idle_only_skips_when_the_engine_cannot_be_asked(monkeypatch, capsys):
    monkeypatch.setattr(docker_maint, "running_containers", lambda: -1)
    monkeypatch.setattr(
        docker_maint, "docker_info_ok", lambda *_a, **_kw: pytest.fail("touched the engine")
    )
    assert docker_maint.generic_prune(idle_only=True) == 0
    assert "could not be asked" in capsys.readouterr().out


def test_a_skipped_prune_is_a_success_not_a_failure(monkeypatch):
    """It reports 0 so a scheduled run that correctly declines does not look broken.
    "Nothing to do right now" is the expected outcome most nights.

    `free_gb` is stubbed, and that is a fix rather than boilerplate: this test used to
    stub only the container count, so `prune_verdict` read the **host's** free disk and
    the assertion held only while this machine happened to sit above the floor. On
    2026-08-21 it dropped below and the test went on to `sc start` the Docker service and
    wait 90 seconds for a real engine. A unit test that reaches the machine passes for a
    reason that has nothing to do with the code, which is the same as not covering it.
    The `pytest.fail` tripwire below is the neighbouring test's, for the same reason."""
    monkeypatch.setattr(docker_maint, "running_containers", lambda: 3)
    monkeypatch.setattr(docker_maint, "free_gb", lambda *_a, **_kw: 200.0)
    monkeypatch.setattr(
        docker_maint, "docker_info_ok", lambda *_a, **_kw: pytest.fail("touched the engine")
    )
    assert docker_maint.generic_prune(idle_only=True) == 0


def test_an_interactive_prune_never_consults_the_guard(monkeypatch):
    """A human choosing this from the task list has already decided; asking again would
    make the one-click action refuse for a reason it cannot explain there."""
    monkeypatch.setattr(
        docker_maint, "running_containers", lambda: pytest.fail("guarded an interactive run")
    )
    monkeypatch.setattr(docker_maint, "docker_info_ok", lambda *_a, **_kw: False)
    monkeypatch.setattr(docker_maint, "start_docker", lambda: None)
    monkeypatch.setattr(docker_maint, "poll_engine", lambda *_a, **_kw: False)
    assert docker_maint.generic_prune() == 1


def test_the_prune_never_removes_containers_or_volumes(monkeypatch, tmp_path):
    """The disk half must never cost a rebuild.

    `docker system prune -af`, which this used to run, deletes stopped containers first
    and then every image that leaves unreferenced. Paired with the 03:30 `stop-idle`
    job it therefore undid that job's careful `stop`-not-`down` half an hour later:
    carameli was parked at 03:30 and by 04:00 had no containers and no `carameli-*`
    images at all, so the next "Docker: Start Stack" was a cold rebuild. `image prune
    -a` counts a stopped container as a reference, which is the whole fix -- a parked
    stack survives, a genuinely orphaned layer still goes.

    Asserted on the verbs rather than one exact argv, so any later line reaching for
    `system prune` or `container prune` fails here whatever else it changes.
    """
    calls: list[list[str]] = []
    monkeypatch.setattr(docker_maint, "run", lambda cmd, **_kw: (calls.append(list(cmd)), 0)[1])
    monkeypatch.setattr(docker_maint, "docker_info_ok", lambda *_a, **_kw: True)
    monkeypatch.setattr(docker_maint, "stop_docker", lambda: None)
    monkeypatch.setattr(docker_maint, "start_docker", lambda: None)
    monkeypatch.setattr(docker_maint, "poll_engine", lambda *_a, **_kw: True)
    # An empty tmp home holds no VHDX, so the Optimize-VHD branch reports [skip]
    # instead of shelling out to PowerShell against this machine's real disk.
    monkeypatch.setattr(docker_maint.Path, "home", staticmethod(lambda: tmp_path))

    assert docker_maint.generic_prune() == 0
    assert calls, "the prune ran nothing at all"

    for argv in calls:
        assert "--volumes" not in argv, f"named volumes are dev databases: {argv}"
        assert argv[:3] != ["docker", "system", "prune"], f"removes stopped containers: {argv}"
        assert argv[:3] != ["docker", "container", "prune"], f"costs a rebuild: {argv}"
        assert argv[:3] != ["docker", "volume", "prune"], f"named volumes are data: {argv}"

    # And it still reclaims: the two lines that are where the GB actually are.
    assert ["docker", "image", "prune", "-af"] in calls
    assert ["docker", "builder", "prune", "-af"] in calls


def test_the_flag_reaches_the_generic_prune(monkeypatch):
    """`--idle-only` is forwarded like any other argument, so it has to be read off the
    forwarded list rather than parsed as a mode."""
    seen: list[bool] = []
    monkeypatch.setattr(docker_maint, "find_delegate", lambda _mode: None)
    monkeypatch.setattr(
        docker_maint, "generic_prune", lambda idle_only=False: (seen.append(idle_only), 0)[1]
    )
    docker_maint.main(["prune", "--generic", "--idle-only"])
    docker_maint.main(["prune", "--generic"])
    assert seen == [True, False]


# --- the unattended stop-idle pass ----------------------------------------------
# `stop-idle` runs nightly against every compose stack at once, so its two invariants
# are the expensive-to-get-wrong ones: it stops nothing that has not opted in, and
# every ambiguity -- unreadable engine, unreadable netstat, unparseable start time --
# reads as "in use" rather than as "idle". The parsers are pure and tested as such;
# the orchestration is tested with every spawn faked out.


def test_published_ports_reads_every_mapping_and_nothing_else():
    field = "127.0.0.1:5433->5432/tcp, 0.0.0.0:8080->80/tcp, [::]:8080->80/tcp, 6379/tcp"
    assert docker_maint.published_ports(field) == {5433, 8080}


def test_established_ports_reads_both_ends_v4_and_v6_and_skips_listeners():
    output = (
        "Active Connections\n"
        "\n"
        "  Proto  Local Address          Foreign Address        State\n"
        "  TCP    127.0.0.1:5433         127.0.0.1:52344        ESTABLISHED\n"
        "  TCP    [::1]:52999            [::1]:9200             ESTABLISHED\n"
        "  TCP    0.0.0.0:1433           0.0.0.0:0              LISTENING\n"
    )
    assert docker_maint.established_ports(output) == {5433, 52344, 52999, 9200}


def test_compose_stacks_groups_by_project_and_leaves_unlabelled_containers_alone():
    """An ad-hoc `docker run` -- an MCP server, a one-off shell -- is not a stack."""
    listing = (
        "abc\tcarameli\tC:\\ws\\carameli\t127.0.0.1:5432->5432/tcp\n"
        "def\tcarameli\tC:\\ws\\carameli\t127.0.0.1:6379->6379/tcp\n"
        "mcp\t\t\t\n"
    )
    stacks = docker_maint.compose_stacks(listing)
    assert set(stacks) == {"carameli"}
    assert stacks["carameli"]["ids"] == ["abc", "def"]
    assert stacks["carameli"]["ports"] == {5432, 6379}


def test_auto_stop_is_opt_in_so_absence_in_every_form_means_no(tmp_path):
    """A collector-style stack does scheduled work with no client connected, which no
    connection check can tell apart from idle -- so the default has to be "keep"."""
    assert not docker_maint.auto_stop_enabled("")
    assert not docker_maint.auto_stop_enabled(str(tmp_path))
    (tmp_path / ".devkit.toml").write_text('[project]\nenv_prefix = "X"\n', encoding="utf-8")
    assert not docker_maint.auto_stop_enabled(str(tmp_path))


def test_auto_stop_requires_the_literal_true(tmp_path):
    manifest = tmp_path / ".devkit.toml"
    manifest.write_text("[docker]\nauto_stop = true\n", encoding="utf-8")
    assert docker_maint.auto_stop_enabled(str(tmp_path))
    manifest.write_text('[docker]\nauto_stop = "yes"\n', encoding="utf-8")
    assert not docker_maint.auto_stop_enabled(str(tmp_path))


def test_a_manifest_that_does_not_parse_reads_as_not_opted_in(tmp_path):
    (tmp_path / ".devkit.toml").write_text("[docker\n", encoding="utf-8")
    assert not docker_maint.auto_stop_enabled(str(tmp_path))


def test_youngest_start_trims_dockers_nanoseconds_and_takes_the_newest():
    listing = "2026-08-17T03:00:00.123456789Z\n2026-08-17T09:30:00.987654321Z\n"
    newest = docker_maint.youngest_start(listing)
    assert newest is not None
    assert (newest.hour, newest.minute) == (9, 30)


def test_youngest_start_is_none_when_nothing_parses():
    assert docker_maint.youngest_start("template parsing error\n") is None


def _idle_stack(ports=frozenset({5433})):
    return {"ids": ["abc"], "workdir": "C:\\ws\\project", "ports": set(ports)}


def _old_start(monkeypatch):
    """`docker inspect` answering with a start far outside the grace window."""
    monkeypatch.setattr(
        docker_maint, "_capture", lambda cmd, timeout=60: "2020-01-01T00:00:00.000000000Z\n"
    )


def test_a_stack_that_never_opted_in_is_kept(monkeypatch):
    monkeypatch.setattr(docker_maint, "auto_stop_enabled", lambda workdir: False)
    reason = docker_maint.keep_reason(_idle_stack(), set(), _now())
    assert reason is not None
    assert "auto_stop" in reason


def test_an_unreadable_netstat_keeps_every_stack(monkeypatch):
    """None means "could not be asked", and guessing "no connections" is the guess
    that licenses stopping a database out from under someone."""
    monkeypatch.setattr(docker_maint, "auto_stop_enabled", lambda workdir: True)
    reason = docker_maint.keep_reason(_idle_stack(), None, _now())
    assert reason is not None
    assert "netstat" in reason


def test_an_established_connection_to_a_published_port_keeps_the_stack(monkeypatch):
    monkeypatch.setattr(docker_maint, "auto_stop_enabled", lambda workdir: True)
    reason = docker_maint.keep_reason(_idle_stack({5433}), {5433, 60123}, _now())
    assert reason is not None
    assert "5433" in reason


def test_a_recently_started_stack_is_inside_the_grace_window(monkeypatch):
    """An agent that brought a stack up minutes ago has no connections during the edit
    half of its loop; recency is the only signal that something still wants it."""
    monkeypatch.setattr(docker_maint, "auto_stop_enabled", lambda workdir: True)
    recent = (_now() - timedelta(minutes=10)).isoformat()
    monkeypatch.setattr(docker_maint, "_capture", lambda cmd, timeout=60: recent + "\n")
    reason = docker_maint.keep_reason(_idle_stack(), set(), _now())
    assert reason is not None
    assert "grace" in reason


def test_an_unreadable_start_time_keeps_the_stack(monkeypatch):
    monkeypatch.setattr(docker_maint, "auto_stop_enabled", lambda workdir: True)
    monkeypatch.setattr(docker_maint, "_capture", lambda cmd, timeout=60: None)
    assert docker_maint.keep_reason(_idle_stack(), set(), _now()) is not None


def test_an_opted_in_idle_old_stack_is_cleared_to_stop(monkeypatch):
    monkeypatch.setattr(docker_maint, "auto_stop_enabled", lambda workdir: True)
    _old_start(monkeypatch)
    assert docker_maint.keep_reason(_idle_stack(), set(), _now()) is None


def _now():
    return docker_maint.datetime.now(docker_maint.timezone.utc)


def _routed_capture(monkeypatch, ps=None, netstat=None, inspect=None):
    """Answer each of the pass's three captures by the command being run."""

    def fake(cmd, timeout=60):
        if cmd[:2] == ["docker", "ps"]:
            return ps
        if cmd[0] == "netstat":
            return netstat
        if cmd[:2] == ["docker", "inspect"]:
            return inspect
        raise AssertionError(f"unexpected capture: {cmd}")

    monkeypatch.setattr(docker_maint, "_capture", fake)


def test_stop_idle_reports_nothing_to_do_when_the_engine_cannot_be_asked(
    monkeypatch, commands, capsys
):
    _routed_capture(monkeypatch, ps=None)
    assert docker_maint.generic_stop_idle() == 0
    assert commands == []
    assert "could not be asked" in capsys.readouterr().out


def test_stop_idle_stops_the_idle_opted_in_stack_and_names_why_it_keeps_the_rest(
    monkeypatch, commands, capsys
):
    """The full pass: one stack opted in and idle, one busy on a published port, one
    that never opted in. Only the first is stopped, with `docker stop` -- containers
    and volumes survive, and `unless-stopped` keeps a stopped stack stopped."""
    listing = (
        "abc\tidle-proj\tC:\\ws\\idle\t127.0.0.1:5433->5432/tcp\n"
        "def\tbusy-proj\tC:\\ws\\busy\t127.0.0.1:9200->9200/tcp\n"
        "ghi\tcollector\tC:\\ws\\collector\t\n"
    )
    netstat = "  TCP    127.0.0.1:9200    127.0.0.1:52344    ESTABLISHED\n"
    _routed_capture(
        monkeypatch, ps=listing, netstat=netstat, inspect="2020-01-01T00:00:00.000000000Z\n"
    )
    monkeypatch.setattr(
        docker_maint, "auto_stop_enabled", lambda workdir: workdir != "C:\\ws\\collector"
    )
    assert docker_maint.generic_stop_idle() == 0
    assert commands == [["docker", "stop", "abc"]]
    printed = capsys.readouterr().out
    assert "[stop] idle-proj" in printed
    assert "[keep] busy-proj" in printed
    assert "[keep] collector" in printed


def test_stop_idle_stops_nothing_when_netstat_cannot_be_read(monkeypatch, commands, capsys):
    listing = "abc\tidle-proj\tC:\\ws\\idle\t127.0.0.1:5433->5432/tcp\n"
    _routed_capture(monkeypatch, ps=listing, netstat=None)
    monkeypatch.setattr(docker_maint, "auto_stop_enabled", lambda workdir: True)
    assert docker_maint.generic_stop_idle() == 0
    assert commands == []
    assert "cannot be verified" in capsys.readouterr().out


def test_a_failed_stop_is_the_only_thing_that_reddens_the_pass(monkeypatch, capsys):
    listing = "abc\tidle-proj\tC:\\ws\\idle\t127.0.0.1:5433->5432/tcp\n"
    _routed_capture(monkeypatch, ps=listing, netstat="", inspect="2020-01-01T00:00:00.000000000Z\n")
    monkeypatch.setattr(docker_maint, "auto_stop_enabled", lambda workdir: True)
    monkeypatch.setattr(docker_maint, "run", lambda cmd, **kw: 1)
    assert docker_maint.generic_stop_idle() == 1


def test_the_mode_reaches_the_generic_pass(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(docker_maint, "generic_stop_idle", lambda: seen.append("ran") or 0)
    assert docker_maint.main(["stop-idle", "--generic"]) == 0
    assert seen == ["ran"]


# --- the unattended prune's decision half ---------------------------------
# Lived on 2026-08-20. `docker_data.vhdx` had grown to 26.25 GB holding ~6 GB of live
# data, because the only step that returns bytes to Windows needs `wsl --shutdown` and
# `--idle-only` had refused it every night for months -- something is always up on a
# machine whose stacks carry `restart: unless-stopped`. Free space reached 12.0 GB,
# `reconcile` crossed its own 20 GB floor, and it began destroying boxes whose PRs were
# still open. Compacting by hand returned 16.2 GB.


def test_an_idle_machine_still_prunes_without_consulting_the_disk():
    """The ordinary case, and the one that must not regress: nothing running means
    there is nothing to weigh."""
    proceed, why = docker_maint.prune_verdict(running=0, free=500.0)
    assert proceed
    assert "no containers" in why


def test_containers_up_on_a_roomy_disk_still_skips():
    """The guard's original purpose. 4am is not the time to stop twelve containers
    for disk nobody needs."""
    proceed, why = docker_maint.prune_verdict(running=12, free=400.0)
    assert not proceed
    assert "12 containers up" in why


def test_containers_up_under_the_floor_escalates():
    """Below the floor the alternative is `reconcile` deleting open-PR boxes, so
    stopping containers `unless-stopped` will restart is the cheaper loss."""
    proceed, why = docker_maint.prune_verdict(running=12, free=12.0)
    assert proceed
    assert "12.0 GB free" in why
    assert "cheaper loss" in why


def test_an_unreadable_disk_never_escalates():
    """`free_gb` returns -1.0 for "cannot tell", which must not read as "no space
    left" -- that would license the disruptive half against a machine mid-run."""
    proceed, why = docker_maint.prune_verdict(running=12, free=-1.0)
    assert not proceed
    assert "could not be read" in why


def test_an_unreachable_engine_never_escalates():
    """`running_containers` returns -1 the same way, and for the same reason."""
    proceed, why = docker_maint.prune_verdict(running=-1, free=1.0)
    assert not proceed
    assert "could not be asked" in why


def test_the_floor_sits_above_the_one_reconcile_reaps_at():
    """The whole point of the escalation is to fire while the expensive remedy is
    still avoidable. The two constants live in different files, so nothing but this
    keeps them ordered."""
    worktree = load_script("scripts/worktree.py")
    assert docker_maint.PRESSURE_FREE_GB > worktree.DEFAULT_MIN_FREE_GB


def test_exactly_at_the_floor_escalates():
    """`under_pressure` in worktree.py treats the floor as inclusive; this agrees with
    it rather than leaving a one-gigabyte band where neither remedy acts."""
    assert docker_maint.prune_verdict(running=1, free=docker_maint.PRESSURE_FREE_GB)[0]


def test_one_container_is_not_pluralised():
    """The verdict string is what the nightly artifact shows a human at 8am."""
    assert "1 container up" in docker_maint.prune_verdict(running=1, free=999.0)[1]


def test_free_gb_reports_minus_one_when_the_volume_cannot_be_read(monkeypatch):
    def boom(_path):
        raise OSError("no such volume")

    monkeypatch.setattr(docker_maint.shutil, "disk_usage", boom)
    assert docker_maint.free_gb() == -1.0


def test_the_engine_gets_longer_to_come_back_after_a_cold_start():
    """A cold start that re-mounts the VHDX and restarts every `unless-stopped`
    container took over 90s, so the default poll reported a successful prune as a
    failure."""
    assert docker_maint.COLD_POLL_TIMEOUT > docker_maint.POLL_TIMEOUT


@pytest.mark.parametrize("mode", ["restart-engine", "fix"])
def test_a_stop_and_start_polls_the_cold_budget_not_the_warm_one(monkeypatch, mode):
    """Every mode built on `stop_docker` runs `wsl --shutdown`, so every one of them is
    waiting on a cold start -- but only the compaction path said so.

    `restart-engine` polled the 90s default and `fix` polled its own `POLL_TIMEOUT * 2`,
    both chosen before the cold budget existed. On 2026-08-28 that printed `ENGINE STILL
    NOT RESPONDING` and exited 1 on an engine that answered ~45s later unaided -- a
    verdict that advises a factory reset or a reboot, and the state a machine-reclaim run
    leaves behind when its stop step is followed by an engine restart.
    """
    seen: list[int] = []
    monkeypatch.setattr(docker_maint, "stop_docker", lambda: None)
    monkeypatch.setattr(docker_maint, "start_docker", lambda: None)
    monkeypatch.setattr(docker_maint.time, "sleep", lambda _seconds: None)

    def record(timeout: int = docker_maint.POLL_TIMEOUT) -> bool:
        seen.append(timeout)
        return True

    monkeypatch.setattr(docker_maint, "poll_engine", record)
    runner = (
        docker_maint.generic_restart_engine
        if mode == "restart-engine"
        else docker_maint.generic_fix
    )
    assert runner() == 0
    assert seen == [docker_maint.COLD_POLL_TIMEOUT]


# --- restarting what the stop actually killed ---------------------------------
#
# 2026-08-20: `restart-engine`, `fix` and `prune --generic` all left the engine
# permanently unreachable and blamed the daemon -- "DOCKER STILL WEDGED ... reset to
# factory defaults, or reboot". Nothing was wedged. `stop_docker` taskkills every name in
# DOCKER_PROCESSES, one of which is a Windows *service*, while `start_docker` relaunched
# only the GUI. Starting the service by hand brought `docker version` back in 6 seconds.


def test_the_service_this_kills_is_one_it_can_restart():
    """The defect was a mismatch between what gets killed and what gets started, so pin
    the overlap itself rather than either list."""
    assert docker_maint.DOCKER_SERVICE in docker_maint.DOCKER_PROCESSES


def test_start_docker_starts_the_service_before_the_gui(monkeypatch, commands, tmp_path):
    """Order matters: Docker Desktop reads the service at launch and will not start it,
    so a GUI launched first comes up attached to nothing."""
    exe = tmp_path / "Docker Desktop.exe"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(docker_maint, "DOCKER_DESKTOP_EXE", exe)
    launched: list[list[str]] = []
    monkeypatch.setattr(docker_maint.subprocess, "Popen", lambda cmd, **kw: launched.append(cmd))
    docker_maint.start_docker()
    assert ["sc", "start", docker_maint.DOCKER_SERVICE] in commands
    assert launched, "the GUI must still be launched"


def test_an_already_running_service_is_not_an_error(monkeypatch, capsys):
    """1056 is ERROR_SERVICE_ALREADY_RUNNING -- the normal case on a machine where
    nothing stopped it, and it must not print a warning."""
    monkeypatch.setattr(docker_maint, "run", lambda *a, **kw: 1056)
    docker_maint.start_docker_service()
    assert "[warn]" not in capsys.readouterr().out


def test_a_service_that_will_not_start_warns_but_does_not_raise(monkeypatch, capsys):
    """Soft failure on purpose: a machine whose Docker ships without the service must
    still reach the GUI launch, because this repairs a state `stop_docker` creates rather
    than gating start on a new precondition."""
    monkeypatch.setattr(docker_maint, "run", lambda *a, **kw: 5)
    docker_maint.start_docker_service()
    assert "[warn]" in capsys.readouterr().out
