"""Tests for scripts/release-pipeline.py (cutting a release as one click).

The suite is weighted towards `gate_verdict`, because that is the step the pipeline
exists to encode. Every other step is a command a human could type correctly; that one
is a judgement -- *this* red, and only this red, is the release's own chicken-and-egg --
and the cost of getting it wrong is a broken release tagged and adopted by five
consumers before anyone looks.

The plumbing that talks to `gh` is deliberately thin and is exercised through its pure
seams (`gate_state` over a rollup payload, `failing_test_names` over an artifact) rather
than by mocking a subprocess tree, which would pin `gh`'s flag spellings and nothing
else.
"""

import inspect
import subprocess
from pathlib import Path

from support import REPO_ROOT, load_script

rp = load_script("scripts/release-pipeline.py")
up = load_script("scripts/upgrade-project.py")
devkit_project = load_script("scripts/devkit_project.py")

TAGS = ["v0.1.0", "v0.9.0", "v0.10.0", "v0.11.1"]


# --- picking the version ------------------------------------------------------


def test_the_newest_release_is_the_highest_not_the_last_alphabetically():
    """`v0.9.0` sorts after `v0.11.1` as a string, and a pipeline that believes that
    cuts v0.9.1 over the top of eleven releases -- moving nothing, but pinning every
    project generated afterwards to a version behind the one it shipped with."""
    assert rp.newest_release(TAGS) == "v0.11.1"
    assert rp.newest_release(["v0.9.0", "v0.10.0"]) == "v0.10.0"


def test_a_bump_level_moves_the_newest_release():
    assert rp.next_version(TAGS, "patch") == ("v0.11.2", "")
    assert rp.next_version(TAGS, "minor") == ("v0.12.0", "")
    assert rp.next_version(TAGS, "major") == ("v1.0.0", "")


def test_a_minor_or_major_bump_resets_what_it_supersedes():
    """v0.11.1 -> v0.12.0, never v0.12.1: the patch component is a count within a minor
    line, so carrying it forward invents a release that was never cut."""
    assert rp.next_version(["v3.4.7"], "minor") == ("v3.5.0", "")
    assert rp.next_version(["v3.4.7"], "major") == ("v4.0.0", "")


def test_an_explicit_version_is_accepted_and_still_checked_for_reuse():
    assert rp.next_version(TAGS, "v2.0.0") == ("v2.0.0", "")
    version, refusal = rp.next_version(TAGS, "v0.9.0")
    assert version == "" and "immutable" in refusal


def test_a_level_that_is_neither_a_bump_nor_a_tag_is_refused():
    version, refusal = rp.next_version(TAGS, "0.11.2")
    assert version == ""
    assert "vMAJOR.MINOR.PATCH" in refusal


def test_a_repo_with_no_tags_still_resolves_to_a_first_release():
    """ "No tags" is the state this script exists to end, so it may not be the state that
    makes it crash."""
    assert rp.next_version([], "patch") == (rp.FIRST_VERSION, "")


def test_unparseable_tags_are_ignored_rather_than_ranked():
    """A repo carries tags that are not releases -- `nightly`, `v2-old`, a vendor pin."""
    assert rp.newest_release(["nightly", "v0.2.0", "v2-old"]) == "v0.2.0"
    assert rp.newest_release(["nightly"]) == ""


# --- reading the gate ---------------------------------------------------------


def check(name, conclusion="SUCCESS", status="COMPLETED"):
    return {"name": name, "status": status, "conclusion": conclusion, "detailsUrl": ""}


def test_an_empty_rollup_is_unknown_not_green():
    """A PR opened by a token gets no gate at all, and "no checks" read as "all passed"
    is how that failure merges itself."""
    assert rp.gate_state([]).state == "NONE"
    verdict, reason = rp.gate_verdict(rp.Gate("NONE"), [])
    assert verdict == rp.VERDICT_STOP
    assert "gh auth status" in reason


def test_a_running_check_holds_the_whole_gate_pending():
    gate = rp.gate_state([check("A"), check("B", conclusion="", status="IN_PROGRESS")])
    assert gate.state == "PENDING" and gate.pending == ("B",)
    assert rp.gate_verdict(gate, [])[0] == rp.VERDICT_WAIT


