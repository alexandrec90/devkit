"""Tests for `scripts/project_env.py`: the new project's lock, `.venv`, and commit tooling.

Found generating `web-lod`: uv was installed after VS Code started, so the task could
not see it, the project got no `.venv`, and the initial commit's pre-commit gate
refused the commit after the whole tree had been written.
"""

import subprocess

import pytest
from support import load_script

project_env = load_script("scripts/project_env.py")


def _which(*present: str):
    return lambda name: f"/bin/{name}" if name in present else None


def _recording_run(monkeypatch, returncode: int = 0, stderr: str = "") -> list:
    calls: list = []

    def fake_run(cmd, cwd, **_kwargs):
        calls.append((cmd, cwd))
        return subprocess.CompletedProcess(cmd, returncode, "", stderr)

    monkeypatch.setattr(project_env.subprocess, "run", fake_run)
    return calls


@pytest.mark.parametrize(
    ("present", "blocked"),
    [
        pytest.param((), True, id="neither"),
        pytest.param(("uv",), False, id="uv-builds-the-venv"),
        pytest.param(("pre-commit",), False, id="pre-commit-on-path"),
        pytest.param(("uv", "pre-commit"), False, id="both"),
    ],
)
def test_commit_tooling_is_missing_only_without_uv_and_pre_commit(monkeypatch, present, blocked):
    monkeypatch.setattr(project_env.shutil, "which", _which(*present))
    assert (project_env.missing_commit_tooling() is not None) is blocked


def test_the_missing_tooling_message_names_the_stale_path_case(monkeypatch):
    """uv installed after VS Code started is invisible to its tasks: the reported case."""
    monkeypatch.setattr(project_env.shutil, "which", _which())
    message = project_env.missing_commit_tooling()
    assert "uv" in message and "pre-commit" in message
    assert "Restart VS Code" in message


def test_provisioning_syncs_every_extra_and_group_in_the_new_root(tmp_path, capsys, monkeypatch):
    """pre-commit is in the template's dev extra, so a sync without the extras builds a
    `.venv` the commit's gate still cannot find."""
    monkeypatch.setattr(project_env.shutil, "which", _which("uv"))
    calls = _recording_run(monkeypatch)

    project_env.provision_environment(tmp_path, dry_run=False)

    assert calls == [(["uv", "sync", "--all-extras", "--all-groups"], tmp_path)]
    assert "write   .venv" in capsys.readouterr().out


def test_a_failed_sync_warns_and_leaves_the_verdict_to_the_commit(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(project_env.shutil, "which", _which("uv"))
    _recording_run(monkeypatch, returncode=2, stderr="error: no network")

    project_env.provision_environment(tmp_path, dry_run=False)

    out = capsys.readouterr().out
    assert "warn    uv sync failed" in out
    assert "error: no network" in out
    assert "write" not in out


def test_provisioning_is_skipped_without_uv_and_never_runs_under_dry_run(
    tmp_path, capsys, monkeypatch
):
    calls = _recording_run(monkeypatch)

    monkeypatch.setattr(project_env.shutil, "which", _which("pre-commit"))
    project_env.provision_environment(tmp_path, dry_run=False)
    assert "skip    .venv" in capsys.readouterr().out

    monkeypatch.setattr(project_env.shutil, "which", _which("uv"))
    project_env.provision_environment(tmp_path, dry_run=True)
    assert "run     uv sync --all-extras --all-groups" in capsys.readouterr().out

    assert calls == []


def test_locking_runs_uv_lock_in_the_new_root(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(project_env.shutil, "which", _which("uv"))
    calls = _recording_run(monkeypatch)

    project_env.lock_dependencies(tmp_path, dry_run=False)

    assert calls == [(["uv", "lock"], tmp_path)]
    assert "write   uv.lock" in capsys.readouterr().out


def test_locking_without_uv_says_how_to_add_the_lock_later(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(project_env.shutil, "which", _which())
    calls = _recording_run(monkeypatch)

    project_env.lock_dependencies(tmp_path, dry_run=False)

    assert calls == []
    assert "run `uv lock` in it" in capsys.readouterr().out


def test_a_failed_lock_continues_without_a_lockfile(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(project_env.shutil, "which", _which("uv"))
    _recording_run(monkeypatch, returncode=1, stderr="resolution failed")

    project_env.lock_dependencies(tmp_path, dry_run=False)

    out = capsys.readouterr().out
    assert "warn    uv lock failed -- continuing without a lockfile" in out
    assert "resolution failed" in out
