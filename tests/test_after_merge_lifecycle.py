"""No after-merge task is ever silently lost, and none runs twice by accident.

Each test here was written from a reproduction against real bare repositories, and failed
before its fix: a task's identity, what "done" means, the clock every state owes, and what
a slow or second PC may do on a board read minutes ago.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
from after_merge_world import (
    TOUCH,
    after_merge_pass,
    am,
    fix_reports,
    git,
    jc,
    make_world,
    out,
    refusing_push,
)

QUEUED = f"{am.QUEUE_DIR}/job.toml"
SLEEPING = (
    'start /b "" "{py}" -c "import time; time.sleep(9)" & exit 0'
    if sys.platform == "win32"
    else "sleep 9 & exit 0"
)


@pytest.fixture
def world(tmp_path, monkeypatch):
    return make_world(tmp_path, monkeypatch)


# --- identity: a task is its path and the commit that added it -------------------------------


def test_the_same_bytes_queued_again_later_are_a_new_task(world):
    world.queue(TOUCH)
    world.run()
    assert world.ran() == 1
    world.remove("carameli", QUEUED)
    world.run()
    world.queue(TOUCH)
    world.run()
    assert world.ran() == 2


def test_two_files_with_the_same_bytes_are_two_tasks(world):
    first = world.queue(TOUCH, name="one")
    second = world.queue(TOUCH, name="two")
    world.run()
    assert first != second and world.ran() == 2
    assert world.done(first) and world.done(second)


def test_a_whitespace_rewrite_of_a_finished_task_is_not_a_new_task(world):
    """What a tidy's end-of-file fixer does to a file: it must not run the task again."""
    first = world.queue(TOUCH)
    world.run()
    assert world.queue(TOUCH + "\n") == first
    world.run()
    assert world.ran() == 1


def test_an_edit_to_a_finished_task_does_not_run_it_again(world):
    first = world.queue(TOUCH)
    world.run()
    assert world.queue(TOUCH.replace("ran>>", "again>>")) == first
    world.run()
    assert world.ran() == 1


def test_an_edit_to_an_unfinished_task_runs_as_edited(world):
    world.queue('run = "echo stale>> ran.txt"\nwhen = "released"\n')
    world.queue('run = "echo fresh>> ran.txt"\nwhen = "merged"\n')
    world.run()
    assert (world.checkout / "ran.txt").read_text(encoding="utf-8").strip() == "fresh"


# --- done means finished ----------------------------------------------------------------------


def test_an_agent_task_handed_to_a_session_is_handed_not_done(world):
    task = world.queue('agent = "x"\nwhen = "merged"\n')
    world.run()
    assert world.on_remote(jc.handed_ref(task)) and not world.done(task)
    assert world.claimed(task), "the claim stands until done is on the server"


def test_a_tree_that_shipped_an_intent_marks_the_task_done(world):
    task = world.queue('agent = "x"\nwhen = "merged"\n')
    world.run()
    world.ship_from(world.trees[0])
    world.run()
    assert world.done(task) and not world.claimed(task)
    assert len(world.launches) == 1


def test_it_is_judged_on_a_pc_whose_fix_pass_is_off(world):
    """The second PC is off today, so nothing else there would ever read the tree."""
    world.mode = "off"
    task = world.queue('agent = "x"\nwhen = "merged"\n')
    world.run()
    world.ship_from(world.trees[0])
    world.run()
    assert world.done(task)


def test_a_tree_gone_after_its_pr_merged_is_done(world):
    task = world.queue('agent = "x"\nwhen = "merged"\n')
    world.run()
    world.merged_branches.add(world.result(task)["branch"])
    tree = world.trees[0]
    (tree / "logs" / "fix-dispatch.json").unlink()
    (tree / "logs").rmdir()
    tree.rmdir()
    world.run()
    assert world.done(task) and len(world.launches) == 1


@pytest.mark.parametrize(
    "state", [fix_reports.NO_OUTCOME, fix_reports.INTERRUPTED, fix_reports.NEVER_STARTED]
)
def test_a_dead_session_offers_the_task_again_on_the_next_cycle(world, state):
    task = world.queue('agent = "x"\nwhen = "merged"\n')
    world.run()
    world.session = state
    world.run()
    assert not world.done(task)
    world.session = fix_reports.WORKING
    world.run()
    assert len(world.launches) == 2