def test_a_skipped_or_neutral_check_is_not_a_failure():
    gate = rp.gate_state([check("A", "SKIPPED"), check("B", "NEUTRAL")])
    assert gate.state == "PASSED"


def test_a_legacy_commit_status_is_read_from_state():
    """`statusCheckRollup` mixes CheckRun entries with legacy StatusContext ones, which
    carry `context`/`state` instead of `name`/`conclusion`."""
    gate = rp.gate_state([{"context": "legacy", "state": "FAILURE"}])
    assert gate.state == "FAILED" and gate.failed == ("legacy",)


def test_the_expected_red_is_the_only_red_that_may_be_merged():
    gate = rp.Gate("FAILED", failed=(rp.GATE_TEST_JOB,))
    verdict, reason = rp.gate_verdict(gate, [rp.EXPECTED_RED_TEST])
    assert verdict == rp.VERDICT_PROCEED
    assert rp.EXPECTED_RED_TEST in reason


def test_a_second_failing_test_in_the_same_job_stops_the_release():
    """The failure this encodes: the expected red is present, so a human scanning the
    job name sees what they expected and merges. The second name is the broken release."""
    gate = rp.Gate("FAILED", failed=(rp.GATE_TEST_JOB,))
    verdict, reason = rp.gate_verdict(gate, [rp.EXPECTED_RED_TEST, "test_manifest_is_complete"])
    assert verdict == rp.VERDICT_STOP
    assert "test_manifest_is_complete" in reason


def test_a_failure_in_another_job_stops_the_release():
    gate = rp.Gate("FAILED", failed=("Pre-commit gate (the channel devkit publishes)",))
    verdict, reason = rp.gate_verdict(gate, [rp.EXPECTED_RED_TEST])
    assert verdict == rp.VERDICT_STOP
    assert "Pre-commit" in reason


def test_a_test_job_that_named_no_tests_stops_the_release():
    """The test job fails before the suite too -- markdown, format, lint, mypy, and the
    vendored hook tests all run first. An unreadable or absent artifact is therefore
    "nobody could look", which is the same evidence as "nothing failed" and must not be
    read as the safe one."""
    verdict, reason = rp.gate_verdict(rp.Gate("FAILED", failed=(rp.GATE_TEST_JOB,)), [])
    assert verdict == rp.VERDICT_STOP
    assert "lint" in reason


def test_a_fully_green_gate_proceeds():
    """Not the documented case -- it means CI resolved no tag to compare against, so the
    check skipped -- but green is no reason to strand a release."""
    verdict, _ = rp.gate_verdict(rp.Gate("PASSED"), [])
    assert verdict == rp.VERDICT_PROCEED


# --- waiting for the gate to settle -------------------------------------------


def watcher(monkeypatch, watch_results, rollups):
    """Drive `wait_for_checks` over scripted `gh` outcomes; returns the calls made."""
    calls: list[list[str]] = []
    results = iter(watch_results)
    payloads = iter(rollups)

    def run(cmd, cwd=None, capture=True):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, next(results))

    monkeypatch.setattr(rp, "_run", run)
    monkeypatch.setattr(rp, "pr_rollup", lambda _devkit, _number: next(payloads))
    monkeypatch.setattr(rp.time, "sleep", lambda _seconds: None)
    return calls


def test_a_rollup_that_is_still_running_does_not_end_the_wait(tmp_path, monkeypatch):
    """The failure this encodes stranded the v0.11.8 release. `--watch` lost the enqueue
    race and exited 1 with "no checks reported"; by the time the retry read the rollup
    the gate had appeared, so a non-empty rollup ended the wait while both jobs were
    still running -- and `gate_verdict` turned that PENDING gate into a STOP."""
    calls = watcher(
        monkeypatch,
        watch_results=[1, 1],
        rollups=[
            [check("A", conclusion="", status="IN_PROGRESS")],
            [check(rp.GATE_TEST_JOB, conclusion="FAILURE")],
        ],
    )

    rp.wait_for_checks(tmp_path, 256)

    assert len(calls) == 2
    assert all(call == ["gh", "pr", "checks", "256", "--watch"] for call in calls)


