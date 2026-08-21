"""Tests for `scripts/reclaim.py`.

The script's side effects are deleting files and stopping containers, so the two things
worth guarding are the **order** it does them in and the **gate** that stops it doing
them at all. Both were real failures before they were rules:

  - reconcile run while the disk is under its own floor destroys boxes whose PR is still
    open (11 -> 2 on 2026-08-20), so `reconcile_is_safe` is asserted at, above and below
    the floor, and on an unreadable volume;
  - dry-run is the default, so a mis-click cannot delete anything. Every destructive
    path is asserted to be a no-op without `--yes`, because the cost of that assertion
    being wrong is somebody's `%TEMP%` mid-build.

The `docker stop`-not-`down` choice is pinned for the same reason it is pinned in
`test_docker_maint.py`: this runs from a one-click task, and `down` discards anything not
on a named volume.
"""

from __future__ import annotations

import subprocess

from support import load_script

reclaim = load_script("scripts/reclaim.py")


# --- the reconcile ordering gate -----------------------------------------------


def test_a_roomy_disk_lets_reconcile_run():
    safe, why = reclaim.reconcile_is_safe(45.0, floor_gb=20.0)
    assert safe
    assert "45.0 GB free" in why


def test_a_disk_under_the_floor_blocks_reconcile():
    safe, why = reclaim.reconcile_is_safe(12.0, floor_gb=20.0)
    assert not safe
    assert "12.0 GB free" in why
    assert "still open" in why


def test_exactly_at_the_floor_blocks_reconcile():
    """`worktree.under_pressure` treats the floor as inclusive; this must agree with it,
    or the one disk level that matters most is the one they disagree about."""
    safe, _ = reclaim.reconcile_is_safe(20.0, floor_gb=20.0)
    assert not safe


def test_an_unreadable_volume_blocks_reconcile():
    safe, why = reclaim.reconcile_is_safe(-1.0, floor_gb=20.0)
    assert not safe
    assert "could not be read" in why


def test_the_default_floor_matches_the_one_worktree_reaps_at():
    """The two constants live in different files, so assert the equality rather than
    trusting the comment that says they agree."""
    worktree = load_script("scripts/worktree.py")
    assert reclaim.DEFAULT_RECONCILE_FLOOR_GB == worktree.DEFAULT_MIN_FREE_GB


# --- the sweep -----------------------------------------------------------------


def test_sweep_targets_are_all_under_the_given_temp(tmp_path):
    targets = reclaim.sweep_targets(tmp_path, "someone")
    assert targets
    for target in targets:
        assert tmp_path in target.path.parents
        assert target.why, f"{target.label} must say why it is disposable"


def test_the_pytest_tree_is_named_for_the_current_user(tmp_path):
    targets = reclaim.sweep_targets(tmp_path, "alex")
    assert any(t.path.name == "pytest-of-alex" for t in targets)


def test_dir_size_sums_a_tree(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "one.bin").write_bytes(b"x" * 100)
    (tmp_path / "two.bin").write_bytes(b"y" * 50)
    assert reclaim.dir_size(tmp_path) == 150


def test_dir_size_of_a_missing_tree_is_zero(tmp_path):
    assert reclaim.dir_size(tmp_path / "nope") == 0


def test_stale_files_respects_the_age_cutoff(tmp_path):
    old = tmp_path / "old.log"
    new = tmp_path / "new.log"
    old.write_bytes(b"o")
    new.write_bytes(b"n")
    import os

    os.utime(old, (0, 0))
    stale = reclaim.stale_files(tmp_path, min_age_days=3.0)
    assert stale == [old]


def test_stale_files_never_descends(tmp_path):
    """A recursive age sweep of %TEMP% would half-empty a cache tree whose owner is
    still running, which is worse than leaving it full."""
    nested = tmp_path / "somecache"
    nested.mkdir()
    buried = nested / "old.bin"
    buried.write_bytes(b"x")
    import os

    os.utime(buried, (0, 0))
    assert reclaim.stale_files(tmp_path, min_age_days=1.0) == []


def test_sweep_without_yes_deletes_nothing(tmp_path, capsys):
    diag = tmp_path / "DiagOutputDir"
    diag.mkdir()
    victim = diag / "trace.etl"
    victim.write_bytes(b"z" * 2048)
    freed = reclaim.sweep(reclaim.sweep_targets(tmp_path, "u"), tmp_path, 3.0, apply=False)
    assert victim.exists(), "dry run must not delete"
    assert freed == 2048, "but it must still report what it would have freed"


def test_sweep_with_yes_deletes(tmp_path):
    diag = tmp_path / "DiagOutputDir"
    diag.mkdir()
    victim = diag / "trace.etl"
    victim.write_bytes(b"z" * 2048)
    reclaim.sweep(reclaim.sweep_targets(tmp_path, "u"), tmp_path, 3.0, apply=True)
    assert not victim.exists()


# --- containers ----------------------------------------------------------------


