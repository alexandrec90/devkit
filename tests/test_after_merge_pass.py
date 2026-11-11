"""`scripts/after-merge-pass.py`: a project's queue, read off origin and carried out.

What each kind of task does, end to end against real refs (`after_merge_world`). What a
task's state may and may not become is `tests/test_after_merge_lifecycle.py`.
"""

from __future__ import annotations

import subprocess
import time

import elevated_request
import pytest
from after_merge_world import (
    TOUCH,
    after_merge_pass,
    am,
    git,
    jc,
    make_world,
    out,
    refusing_push,
    triage,
)


@pytest.fixture
def world(tmp_path, monkeypatch):
    return make_world(tmp_path, monkeypatch)


# --- a run task ---------------------------------------------------------------------------------


def test_a_due_command_runs_in_the_static_checkout_and_is_recorded_on_github(world):
    task = world.queue(TOUCH)
    assert world.run() == 0
    assert (world.checkout / "ran.txt").is_file()
    assert world.done(task) and not world.claimed(task), "done, and the claim given back"
    assert world.result(task)["status"] == am.PASSED
    assert world.result(task)["exit_code"] == 0
    assert world.launches == []


def test_a_finished_task_never_runs_again(world):
    world.queue(TOUCH)
    world.run()
    (world.checkout / "ran.txt").unlink()
    world.run()
    assert not (world.checkout / "ran.txt").exists()


def test_a_checkout_behind_origin_is_fast_forwarded_first(world):
    world.queue('run = "echo x"\nwhen = "merged"\n')
    world.run()
    assert out(world.checkout, "rev-parse", "HEAD") == out(world.remote(), "rev-parse", "main")


@pytest.mark.parametrize("state", ["branch", "dirty"])
def test_a_checkout_a_person_is_in_is_left_and_a_throwaway_tree_runs_it(world, state):
    """Skipped would be the dead end; moved would be someone's work lost."""
    if state == "branch":
        git(world.checkout, "checkout", "-q", "-b", "agent/theirs")
    else:
        (world.checkout / "README.md").write_text("edited\n", encoding="utf-8")
    task = world.queue(TOUCH)
    head = out(world.checkout, "rev-parse", "HEAD")
    world.run()
    assert not (world.checkout / "ran.txt").exists()
    assert out(world.checkout, "rev-parse", "HEAD") == head
    assert "after-merge" in world.result(task)["where"]
    assert world.done(task)
    assert len(out(world.checkout, "worktree", "list").splitlines()) == 1, "the tree was removed"


def test_a_failed_command_goes_to_an_agent_with_its_command_and_output(world):
    task = world.queue('run = "echo it-broke& exit 3"\nwhen = "merged"\n')
    world.run()
    [(name, prompt)] = world.launches
    assert name == "job" and "exit 3" in prompt and "it-broke" in prompt and "/ship" in prompt
    assert str(world.devkit) in prompt, "the whole output's path is in the prompt"
    result = world.result(task)
    assert result["exit_code"] == 3 and "it-broke" in result["output"]
    assert result["status"] == after_merge_pass.HANDED


def test_an_untrusted_checkout_refuses_the_command_and_files_it(world):
    real = world.seams().git_for

    def git_for(path):
        git_call = real(path)

        def fake(*args, env=None):
            if args[0] == "rev-parse" and "--is-inside-work-tree" in args:
                said = "fatal: detected dubious ownership in repository at x"
                return subprocess.CompletedProcess(args, 128, "", said)
            return git_call(*args, env=env)

        return fake

    task = world.queue(TOUCH)
    world.run(git_for=git_for)
    assert not (world.checkout / "ran.txt").exists()
    assert not world.done(task) and not world.claimed(task)
    assert any("dubious ownership" in line for line in world.findings())


def test_a_done_ref_that_failed_to_push_is_retried_without_running_again(world):
    task = world.queue(TOUCH)
    world.run()
    # As a pass that finished the job and lost the network before the done ref landed.
    git(world.remote(), "update-ref", "-d", jc.done_ref(task))
    (world.checkout / "ran.txt").unlink()
    world.run()
    assert not (world.checkout / "ran.txt").exists()
    assert world.done(task)


