"""`scripts/fix_dependabot.py`: what Dependabot cannot do, as one failure per project.

The shapes are GitHub's own, cut down from ibkr_trader's on 2026-10-04: update runs that
all died on `"data-lake" at /pyproject.toml`, and 39 open alerts no PR answered.
"""

from __future__ import annotations

import datetime as _dt
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fix_dependabot as fd
import fix_plan

NOW = _dt.datetime(2026, 10, 4, 12, 0, tzinfo=_dt.UTC)
OLD = "2026-10-02T22:08:45Z"

UNFETCHABLE_LOG = (
    "Dependabot\tUNKNOWN STEP\t2026-10-04T11:03:56.6Z updater | 2026/10/04 11:03:56 ERROR "
    "<job_1607644272> Error during file fetching; aborting: The following path based "
    'dependencies could not be retrieved: "data-lake" at /pyproject.toml\n'
    "Dependabot\tUNKNOWN STEP\t2026-10-04T11:03:57.5Z ##[error]Dependabot encountered an error\n"
)
HANDLED_LOG = (
    "updater | 2026/09/28 11:02:57 INFO <job_1595087814> Handled error whilst updating "
    'sqlalchemy: dependency_file_content_not_changed {message: "Content did not change!"}\n'
)


def run(job: str, conclusion: str, number: int, url: str = "") -> dict:
    return {
        "databaseId": number,
        "conclusion": conclusion,
        "status": "completed",
        "url": url or f"https://github.com/o/r/actions/runs/{number}",
        "displayTitle": f"{job} - Update #{number}",
    }


def alert(package: str, patched: str | None, created: str = OLD, number: int = 1) -> dict:
    return {
        "number": number,
        "created_at": created,
        "html_url": f"https://github.com/o/r/security/dependabot/{number}",
        "dependency": {"package": {"ecosystem": "pip", "name": package}},
        "security_vulnerability": {
            "first_patched_version": {"identifier": patched} if patched else None
        },
        "security_advisory": {"summary": f"{package} is vulnerable"},
    }


def test_only_the_newest_run_of_each_update_job_decides_whether_it_is_failing():
    runs = [
        run("uv in /. for urllib3", "failure", 9),
        run("pip in /.", "success", 8),
        run("uv in /. for urllib3", "success", 7),
        run("pip in /.", "failure", 6),
        run("github_actions in /.", "cancelled", 5),
    ]
    assert fd.update_job(runs[0]) == "uv in /. for urllib3"
    assert [r["databaseId"] for r in fd.failing_updates(runs)] == [9]


def test_an_unfetchable_path_dependency_is_one_entry_named_by_the_dependency():
    """Every job that dies on it says the same thing, so it is one entry, not one per job."""
    assert fd.run_errors(UNFETCHABLE_LOG + UNFETCHABLE_LOG) == [
        f"{fix_plan.UNFETCHABLE_ENTRY}data-lake"
    ]


def test_a_handled_error_is_its_package_and_kind_and_any_other_error_its_text():
    assert fd.run_errors(HANDLED_LOG) == ["run sqlalchemy: dependency_file_content_not_changed"]
    other = "updater | ERROR <job_1> Something else broke for job 1\n"
    assert fd.run_errors(other) == ["run Something else broke for job 1"]
    assert fd.run_errors("nothing here\n") == []


def test_an_alert_no_open_pr_names_is_unanswered_at_its_highest_patched_version():
    alerts = [
        alert("urllib3", "2.6.0", number=1),
        alert("urllib3", "2.8.0", number=2),
        alert("GitPython", "3.1.62", number=3),
        alert("mlflow", None, number=4),  # no fix exists yet: nothing to bump to
        alert("aiohttp", "3.14.3", created="2026-10-04T08:00:00Z", number=5),  # in grace
    ]
    assert fd.package_of(alerts[2]) == "gitpython"
    assert fd.unanswered(alerts, [], NOW) == {"urllib3": "2.8.0", "gitpython": "3.1.62"}