def test_attempts_are_counted_on_the_remote_and_then_it_fails_visibly(world):
    task = world.queue('agent = "x"\nwhen = "merged"\n')
    world.run()
    world.session = fix_reports.NO_OUTCOME
    for _cycle in range(2 * am.MAX_ATTEMPTS + 2):
        world.run()
    assert len(world.launches) == am.MAX_ATTEMPTS
    assert not world.done(task) and world.on_remote(jc.failed_ref(task))
    assert not world.claimed(task)
    assert any("failed after" in line and "job" in line for line in world.findings())


def test_a_failed_task_is_visible_in_status_and_stays_in_the_repo(world, capsys):
    task = world.queue('agent = "x"\nwhen = "merged"\n')
    world.run()
    world.session = fix_reports.NO_OUTCOME
    for _cycle in range(2 * am.MAX_ATTEMPTS + 2):
        world.run()
    capsys.readouterr()
    after_merge_pass.status(world.workspace, [], world.seams())
    assert f"[{task}]: failed" in capsys.readouterr().out
    boards = after_merge_pass._read_boards(world.workspace, ["carameli"], world.seams())
    assert am.removable(boards["carameli"].tasks, boards["carameli"].done) == []


def test_a_refused_launch_writes_nothing_and_retries_next_cycle(world):
    world.launch_code = 1
    task = world.queue('agent = "x"\nwhen = "merged"\n')
    world.run()
    assert not world.done(task) and not world.on_remote(jc.handed_ref(task))
    assert not world.claimed(task)
    assert world.findings() == []
    world.launch_code = 0
    world.run()
    assert world.on_remote(jc.handed_ref(task))


def test_a_refused_launch_does_not_run_the_failed_command_again(world):
    task = world.queue('run = "echo ran>> ran.txt& exit 3"\nwhen = "merged"\n')
    world.launch_code = 1
    world.run()
    world.launch_code = 0
    world.run()
    assert world.ran() == 1 and world.on_remote(jc.handed_ref(task))


def test_a_failed_command_is_handed_and_not_done(world):
    task = world.queue('run = "exit 3"\nwhen = "merged"\n')
    world.run()
    assert not world.done(task) and world.on_remote(jc.handed_ref(task))
    world.ship_from(world.trees[0])
    world.run()
    assert world.done(task)


def test_a_session_on_a_pc_gone_for_days_is_offered_to_another(world):
    task = world.queue('agent = "x"\nwhen = "merged"\n')
    world.run()
    world.host = "pc-b"
    world.run()
    assert len(world.launches) == 1, "it is pc-a's while pc-a may yet read it back"
    world.advance(am.UNCLAIMED_AFTER_DAYS + 0.1)
    world.run()
    assert len(world.launches) == 2 and not world.done(task)


def test_an_elevated_request_is_not_done_until_the_worker_takes_it(world):
    world.spool.mkdir()
    task = world.queue('elevated = "restart-wsl-service"\nwhen = "merged"\n')
    world.run()
    assert (world.spool / "restart-wsl-service.request").is_file()
    assert not world.done(task) and world.on_remote(jc.handed_ref(task))
    world.run()
    assert not world.done(task)
    (world.spool / "restart-wsl-service.request").unlink()
    world.run()
    assert world.done(task)


def test_only_done_is_removable_in_the_tidy(world):
    world.queue('agent = "x"\nwhen = "merged"\n')
    world.run()
    boards = after_merge_pass._read_boards(world.workspace, ["carameli"], world.seams())
    assert am.removable(boards["carameli"].tasks, boards["carameli"].done) == []


# --- a dependency needs success ----------------------------------------------------------------


def test_a_dependency_that_failed_does_not_release_what_waits_on_it(world):
    world.queue('when = "merged"\nrun = "exit 3"\n', name="a")
    world.queue('when = "task:a"\nrun = "echo ran>> ran.txt"\n', name="b")
    world.run()
    world.run()
    assert world.ran() == 0


def test_a_dependency_finished_by_its_session_releases_what_waits_on_it(world):
    world.queue('when = "merged"\nrun = "exit 3"\n', name="a")
    world.queue('when = "task:a"\nrun = "echo ran>> ran.txt"\n', name="b")
    world.run()
    world.ship_from(world.trees[0])
    world.run()
    world.run()
    assert world.ran() == 1


def test_a_dependency_on_an_every_machine_task_means_this_hosts_own_run(world):
    world.queue('when = "merged"\non = "every-machine"\nrun = "echo x"\n', name="a")
    world.run()
    world.queue('when = "task:a"\non = "pc-b"\nrun = "echo ran>> ran.txt"\n', name="b")
    world.host = "pc-b"
    facts_board = after_merge_pass._read_boards(world.workspace, ["carameli"], world.seams())
    board = facts_board["carameli"]
    assert not am.named_done("a", board.tasks, board.successes, board.done_names, "pc-b")
    assert am.named_done("a", board.tasks, board.successes, board.done_names, "pc-a")


