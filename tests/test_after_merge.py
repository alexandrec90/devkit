"""`scripts/after_merge.py`: what a task file says, and whether it is due on this machine."""

from __future__ import annotations

import sys
from dataclasses import FrozenInstanceError

import pytest
from support import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import after_merge as am

ID = "a" * am.ID_LENGTH
PATH = ".devkit-queue/rebuild.toml"
NOW = 1_800_000_000.0
DAYS = am.DAY_SECONDS
HOST = "pc-a"


def parsed(text: str, path: str = PATH) -> am.Task:
    return am.parse("carameli", path, ID, text)


def task(**fields) -> am.Task:
    return am.Task("carameli", PATH, ID, **{"run": "make", **fields})


def facts(**fields) -> am.Facts:
    return am.Facts(HOST, **fields)


# --- the file --------------------------------------------------------------------------------


def test_a_task_needs_only_its_action_and_the_rest_defaults():
    found = parsed('run = "make build"')
    assert (found.when, found.on, found.run) == (am.GREEN, am.ANY, "make build")
    assert found.kind == am.RUN and found.name == "rebuild" and not found.malformed


def test_a_task_is_frozen():
    with pytest.raises(FrozenInstanceError):
        parsed('run = "x"').when = am.MERGED


def test_its_id_hashes_its_path_and_the_commit_that_added_it():
    first = am.task_id(PATH, "c1")
    assert len(first) == am.ID_LENGTH == 12
    assert first == am.task_id(PATH, "c1")
    assert first != am.task_id(PATH, "c2"), "queued again later is a new task"
    assert first != am.task_id(".devkit-queue/other.toml", "c1"), "another file is another"
    assert parsed('run = "x"').id == ID


@pytest.mark.parametrize(
    "when",
    [
        "green",
        "merged",
        "released",
        "pr:carameli#12",
        "pr:alexandrec90/carameli#12",
        "task:rebuild-db",
        "task:carameli/rebuild-db",
        "outside:09:30-16:00 UTC-05:00 mon-fri",
        "inside:00:00-24:00 UTC",
    ],
)
def test_every_condition_the_contract_names_parses(when):
    assert parsed(f'when = "{when}"\nrun = "x"').when == when


@pytest.mark.parametrize("on", ["any", "every-machine", "docker", "DESKTOP-900MT6F", "pc.local"])
def test_every_machine_clause_the_contract_names_parses(on):
    assert parsed(f'on = "{on}"\nrun = "x"').on == on


@pytest.mark.parametrize(
    ("body", "kind"),
    [
        ('run = "x"', am.RUN),
        ('agent = "do it"', am.AGENT),
        ('elevated = "restart-wsl-service"', am.ELEVATED),
    ],
)
def test_each_action_is_its_own_kind(body, kind):
    assert parsed(body).kind == kind


@pytest.mark.parametrize(
    ("text", "said"),
    [
        ('run = "x"\nagent = "y"', "exactly one"),
        ('when = "merged"', "exactly one"),
        ('run = ""', "non-empty"),
        ("run = 3", "non-empty"),
        ('run = "x"\nretries = "2"', "unknown keys"),
        ('run = "x"\nwhen = "soon"', "soon"),
        ('run = "x"\nwhen = "pr:carameli"', "pr:carameli"),
        ('run = "x"\nwhen = "pr:carameli#0"', "pr:carameli#0"),
        ('run = "x"\non = "two words"', "two words"),
        ('elevated = "format-the-disk"', "restart-wsl-service"),
        ('elevated = "reboot-when-idle"', "did not happen"),
        ('elevated = "restart-docker-service"', "did not happen"),
        ('run = "x"\nwhen = "task:rebuild"', "waits on itself"),
        ('run = "x"\nwhen = "outside:09:30-16:00 America/New_York"', "time-zone database"),
        ('run = "x"\nwhen = "inside:16:00-09:30 UTC"', "spans midnight"),
        ('run = "x"\nwhen = "outside:soon"', "not a window"),
        ('run = "x', "not TOML"),
    ],
)
def test_a_malformed_file_becomes_an_agent_task_saying_so(text, said):
    """The operator will never look: nothing may end in a dropped file."""
    found = parsed(text)
    assert found.kind == am.AGENT and found.malformed
    assert said in found.malformed
    assert found.when == am.MERGED and found.on == am.ANY, "its own clauses cannot be trusted"
    assert PATH in found.agent and found.malformed in found.agent
    assert text[:20] in found.agent, "the prompt quotes the file, for a session with no other view"


