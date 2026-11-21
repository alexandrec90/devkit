"""Which project owns which scheduled task, and which hand-made task nobody owns."""

from __future__ import annotations

import csv
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "hooks"))

import project_tasks

HEADER = ["HostName", "TaskName", "Author", "Last Result"]


def _csv(*rows: tuple[str, str]) -> str:
    """`schtasks /Query /FO CSV /V`, cut to the columns read, header repeated as it is."""
    out = io.StringIO()
    writer = csv.writer(out, quoting=csv.QUOTE_ALL)
    writer.writerow(HEADER)
    for name, author in rows:
        writer.writerow(["PC", name, author, "0"])
    writer.writerow(HEADER)
    return out.getvalue()


MACHINE = _csv(
    ("\\D2R Net Logger", "N/A"),
    ("\\Hardware Health Check", ""),
    ("\\SeaweedFS data-lake pool", "N/A"),
    ("\\devkit-tray", "devkit"),
    ("\\devkit-scratch", "N/A"),
    ("\\NvDriverUpdateCheckDaily_{B2FE}", "NVIDIA Corporation"),
    ("\\Microsoft\\Windows\\Maps\\MapsUpdateTask", "N/A"),
    ("\\Hardware Health Check", ""),
)


def _project(root: Path, name: str, manifest: str | None) -> None:
    (root / name).mkdir()
    if manifest is not None:
        (root / name / ".devkit.toml").write_text(manifest, encoding="utf-8")


def test_a_project_claims_the_tasks_its_manifest_names(tmp_path):
    _project(tmp_path, "data-lake", '[schedule]\ntasks = ["SeaweedFS data-lake pool"]\n')
    _project(tmp_path, "host-scripts", '[schedule]\ntasks = ["D2R Net Logger", " "]\n')
    _project(tmp_path, "roguelike", "[python]\nversion = '3.12'\n")
    _project(tmp_path, "bare", None)
    owners = project_tasks.declared(tmp_path, ["data-lake", "host-scripts", "roguelike", "bare"])
    assert owners == {
        "SeaweedFS data-lake pool": "data-lake",
        "D2R Net Logger": "host-scripts",
    }, "a blank name claims nothing, and a project with no [schedule] claims nothing"


def test_a_task_two_projects_claim_stays_with_the_first(tmp_path):
    _project(tmp_path, "a", '[schedule]\ntasks = ["Pool"]\n')
    _project(tmp_path, "b", '[schedule]\ntasks = ["Pool"]\n')
    assert project_tasks.declared(tmp_path, ["b", "a"]) == {"Pool": "b"}


def test_a_registered_project_missing_from_disk_claims_nothing(tmp_path):
    assert project_tasks.declared(tmp_path, ["not-cloned-here"]) == {}


def test_a_malformed_claim_is_the_neutral_default(tmp_path):
    _project(tmp_path, "x", '[schedule]\ntasks = "Pool"\n')
    assert project_tasks.declared(tmp_path, ["x"]) == {}


def test_hand_made_is_the_root_tasks_with_no_author():
    assert project_tasks.hand_made(MACHINE) == [
        "D2R Net Logger",
        "Hardware Health Check",
        "SeaweedFS data-lake pool",
        "devkit-scratch",
    ], "vendors' tasks, Windows' subfolders and the repeated header are not a person's"


def test_unowned_leaves_out_devkits_jobs_and_every_claimed_task():
    assert project_tasks.unowned(MACHINE, ["SeaweedFS data-lake pool"]) == [
        "D2R Net Logger",
        "Hardware Health Check",
    ]


def test_the_cli_prints_each_claim_then_what_is_unowned(tmp_path, monkeypatch, capsys):
    import sweep

    workspace = tmp_path / "w.code-workspace"
    workspace.write_text('{"folders": [{"path": "data-lake"}]}', encoding="utf-8")
    _project(tmp_path, "data-lake", '[schedule]\ntasks = ["SeaweedFS data-lake pool"]\n')
    monkeypatch.setattr(sweep, "default_workspace", lambda root: workspace)
    monkeypatch.setattr(project_tasks.schedule_health, "scheduler_csv", lambda: MACHINE)
    assert project_tasks.main([]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "data-lake: SeaweedFS data-lake pool",
        "UNOWNED: D2R Net Logger",
        "UNOWNED: Hardware Health Check",
    ]
    monkeypatch.setattr(sweep, "default_workspace", lambda root: tmp_path / "absent")
    assert project_tasks.main([]) == 2


def test_a_scheduler_that_cannot_be_asked_reports_nothing_unowned(monkeypatch):
    monkeypatch.setattr(project_tasks.schedule_health, "scheduler_csv", lambda: None)
    assert project_tasks.unowned_on_machine([]) == []
    monkeypatch.setattr(project_tasks.schedule_health, "scheduler_csv", lambda: MACHINE)
    assert project_tasks.unowned_on_machine(["D2R Net Logger", "SeaweedFS data-lake pool"]) == [
        "Hardware Health Check"
    ]