# --- an agent task --------------------------------------------------------------------------


def test_an_agent_task_starts_a_session_told_what_to_do(world):
    world.queue('agent = "Rebuild the index."\nwhen = "merged"\n')
    world.run()
    [(_, prompt)] = world.launches
    assert "Rebuild the index." in prompt and "/ship" in prompt


def test_a_malformed_file_reaches_an_agent_instead_of_being_dropped(world):
    world.queue('run = "no closing quote\n', name="broken")
    world.run()
    [(name, prompt)] = world.launches
    assert name == "broken" and "not a valid after-merge task" in prompt


def test_a_stray_file_in_the_queue_reaches_an_agent_too(world):
    world.write("carameli", f"{am.QUEUE_DIR}/notes.md", "remember to rebuild\n")
    world.run()
    [(_, prompt)] = world.launches
    assert "remember to rebuild" in prompt


@pytest.mark.parametrize("devkit_toml", ["", '[delivery]\nflow = "direct"\n'])
def test_a_session_named_for_a_pc_starts_there_whatever_its_fix_pass_is_set_to(world, devkit_toml):
    """Holding it would stall the task three days and send it elsewhere, which is the
    cross-PC case this queue exists for; an intent left a while is what the ship task
    is for, and a stalled task is what nothing picks up."""
    world.mode = "off"
    if devkit_toml:
        world.write("carameli", ".devkit.toml", devkit_toml)
    world.queue(f'agent = "x"\nwhen = "merged"\non = "{world.host}"\n')
    world.run()
    assert [name for name, _ in world.launches] == ["job"]


def test_a_failed_command_reaches_an_agent_whatever_the_fix_pass_is_set_to(world):
    world.mode = "off"
    task = world.queue('run = "exit 3"\nwhen = "merged"\n')
    world.run()
    assert world.result(task)["status"] == after_merge_pass.HANDED


# --- what is read, and when it counts --------------------------------------------------------


def test_the_queue_comes_from_origin_never_from_a_working_tree(world):
    queue = world.checkout / am.QUEUE_DIR
    queue.mkdir()
    (queue / "local.toml").write_text(TOUCH, encoding="utf-8")
    world.run()
    assert not (world.checkout / "ran.txt").exists() and world.launches == []


def test_a_revert_removes_the_task_with_its_file(world):
    world.queue('run = "echo x> ran.txt"\nwhen = "pr:carameli#5"\n')
    world.remove("carameli", f"{am.QUEUE_DIR}/job.toml")
    world.merged_prs.add(("carameli", 5))
    world.run()
    assert not (world.checkout / "ran.txt").exists()


def test_green_waits_for_the_green_branch_in_a_direct_project(world):
    world.write("carameli", ".devkit.toml", '[delivery]\nflow = "direct"\n')
    task = world.queue('run = "echo x> ran.txt"\n')
    world.run()
    assert not world.done(task), "a missing green branch is not yet"
    git(world.remote(), "branch", "green", "main")
    world.run()
    assert world.done(task)


def test_green_reads_as_merged_in_a_gated_project_with_no_green_branch_yet(world):
    """Every commit of its default branch passed the gate before it merged, and nothing
    should stall before the project adopts the branch."""
    task = world.queue('run = "echo x> ran.txt"\n')
    world.run()
    assert world.done(task)


@pytest.mark.parametrize("devkit_toml", ["", '[delivery]\nflow = "direct"\n'])
def test_a_green_branch_is_honoured_in_every_flow(world, devkit_toml):
    """The post-merge job advances it in gated projects too, and a commit it has not
    reached is not green there either."""
    if devkit_toml:
        world.write("carameli", ".devkit.toml", devkit_toml)
    git(world.remote(), "branch", "green", "main")
    task = world.queue('run = "echo x> ran.txt"\n')
    world.run()
    assert not world.done(task), "green is behind the commit that queued it"
    git(world.remote(), "branch", "-f", "green", "main")
    world.run()
    assert world.done(task)


