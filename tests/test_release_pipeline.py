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