def test_a_pr_naming_the_package_answers_its_alert_whoever_opened_it():
    """Dependabot's own branch, or a fixer's hand bump that names it in its title."""
    alerts = [alert("urllib3", "2.8.0"), alert("requests", "2.33.0", number=2)]
    by_bot = {"headRefName": "dependabot/uv/urllib3-2.8.0", "title": "", "body": ""}
    by_hand = {"headRefName": "agent/fix", "title": "Bump requests to 2.33.0", "body": ""}
    assert fd.unanswered(alerts, [by_bot, by_hand], NOW) == {}


def test_a_name_inside_another_packages_name_answers_nothing():
    alerts = [alert("requests", "2.33.0")]
    oauth = {"headRefName": "dependabot/pip/requests-oauthlib-2.0.0", "title": "", "body": ""}
    assert fd.unanswered(alerts, [oauth], NOW) == {"requests": "2.33.0"}


def test_the_signature_puts_the_alerts_first_and_says_whether_a_session_can_act():
    unfetchable = f"{fix_plan.UNFETCHABLE_ENTRY}data-lake"
    sig = fd.signature([unfetchable], {"urllib3": "2.8.0"})
    assert sig == (f"{fix_plan.ALERT_ENTRY}urllib3 >= 2.8.0", unfetchable)
    assert fd.actionable(sig)
    assert fd.unfetchable(sig) == ["data-lake"]
    assert not fd.actionable((unfetchable,)), "no session can make Dependabot fetch a sibling"
    assert fd.actionable(("run sqlalchemy: dependency_file_content_not_changed",))


def gh_world(runs=(), alerts=None, prs=(), logs=None):
    """A fake `gh` answering the four calls `read_project` makes; `alerts=None` is the
    403 a repository with alerts switched off answers."""
    asked: list = []

    def gh(*args):
        asked.append(args)
        if args[:2] == ("run", "list"):
            return subprocess.CompletedProcess(args, 0, json.dumps(list(runs)), "")
        if args[0] == "api":
            if alerts is None:
                return subprocess.CompletedProcess(args, 1, "", "HTTP 403: alerts are disabled")
            return subprocess.CompletedProcess(args, 0, json.dumps(alerts), "")
        if args[:2] == ("pr", "list"):
            return subprocess.CompletedProcess(args, 0, json.dumps(list(prs)), "")
        if args[:2] == ("run", "view"):
            return subprocess.CompletedProcess(args, 0, (logs or {}).get(args[2], ""), "")
        raise AssertionError(args)

    return gh, asked