def test_released_waits_for_a_tag_containing_the_commit(world):
    task = world.queue('run = "echo x"\nwhen = "released"\n')
    world.run()
    assert not world.done(task)
    git(world.remote(), "tag", "v1.0.0", "main")
    world.run()
    assert world.done(task)


def test_a_pr_condition_is_asked_of_rest_and_waits_for_merged(world):
    task = world.queue('run = "echo x"\nwhen = "pr:carameli#7"\n')
    world.run()
    assert not world.done(task)
    world.merged_prs.add(("carameli", 7))
    world.run()
    assert world.done(task)


def test_a_task_condition_can_wait_on_another_projects_queue(world):
    world.add_project("devkit")
    first = world.queue('run = "echo x"\nwhen = "released"\n', name="first", project="devkit")
    second = world.queue('run = "echo y"\nwhen = "task:devkit/first"\n')
    world.run()
    assert not world.done(second)
    git(world.remote("devkit"), "tag", "v1", "main")
    world.run()
    assert world.done(first, project="devkit")
    world.run()
    assert world.done(second)


def test_a_task_condition_survives_the_tidy_that_removed_the_file_it_names(world):
    world.queue('run = "echo x"\nwhen = "merged"\n', name="first")
    world.run()
    world.remove("carameli", f"{am.QUEUE_DIR}/first.toml")
    second = world.queue('run = "echo y"\nwhen = "task:first"\n', name="second")
    world.run()
    assert world.done(second)


# --- which PC ------------------------------------------------------------------------------------


def test_a_hostname_runs_only_there(world):
    task = world.queue('run = "echo x"\nwhen = "merged"\non = "pc-b"\n')
    world.run()
    assert not world.done(task)
    world.host = "pc-b"
    world.run()
    assert world.done(task)


def test_a_task_any_pc_can_take_is_taken_once(world):
    task = world.queue(TOUCH)
    world.run()
    world.host = "pc-b"
    (world.checkout / "ran.txt").unlink()
    world.run()
    assert not (world.checkout / "ran.txt").exists() and world.done(task)


def test_a_task_another_pc_has_claimed_is_left_to_it(world):
    task = world.queue(TOUCH)
    git(world.checkout, "fetch", "-q", "origin")
    assert jc.claim(after_merge_pass.sweep.git_for(world.checkout), task, jc.RUN, "pc-b").won
    world.run()
    assert not (world.checkout / "ran.txt").exists() and not world.done(task)
    assert world.slept == [], "a claim already seen is not worth waiting a turn for"


def test_a_stale_claim_of_a_gone_pc_is_broken_and_the_task_runs(world):
    task = world.queue(TOUCH)
    git(world.checkout, "fetch", "-q", "origin")
    long_ago = time.time() - jc.TTL[jc.RUN].total_seconds() - 60
    git_call = after_merge_pass.sweep.git_for(world.checkout)
    assert jc.claim(git_call, task, jc.RUN, "pc-b", long_ago).won
    world.run()
    assert world.done(task) and (world.checkout / "ran.txt").is_file()


def test_every_machine_runs_once_per_host_with_no_claim_between_them(world):
    task = world.queue('run = "echo x> ran.txt"\nwhen = "merged"\non = "every-machine"\n')
    world.run()
    assert world.done(task, "pc-a") and not world.claimed(task)
    (world.checkout / "ran.txt").unlink()
    world.run()
    assert not (world.checkout / "ran.txt").exists(), "once on each"
    world.host = "pc-b"
    world.run()
    assert world.done(task, "pc-b")


def test_a_network_failure_while_claiming_is_not_a_win(world):
    """The whole of why `FAILED` exists: try next cycle, never go ahead."""
    task = world.queue(TOUCH)
    world.run(git_for=refusing_push(world))
    assert not (world.checkout / "ran.txt").exists()
    world.run()
    assert (world.checkout / "ran.txt").is_file() and world.done(task)


def test_a_pc_under_the_memory_hold_claims_nothing(world):
    world.free_mb = 1000
    task = world.queue(TOUCH)
    world.run()
    assert not world.claimed(task) and not world.done(task)


