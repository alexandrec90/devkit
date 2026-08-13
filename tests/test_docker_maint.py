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
