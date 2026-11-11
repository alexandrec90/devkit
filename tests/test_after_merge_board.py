"""`scripts/after_merge_board.py`: a project's queue as origin has it, and the facts it is
judged on. What is read from where, end to end against real refs, is in
`tests/test_after_merge_pass.py`; these drive each answer directly."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from support import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import after_merge as am
import after_merge_board as board_module
import job_claims as jc

SHA = "a" * 40
PATH = f"{am.QUEUE_DIR}/job.toml"


def done(args, out: str = "", code: int = 0, err: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, code, out, err)


def origin(blobs: dict[str, str], unread: tuple[str, ...] = (), when: str = "c1 100"):
    """A git holding `blobs` by path. The sha of each is its path's own letter run."""

    def git(*args, env=None):
        if args[0] == "ls-tree":
            rows = [f"100644 blob {path[-8]}{'0' * 39}\t{path}" for path in blobs]
            rows.append("160000 commit deadbeef\tvendor/sub")
            return done(args, "\0".join(rows) + "\0")
        if args[0] == "show":
            found = next(p for p in blobs if args[1].startswith(p[-8]))
            return done(args, code=1) if found in unread else done(args, blobs[found])
        if args[0] == "log":
            return done(args, when)
        return done(args)

    return git


def test_every_file_under_the_queue_is_read_off_the_ref():
    tasks = board_module.read_tasks(origin({PATH: 'run = "x"'}), "carameli", "main")
    assert [task.run for task in tasks] == ["x"]
    assert tasks[0].commit == "c1" and tasks[0].added == 100.0


def test_a_blob_that_could_not_be_read_is_left_for_next_cycle_not_parsed():
    """Read as an empty file it would be a malformed task for nothing that is wrong."""
    git = origin({PATH: 'run = "x"'}, unread=(PATH,))
    assert board_module.read_tasks(git, "carameli", "main") == []


def test_a_submodule_entry_is_not_a_task():
    git = origin({})
    assert board_module.read_tasks(git, "carameli", "main") == []


def test_a_task_whose_adding_commit_cannot_be_found_has_no_identity_yet():
    """Next cycle's to read: its id is what tells it from every task before it."""
    assert board_module.read_tasks(origin({PATH: 'run = "x"'}, when=""), "carameli", "main") == []


def test_a_task_is_identified_by_the_commit_that_added_it():
    asked = []
    real = origin({PATH: 'run = "x"'})

    def git(*args, env=None):
        asked.append(args)
        return real(*args, env=env)

    [task] = board_module.read_tasks(git, "carameli", "main")
    assert task.id == am.task_id(PATH, "c1") and task.commit == "c1" and task.added == 100.0
    assert any(args[0] == "log" and "--diff-filter=A" in args for args in asked)


def test_read_task_reads_one_file_or_says_it_could_not():
    git = origin({PATH: 'run = "x"'})
    blob = f"{PATH[-8]}{'0' * 39}"
    found = board_module.read_task(git, "carameli", "main", PATH, blob)
    assert found is not None and found.run == "x"
    unread = origin({PATH: 'run = "x"'}, unread=(PATH,))
    assert board_module.read_task(unread, "carameli", "main", PATH, blob) is None


def serving(tree: str, fetch: int = 0, added: str = "c1 100"):
    """A git whose `origin/main` holds `tree` at `PATH`, "" for no such file."""

    def git(*args, env=None):
        if args[0] == "fetch":
            return done(args, code=fetch, err="no network")
        if args[0] == "ls-tree":
            row = f"100644 blob abc\t{PATH}\0" if tree else ""
            return done(args, row)
        if args[0] == "log":
            return done(args, added)
        return done(args, tree)

    return git


def test_a_task_still_queued_comes_back_as_it_now_reads():
    task = am.parse("carameli", PATH, am.task_id(PATH, "c1"), 'run = "old"')
    fresh, why = board_module.current(serving('run = "new"'), task, "main")
    assert fresh is not None and fresh.run == "new" and why == ""


def test_a_task_whose_file_is_gone_is_gone():
    task = am.parse("carameli", PATH, am.task_id(PATH, "c1"), 'run = "old"')
    assert board_module.current(serving(""), task, "main") == (None, "")


def test_a_file_added_again_since_is_another_task():
    task = am.parse("carameli", PATH, am.task_id(PATH, "c1"), 'run = "old"')
    assert board_module.current(serving('run = "x"', added="c2 200"), task, "main") == (None, "")


def test_origin_that_cannot_be_read_says_so_rather_than_saying_gone():
    task = am.parse("carameli", PATH, am.task_id(PATH, "c1"), 'run = "old"')
    fresh, why = board_module.current(serving('run = "x"', fetch=1), task, "main")
    assert fresh is None and "could not be read" in why


def test_a_fetch_that_failed_is_a_board_saying_so():
    def git(*args, env=None):
        return done(args, code=1, err="line one\nfatal: unable to access\n")

    found = board_module.read_board("carameli", Path("x"), git)
    assert "fatal: unable to access" in found.error and found.tasks == ()


