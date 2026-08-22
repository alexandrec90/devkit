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