def test_a_pc_waits_its_turn_once_a_pass_by_how_full_it_is(world):
    world.free_mb = 6000
    world.queue('run = "echo x"\nwhen = "merged"\n', name="one")
    world.queue('run = "echo y"\nwhen = "merged"\n', name="two")
    world.run()
    assert world.slept == [jc.delay_seconds(6000)]


def test_docker_is_only_asked_when_a_task_wants_it(world):
    asked = []

    def docker():
        asked.append(True)
        return True

    world.queue('run = "echo x"\nwhen = "merged"\n')
    world.run(docker=docker)
    assert asked == []
    task = world.queue('run = "echo y"\nwhen = "merged"\non = "docker"\n', name="rebuild")
    world.run(docker=docker)
    assert asked == [True] and world.done(task)


def test_a_machine_without_docker_leaves_a_docker_task(world):
    task = world.queue('run = "echo y"\nwhen = "merged"\non = "docker"\n')
    world.run()
    assert not world.done(task) and not world.claimed(task)


def test_a_host_gone_for_days_loses_its_task_to_an_agent_on_a_pc_that_is_awake(world):
    task = world.queue('run = "make db"\nwhen = "merged"\non = "never-seen"\n')
    world.run()
    world.advance(am.UNCLAIMED_AFTER_DAYS + 0.1)
    world.run()
    [(_, prompt)] = world.launches
    assert "never-seen" in prompt and "unreachable" in prompt and "make db" in prompt
    assert world.on_remote(jc.handed_ref(task)) and not world.done(task)


def test_the_first_sight_of_a_task_starts_its_clock_and_it_is_kept(world):
    task = world.queue('run = "x"\nwhen = "merged"\non = "never-seen"\n')
    world.run()
    first = after_merge_pass.read_state(world.seams())["seen"][f"carameli/{task}"]
    world.run()
    assert after_merge_pass.read_state(world.seams())["seen"][f"carameli/{task}"] == first
    assert world.launches == []


# --- elevated ----------------------------------------------------------------------------------


def test_an_elevated_task_writes_its_request_for_the_worker(world):
    world.spool.mkdir()
    task = world.queue('elevated = "restart-wsl-service"\nwhen = "merged"\n')
    world.run()
    assert [path.name for path in world.spool.iterdir()] == ["restart-wsl-service.request"], (
        "the name is the whole request, and nothing else belongs in the folder"
    )
    assert (world.spool / "restart-wsl-service.request").stat().st_size == 0, (
        "an unelevated process leaves only an empty file (elevated_worker.py)"
    )
    record = world.result(task)
    assert record["id"] == task and record["project"] == "carameli", "kept as a record"
    assert record["requested_by"] == world.host and record["status"] == "requested"


def test_the_queue_spells_the_request_as_the_worker_reads_it():
    """One spelling, owned by `elevated_request.py`, so a verb the worker gains or a spool
    it moves reaches the queue without a second edit."""
    assert am.ELEVATED_VERBS == elevated_request.VERBS
    assert am.ELEVATED_SPOOL == elevated_request.SPOOL
    assert am.REQUEST_SUFFIX == elevated_request.REQUEST_SUFFIX


@pytest.mark.parametrize("verb", am.ELEVATED_VERBS)
def test_the_request_file_is_exactly_the_verb_with_its_suffix(world, verb):
    world.spool.mkdir()
    world.queue(f'elevated = "{verb}"\nwhen = "merged"\n')
    world.run()
    assert (world.spool / f"{verb}.request").is_file()


def test_a_session_asking_for_a_reboot_gets_an_agent_told_why_it_did_not_happen(world):
    """No such verb, no request and no person's step: the worker is not what is missing."""
    world.spool.mkdir()
    world.queue('elevated = "reboot-when-idle"\nwhen = "merged"\n')
    world.run()
    [(_, prompt)] = world.launches
    assert "did not happen" in prompt and "reboot-when-idle" in prompt
    assert list(world.spool.iterdir()) == []
    assert not [line for line in world.findings() if triage.PERSON_STEP in line]