def test_a_settled_rollup_ends_the_wait(tmp_path, monkeypatch):
    """The other half: `--watch` exits 1 for a red gate too, and once nothing is running
    that red *is* the answer. Re-watching it would cost another blocking call and change
    no verdict."""
    calls = watcher(
        monkeypatch,
        watch_results=[1],
        rollups=[[check(rp.GATE_TEST_JOB, conclusion="FAILURE")]],
    )

    rp.wait_for_checks(tmp_path, 256)

    assert len(calls) == 1


def test_an_empty_rollup_keeps_waiting(tmp_path, monkeypatch):
    """ "No checks reported" with nothing in the rollup is the race itself -- the case
    the retry loop was written for, which the settled check must not regress."""
    calls = watcher(
        monkeypatch,
        watch_results=[1, 0],
        rollups=[[]],
    )

    rp.wait_for_checks(tmp_path, 256)

    assert len(calls) == 2


def test_the_retry_is_bounded_by_the_deadline(tmp_path, monkeypatch):
    """Waiting on a settled rollup is now the only way out of the loop, so the deadline
    is the thing standing between a gate that never settles and a pipeline that never
    returns. It reports WAIT and stops, which a human can act on."""
    clock = iter([0.0, rp.CHECKS_APPEAR_TIMEOUT + 1.0])
    monkeypatch.setattr(rp.time, "monotonic", lambda: next(clock))
    calls = watcher(monkeypatch, watch_results=[1], rollups=[])

    rp.wait_for_checks(tmp_path, 256)

    assert len(calls) == 1


# --- reading the failures out of the artifact ---------------------------------


ARTIFACT_SAMPLE = """\
# source: devkit scripts/run-tests.py
=================================== FAILURES ===================================
_________________ test_fallback_devkit_ref_tracks_the_newest_tag _______________
    assert tag == new_project.FALLBACK_DEVKIT_REF
E   AssertionError: FALLBACK_DEVKIT_REF is 'v0.11.2' but devkit's newest tag is 'v0.11.1'
=========================== short test summary info ============================
FAILED tests/test_new_project.py::test_fallback_devkit_ref_tracks_the_newest_tag - Asse
"""


def test_the_failing_test_is_read_from_either_spelling():
    assert rp.failing_test_names(ARTIFACT_SAMPLE) == [rp.EXPECTED_RED_TEST]


def test_a_summary_line_is_enough_when_the_block_was_truncated():
    """`run-tests.py` caps each failure block, so a run with many failures can lose a
    banner and keep its summary line. Reading one source only would under-report --
    which here means merging on a failure list that was quietly incomplete."""
    capped = "... (60 lines total, truncated)\nFAILED tests/test_sweep.py::test_classify - X\n"
    assert rp.failing_test_names(capped) == ["test_classify"]


def test_a_parametrised_failure_keeps_its_case():
    text = "FAILED tests/test_ports.py::test_slot[carameli-8080] - AssertionError\n"
    assert rp.failing_test_names(text) == ["test_slot[carameli-8080]"]


def test_an_artifact_with_no_failures_names_nothing():
    assert rp.failing_test_names("") == []
    assert rp.failing_test_names("3 passed in 1.2s\n") == []


# --- odds and ends the executor depends on ------------------------------------


def test_the_pr_number_is_read_out_of_the_url_gh_prints():
    assert rp.pr_number_from_url("https://github.com/a/b/pull/173\n") == 173
    assert rp.pr_number_from_url("nothing useful") == 0


def test_the_run_id_is_read_from_the_failing_jobs_details_url():
    rollup = [
        check("other"),
        {
            "name": rp.GATE_TEST_JOB,
            "status": "COMPLETED",
            "conclusion": "FAILURE",
            "detailsUrl": "https://github.com/a/b/actions/runs/1234567/job/99",
        },
    ]
    assert rp.failed_run_id(Path("."), rollup) == 1234567


def test_the_plan_says_whether_consumers_will_be_upgraded():
    """The dry run is the only place the blast radius is stated before it happens, and
    "opens a PR in five repositories" is the part worth seeing first."""
    assert any("upgrade-project.py" in step for step in rp.plan_steps("v1.0.0", adopt=True))
    assert not any("upgrade-project.py" in step for step in rp.plan_steps("v1.0.0", adopt=False))