def test_another_projects_task_of_the_same_name_is_not_waiting_on_itself():
    assert not parsed('run = "x"\nwhen = "task:other/rebuild"').malformed


@pytest.mark.parametrize(
    "path",
    [".devkit-queue/notes.md", ".devkit-queue/sub/deep.toml", ".devkit-queue/-odd.toml"],
)
def test_anything_else_in_the_directory_is_malformed_rather_than_ignored(path):
    found = parsed('run = "x"', path)
    assert found.malformed and found.kind == am.AGENT and path in found.agent


def test_the_flow_comes_from_devkit_toml_and_defaults_to_gated():
    assert am.flow_of('[delivery]\nflow = "direct"\n') == am.DIRECT
    assert am.flow_of('[delivery]\nflow = "gated"\n') == am.GATED
    assert am.flow_of("[project]\n") == am.GATED
    assert am.flow_of("") == am.GATED
    assert am.flow_of('[delivery]\nflow = "something-else"\n') == am.GATED
    assert am.flow_of("delivery = 3\n") == am.GATED
    assert am.flow_of("not toml [") == am.GATED


def test_each_flow_has_the_label_its_prs_need():
    assert am.LABELS == {am.DIRECT: "merge-now", am.GATED: "automerge"}


# --- conditions ------------------------------------------------------------------------------


def test_merged_is_always_true_and_nothing_else_is_by_default():
    assert am.met(task(when=am.MERGED), facts(), NOW)
    for when in (am.GREEN, am.RELEASED, "pr:x#1", "task:y"):
        assert not am.met(task(when=when), facts(), NOW), when


def test_green_and_released_ask_about_the_tasks_own_commit():
    asked = []
    found = facts(
        green=lambda commit: asked.append(commit) or True,
        released=lambda commit: asked.append(commit) or True,
    )
    assert am.met(task(when=am.GREEN, commit="c1"), found, NOW)
    assert am.met(task(when=am.RELEASED, commit="c2"), found, NOW)
    assert asked == ["c1", "c2"]


def test_a_pr_condition_asks_about_that_pr():
    asked = []
    found = facts(merged=lambda repo, number: asked.append((repo, number)) or True)
    assert am.met(task(when="pr:a/b#7"), found, NOW)
    assert asked == [("a/b", 7)]


def test_a_task_condition_defaults_to_this_project_and_can_name_another():
    asked = []
    found = facts(done=lambda project, name: asked.append((project, name)) or True)
    assert am.met(task(when="task:first"), found, NOW)
    assert am.met(task(when="task:devkit/first"), found, NOW)
    assert asked == [("carameli", "first"), ("devkit", "first")]


# NOW is Friday 08:00 UTC: 03:00 in New York's winter, before its market opens at 09:30.
MARKET_CLOSED = "outside:09:30-16:00 UTC-05:00 mon-fri"
TRADING = NOW + 7 * 3600  # Friday 10:00 there


def test_a_window_condition_reads_the_clock_and_asks_nothing_else():
    """The ibkr_trader case: a gateway restart that must wait out the trading day."""
    asked = []
    found = facts(done=lambda project, name: asked.append(name) or True)
    assert am.met(task(when=MARKET_CLOSED), found, NOW)
    assert not am.met(task(when=MARKET_CLOSED), found, TRADING)
    assert am.met(task(when="inside:09:30-16:00 UTC-05:00 mon-fri"), found, TRADING)
    assert asked == []