def test_a_done_ref_of_an_unknown_status_is_no_success():
    assert am.is_success(am.PASSED) and am.is_success(am.SHIPPED)
    assert not am.is_success("filed") and not am.is_success("requested")


# --- a reroute consumes nothing ---------------------------------------------------------------


def test_a_condition_never_met_sends_an_agent_and_the_task_still_runs_when_it_comes_true(world):
    task = world.queue('when = "pr:carameli#7"\nrun = "echo ran>> ran.txt"\n')
    world.run()
    world.advance(am.UNCLAIMED_AFTER_DAYS + 0.1)
    world.run()
    assert len(world.launches) == 1 and "pr:carameli#7" in world.launches[0][1]
    assert not world.done(task) and world.on_remote(jc.rerouted_ref(task))
    world.run()
    assert len(world.launches) == 1, "at most once per three days"
    world.advance(am.UNCLAIMED_AFTER_DAYS + 0.1)
    world.run()
    assert len(world.launches) == 2
    world.merged_prs.add(("carameli", 7))
    world.run()
    assert world.ran() == 1 and world.done(task)


def test_a_reroute_on_a_second_pc_does_not_double_up(world):
    world.queue('when = "pr:carameli#7"\nrun = "echo x"\n')
    world.run()
    world.host = "pc-b"
    world.run()
    world.advance(am.UNCLAIMED_AFTER_DAYS + 0.1)
    world.run()
    world.host = "pc-a"
    world.run()
    assert len(world.launches) == 1


# --- every state has a clock ------------------------------------------------------------------


def stuck(world) -> list[str]:
    return [line for line in world.findings() if "after-merge-stuck" in line]


def test_a_claim_refused_for_three_days_is_filed_once(world):
    world.queue(TOUCH)
    git_for = refusing_push(world)
    world.run(git_for=git_for)
    assert stuck(world) == []
    world.advance(am.UNCLAIMED_AFTER_DAYS + 0.1)
    world.run(git_for=git_for)
    world.run(git_for=git_for)
    assert len(stuck(world)) == 1 and world.ran() == 0


def test_a_memory_hold_for_three_days_is_filed(world):
    world.free_mb = 1000
    world.queue('agent = "x"\nwhen = "merged"\n')
    world.run()
    world.advance(am.UNCLAIMED_AFTER_DAYS + 0.1)
    world.run()
    assert len(stuck(world)) == 1


def test_a_spool_unwritable_for_three_days_is_filed(world):
    world.spool.mkdir()
    (world.spool / "restart-wsl-service.request").mkdir()
    world.queue('elevated = "restart-wsl-service"\nwhen = "merged"\n')
    world.run()
    world.advance(am.UNCLAIMED_AFTER_DAYS + 0.1)
    world.run()
    assert len(stuck(world)) == 1


def test_a_refused_launch_for_three_days_is_filed(world):
    world.launch_code = 1
    world.queue('agent = "x"\nwhen = "merged"\n')
    world.run()
    world.advance(am.UNCLAIMED_AFTER_DAYS + 0.1)
    world.run()
    assert len(stuck(world)) == 1


def test_a_fetch_failing_for_three_days_is_filed(world):
    git(world.checkout, "remote", "set-url", "origin", "../gone.git")
    world.run()
    assert not [line for line in world.findings() if "after-merge-unread" in line]
    world.advance(am.UNCLAIMED_AFTER_DAYS + 0.1)
    world.run()
    world.run()
    assert len([line for line in world.findings() if "after-merge-unread" in line]) == 1


def test_a_task_that_starts_stops_its_clock(world):
    task = world.queue(TOUCH)
    world.free_mb = 1000
    world.run()
    world.free_mb = 32 * 1024
    world.run()
    assert world.done(task)
    world.advance(am.UNCLAIMED_AFTER_DAYS + 0.1)
    world.run()
    assert stuck(world) == []


def test_a_stuck_finding_resolved_while_still_stuck_is_filed_again(world):
    world.free_mb = 1000
    world.queue('agent = "x"\nwhen = "merged"\n')
    world.run()
    world.advance(am.UNCLAIMED_AFTER_DAYS + 0.1)
    world.run()
    [item] = [row for row in after_merge_pass.triage.load(world.devkit) if "stuck" in row.detail]
    after_merge_pass.triage.resolve([item.id], "looked", root=world.devkit)
    world.run()
    assert len(stuck(world)) == 2