def test_the_plan_names_the_consumers_that_were_ticked():
    """Blast radius is the whole point of the dry run, so a narrowed run must not print
    the sentence a full one does. `every consumer` is the honest phrasing of `--all`."""
    narrowed = rp.plan_steps("v1.0.0", adopt=True, projects=["carameli", "data-lake"])
    step = next(s for s in narrowed if "upgrade-project.py" in s)
    assert "carameli, data-lake" in step
    assert "every consumer" not in step

    everywhere = next(s for s in rp.plan_steps("v1.0.0", adopt=True) if "upgrade-project.py" in s)
    assert "every consumer" in everywhere


# --- which consumers adopt: the checklist, not `--all` ------------------------


def test_an_unticked_checklist_still_reaches_every_consumer():
    """`--all` is what the scheduled pass wants and what this script did unconditionally
    before the picker existed: a release nobody is watching should reach every consumer,
    and only a human at the dropdown has a reason to narrow it."""
    assert rp.adoption_scope([]) == "--all"


def test_a_ticked_selection_is_one_argv_token():
    """`upgrade-project.py` takes the names as a positional, and `plan_command` drops
    empty arguments -- so a flag and a value could leave a dangling flag. One token."""
    assert rp.adoption_scope(["carameli", "data-lake"]) == "carameli,data-lake"


def test_the_run_never_spells_the_scope_a_second_time():
    """The scope appears three times -- the argv handed to `upgrade-project.py`, the dry
    run's plan, and the retry line printed when adoption fails -- and a retry naming a
    different scope from the run it retries is a remedy for something else. So
    `adoption_scope` is the only place `--all` is written."""
    source = inspect.getsource(rp.run_pipeline)
    assert "adoption_scope(" in source
    assert '"--all"' not in source

    step = next(
        s
        for s in rp.plan_steps("v1.0.0", adopt=True, projects=["carameli"])
        if "upgrade-project.py" in s
    )
    assert rp.adoption_scope(["carameli"]) in step


def test_backing_out_of_the_consumer_checklist_cuts_no_release(capsys):
    """The other half of the pair in `upgrade-project`: there an escaped picker is a
    graceful no-op, because the picker IS the subject. Here the click is the whole
    release, so escaping refuses before anything is tagged -- and it must refuse before
    `gh` is even probed, or a machine without it would report the wrong reason."""
    assert rp.main(["--projects=${input:adoptProjects}"]) == 1
    err = capsys.readouterr().err
    assert "nothing was picked" in err
    # `--no-adopt` is the spelling for "release, but adopt nowhere"; the refusal has to
    # name it, or the reader's only way out of the loop is to tick a box they don't want.
    assert "--no-adopt" in err


def test_the_projects_flag_is_the_checklists_own_spelling():
    """The task passes `--projects=${input:adoptProjects}`, so the flag has to exist,
    default to every consumer, and read through the same splitter the positional does."""
    assert rp.build_parser().parse_args([]).projects == ""
    parsed = rp.build_parser().parse_args(["--projects=carameli,data-lake"])
    assert rp.upgrade_module().project_selection(parsed.projects) == ["carameli", "data-lake"]


# --- the seams between this script and the two files that quote it ------------


def test_the_remedy_upgrade_project_prints_is_this_scripts_cli():
    """`upgrade-project.py` tells its reader to run this, in a string it cannot check.
    A remedy printed in one file and implemented in another is the pairing that goes
    stale in silence -- nothing runs the sentence, so nothing reports it."""
    tokens = up.RELEASE_COMMAND.split()
    assert tokens[0] == "python"
    assert (REPO_ROOT / tokens[1]).is_file()
    args = rp.build_parser().parse_args(tokens[2:])
    assert args.level == "patch"
    assert args.dry_run is False


def test_the_task_upgrade_project_names_is_the_task_that_exists():
    """Same pairing, other half: the artifact tells the reader to click a task by name,
    and `ACTIONS` is what decides that name."""
    assert devkit_project.ACTIONS["release"].label == up.RELEASE_TASK
    assert devkit_project.ACTIONS["release"].script == "scripts/release-pipeline.py"


def test_the_release_task_is_scoped_to_devkit_alone():
    """A release is devkit's own act. Unscoped, the action would be offered for every
    checkout in the picker and would cut a devkit release from whichever one was
    chosen -- the same class of mistake `reclaim` and `preview` are scoped against."""
    assert devkit_project.ACTIONS["release"].projects == devkit_project.DEVKIT_ONLY


