"""Tests for `scripts/docker-maint.py`'s stack modes and its argument plumbing.

The daemon modes (`restart-engine`, `fix`, `prune`) are deliberately not exercised
here: they kill Docker Desktop and compact a WSL2 VHDX, so there is nothing to assert
that does not involve doing it. What IS tested is everything the `up`/`down` hoist
added, plus the two invariants that would be expensive to get wrong:

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
    """The scheduled case. Stopping twelve containers at 4am to reclaim disk is not a
    trade anything should make unattended."""
    monkeypatch.setattr(docker_maint, "running_containers", lambda: 12)
    monkeypatch.setattr(
        docker_maint, "docker_info_ok", lambda *_a, **_kw: pytest.fail("touched the engine")
    )
    assert docker_maint.generic_prune(idle_only=True) == 0
    printed = capsys.readouterr().out
    assert "SKIPPED" in printed and "12 container(s) up" in printed


def test_idle_only_skips_when_the_engine_cannot_be_asked(monkeypatch, capsys):
    monkeypatch.setattr(docker_maint, "running_containers", lambda: -1)
    monkeypatch.setattr(
        docker_maint, "docker_info_ok", lambda *_a, **_kw: pytest.fail("touched the engine")
    )
    assert docker_maint.generic_prune(idle_only=True) == 0
    assert "could not be asked" in capsys.readouterr().out


def test_a_skipped_prune_is_a_success_not_a_failure(monkeypatch):
    """It reports 0 so a scheduled run that correctly declines does not look broken.
    "Nothing to do right now" is the expected outcome most nights."""
    monkeypatch.setattr(docker_maint, "running_containers", lambda: 3)
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