def test_a_task_waiting_for_its_window_waits_in_the_queue_then_runs():
    """Without a window the task could only be queued again as `merged`, a session and a
    PR per attempt; with one it waits here, and the reason says the window's own clock."""
    during = am.judge(task(when=MARKET_CLOSED), am.Marks(), facts(), TRADING, TRADING)
    assert during.action == am.WAIT
    assert MARKET_CLOSED in during.why and "Fri 10:00" in during.why
    after = TRADING + 7 * 3600
    assert am.judge(task(when=MARKET_CLOSED), am.Marks(), facts(), TRADING, after).action == (
        am.TAKE
    )


# --- which machine ----------------------------------------------------------------------------


def test_a_hostname_takes_only_that_host_however_it_was_spelled():
    assert am.takes(task(on="PC-A"), HOST, False)
    assert not am.takes(task(on="pc-b"), HOST, True)


def test_docker_takes_any_machine_with_docker():
    assert am.takes(task(on=am.DOCKER), HOST, True)
    assert not am.takes(task(on=am.DOCKER), HOST, False)


def test_any_and_every_machine_take_whoever_asks():
    assert am.takes(task(on=am.ANY), HOST, False)
    assert am.takes(task(on=am.EVERY_MACHINE), HOST, False)


def test_an_every_machine_task_is_done_once_per_host():
    every = task(on=am.EVERY_MACHINE)
    assert am.done_key(every, HOST) == f"{ID}/{HOST}"
    assert am.is_done(every, frozenset({f"{ID}/{HOST}"}), HOST)
    assert not am.is_done(every, frozenset({f"{ID}/pc-b"}), HOST)
    assert not am.is_done(every, frozenset({ID}), HOST)


def test_an_ordinary_task_is_done_everywhere_at_once():
    assert am.is_done(task(), frozenset({ID}), HOST)


# --- the verdict -------------------------------------------------------------------------------


NOTHING: frozenset[str] = frozenset()


def verdict(which: am.Task, found: am.Facts, age_days: float = 0.0, done=NOTHING) -> am.Verdict:
    return am.judge(which, am.Marks(done), found, NOW - age_days * DAYS, NOW)


def test_failed_is_a_state_that_ends_the_question():
    marks = am.Marks(failed=frozenset({ID}))
    assert am.judge(task(when=am.MERGED), marks, facts(), NOW, NOW).action == am.FAILED


def test_a_task_handed_here_is_this_machines_to_read_back():
    marks = am.Marks(handed={ID: am.Handed(HOST, 1, NOW)})
    assert am.judge(task(when=am.MERGED), marks, facts(), NOW, NOW).action == am.HANDED


def test_a_task_handed_elsewhere_waits_until_that_pc_has_gone_quiet():
    marks = am.Marks(handed={ID: am.Handed("pc-b", 1, NOW)})
    assert am.judge(task(when=am.MERGED), marks, facts(), NOW, NOW).action == am.WAIT
    late = NOW + am.UNCLAIMED_SECONDS
    assert am.judge(task(when=am.MERGED), marks, facts(), NOW, late).action == am.TAKE


def test_a_task_offered_again_is_open_to_any_pc():
    marks = am.Marks(handed={ID: am.Handed("pc-b", 1, NOW, open=True)})
    assert am.judge(task(when=am.MERGED), marks, facts(), NOW, NOW).action == am.TAKE


def test_a_late_condition_is_noted_at_most_once_per_three_days():
    late = am.Marks(rerouted={ID: NOW - DAYS})
    since = NOW - 9 * DAYS
    which = task(when=am.RELEASED)
    assert am.judge(which, am.Marks(), facts(), since, NOW).action == am.NOTE
    assert am.judge(which, late, facts(), since, NOW).action == am.WAIT
    assert am.judge(which, late, facts(), since, NOW + 3 * DAYS).action == am.NOTE


