"""`scripts/host_memory.py`: the free memory the fix pass checks before a session."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import host_memory

MEMINFO = "MemTotal:       16303440 kB\nMemFree:          812344 kB\nMemAvailable:    6970112 kB\n"


def test_linux_reads_available_not_free():
    """`MemFree` leaves out the page cache the kernel gives back on demand."""
    assert host_memory.meminfo_mb(MEMINFO) == 6806
    assert host_memory.meminfo_mb("MemTotal: 1 kB\n") is None
    assert host_memory.meminfo_mb("MemAvailable:\n") is None


def test_this_machine_answers_with_a_plausible_figure():
    """The one probe a stub cannot vouch for: the platform call on the machine running it."""
    free = host_memory.available_mb()
    assert free is None or 0 < free < 10_000_000


def test_a_probe_that_fails_reads_as_no_answer(monkeypatch, tmp_path):
    monkeypatch.setattr(host_memory.sys, "platform", "linux")
    monkeypatch.setattr(host_memory, "MEMINFO", tmp_path / "missing")
    assert host_memory.available_mb() is None
    (tmp_path / "meminfo").write_text("MemAvailable: lots kB\n", encoding="utf-8")
    monkeypatch.setattr(host_memory, "MEMINFO", tmp_path / "meminfo")
    assert host_memory.available_mb() is None
    (tmp_path / "meminfo").write_text(MEMINFO, encoding="utf-8")
    assert host_memory.available_mb() == 6806