def test_an_elevated_task_with_no_worker_sends_an_agent_as_well_as_the_persons_step(world):
    task = world.queue('elevated = "restart-wsl-service"\nwhen = "merged"\n')
    world.run()
    assert world.launches == []
    world.advance(am.UNCLAIMED_AFTER_DAYS + 0.1)
    world.run()
    assert len(world.launches) == 1 and "restart-wsl-service" in world.launches[0][1]
    assert any(after_merge_pass.triage.PERSON_STEP in line for line in world.findings())
    assert world.on_remote(jc.handed_ref(task)) and not world.done(task)


# --- a slow or second PC -----------------------------------------------------------------------


def test_a_pc_that_wins_a_claim_after_another_finished_stops(world):
    """pc-b waits its memory turn while pc-a takes, finishes and releases the same task."""
    task = world.queue(TOUCH)
    world.free_mb = 8 * 1024

    def other_pc_runs(_seconds):
        host, world.host, world.free_mb = world.host, "pc-a", 32 * 1024
        world.run()
        world.host, world.free_mb = host, 8 * 1024

    world.host = "pc-b"
    world.run(sleep=other_pc_runs)
    assert world.ran() == 1 and world.done(task)


def test_an_elevated_request_is_not_written_twice_by_that_race(world):
    world.spool.mkdir()
    world.queue('elevated = "restart-wsl-service"\nwhen = "merged"\n')
    world.free_mb = 8 * 1024
    seen = []

    def other_pc_runs(_seconds):
        host, world.host = world.host, "pc-a"
        world.run(free_mb=lambda: 32 * 1024)
        (world.spool / "restart-wsl-service.request").unlink()
        world.run(free_mb=lambda: 32 * 1024)
        seen.append(world.host)
        world.host = host

    world.host = "pc-b"
    world.run(sleep=other_pc_runs)
    assert seen and not (world.spool / "restart-wsl-service.request").exists()


def reject_done(world) -> None:
    """A server-side `update` hook that refuses every done ref, as a bad day on GitHub does."""
    hooks = world.remote() / "hooks"
    hooks.mkdir(exist_ok=True)
    script = hooks / "update"
    script.write_text(
        '#!/bin/sh\ncase "$1" in refs/devkit/done/*) echo "fatal" >&2; exit 1;; esac\nexit 0\n',
        encoding="utf-8",
        newline="\n",
    )
    script.chmod(0o755)  # git on Linux ignores a hook without its executable bit
    git(world.remote(), "config", "core.hooksPath", "hooks")


def test_a_done_push_the_server_rejected_keeps_the_claim_and_never_reruns(world):
    reject_done(world)
    task = world.queue(TOUCH)
    world.run()
    assert not world.done(task) and world.claimed(task)
    world.host = "pc-b"
    world.run()
    world.host = "pc-a"
    world.run()
    assert world.ran() == 1, "the job ran once however often its done ref was refused"
    (world.remote() / "hooks" / "update").unlink()
    world.run()
    assert world.done(task) and not world.claimed(task) and world.ran() == 1


def test_a_task_reverted_while_the_pass_ran_an_earlier_one_does_not_run(world):
    world.queue('when = "merged"\nrun = "echo first"\n', name="a-first")
    second = world.queue(TOUCH, name="b-second")
    real = world.seams().runner

    def runner(command, timeout, **kwargs):
        if "echo first" in command:
            world.remove("carameli", f"{am.QUEUE_DIR}/b-second.toml")
        return real(command, timeout, **kwargs)

    world.run(runner=runner)
    assert world.ran() == 0 and not world.claimed(second) and not world.done(second)


def test_an_identity_that_cannot_be_confirmed_runs_nothing(world):
    """Fail closed: a task that may have been reverted waits for a fetch that works."""
    world.queue(TOUCH)
    real = world.seams().git_for
    fetches = []

    def git_for(path):
        call = real(path)

        def fake(*args, env=None):
            if args[0] == "fetch":
                fetches.append(args)
                if len(fetches) > 1:
                    return subprocess.CompletedProcess(args, 1, "", "no network")
            return call(*args, env=env)

        return fake

    world.run(git_for=git_for)
    assert world.ran() == 0 and len(fetches) >= 2


# --- running ----------------------------------------------------------------------------------