def test_said_by_falls_back_to_the_exit_code():
    assert board_module.said_by(done(("x",), code=7)) == "exit 7"


def test_the_flow_is_read_off_origin_too():
    def git(*args, env=None):
        if args[0] == "show":
            return done(args, '[delivery]\nflow = "direct"\n')
        return done(args, "main\n" if args[0] == "symbolic-ref" else "")

    assert board_module.read_board("carameli", Path("x"), git).flow == am.DIRECT


def test_a_project_with_no_devkit_toml_is_gated():
    def git(*args, env=None):
        return done(args, code=1 if args[0] == "show" else 0)

    assert board_module.read_board("carameli", Path("x"), git).flow == am.GATED


def ref(name: str, subject: str) -> jc.Ref:
    return jc.Ref(name, "1", 0.0, subject)


def test_a_board_knows_what_is_done_by_id_and_by_name():
    board = board_module.Board(
        "carameli",
        Path("x"),
        refs=(
            ref(jc.done_ref("abc"), "done carameli/rebuild by pc-a: passed"),
            ref(jc.claim_ref("def"), "claim run by pc-a at now"),
        ),
    )
    assert board.done == {"abc"} and board.done_names == {"rebuild": frozenset({"abc"})}


def test_a_board_reads_each_state_of_a_task_off_its_refs():
    subject = "handed carameli/job by pc-a: attempt 2"
    board = board_module.Board(
        "carameli",
        Path("x"),
        refs=(
            ref(jc.handed_ref("abc"), subject),
            ref(jc.handed_ref("odd"), "something else"),
            ref(jc.failed_ref("def"), "failed carameli/x by pc-a: why"),
            ref(jc.rerouted_ref("ghi"), "rerouted carameli/y by pc-a"),
        ),
    )
    assert board.marks.handed == {"abc": am.Handed("pc-a", 2, 0.0)}
    assert board.marks.failed == {"def"} and board.marks.rerouted == {"ghi": 0.0}


def test_a_done_ref_without_a_success_word_is_not_a_success():
    board = board_module.Board(
        "carameli",
        Path("x"),
        refs=(ref(jc.done_ref("abc"), "done carameli/job by pc-a: gave up"),),
    )
    assert board.done == {"abc"} and board.successes == frozenset()
    assert board.done_names == {} and board.done_rows == [("abc", 0.0, "job")]


def facts(
    flow: str = am.GATED,
    code: int = 0,
    tags: str = "",
    api: str = "true",
    boards=None,
    green_branch: bool = True,
):
    calls: list[tuple[str, ...]] = []

    def git(*args, env=None):
        calls.append(args)
        if args[0] == "rev-parse":
            return done(args, code=0 if green_branch else 1)
        return done(args, tags, code)

    def gh(*args):
        calls.append(args)
        return done(args, api)

    board = board_module.Board("carameli", Path("x"), flow=flow)
    found = board_module.facts_for(board, boards or {}, "pc-a", True, git, gh)
    return found, calls


def test_no_green_branch_yet_is_merged_in_a_gated_project_and_not_yet_in_a_direct_one():
    assert facts(am.GATED, green_branch=False)[0].green("abc")
    assert not facts(am.DIRECT, green_branch=False)[0].green("abc")


@pytest.mark.parametrize("flow", [am.GATED, am.DIRECT])
def test_a_green_branch_decides_in_either_flow(flow):
    found, calls = facts(flow)
    assert found.green("abc")
    assert calls[-1] == ("merge-base", "--is-ancestor", "abc", "origin/green")
    assert not facts(flow, code=1)[0].green("abc")
    assert not facts(flow)[0].green("")


def test_released_needs_a_tag_containing_the_commit():
    assert facts(tags="v1.0.0\n")[0].released("abc")
    assert not facts(tags="")[0].released("abc")
    assert not facts(tags="v1\n")[0].released("")


def test_a_bare_repo_name_is_asked_of_this_checkouts_owner():
    found, calls = facts()
    assert found.merged("carameli", 7)
    assert calls[-1][:2] == ("api", "repos/{owner}/carameli/pulls/7")


def test_an_owner_named_is_used_as_it_stands():
    found, calls = facts()
    found.merged("someone/else", 7)
    assert calls[-1][1] == "repos/someone/else/pulls/7"


def test_an_unmerged_pr_is_not_merged():
    assert not facts(api="false")[0].merged("carameli", 7)


def test_a_task_in_a_project_this_machine_has_not_read_is_not_done():
    assert not facts()[0].done("elsewhere", "first")


def test_a_task_condition_reads_the_other_projects_board():
    other = board_module.Board(
        "devkit", Path("y"), refs=(ref(jc.done_ref("abc"), "done devkit/first by pc-a: passed"),)
    )
    assert facts(boards={"devkit": other})[0].done("devkit", "first")
