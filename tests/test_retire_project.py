"""Tests for `scripts/retire-project.py`.

What is pinned is what would fail quietly: the plan's order (the registry, the only step
with gates, before anything is stopped), that no step ever removes a volume, that a
restore puts back exactly what the record says and nothing it does not, and that a
second retire does not overwrite the first record with the state the first one left.
"""

import subprocess
from pathlib import Path

from support import load_script

retire = load_script("scripts/retire-project.py")
config = retire.config

Facts = retire.Facts
Step = retire.Step


def facts(**overrides):
    base = {
        "name": "geo",
        "checkout": Path("/ws/geo"),
        "registered": True,
        "on_disk": True,
    }
    return Facts(**(base | overrides))


def container(project="geo", service="collector"):
    return config.Collector(project, service)


def labels(plan):
    return [step.label for step in plan.steps]


def done(stdout="", code=0, stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


# --- retire -------------------------------------------------------------------------


def test_the_registry_is_retired_before_anything_is_stopped(tmp_path):
    plan = retire.retire_plan(
        facts(collector=container(), assigned=config.RUN, has_compose=True, running=("db",)),
        tmp_path,
    )
    assert labels(plan)[0] == "unplug from the registry"
    assert "--unplug" in plan.steps[0].argv and "--yes" in plan.steps[0].argv
    assert labels(plan)[1:] == ["stop its collector here", "remove its containers (volumes kept)"]


def test_no_retire_step_ever_removes_a_volume(tmp_path):
    plan = retire.retire_plan(facts(has_compose=True, running=("db", "web")), tmp_path)
    down = plan.steps[-1]
    assert down.argv == ("docker", "compose", "down")
    assert all("-v" not in s.argv and "--volumes" not in s.argv for s in plan.steps)
    assert down.cwd == Path("/ws/geo")


def test_force_reaches_the_unplug_and_nothing_else(tmp_path):
    plan = retire.retire_plan(facts(), tmp_path, force=True)
    assert plan.steps[0].argv[-1] == "--force"


def test_an_unregistered_project_skips_the_registry_and_says_so(tmp_path):
    plan = retire.retire_plan(facts(registered=False, has_compose=True), tmp_path)
    assert labels(plan) == ["remove its containers (volumes kept)"]
    assert any("already done" in note for note in plan.notes)


def test_a_collector_already_stopped_here_is_not_stopped_again(tmp_path):
    plan = retire.retire_plan(facts(collector=container(), assigned=config.STOP), tmp_path)
    assert "stop its collector here" not in labels(plan)


def test_a_hands_off_collector_is_still_stopped_here(tmp_path):
    """Hands-off neither starts nor stops it, so a container left up would keep running."""
    plan = retire.retire_plan(facts(collector=container(), assigned=None), tmp_path)
    assert "stop its collector here" in labels(plan)


def test_a_claimed_task_is_exported_before_it_is_deleted(tmp_path):
    plan = retire.retire_plan(facts(registered=False, tasks=("Geo nightly",)), tmp_path)
    export, delete = plan.steps
    assert export.save_to == tmp_path / "logs/retired-projects/geo/Geo-nightly.xml"
    assert "/Query" in export.argv and "/Delete" in delete.argv


def test_a_docker_that_did_not_answer_is_a_note_not_a_silent_skip(tmp_path):
    plan = retire.retire_plan(facts(registered=False, has_compose=True, running=None), tmp_path)
    assert plan.steps == []
    assert any("docker did not answer" in note for note in plan.notes)


def test_devkit_itself_cannot_be_retired():
    own = retire.sweep.source_checkout(retire.REPO_ROOT).name
    assert "cannot be retired" in retire.refusal(facts(name=own))
    assert retire.refusal(facts()) == ""


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
    path.write_text("not json", encoding="utf-8")
    assert retire.load_records(path) == {}


# --- restore ------------------------------------------------------------------------


def test_a_restore_with_no_record_only_plugs_and_says_why(tmp_path):
    plan = retire.restore_plan(facts(registered=False), tmp_path)
    assert labels(plan) == ["plug into the registry"]
    assert "--plug" in plan.steps[0].argv
    assert any("no record" in note for note in plan.notes)


def test_a_registered_project_missing_here_is_plugged_so_it_is_cloned(tmp_path):
    plan = retire.restore_plan(facts(on_disk=False, record={}), tmp_path)
    assert labels(plan)[0] == "plug into the registry"


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


def test_claimed_tasks_come_from_the_projects_manifest(tmp_path):
    assert retire.claimed_tasks(tmp_path) == ()
    (tmp_path / ".devkit.toml").write_text(
        '[schedule]\ntasks = ["Geo nightly"]\n', encoding="utf-8"
    )
    assert retire.claimed_tasks(tmp_path) == ("Geo nightly",)


def test_a_restore_that_finished_drops_its_record(tmp_path, monkeypatch):
    path = tmp_path / retire.RECORD
    retire.save_records(path, {"geo": {"collector": None, "tasks": ["t"]}, "other": {}})
    saved = retire.task_file(tmp_path, "geo", "t")
    saved.parent.mkdir(parents=True)
    saved.write_text("x", encoding="utf-8")
    args = retire.parse_args(["restore", "geo"])
    retire.finish_record(args, facts(record={"tasks": ["t"]}), tmp_path, failed="")
    assert set(retire.load_records(path)) == {"other"}
    assert not saved.exists()


def test_a_failed_restore_keeps_its_record_for_the_next_try(tmp_path):
    path = tmp_path / retire.RECORD
    retire.save_records(path, {"geo": {"collector": "run"}})
    args = retire.parse_args(["restore", "geo"])
    retire.finish_record(args, facts(record={}), tmp_path, failed="start: exit 1")
    assert "geo" in retire.load_records(path)


def test_a_dry_run_and_an_unconfirmed_run_change_nothing(capsys):
    plan = retire.Plan(steps=[Step("one", ("a",))])
    assert not retire.confirmed(plan, retire.parse_args(["retire", "geo", "--dry-run", "--yes"]))
    assert not retire.confirmed(plan, retire.parse_args(["retire", "geo"]), ask=lambda _p: "n")
    assert retire.confirmed(plan, retire.parse_args(["retire", "geo"]), ask=lambda _p: "y")

    def eof(_prompt):
        raise EOFError

    assert not retire.confirmed(plan, retire.parse_args(["retire", "geo"]), ask=eof)
    assert "retire geo will:" in capsys.readouterr().out


# --- gathering and the command line -------------------------------------------------


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


def _stub_registry(monkeypatch, tmp_path, *, known=("geo",)):
    monkeypatch.setattr(retire.sweep, "default_workspace", lambda base: tmp_path / "ws.code")
    monkeypatch.setattr(retire.devkit_project, "canonical_text", lambda: "")
    monkeypatch.setattr(retire.config, "parse_setting", lambda text: ([container()], []))
    monkeypatch.setattr(retire.devkit_project, "known_projects", lambda text: set(known))
    monkeypatch.setattr(retire.config, "load_assignment", lambda path: {"geo": config.RUN})


def test_gather_reads_the_registry_the_assignment_and_the_record(tmp_path, monkeypatch):
    _stub_registry(monkeypatch, tmp_path)
    (tmp_path / "geo").mkdir()
    monkeypatch.setattr(retire.collector_build, "compose_files", lambda checkout: ["c.yml"])
    retire.save_records(tmp_path / retire.RECORD, {"geo": {"collector": "run"}})
    found = retire.gather("geo", tmp_path, lambda argv, timeout, cwd=None: done("db\n"))
    assert found.checkout == tmp_path / "geo"
    assert found.registered and found.on_disk and found.has_compose
    assert found.collector == container() and found.assigned == config.RUN
    assert found.running == ("db",)
    assert found.record == {"collector": "run"}


def test_gather_for_a_restore_asks_docker_nothing(tmp_path, monkeypatch):
    _stub_registry(monkeypatch, tmp_path, known=())

    def run(*_a, **_k):
        raise AssertionError("a restore reads only the record")

    found = retire.gather("geo", tmp_path, run, local=False)
    assert not found.registered and not found.on_disk
    assert found.running == () and found.tasks == () and found.record is None


def test_save_xml_creates_its_folder_and_normalises_line_endings(tmp_path):
    target = tmp_path / "deep" / "t.xml"
    retire.save_xml("<a/>\r\r\n<b/>\r\n", target)
    assert target.read_text(encoding="utf-16") == "<a/>\n<b/>\n"


def test_write_artifact_keeps_failures_and_a_clean_run_empties_it(tmp_path, monkeypatch):
    artifact = tmp_path / "logs" / "retire-project.log"
    monkeypatch.setattr(retire, "ARTIFACT", artifact)
    retire.write_artifact(["one", "two"])
    assert artifact.read_text(encoding="utf-8") == "one\ntwo\n"
    retire.write_artifact([])
    assert artifact.read_text(encoding="utf-8") == ""


def test_list_records_names_each_retired_project(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(retire, "ARTIFACT", tmp_path / "a.log")
    assert retire.list_records(tmp_path) == 0
    assert "nothing is retired" in capsys.readouterr().out
    retire.save_records(
        tmp_path / retire.RECORD,
        {"geo": {"collector": None, "services": ["db"], "tasks": [], "retired_at": "then"}},
    )
    assert retire.list_records(tmp_path) == 0
    out = capsys.readouterr().out
    assert "geo: retired then; collector was hands-off; running: db; tasks: none" in out


def test_act_refuses_a_project_it_knows_nothing_about(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(retire, "ARTIFACT", tmp_path / "a.log")
    monkeypatch.setattr(retire, "gather", lambda *a, **k: facts(registered=False, on_disk=False))
    args = retire.parse_args(["retire", "geo", "--yes"])
    assert retire.act(args, tmp_path, run=lambda *a, **k: done()) == 2
    assert "not retired here" in capsys.readouterr().err
    assert "not retired here" in (tmp_path / "a.log").read_text(encoding="utf-8")


def test_act_records_a_retire_before_it_runs_and_reports_a_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(retire, "ARTIFACT", tmp_path / "a.log")
    monkeypatch.setattr(retire, "gather", lambda *a, **k: facts(assigned=config.RUN))
    seen = []

    def run(argv, timeout, cwd=None):
        seen.append(set(retire.load_records(tmp_path / retire.RECORD)))
        return done(code=1, stderr="refused")

    args = retire.parse_args(["retire", "geo", "--yes"])
    assert retire.act(args, tmp_path, run) == 1
    assert seen == [{"geo"}]
    assert "exit 1 -- refused" in (tmp_path / "a.log").read_text(encoding="utf-8")


def test_act_dry_run_changes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(retire, "ARTIFACT", tmp_path / "a.log")
    monkeypatch.setattr(retire, "gather", lambda *a, **k: facts())

    def run(*_a, **_k):
        raise AssertionError("a dry run runs nothing")

    args = retire.parse_args(["retire", "geo", "--dry-run"])
    assert retire.act(args, tmp_path, run) == 0
    assert retire.load_records(tmp_path / retire.RECORD) == {}


def test_main_lists_from_the_machines_home_and_reports_an_os_error(tmp_path, monkeypatch):
    monkeypatch.setattr(retire, "ARTIFACT", tmp_path / "a.log")
    monkeypatch.setattr(retire.config, "home", lambda root: tmp_path)
    assert retire.main(["list"]) == 0

    def broken(*_a, **_k):
        raise OSError("disk gone")

    monkeypatch.setattr(retire, "gather", broken)
    assert retire.main(["retire", "geo", "--yes"]) == 1
    assert "disk gone" in (tmp_path / "a.log").read_text(encoding="utf-8")