def test_a_note_leaves_the_task_as_it_is():
    prompt = am.noted(task(run="make db"), "when = released has not come true").agent
    assert "make db" in prompt and "Do not carry it out" in prompt


def test_a_handed_ref_says_who_and_how_many_attempts():
    subject = am.handed_subject(task(), HOST, 2)
    assert am.parse_handed(subject, NOW) == am.Handed(HOST, 2, NOW)
    offered = am.handed_subject(task(), HOST, 2, offered=True)
    assert am.parse_handed(offered, NOW) == am.Handed(HOST, 2, NOW, open=True)
    assert am.parse_handed("anything else", NOW) is None


def test_a_done_subject_carries_its_status():
    assert am.status_of(am.done_subject(task(), HOST, am.SHIPPED)) == am.SHIPPED


def test_only_a_finish_is_a_success():
    for word in (am.PASSED, am.SHIPPED, am.CONSUMED):
        assert am.is_success(word)
    for word in ("filed", "requested", "", "failed"):
        assert not am.is_success(word)


def test_done_is_done_whatever_else_is_true():
    assert verdict(task(when=am.MERGED), facts(), 99, frozenset({ID})).action == am.DONE


def test_a_condition_not_met_waits():
    assert verdict(task(when=am.RELEASED), facts()).action == am.WAIT


def test_a_due_task_on_its_machine_is_taken():
    assert verdict(task(when=am.MERGED), facts()).action == am.TAKE


def test_a_task_for_another_host_waits_for_it():
    assert verdict(task(when=am.MERGED, on="pc-b"), facts()).action == am.WAIT


def test_a_host_never_seen_does_not_lose_the_task():
    """After the named constant, whichever PC is awake sends an agent, told why."""
    days = am.UNCLAIMED_AFTER_DAYS
    assert days == 3
    late = verdict(task(when=am.MERGED, on="pc-b"), facts(), days)
    assert late.action == am.REROUTE and "pc-b" in late.why and "unreachable" in late.why
    assert verdict(task(when=am.MERGED, on="pc-b"), facts(), days - 0.1).action == am.WAIT


def test_no_docker_anywhere_does_not_lose_the_task_either():
    late = verdict(task(when=am.MERGED, on=am.DOCKER), facts(), 3)
    assert late.action == am.REROUTE and "docker" in late.why


def test_a_condition_that_never_comes_true_does_not_lose_the_task():
    """A PR closed unmerged, a dependency reverted, a project that never tags."""
    late = verdict(task(when="pr:x#1"), facts(), 3)
    assert late.action == am.NOTE and "pr:x#1" in late.why


def test_a_rerouted_task_keeps_its_id_and_tells_its_agent_what_was_asked():
    moved = am.rerouted(task(run="make db"), "it was meant for pc-b")
    assert moved.id == ID and moved.kind == am.AGENT and moved.on == am.ANY
    assert "meant for pc-b" in moved.agent and "make db" in moved.agent


def test_a_rerouted_elevated_task_asks_for_what_an_agent_can_attempt():
    moved = am.rerouted(task(run="", elevated="restart-wsl-service"), "why")
    assert moved.kind == am.AGENT and "restart-wsl-service" in moved.agent
    assert "cannot do it itself" in moved.agent


def test_a_rerouted_every_machine_task_is_still_owed_once_per_host():
    assert am.rerouted(task(on=am.EVERY_MACHINE), "why").on == am.EVERY_MACHINE


def test_a_task_first_seen_now_stays_open_until_done():
    seen = am.first_seen({}, [task()], NOTHING, HOST, NOW)
    assert seen == {am.seen_key(task()): NOW} and am.seen_key(task()) == f"carameli/{ID}"
    later = am.first_seen(seen, [task()], NOTHING, HOST, NOW + 50)
    assert later == seen, "the clock starts once"
    assert am.first_seen(seen, [task()], frozenset({ID}), HOST, NOW + 50) == {}