def test_a_command_that_leaves_a_background_child_is_a_success_not_a_hang(world, monkeypatch):
    monkeypatch.setattr(after_merge_pass, "RUN_TIMEOUT", 6)
    command = SLEEPING.format(py=sys.executable)
    task = world.queue(f"when = {json.dumps('merged')}\nrun = {json.dumps(command)}\n")
    world.run()
    assert world.result(task)["exit_code"] == 0 and world.done(task)


# --- every machine ------------------------------------------------------------------------------


def devkit_world(world) -> None:
    """Heartbeats live in devkit's repository, so the world needs one."""
    world.add_project("devkit")


def test_a_pass_records_its_host_in_devkits_repo_at_most_daily(world):
    devkit_world(world)
    world.run()
    ref = jc.host_ref("pc-a")
    assert world.on_remote(ref, "devkit")
    first = out(world.remote("devkit"), "rev-parse", ref)
    world.run()
    assert out(world.remote("devkit"), "rev-parse", ref) == first
    world.advance(1.1)
    world.run()
    assert out(world.remote("devkit"), "rev-parse", ref) != first


def test_an_every_machine_task_stays_until_each_counted_host_ran_it(world):
    devkit_world(world)
    task = world.queue('when = "merged"\non = "every-machine"\nrun = "echo x"\n')
    world.host = "pc-b"
    world.run(free_mb=lambda: 1000)
    world.host = "pc-a"
    world.run()
    boards = after_merge_pass._read_boards(world.workspace, ["carameli", "devkit"], world.seams())
    board = boards["carameli"]
    hosts = jc.live_hosts(boards["devkit"].refs, world.now())
    assert hosts == {"pc-a", "pc-b"}
    assert am.removable(board.tasks, board.done, hosts) == []
    assert am.removable(board.tasks, frozenset({f"{task}/pc-a", f"{task}/pc-b"}), hosts)


def test_a_host_unseen_for_thirty_days_is_not_counted():
    now = 1_800_000_000.0
    refs = [
        jc.Ref(jc.host_ref("pc-a"), "1", now - 29 * 86_400, "host pc-a seen"),
        jc.Ref(jc.host_ref("pc-b"), "2", now - 31 * 86_400, "host pc-b seen"),
    ]
    assert jc.live_hosts(refs, now) == {"pc-a"}


def test_with_no_host_list_an_every_machine_task_is_kept():
    task = am.Task("p", am.QUEUE_DIR + "/x.toml", "abc", on=am.EVERY_MACHINE, run="x")
    assert am.removable([task], frozenset({"abc/pc-a"})) == []


def test_a_host_back_after_the_file_left_is_told_once_naming_the_task(world):
    devkit_world(world)
    world.host = "pc-b"
    world.run()
    world.host = "pc-a"
    world.advance(31)
    task = world.queue('when = "merged"\non = "every-machine"\nrun = "echo x"\n', name="sync")
    world.run()
    assert world.done(task, "pc-a")
    world.remove("carameli", f"{am.QUEUE_DIR}/sync.toml")
    world.advance(1)
    world.host = "pc-b"
    world.run()
    world.run()
    missed = [line for line in world.findings() if "after-merge-missed" in line]
    assert len(missed) == 1 and "sync" in missed[0]


def test_a_new_host_is_not_told_of_every_task_before_it_existed(world):
    devkit_world(world)
    world.queue('when = "merged"\non = "every-machine"\nrun = "echo x"\n', name="sync")
    world.run()
    world.remove("carameli", f"{am.QUEUE_DIR}/sync.toml")
    world.host = "pc-new"
    world.run()
    assert [line for line in world.findings() if "after-merge-missed" in line] == []


def test_fix_loop_leaves_an_after_merge_tree_to_the_queue(world, tmp_path):
    """A restart under a handed session read "sent again" with nothing to send it."""
    fix_loop = after_merge_pass.fix_send.fix_loop
    tree = tmp_path / "tree"
    (tree / "logs").mkdir(parents=True)
    key = f"{am.SESSION_KEY}carameli/abc"
    fix_reports.stamp(tree, key, "after-merge", problem=key, agent="claude-bg")
    found = fix_reports.Tree("carameli", tree, "b", fix_reports.read_stamp(tree), [])
    ledger = tmp_path / "dispatch.json"
    ledger.write_text("{}", encoding="utf-8")

    class Context:
        writes = True
        ledger_path = ledger
        root = tmp_path

    journal = after_merge_pass.fix_findings.Journal(tmp_path)
    lines = fix_loop.judge_dead(Context, found, "w", fix_reports.INTERRUPTED, "t", journal)
    assert journal.findings == [] and "sent again" not in " ".join(lines)
    assert fix_reports.read_stamp(tree).get("dead") is None