def test_no_elevated_task_means_no_persons_step_however_absent_the_worker(world):
    """A machine with no worker and nothing asking for one is told nothing."""
    world.queue(TOUCH)
    world.run()
    assert not [line for line in world.findings() if "after-merge-no-worker" in line]


def test_no_spool_folder_is_a_persons_step_and_the_task_stays_open(world):
    """The worker may yet be installed, and then it should still happen."""
    task = world.queue('elevated = "restart-wsl-service"\nwhen = "merged"\n')
    world.run()
    world.run()
    assert not world.done(task) and not world.claimed(task)
    [detail] = [line for line in world.findings() if "after-merge-no-worker" in line]
    assert triage.PERSON_STEP in detail and "restart-wsl-service" in detail
    assert "python -I -S scripts/install-elevated-worker.py --yes" in detail
    assert "administrator shell" in detail and world.host in detail
    world.spool.mkdir()
    world.run()
    assert (world.spool / "restart-wsl-service.request").is_file()


def test_a_spool_that_cannot_be_written_is_tried_next_cycle(world):
    world.spool.mkdir()
    (world.spool / "restart-wsl-service.request").mkdir()
    task = world.queue('elevated = "restart-wsl-service"\nwhen = "merged"\n')
    world.run()
    assert not world.done(task) and not world.claimed(task)


# --- the verbs -----------------------------------------------------------------------------------


def test_status_says_what_is_due_and_touches_nothing(world, capsys):
    task = world.queue(TOUCH)
    assert after_merge_pass.status(world.workspace, [], world.seams()) == 0
    said = capsys.readouterr().out
    assert f"carameli job [{task}]: take" in said
    assert not world.claimed(task) and not (world.checkout / "ran.txt").exists()


def test_tidy_now_takes_no_task_and_keeps_the_rest_of_the_state(world):
    finished = world.queue(TOUCH, name="finished")
    world.run()
    world.queue(TOUCH.replace("ran.txt", "new.txt"), name="waiting")
    seen = after_merge_pass.read_state(world.seams())["seen"]
    created = []

    def gh_for(path):
        real = world.gh_for(path)

        def gh(*args):
            created.append(args)
            return real(*args)

        return gh

    assert after_merge_pass.tidy_now(world.workspace, [], world.seams(gh_for=gh_for)) == 0
    assert not (world.checkout / "new.txt").exists(), "the queue is not run by a tidy"
    assert any(call[:2] == ("pr", "create") and finished in " ".join(call) for call in created)
    assert after_merge_pass.read_state(world.seams())["seen"] == seen


def test_a_pass_with_nothing_queued_reports_and_writes_its_state(world, capsys):
    seams = world.seams()
    assert isinstance(seams, after_merge_pass.Seams)
    assert after_merge_pass.run_pass(world.workspace, [], seams) == 0
    assert "1 queues read on pc-a" in capsys.readouterr().out
    assert set(after_merge_pass.read_state(seams)) == {"seen", "due", "unread", "tidied"}


def test_a_project_with_no_checkout_here_is_another_pcs(world):
    world.projects.append("elsewhere")
    world.write_workspace()
    assert after_merge_pass.here(world.workspace, []) == ["carameli"]


def test_a_named_project_narrows_the_pass(world):
    world.add_project("devkit")
    assert after_merge_pass.here(world.workspace, ["devkit"]) == ["devkit"]


def test_a_fetch_that_fails_is_next_cycles_and_no_other_projects_problem(world, capsys):
    world.add_project("devkit")
    git(world.ws / "devkit", "remote", "set-url", "origin", "../gone.git")
    task = world.queue(TOUCH)
    assert world.run() == 0
    assert world.done(task)
    assert "devkit: fetch failed" in capsys.readouterr().out