# --- the trigger predicate ------------------------------------------------------
#
# `--if-needed` is what turns a click into a nightly job, and the whole of its judgement
# is `deliverable_changes`. Pure and tested from filename lists rather than from a
# repository, because the question "would tonight cut a release?" has to be answerable
# without putting a checkout into a particular state.

VENDORED = [
    "scripts/hooks/enforce-capped-bash.py",
    "scripts/hooks/harness_config.py",
    "scripts/sync-devkit.py",
    ".claude/rules/engineering.md",
]


def test_a_vendored_change_is_deliverable_only_by_a_release():
    """The state that made `sync-devkit.py --check` red in all five consumers at once:
    the fix is merged, devkit's CI is green, and no tag carries it."""
    changed = ["scripts/hooks/enforce-capped-bash.py", "README.md"]
    assert rp.deliverable_changes(changed, VENDORED) == ["scripts/hooks/enforce-capped-bash.py"]


def test_devkits_own_changes_are_not_a_reason_to_release():
    """The half that makes a nightly cadence affordable. A doc fix, a test and a
    generator change reach no consumer, so tagging them spends a release and an adoption
    PR per project to deliver nothing anyone can run."""
    changed = [
        "README.md",
        "tests/test_release_pipeline.py",
        "scripts/new-project.py",
        "scripts/release-pipeline.py",
        ".github/workflows/pr-gate.yml",
    ]
    assert rp.deliverable_changes(changed, VENDORED) == []


def test_the_published_pre_commit_channel_counts_too():
    """The tier `MANIFEST` does not cover, and the one a predicate written from the
    vendored list alone would miss entirely. A consumer reaches these files by pinning a
    `rev`, so they are unreachable until a tag exists -- which is how a generated project
    once asked for hook ids its pinned tag could not serve."""
    assert rp.deliverable_changes(["scripts/precommit/devkit_drift.py"], VENDORED) == [
        "scripts/precommit/devkit_drift.py"
    ]
    assert rp.deliverable_changes([".pre-commit-hooks.yaml"], VENDORED) == [
        ".pre-commit-hooks.yaml"
    ]


def test_devkits_own_pre_commit_wiring_is_not_published():
    """`.pre-commit-config.yaml` sits beside the published file and reaches nobody --
    devkit wires its own hooks as `repo: local`. A prefix match on `.pre-commit` would
    release for an edit to it."""
    assert rp.deliverable_changes([".pre-commit-config.yaml"], VENDORED) == []


def test_a_directory_entry_matches_what_is_under_it_and_not_a_lookalike():
    assert rp.in_published_channel("scripts/precommit/_loader.py")
    assert not rp.in_published_channel("scripts/precommit_helpers.py")
    assert not rp.in_published_channel("scripts/precommit")


def test_windows_path_separators_do_not_hide_a_deliverable_change():
    """`git diff --name-only` answers in forward slashes, but nothing in this pipeline
    guarantees its caller does -- and a separator mismatch would read as "quiet night"."""
    assert rp.deliverable_changes([r"scripts\hooks\harness_config.py"], VENDORED) == [
        "scripts/hooks/harness_config.py"
    ]


def test_the_result_is_deduplicated_and_ordered():
    """It is printed into the artifact as the reason the job fired, so it has to read
    the same way twice."""
    changed = ["scripts/sync-devkit.py", "scripts/sync-devkit.py", ".pre-commit-hooks.yaml"]
    assert rp.deliverable_changes(changed, VENDORED) == [
        ".pre-commit-hooks.yaml",
        "scripts/sync-devkit.py",
    ]


def test_blank_lines_in_a_diff_are_not_files():
    assert rp.deliverable_changes(["", "   ", "scripts/sync-devkit.py"], VENDORED) == [
        "scripts/sync-devkit.py"
    ]


def test_an_unreadable_checkout_answers_nothing_owed(tmp_path):
    """Best-effort by design, and biased towards the quiet answer: a false negative
    costs a night's delay and the nightly upgrade pass still reports that a release is
    owed, while a false positive would cut one from a state this could not read."""
    assert rp.release_needed(tmp_path, "v0.11.1") == []


