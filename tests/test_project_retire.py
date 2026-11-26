"""Tests for `scripts/project_retire.py`, the local and GitHub halves of
`plug-projects.py --unplug` / `--plug`.

What is pinned is what would fail quietly: no step ever removes a volume, the repo is
archived last and only when gh has said it is not archived already, a restore puts back
exactly what the record says and nothing it does not, and a second retire does not
overwrite the first record with the state the first one left.
"""

import subprocess
from pathlib import Path

from support import load_script

retire = load_script("scripts/project_retire.py")
config = retire.config

Facts = retire.Facts
Step = retire.Step


def facts(**overrides):
    base = {"name": "geo", "checkout": Path("/ws/geo"), "on_disk": True}
    return Facts(**(base | overrides))


def container(project="geo", service="collector"):
    return config.Collector(project, service)


def labels(plan):
    return [step.label for step in plan.steps]


def done(stdout="", code=0, stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


# --- retire -------------------------------------------------------------------------


def test_the_repo_is_archived_last(tmp_path):
    plan = retire.retire_plan(
        facts(
            collector=container(),
            assigned=config.RUN,
            has_compose=True,
            running=("db",),
            repo="acme/geo",
        ),
        tmp_path,
    )
    assert labels(plan) == [
        "stop its collector here",
        "remove its containers (volumes kept)",
        "archive acme/geo on GitHub (stops Dependabot)",
    ]
    assert plan.steps[-1].argv == ("gh", "repo", "archive", "acme/geo", "--yes")


def test_an_archived_repo_is_not_archived_again(tmp_path):
    plan = retire.retire_plan(facts(repo="acme/geo", archived=True), tmp_path)
    assert plan.steps == []
    assert any("already archived" in note for note in plan.notes)


def test_a_gh_that_could_not_say_is_a_note_not_an_archive(tmp_path):
    plan = retire.retire_plan(facts(repo="acme/geo", archived=None), tmp_path)
    assert plan.steps == []
    assert any("could not say" in note for note in plan.notes)


def test_a_project_with_no_repo_says_so(tmp_path):
    plan = retire.retire_plan(facts(), tmp_path)
    assert any("no GitHub repo" in note for note in plan.notes)


def test_no_retire_step_ever_removes_a_volume(tmp_path):
    plan = retire.retire_plan(facts(has_compose=True, running=("db", "web")), tmp_path)
    down = plan.steps[-1]
    assert down.argv == ("docker", "compose", "down")
    assert all("-v" not in s.argv and "--volumes" not in s.argv for s in plan.steps)
    assert down.cwd == Path("/ws/geo")


def test_a_collector_already_stopped_here_is_not_stopped_again(tmp_path):
    plan = retire.retire_plan(facts(collector=container(), assigned=config.STOP), tmp_path)
    assert "stop its collector here" not in labels(plan)


def test_a_hands_off_collector_is_still_stopped_here(tmp_path):
    """Hands-off neither starts nor stops it, so a container left up would keep running."""
    plan = retire.retire_plan(facts(collector=container(), assigned=None), tmp_path)
    assert "stop its collector here" in labels(plan)


def test_a_claimed_task_is_exported_before_it_is_deleted(tmp_path):
    plan = retire.retire_plan(facts(tasks=("Geo nightly",)), tmp_path)
    export, delete = plan.steps
    assert export.save_to == tmp_path / "logs/retired-projects/geo/Geo-nightly.xml"
    assert "/Query" in export.argv and "/Delete" in delete.argv


def test_a_docker_that_did_not_answer_is_a_note_not_a_silent_skip(tmp_path):
    plan = retire.retire_plan(facts(has_compose=True, running=None), tmp_path)
    assert plan.steps == []
    assert any("docker did not answer" in note for note in plan.notes)


def test_devkit_itself_cannot_be_retired():
    own = retire.sweep.source_checkout(retire.REPO_ROOT).name
    assert "cannot be retired" in retire.refusal(own)
    assert retire.refusal("geo") == ""


# --- the record ---------------------------------------------------------------------


def test_a_first_retire_records_the_machine_as_it_stands():
    record = retire.merged_record(
        facts(assigned=config.RUN, running=("web", "db"), tasks=("t",)), "now"
    )
    assert record == {
        "collector": "run",
        "services": ["db", "web"],
        "tasks": ["t"],
        "retired_at": "now",
    }


def test_a_second_retire_keeps_what_the_first_one_recorded():
    """The first retire set the collector to `stop` and took the stack down, so the
    machine as it stands now is the first retire's doing, not what to restore."""
    first = {"collector": "run", "services": ["db"], "tasks": ["t"], "retired_at": "then"}
    again = retire.merged_record(facts(assigned=config.STOP, running=(), record=first), "now")
    assert again == first


def test_records_round_trip_and_an_unreadable_file_is_empty(tmp_path):
    path = tmp_path / "r.json"
    assert retire.load_records(path) == {}
    retire.save_records(path, {"geo": {"collector": None}})
    assert retire.load_records(path) == {"geo": {"collector": None}}
    assert retire.is_retired("geo", tmp_path) is False
    retire.save_records(tmp_path / retire.RECORD, {"geo": {}})
    assert retire.is_retired("geo", tmp_path)
    path.write_text("not json", encoding="utf-8")
    assert retire.load_records(path) == {}


# --- restore ------------------------------------------------------------------------


def test_a_restore_with_no_record_starts_nothing_and_says_why(tmp_path):
    plan = retire.restore_plan(facts(), tmp_path)
    assert plan.steps == []
    assert any("no record" in note for note in plan.notes)


def test_a_restore_puts_back_what_was_running_but_leaves_the_collector_to_run_here(tmp_path):
    record = {"collector": "run", "services": ["collector", "db"], "tasks": ["t"]}
    plan = retire.restore_plan(
        facts(collector=container(), assigned=config.STOP, record=record), tmp_path
    )
    assert labels(plan) == ["register task t", "start what was running", "start its collector here"]
    assert plan.steps[1].argv == ("docker", "compose", "up", "-d", "db")
    assert str(retire.task_file(tmp_path, "geo", "t")) in plan.steps[0].argv


def test_a_collector_that_was_hands_off_goes_back_to_hands_off(tmp_path):
    plan = retire.restore_plan(
        facts(collector=container(), assigned=config.STOP, record={"collector": None}), tmp_path
    )
    assert labels(plan) == ["hand its collector back (hands off)"]


def test_a_collector_that_was_stopped_here_stays_stopped(tmp_path):
    plan = retire.restore_plan(
        facts(collector=container(), assigned=config.STOP, record={"collector": "stop"}), tmp_path
    )
    assert plan.steps == []


# --- running it ---------------------------------------------------------------------


def test_execute_stops_at_the_first_failure_and_names_it():
    calls = []

    def run(argv, timeout, cwd=None):
        calls.append(argv)
        return done(code=1, stderr="boom") if argv == ["b"] else done()

    steps = [Step("one", ("a",)), Step("two", ("b",)), Step("three", ("c",))]
    assert retire.execute(steps, run, out=lambda _line: None) == "two: exit 1 -- boom"
    assert calls == [["a"], ["b"]]


def test_an_exported_task_is_kept_in_the_encoding_it_declares(tmp_path):
    xml = '<?xml version="1.0" encoding="UTF-16"?>\r\r\n<Task/>\r\r\n'
    target = tmp_path / "t.xml"
    step = Step("export", ("schtasks",), save_to=target)
    assert (
        retire.execute([step], lambda argv, timeout, cwd=None: done(xml), out=lambda _l: None) == ""
    )
    assert (
        target.read_text(encoding="utf-16") == '<?xml version="1.0" encoding="UTF-16"?>\n<Task/>\n'
    )


def test_running_services_reads_compose_and_none_means_docker_did_not_say(tmp_path):
    assert retire.running_services(tmp_path, lambda *a, **k: done("web\ndb\n\n")) == ("db", "web")
    assert retire.running_services(tmp_path, lambda *a, **k: done(code=1)) is None


def test_repo_archived_tells_no_repo_from_a_gh_that_could_not_say():
    def answer(result):
        return lambda argv, timeout, cwd=None: result

    assert retire.repo_archived("a/g", answer(done('{"isArchived": true}'))) == ("a/g", True)
    assert retire.repo_archived("a/g", answer(done('{"isArchived": false}'))) == ("a/g", False)
    missing = done(code=1, stderr="GraphQL: Could not resolve to a Repository with the name")
    assert retire.repo_archived("a/g", answer(missing)) == ("", False)
    assert retire.repo_archived("a/g", answer(done(code=1, stderr="auth"))) == ("a/g", None)
    assert retire.repo_archived("a/g", answer(done("not json"))) == ("a/g", None)


def test_claimed_tasks_come_from_the_projects_manifest(tmp_path):
    assert retire.claimed_tasks(tmp_path) == ()
    (tmp_path / ".devkit.toml").write_text(
        '[schedule]\ntasks = ["Geo nightly"]\n', encoding="utf-8"
    )
    assert retire.claimed_tasks(tmp_path) == ("Geo nightly",)


def test_forget_drops_the_record_and_its_kept_task_files(tmp_path):
    path = tmp_path / retire.RECORD
    retire.save_records(path, {"geo": {"collector": None, "tasks": ["t"]}, "other": {}})
    saved = retire.task_file(tmp_path, "geo", "t")
    saved.parent.mkdir(parents=True)
    saved.write_text("x", encoding="utf-8")
    retire.forget(tmp_path, "geo", {"tasks": ["t"]})
    assert set(retire.load_records(path)) == {"other"}
    assert not saved.exists()
    retire.forget(tmp_path, "never", None)
    assert set(retire.load_records(path)) == {"other"}


# --- gathering ----------------------------------------------------------------------


def test_present_tasks_keeps_only_what_the_scheduler_holds(monkeypatch):
    monkeypatch.setattr(retire, "WINDOWS", True)
    held = {"Geo nightly": '<?xml version="1.0"?><Task/>'}

    def run(argv, timeout, cwd=None):
        name = next((n for n in held if n in argv), None)
        return done(held[name]) if name else done(code=1)

    monkeypatch.setattr(retire.devkit_schtasks, "query_xml_argv", lambda n: ("schtasks", n))
    monkeypatch.setattr(retire.devkit_schtasks, "parse_task", lambda text: text or None)
    assert retire.present_tasks(["Geo nightly", "Geo gone"], run) == ("Geo nightly",)


def test_present_tasks_is_empty_off_windows(monkeypatch):
    monkeypatch.setattr(retire, "WINDOWS", False)

    def run(*_a, **_k):
        raise AssertionError("no scheduler to ask off Windows")

    assert retire.present_tasks(["Geo nightly"], run) == ()


def _stub_setting(monkeypatch):
    monkeypatch.setattr(retire.devkit_project, "canonical_text", lambda: "")
    monkeypatch.setattr(retire.config, "parse_setting", lambda text: ([container()], []))
    monkeypatch.setattr(retire.config, "load_assignment", lambda path: {"geo": config.RUN})


def test_gather_reads_the_assignment_the_record_docker_and_github(tmp_path, monkeypatch):
    _stub_setting(monkeypatch)
    (tmp_path / "geo").mkdir()
    monkeypatch.setattr(retire.collector_build, "compose_files", lambda checkout: ["c.yml"])
    retire.save_records(tmp_path / retire.RECORD, {"geo": {"collector": "run"}})

    def run(argv, timeout, cwd=None):
        return done('{"isArchived": false}') if argv[0] == "gh" else done("db\n")

    found = retire.gather("geo", tmp_path / "geo", tmp_path, run, owner="acme")
    assert found.on_disk and found.has_compose
    assert found.collector == container() and found.assigned == config.RUN
    assert found.running == ("db",)
    assert found.record == {"collector": "run"}
    assert (found.repo, found.archived) == ("acme/geo", False)


def test_gather_for_a_restore_asks_docker_and_github_nothing(tmp_path, monkeypatch):
    _stub_setting(monkeypatch)

    def run(*_a, **_k):
        raise AssertionError("a restore reads only the record")

    found = retire.gather("geo", tmp_path / "geo", tmp_path, run, owner="acme", local=False)
    assert not found.on_disk
    assert found.running == () and found.tasks == () and found.record is None
    assert found.repo == ""


def test_save_xml_creates_its_folder_and_normalises_line_endings(tmp_path):
    target = tmp_path / "deep" / "t.xml"
    retire.save_xml("<a/>\r\r\n<b/>\r\n", target)
    assert target.read_text(encoding="utf-16") == "<a/>\n<b/>\n"


# --- retire and restore, end to end -------------------------------------------------


def test_retire_records_before_it_runs_and_reports_a_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(
        retire, "gather", lambda *a, **k: facts(assigned=config.RUN, repo="acme/geo")
    )
    seen = []

    def run(argv, timeout, cwd=None):
        seen.append(set(retire.load_records(tmp_path / retire.RECORD)))
        return done(code=1, stderr="refused")

    failed = retire.retire("geo", Path("/ws/geo"), "acme", run, lambda _l: None, tmp_path)
    assert failed == "archive acme/geo on GitHub (stops Dependabot): exit 1 -- refused"
    assert seen == [{"geo"}]


def test_retire_and_restore_refuse_devkit(tmp_path):
    own = retire.sweep.source_checkout(retire.REPO_ROOT).name

    def run(*_a, **_k):
        raise AssertionError("a refusal runs nothing")

    assert "cannot be retired" in retire.retire(own, tmp_path, "acme", run, print, tmp_path)
    assert "cannot be retired" in retire.restore(own, tmp_path, run, print, tmp_path)


def test_a_restore_that_finished_drops_its_record(tmp_path, monkeypatch):
    retire.save_records(tmp_path / retire.RECORD, {"geo": {"collector": "stop"}})
    monkeypatch.setattr(retire, "gather", lambda *a, **k: facts(record={"collector": "stop"}))
    assert retire.restore("geo", Path("/ws/geo"), lambda *a, **k: done(), print, tmp_path) == ""
    assert retire.load_records(tmp_path / retire.RECORD) == {}


def test_a_failed_restore_keeps_its_record_for_the_next_try(tmp_path, monkeypatch):
    record = {"collector": None, "services": ["db"]}
    retire.save_records(tmp_path / retire.RECORD, {"geo": record})
    monkeypatch.setattr(retire, "gather", lambda *a, **k: facts(record=record))
    failed = retire.restore(
        "geo", Path("/ws/geo"), lambda *a, **k: done(code=1), lambda _l: None, tmp_path
    )
    assert failed == "start what was running: exit 1"
    assert "geo" in retire.load_records(tmp_path / retire.RECORD)
