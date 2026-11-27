"""Which project owns which scheduled task, and which hand-made task nobody owns."""

from __future__ import annotations

import csv
import io
import subprocess
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


def test_a_merged_claim_the_checkout_has_not_pulled_still_claims(tmp_path):
    # 002f2388: host-scripts' claim merged, its checkout stayed behind, and the next pass
    # filed "Dell Idle Blank" as unowned again.
    _project(tmp_path, "host-scripts", '[schedule]\ntasks = ["D2R Net Logger"]\n')
    merged = '[schedule]\ntasks = ["D2R Net Logger", "Dell Idle Blank"]\n'
    owners = project_tasks.declared(tmp_path, ["host-scripts"], committed=lambda _: merged)
    assert owners == {"D2R Net Logger": "host-scripts", "Dell Idle Blank": "host-scripts"}


def test_a_claim_only_the_working_tree_has_still_claims(tmp_path):
    _project(tmp_path, "x", '[schedule]\ntasks = ["Local"]\n')
    owners = project_tasks.declared(tmp_path, ["x"], committed=lambda _: "[python]\n")
    assert owners == {"Local": "x"}


def test_an_unparseable_committed_manifest_claims_nothing(tmp_path):
    _project(tmp_path, "x", None)
    assert project_tasks.declared(tmp_path, ["x"], committed=lambda _: "[schedule\n") == {}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_committed_manifest_reads_the_default_branch_not_the_working_tree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / ".devkit.toml").write_text('[schedule]\ntasks = ["Merged"]\n', encoding="utf-8")
    _git(repo, "add", ".devkit.toml")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "claim")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    (repo / ".devkit.toml").write_text("[python]\n", encoding="utf-8")
    assert project_tasks.committed_manifest(repo) == '[schedule]\ntasks = ["Merged"]\n'


def test_committed_manifest_is_none_with_no_remote_branch(tmp_path):
    _git(tmp_path, "init", "-q")
    assert project_tasks.committed_manifest(tmp_path) is None


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
    """Off the project registry and the projects root: no workspace file is read."""
    import project_registry

    _project(tmp_path, "data-lake", '[schedule]\ntasks = ["SeaweedFS data-lake pool"]\n')
    monkeypatch.setattr(project_registry, "all_projects", lambda *_a, **_k: ["data-lake"])
    monkeypatch.setattr(project_registry, "projects_root", lambda root: tmp_path)
    monkeypatch.setattr(project_tasks.schedule_health, "scheduler_csv", lambda: MACHINE)
    assert project_tasks.main([]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "data-lake: SeaweedFS data-lake pool",
        "UNOWNED: D2R Net Logger",
        "UNOWNED: Hardware Health Check",
    ]
    monkeypatch.setattr(project_registry, "projects_root", lambda root: tmp_path / "absent")
    assert project_tasks.main([]) == 2


def test_a_scheduler_that_cannot_be_asked_reports_nothing_unowned(monkeypatch):
    monkeypatch.setattr(project_tasks.schedule_health, "scheduler_csv", lambda: None)
    assert project_tasks.unowned_on_machine([]) == []
    monkeypatch.setattr(project_tasks.schedule_health, "scheduler_csv", lambda: MACHINE)
    assert project_tasks.unowned_on_machine(["D2R Net Logger", "SeaweedFS data-lake pool"]) == [
        "Hardware Health Check"
    ]