def test_the_flag_that_carries_the_predicate_exists_and_is_off_by_default():
    """A click releases what it was asked to release; only the scheduled pass asks the
    question first."""
    assert rp.build_parser().parse_args([]).if_needed is False
    assert rp.build_parser().parse_args(["--if-needed"]).if_needed is True


# --- the console discipline the scheduled caller needs --------------------------


def test_every_spawn_carries_the_no_window_flag():
    """Asserted here as well as in `test_scheduled_jobs.py` because this module's single
    spawn site is the thing that makes the blanket rule cheap: one keyword, not a rule
    every future `gh` call has to remember."""
    source = (REPO_ROOT / "scripts" / "release-pipeline.py").read_text(encoding="utf-8")
    assert "creationflags=NO_WINDOW" in source


def test_a_streaming_child_is_handed_the_callers_handles(tmp_path, monkeypatch):
    """The other half of the flag: it gives the child a console of its own, so a
    non-capturing child writes there instead of into `log-wrap.py`'s pipe. Without this
    the nightly artifact would say `# exit: 0` and nothing else."""
    handle = (tmp_path / "out.txt").open("w", encoding="utf-8")
    monkeypatch.setattr(rp.sys, "stdout", handle)
    monkeypatch.setattr(rp.sys, "stderr", handle)
    try:
        assert rp.inherited_streams() == {"stdout": handle, "stderr": handle}
    finally:
        handle.close()


def test_a_stream_with_no_descriptor_is_left_to_inherit(monkeypatch):
    """`None` under a bare `pythonw.exe`, a capture object under pytest. Both would
    raise if handed to `subprocess`."""

    class Captured:
        def fileno(self):
            raise OSError("not a real descriptor")

    monkeypatch.setattr(rp.sys, "stdout", None)
    monkeypatch.setattr(rp.sys, "stderr", Captured())
    assert rp.inherited_streams() == {}


# --- what the predicate reads, before it judges it ------------------------------
#
# `deliverable_changes` above is pure and thoroughly covered; these two are the halves
# that go out to the repository for its arguments, and a wrong answer from either is a
# release that does not fire (or fires nightly) with nothing in the log to say why.


def test_the_vendored_list_is_the_manifest_and_not_a_second_spelling_of_it():
    """Two lists of "what is vendored" would agree until the day one of them was the
    reason a release did not fire. This one is the manifest's owner, read through it."""
    paths = rp.vendored_paths()
    assert paths == list(up.manifest_paths())
    assert "scripts/sync-devkit.py" in paths


def test_changes_since_a_tag_are_the_ones_the_default_branch_carries(tmp_path):
    """The diff is `tag..origin/<default>`, not `tag..HEAD`: the pipeline runs from a
    checkout whose local branch may be anywhere, and the question is what *main* has."""
    run = _a_repo(tmp_path)
    (tmp_path / "released.py").write_text("", encoding="utf-8")
    run("add", "-A")
    run("commit", "-m", "released")
    run("tag", "v0.0.1")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "sync-devkit.py").write_text("", encoding="utf-8")
    run("add", "-A")
    run("commit", "-m", "unreleased")
    run("update-ref", "refs/remotes/origin/main", "HEAD")
    run("checkout", "--quiet", "-b", "somewhere-else")

    assert rp.changed_since_tag(tmp_path, "v0.0.1") == ["scripts/sync-devkit.py"]


def test_a_tag_the_checkout_does_not_have_answers_nothing_rather_than_raising(tmp_path):
    """`git diff` exits non-zero on an unknown revision, and the caller's contract is
    best-effort: a night this cannot read is a night that does not release."""
    run = _a_repo(tmp_path)
    (tmp_path / "a.py").write_text("", encoding="utf-8")
    run("add", "-A")
    run("commit", "-m", "only commit")
    run("update-ref", "refs/remotes/origin/main", "HEAD")

    assert rp.changed_since_tag(tmp_path, "v9.9.9") == []


def test_a_directory_that_is_not_a_checkout_answers_nothing(tmp_path):
    assert rp.changed_since_tag(tmp_path, "v0.0.1") == []


def _a_repo(root):
    """A real repository, because `changed_since_tag` resolves the default branch and
    then diffs against a remote-tracking ref -- two behaviours a fake `git` would only
    restate."""

    def run(*args):
        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            check=True,
        )

    run("init", "--quiet")
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "Test")
    run("config", "commit.gpgsign", "false")
    return run