def project(tmp_path: Path, monkeypatch) -> Path:
    checkout = tmp_path / "ibkr_trader"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text(
        '[tool.uv.sources]\ndata-lake = { path = "../data-lake", editable = true }\n',
        encoding="utf-8",
    )
    workflows = checkout / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "pr-gate.yml").write_text(
        "jobs:\n  t:\n    steps:\n      - uses: actions/checkout@v7\n"
        "        with:\n          repository: o/data-lake\n          ref: 430536af\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(fd.tb, "detect_default_branch", lambda _git, fallback="main": "main")
    return checkout


def test_alerts_and_a_failing_job_are_one_failure_with_its_evidence_on_disk(tmp_path, monkeypatch):
    checkout = project(tmp_path, monkeypatch)
    runs = [run("uv in /. for urllib3", "failure", 9, "https://run/9")]
    gh, _ = gh_world(runs, [alert("urllib3", "2.8.0")], logs={"9": UNFETCHABLE_LOG})
    failure, note = fd.read_project("ibkr_trader", checkout, tmp_path / "ev", NOW, gh)
    assert note == ""
    assert failure is not None and failure.kind == fix_plan.DEPENDABOT
    assert failure.signature == (
        f"{fix_plan.ALERT_ENTRY}urllib3 >= 2.8.0",
        f"{fix_plan.UNFETCHABLE_ENTRY}data-lake",
    )
    assert failure.url == "https://run/9" and failure.base == "main" and failure.number == 0
    assert failure.title.startswith("Dependabot in ibkr_trader: 1 package(s)")
    text = (Path(failure.evidence) / fd.EVIDENCE_FILE).read_text(encoding="utf-8")
    assert "urllib3: bump to 2.8.0" in text and "https://run/9" in text
    assert "data-lake: declared as a [tool.uv.sources] path, ../data-lake" in text
    assert "the PR gate checks it out at 430536af" in text


def test_a_path_dependency_alone_with_every_alert_answered_is_a_note_and_no_failure(
    tmp_path, monkeypatch
):
    """Nothing a session can do makes Dependabot fetch the sibling, and a skip standing
    for a day is filed as a stall -- which would send the devkit session instead."""
    checkout = project(tmp_path, monkeypatch)
    runs = [run("uv in /. for urllib3", "failure", 9)]
    pr = {"headRefName": "dependabot/uv/urllib3-2.8.0", "title": "", "body": ""}
    gh, _ = gh_world(runs, [alert("urllib3", "2.8.0")], [pr], {"9": UNFETCHABLE_LOG})
    failure, note = fd.read_project("ibkr_trader", checkout, tmp_path / "ev", NOW, gh)
    assert failure is None
    assert "cannot fetch the path dependency data-lake" in note


def test_a_repository_with_alerts_off_and_no_runs_says_nothing(tmp_path, monkeypatch):
    checkout = project(tmp_path, monkeypatch)
    gh, asked = gh_world()
    assert fd.read_project("devkit", checkout, tmp_path / "ev", NOW, gh) == (None, "")
    assert not [a for a in asked if a[:2] == ("pr", "list")], "no alert, no PR read"


def test_a_failed_job_whose_log_names_nothing_still_has_a_signature(tmp_path, monkeypatch):
    checkout = project(tmp_path, monkeypatch)
    gh, _ = gh_world([run("uv in /.", "failure", 3)])
    failure, _ = fd.read_project("data-lake", checkout, tmp_path / "ev", NOW, gh)
    assert failure is not None and failure.signature == ("run uv in /.: failed",)


def test_only_the_newest_failing_jobs_logs_are_read(tmp_path, monkeypatch):
    """Ten alerts are ten jobs failing for one reason: a pass reads a few, not all."""
    checkout = project(tmp_path, monkeypatch)
    runs = [run(f"uv in /. for p{n}", "failure", n) for n in range(6)]
    gh, asked = gh_world(runs, logs={str(n): UNFETCHABLE_LOG for n in range(6)})
    failure, _ = fd.read_project("ibkr_trader", checkout, tmp_path / "ev", NOW, gh)
    read = [a[2] for a in asked if a[:2] == ("run", "view")]
    assert read == ["0", "1", "2"] and len(read) == fd.LOGS_READ
    assert failure is None, "every job died on the sibling, and no alert waits"


def test_the_reads_answer_empty_on_a_gh_that_cannot():
    def broken(*args):
        return subprocess.CompletedProcess(args, 1, "", "offline")

    assert fd.update_runs(broken) == [] and fd.open_alerts(broken) == []
    assert fd.open_prs(broken) == [] and fd.failed_log(broken, {"databaseId": 1}) == ""


def test_evidence_text_names_every_job_and_alert():
    text = fd.evidence_text(
        "p",
        [(run("uv in /.", "failure", 1, "https://run/1"), [])],
        [alert("urllib3", "2.8.0")],
        {"urllib3": "2.8.0"},
    )
    assert "(no error line in the log)" in text
    assert "https://github.com/o/r/security/dependabot/1 urllib3 is vulnerable" in text


def test_collect_reads_every_registered_checkout_and_skips_a_missing_one(tmp_path, monkeypatch):
    checkout = project(tmp_path, monkeypatch)
    gh, _ = gh_world([run("uv in /.", "failure", 3)])
    workspace = tmp_path / "w.code-workspace"
    failures, notes = fd.collect(
        workspace, ["ibkr_trader", "absent"], NOW, gh_for=lambda d: gh if d == checkout else None
    )
    assert [f.project for f in failures] == ["ibkr_trader"] and notes == []