def test_stop_stacks_uses_stop_and_never_down(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    reclaim.stop_stacks(["a", "b"], apply=True)
    assert calls == [["docker", "stop", "a", "b"]]
    assert not any("down" in c for c in calls[0])


def test_stop_stacks_without_yes_runs_nothing(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    reclaim.stop_stacks(["a"], apply=False)
    assert calls == []


def test_stop_stacks_with_nothing_up_is_quiet(monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest_fail())
    reclaim.stop_stacks([], apply=True)
    assert "nothing to stop" in capsys.readouterr().out


def pytest_fail():
    raise AssertionError("must not run docker with no containers")


# --- the memory report ---------------------------------------------------------


def test_tasklist_rows_aggregate_by_image():
    rows = [
        ["chrome.exe", "1", "Console", "1", "100,000 K"],
        ["chrome.exe", "2", "Console", "1", "50,000 K"],
        ["code.exe", "3", "Console", "1", "200,000 K"],
    ]
    assert reclaim.aggregate_tasklist(rows) == [
        ("code.exe", 1, 200000),
        ("chrome.exe", 2, 150000),
    ]


def test_tasklist_rows_that_do_not_parse_are_skipped():
    rows = [
        ["good.exe", "1", "Console", "1", "10 K"],
        ["bad.exe", "2", "Console", "1", "N/A"],
        ["short.exe", "3"],
    ]
    assert reclaim.aggregate_tasklist(rows) == [("good.exe", 1, 10)]


def test_a_healthy_commit_says_so_and_names_nobody(monkeypatch):
    monkeypatch.setattr(reclaim, "top_memory_holders", lambda *a, **kw: [("x.exe", 1, 1)])
    lines = reclaim.memory_verdict(reclaim.Snapshot(100.0, 8.0, 10.0, 32.0))
    assert len(lines) == 1
    assert "healthy" in lines[0]


def test_a_near_limit_commit_names_the_holders_and_admits_it_cannot_help(monkeypatch):
    """The reclaim frees disk and CPU, never committed memory. Saying so is the whole
    value of the section -- a silent success would read as 'this is fixed now'."""
    monkeypatch.setattr(
        reclaim, "top_memory_holders", lambda *a, **kw: [("chrome.exe", 37, 2 * 1024 * 1024)]
    )
    lines = reclaim.memory_verdict(reclaim.Snapshot(100.0, 0.3, 33.2, 34.5))
    body = "\n".join(lines)
    assert "near the limit" in body
    assert "does not free committed memory" in body
    assert "chrome.exe" in body and "37 processes" in body


def test_an_unreadable_memory_status_reports_that(monkeypatch):
    lines = reclaim.memory_verdict(reclaim.Snapshot(10.0, -1.0, -1.0, -1.0))
    assert lines == ["  memory: could not be read"]


# --- what the run reports ------------------------------------------------------


def _isolate(monkeypatch, tmp_path, free_gb):
    """Point `main` at a scratch %TEMP% and stub everything that touches the machine."""
    monkeypatch.setattr(reclaim, "tempdir", lambda: str(tmp_path))
    monkeypatch.setattr(reclaim, "username", lambda: "u")
    monkeypatch.setattr(reclaim, "running_container_names", lambda: [])
    monkeypatch.setattr(reclaim, "top_memory_holders", lambda *a, **kw: [])
    monkeypatch.setattr(reclaim, "run_reconcile", lambda *a, **kw: None)
    monkeypatch.setattr(
        reclaim, "snapshot", lambda *a, **kw: reclaim.Snapshot(free_gb, 8.0, 10.0, 32.0)
    )


def test_a_dry_run_never_reports_a_disk_delta(monkeypatch, tmp_path, capsys):
    """Nothing was deleted, so any movement between the two readings belongs to some
    other process on the machine. Printing it as `before -> after` under this script's
    own "Done" banner claims a result it did not produce -- which is exactly how the
    first run read, on a machine where a build happened to finish mid-run."""
    _isolate(monkeypatch, tmp_path, free_gb=24.0)
    assert reclaim.main([]) == 0
    out = capsys.readouterr().out
    assert "->" not in out
    assert "unchanged" in out
    assert "would free" in out
    assert "Nothing was changed" in out


def test_an_applied_run_does_report_the_delta(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path, free_gb=24.0)
    assert reclaim.main(["--yes"]) == 0
    out = capsys.readouterr().out
    assert "24.0 -> 24.0 GB" in out
    assert "Nothing was changed" not in out


def test_keep_stacks_says_what_it_is_giving_up(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path, free_gb=24.0)
    monkeypatch.setattr(reclaim, "running_container_names", lambda: pytest_fail())
    reclaim.main(["--keep-stacks"])
    assert "bind-mount spin stays" in capsys.readouterr().out


def test_a_full_disk_skips_reconcile_rather_than_reaping_open_prs(monkeypatch, tmp_path, capsys):
    """The ordering lesson, asserted end to end: below the floor the run still sweeps,
    and still declines to hand `worktree.py reconcile` a disk it would reclaim boxes on."""
    _isolate(monkeypatch, tmp_path, free_gb=12.0)
    monkeypatch.setattr(reclaim, "run_reconcile", lambda *a, **kw: pytest_fail())
    reclaim.main(["--yes"])
    assert "skipping reconcile" in capsys.readouterr().out