# --- by name --------------------------------------------------------------------------------


def test_a_task_in_the_queue_is_done_by_its_own_id():
    current = task()
    names = {"rebuild": frozenset({"b" * 12})}
    assert am.named_done("rebuild", [current], frozenset({ID}), {}, HOST)
    assert not am.named_done("rebuild", [current], frozenset({"b" * 12}), names, HOST), (
        "an earlier file of that name finishing says nothing of this one"
    )


def test_an_every_machine_task_is_done_on_this_host_alone():
    every = task(on=am.EVERY_MACHINE)
    assert am.named_done("rebuild", [every], frozenset({f"{ID}/{HOST}"}), {}, HOST)
    assert not am.named_done("rebuild", [every], frozenset({f"{ID}/pc-b"}), {}, HOST)


def test_a_tidied_task_is_found_by_the_name_its_done_ref_kept():
    """`task:<name>` must survive the tidy that removed that file."""
    names = {"rebuild": frozenset({ID})}
    assert am.named_done("rebuild", [], frozenset(), names, HOST)
    assert not am.named_done("other", [], frozenset(), names, HOST)


def test_a_tidied_every_machine_task_still_means_this_hosts_own_run():
    names = {"rebuild": frozenset({f"{ID}/pc-b"})}
    assert not am.named_done("rebuild", [], frozenset(), names, HOST)
    assert am.named_done("rebuild", [], frozenset(), names, "pc-b")


# --- the tidy --------------------------------------------------------------------------------


def test_only_done_tasks_are_removable():
    finished, waiting = task(), am.Task("carameli", PATH, "b" * 12, run="make")
    assert am.removable([finished, waiting], frozenset({ID})) == [finished]


def test_an_every_machine_task_waits_for_each_counted_host():
    every = task(on=am.EVERY_MACHINE)
    hosts = frozenset({"pc-a", "pc-b"})
    assert am.removable([every], frozenset({f"{ID}/pc-a"}), hosts) == [], "pc-b owes a run"
    assert am.removable([every], frozenset({f"{ID}/pc-a", f"{ID}/pc-b"}), hosts) == [every]


def test_an_every_machine_task_nobody_ran_stays():
    assert am.removable([task(on=am.EVERY_MACHINE)], frozenset(), {"pc-a"}) == []


def test_a_task_that_came_and_went_while_a_host_was_away_is_named():
    rows = [
        ("old/pc-b", NOW - 90 * DAYS, "before-it-left"),
        ("gone/pc-b", NOW - DAYS, "sync"),
        ("queued/pc-b", NOW - DAYS, "still-here"),
        ("mine/pc-b", NOW - DAYS, "ran-here"),
        ("mine/pc-a", NOW - 40 * DAYS, "ran-here"),
        ("ordinary", NOW - DAYS, "not-every-machine"),
    ]
    tasks = [am.Task("p", PATH, "queued", on=am.EVERY_MACHINE, run="x")]
    assert am.missed(rows, tasks, "pc-a", NOW - 30 * DAYS) == ["sync"]


# --- the prompts --------------------------------------------------------------------------------


def test_the_prompt_says_what_the_task_is_and_finishes_with_ship():
    prompt = am.task_prompt(task(run="", agent="Rebuild the docs index."))
    assert "Rebuild the docs index." in prompt
    assert "/ship" in prompt and "no memory" in prompt and PATH in prompt


def test_a_failed_command_goes_to_its_agent_with_the_command_and_output():
    prompt = am.failure_prompt(task(run="make db"), "boom at line 3", "logs/after-merge/x.json")
    assert "make db" in prompt and "boom at line 3" in prompt
    assert "logs/after-merge/x.json" in prompt and "/ship" in prompt


def test_the_prompt_for_a_command_says_to_run_it():
    assert "run this command" in am.what_it_asks(task(run="make db"))