def test_a_session_is_stamped_as_the_passs_and_told_how_to_finish(world, monkeypatch):
    """Stamped, its PR merges itself and a dead one is the pass's to offer again. The prompt
    carries the fixer's frame, which is how a session knows nobody is watching."""
    tree = world.base / "tree"
    tree.mkdir()
    opened = {}

    def open_session(launch, where, branch, prompt, title):
        opened.update(launch=launch, where=where, prompt=prompt)
        return 0

    fix_trees = after_merge_pass.fix_trees
    monkeypatch.setattr(fix_trees, "cut_fresh_tree", lambda *a: (tree, "agent/after-merge-x-1106"))
    monkeypatch.setattr(fix_trees, "provision_tree", lambda path: ["provisioned"])
    monkeypatch.setattr(after_merge_pass.fix_send.fix_prs, "open_session", open_session)
    task = am.parse("carameli", f"{am.QUEUE_DIR}/x.toml", "a" * 12, 'agent = "do it"')
    board = after_merge_pass.Board("carameli", world.checkout, "main")
    code, said, where, branch = after_merge_pass.launch_session(task, board, am.task_prompt(task))
    assert code == 0 and "provisioned" in said
    assert where == str(tree) and branch == "agent/after-merge-x-1106"
    assert opened["launch"].agent == "claude-bg" and opened["where"] == tree
    assert opened["prompt"].startswith(after_merge_pass.fix_prompts.ROLE)
    assert "do it" in opened["prompt"] and "/ship" in opened["prompt"]
    assert "the fix pass commits, pushes" in opened["prompt"]
    assert "\n" not in opened["prompt"]
    stamp = after_merge_pass.fix_reports.read_stamp(tree)
    assert stamp["key"] == f"{am.SESSION_KEY}{am.seen_key(task)}"
    assert (tree / after_merge_pass.fix_reports.ORIGIN_FILE).is_file()


def test_a_session_with_no_tree_to_open_in_says_so(world, monkeypatch):
    monkeypatch.setattr(after_merge_pass.fix_trees, "cut_fresh_tree", lambda *a: (None, "x"))
    task = am.parse("carameli", f"{am.QUEUE_DIR}/x.toml", "a" * 12, 'agent = "do it"')
    board = after_merge_pass.Board("carameli", world.checkout, "main")
    code, said, _, _ = after_merge_pass.launch_session(task, board, "go")
    assert code == 1 and "could not cut" in said
    assert after_merge_pass.Session(code, said).tree == ""


def test_a_tree_with_no_stamp_has_no_session_left_to_wait_for(tmp_path):
    state = after_merge_pass.session_state(tmp_path, time.time())
    assert state == after_merge_pass.fix_reports.NO_OUTCOME


def test_a_cycle_waits_its_turn_once_and_counts_each_session_against_memory(world):
    journal = after_merge_pass.fix_findings.Journal(world.devkit)
    cycle = after_merge_pass.Cycle(world.seams(), journal, free_mb=8192)
    cycle.turn()
    cycle.turn()
    assert world.slept == [jc.delay_seconds(8192)]
    cycle.spent_session()
    assert cycle.free_mb == 8192 - after_merge_pass.host_memory.SESSION_MB
    assert cycle.machine("carameli").checkouts == {"carameli"}


def test_output_too_long_keeps_both_ends():
    text = "a" * 5000 + "z" * 5000
    kept = after_merge_pass.clipped(text, 1000)
    assert kept.startswith("aaa") and kept.endswith("zzz") and len(kept) < 1100
    assert after_merge_pass.clipped("short", 1000) == "short"


def test_docker_works_asks_the_daemon_and_a_missing_binary_is_no():
    assert after_merge_pass.docker_works(
        lambda argv, timeout: subprocess.CompletedProcess(argv, 0, "27.0\n", "")
    )
    assert not after_merge_pass.docker_works(
        lambda argv, timeout: subprocess.CompletedProcess(argv, 1, "", "no daemon")
    )

    def missing(argv, timeout):
        raise FileNotFoundError("docker")

    assert not after_merge_pass.docker_works(missing)


def test_the_parser_has_the_three_verbs():
    parser = after_merge_pass.build_parser()
    for verb in ("run", "status", "tidy"):
        assert parser.parse_args([verb, "carameli"]).projects == ["carameli"]


def test_main_refuses_a_missing_workspace(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        after_merge_pass.sweep, "default_workspace", lambda root: tmp_path / "nope.code-workspace"
    )
    assert after_merge_pass.main(["run"]) == 2
    assert "no workspace file" in capsys.readouterr().err
