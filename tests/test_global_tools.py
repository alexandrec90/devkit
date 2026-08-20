"""What the nightly global-npm pass has to keep true.

Three properties carry the weight here, and each one is a way this job could quietly
become worse than not having it:

1. **The rollback line survives.** An unpinned auto-update whose record is erased by the
   next night's pass is untraceable by construction -- the breakage is noticed days
   after the bump. `trim` and `write_artifact` are what stop that.
2. **`npm` and Claude Code are never updated by it**, because both are things that would
   have to still work in order to undo a bad update.
3. **A laptop with no network is not a failure.** A job whose alerts fire on the normal
   case is a job whose alerts nobody reads.

Nothing here spawns npm. Every function that would is given a runner.
"""

from __future__ import annotations

import datetime as _dt
import json
import subprocess

import pytest
from support import load_script

global_tools = load_script("scripts/global-tools.py")


class FakeRunner:
    """Records argvs and answers from a queue, falling back to a clean exit."""

    def __init__(self, *, outdated: str = "{}", results: dict[str, tuple[int, str]] | None = None):
        self.calls: list[list[str]] = []
        self.outdated = outdated
        self.results = results or {}

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        if "outdated" in argv:
            return subprocess.CompletedProcess(argv, 1, self.outdated, "")
        spec = argv[-1]
        code, message = self.results.get(spec, (0, ""))
        return subprocess.CompletedProcess(argv, code, "", message)

    @property
    def installed(self) -> list[str]:
        return [call[-1] for call in self.calls if "install" in call]


def outdated_payload(**packages: tuple[str, str]) -> str:
    return json.dumps(
        {
            name: {"current": current, "latest": latest, "wanted": latest, "dependent": "global"}
            for name, (current, latest) in packages.items()
        }
    )


def at(text: str = "2026-08-19 04:30"):
    stamp = _dt.datetime.strptime(text, "%Y-%m-%d %H:%M")
    return lambda: stamp


# --- reading npm's answer ------------------------------------------------------


def test_a_package_behind_its_latest_is_reported():
    entries = global_tools.parse_outdated(outdated_payload(eslint=("10.0.1", "10.8.1")))
    assert entries == [global_tools.Behind("eslint", "10.0.1", "10.8.1")]


def test_entries_come_back_sorted_so_the_artifact_reads_the_same_every_night():
    payload = outdated_payload(stylelint=("1", "2"), eslint=("1", "2"), npm=("1", "2"))
    assert [entry.name for entry in global_tools.parse_outdated(payload)] == [
        "eslint",
        "npm",
        "stylelint",
    ]


def test_a_package_npm_lists_more_than_once_is_read_from_the_list_form():
    """npm maps a name to a *list* when the package is present more than once, and a
    reader expecting a dict silently drops it."""
    payload = json.dumps({"eslint": [{"current": "10.0.1", "latest": "10.8.1"}]})
    assert global_tools.parse_outdated(payload) == [
        global_tools.Behind("eslint", "10.0.1", "10.8.1")
    ]


def test_a_package_with_no_installed_version_is_not_something_this_can_update():
    """npm's spelling of "declared but not installed" is an entry with no `current`.
    There is nothing to roll back to, so there is nothing to move."""
    payload = json.dumps({"ghost": {"latest": "2.0.0", "wanted": "2.0.0"}})
    assert global_tools.parse_outdated(payload) == []


def test_a_package_already_at_its_latest_is_not_reported():
    assert global_tools.parse_outdated(outdated_payload(eslint=("10.8.1", "10.8.1"))) == []


@pytest.mark.parametrize("payload", ["", "not json", "[]", "null"])
def test_unparseable_output_yields_nothing_rather_than_raising(payload):
    """The caller distinguishes "no JSON" from "empty JSON" -- only one is a failure --
    so this side of it must not raise on either."""
    assert global_tools.parse_outdated(payload) == []


def test_the_query_succeeding_is_read_from_its_output_not_its_exit_code():
    """`npm outdated` exits 1 *because* it found something. Treating that as failure is
    the bug that would make this job report a broken machine every night it worked."""
    runner = FakeRunner(outdated=outdated_payload(eslint=("10.0.1", "10.8.1")))
    entries, problem = global_tools.outdated_entries("npm", runner)
    assert problem == ""
    assert [entry.name for entry in entries] == ["eslint"]


def test_output_that_is_not_json_is_the_failure_the_exit_code_could_not_report():
    runner = FakeRunner(outdated="npm ERR! code ENOTFOUND")
    entries, problem = global_tools.outdated_entries("npm", runner)
    assert entries == []
    assert "ENOTFOUND" in problem


# --- what it declines to touch -------------------------------------------------


def test_npm_itself_is_never_updated_by_the_thing_that_needs_npm_to_roll_back():
    entries = global_tools.parse_outdated(outdated_payload(npm=("11.11.1", "11.19.0")))
    updating, skipped = global_tools.partition(entries)
    assert updating == []
    assert [entry.name for entry, _reason in skipped] == ["npm"]


def test_claude_code_is_left_to_its_own_updater():
    entries = global_tools.parse_outdated(
        json.dumps({"@anthropic-ai/claude-code": {"current": "2.1.236", "latest": "2.2.0"}})
    )
    updating, skipped = global_tools.partition(entries)
    assert updating == []
    assert skipped[0][1]


def test_a_skipped_package_is_named_in_the_report_rather_than_omitted():
    """A reader who wonders why npm is still behind must find the reason, not a bug."""
    entries = global_tools.parse_outdated(outdated_payload(npm=("11.11.1", "11.19.0")))
    _updating, skipped = global_tools.partition(entries)
    block = global_tools.render("2026-08-19 04:30", [], skipped)
    assert "skipped npm 11.11.1 -> 11.19.0" in block
    assert global_tools.SKIP["npm"] in block


def test_everything_else_is_in_scope_without_being_listed_anywhere():
    """The set is read from npm, not from a hand-kept list of packages that matter --
    a list is the thing that goes stale silently, which is the failure being fixed."""
    entries = global_tools.parse_outdated(
        outdated_payload(
            **{"chrome-devtools-mcp": ("1.7.0", "1.8.0"), "brand-new-tool": ("1", "2")}
        )
    )
    updating, skipped = global_tools.partition(entries)
    assert {entry.name for entry in updating} == {"brand-new-tool", "chrome-devtools-mcp"}
    assert skipped == []


# --- installing ----------------------------------------------------------------


def test_the_install_pins_the_version_that_was_reported_not_latest():
    """`npm outdated` and the install are two round trips. `@latest` between them would
    install a version this pass never reported, so the artifact's rollback line would
    describe a jump that did not happen."""
    argv = global_tools.install_argv("npm", global_tools.Behind("eslint", "10.0.1", "10.8.1"))
    assert argv == ["npm", "install", "--global", "eslint@10.8.1"]


def test_a_successful_install_carries_the_command_that_undoes_it():
    runner = FakeRunner()
    outcome = global_tools.update_one(
        "npm", global_tools.Behind("eslint", "10.0.1", "10.8.1"), runner
    )
    assert outcome.ok
    assert outcome.rollback == "npm install -g eslint@10.0.1"


def test_a_failed_install_keeps_npm_s_last_word_about_why():
    runner = FakeRunner(results={"eslint@10.8.1": (1, "npm ERR! code EEXIST\nnpm ERR! path C:\\x")})
    outcome = global_tools.update_one(
        "npm", global_tools.Behind("eslint", "10.0.1", "10.8.1"), runner
    )
    assert not outcome.ok
    assert "EEXIST" in outcome.detail or "path" in outcome.detail


def test_a_failure_with_no_output_still_says_something():
    runner = FakeRunner(results={"eslint@10.8.1": (1, "")})
    outcome = global_tools.update_one(
        "npm", global_tools.Behind("eslint", "10.0.1", "10.8.1"), runner
    )
    assert outcome.detail


# --- the artifact --------------------------------------------------------------


def test_the_rollback_command_is_on_its_own_line_for_every_package_moved():
    """The line a reader has come here for: a session broke this morning, and the
    question is which version it was working on yesterday."""
    outcome = global_tools.Outcome("chrome-devtools-mcp", "1.7.0", "1.8.0", True)
    block = global_tools.render("2026-08-19 04:30", [outcome])
    assert "updated chrome-devtools-mcp 1.7.0 -> 1.8.0" in block
    assert "npm install -g chrome-devtools-mcp@1.7.0" in block


def test_a_pass_with_nothing_to_do_says_so_rather_than_writing_an_empty_block():
    block = global_tools.render("2026-08-19 04:30", [])
    assert "every global package is current" in block


def test_a_failed_package_is_marked_so_a_reader_scanning_the_file_finds_it():
    block = global_tools.render(
        "2026-08-19 04:30", [global_tools.Outcome("x", "1", "2", False, "boom")]
    )
    assert "FAILED  x 1 -> 2: boom" in block


def test_the_newest_pass_is_first_and_the_previous_ones_are_kept(tmp_path):
    """Every other devkit job overwrites per run. This one keeps history because the
    bump that broke something is discovered days later -- see the module docstring."""
    global_tools.write_artifact(global_tools.render("2026-08-18 04:30", []), tmp_path)
    global_tools.write_artifact(
        global_tools.render(
            "2026-08-19 04:30", [global_tools.Outcome("eslint", "10.0.1", "10.8.1", True)]
        ),
        tmp_path,
    )
    text = (tmp_path / global_tools.ARTIFACT).read_text(encoding="utf-8")
    assert text.index("2026-08-19") < text.index("2026-08-18")
    assert "npm install -g eslint@10.0.1" in text


def test_the_history_is_bounded_so_the_file_stays_readable(tmp_path):
    for day in range(1, 8):
        global_tools.write_artifact(
            global_tools.render(f"2026-08-0{day} 04:30", []), tmp_path, kept=3
        )
    text = (tmp_path / global_tools.ARTIFACT).read_text(encoding="utf-8")
    assert text.count(global_tools.RUN_MARKER) == 3
    assert "2026-08-07" in text and "2026-08-04" not in text


def test_a_marker_inside_a_run_does_not_cut_it_in_half():
    """`trim` splits on the marker at the *start* of a line, so an npm error quoting it
    cannot make one pass look like two."""
    existing = f"{global_tools.RUN_MARKER}b\n  FAILED x: {global_tools.RUN_MARKER}quoted\n{global_tools.RUN_MARKER}a\n"
    assert global_tools.trim(existing, kept=2).count(global_tools.RUN_MARKER) == 2


def test_the_artifact_directory_is_created_rather_than_assumed(tmp_path):
    path = global_tools.write_artifact(global_tools.render("2026-08-19 04:30", []), tmp_path)
    assert path.is_file()


def test_keeping_one_pass_drops_every_older_one():
    assert global_tools.trim(f"{global_tools.RUN_MARKER}a\n", kept=1) == ""


# --- being offline is not a failure --------------------------------------------


@pytest.mark.parametrize(
    "stderr", ["npm ERR! code ENOTFOUND", "getaddrinfo EAI_AGAIN registry.npmjs.org"]
)
def test_a_registry_that_cannot_be_reached_is_recognised(stderr):
    assert global_tools.looks_offline(stderr)


def test_a_real_npm_defect_is_not_mistaken_for_a_missing_network():
    """A wrong guess here turns a broken machine into a silent exit 0, which is the
    failure mode this whole job exists to remove."""
    assert not global_tools.looks_offline("npm ERR! code EACCES\nnpm ERR! permission denied")


def test_an_offline_pass_is_recorded_and_does_not_redden_session_start(tmp_path):
    runner = FakeRunner(outdated="npm ERR! code ENOTFOUND registry.npmjs.org")
    code = global_tools.main(
        ["--yes"], run=runner, root=tmp_path, now=at(), which=lambda _name: "npm"
    )
    assert code == 0
    assert "registry unreachable" in (tmp_path / global_tools.ARTIFACT).read_text(encoding="utf-8")


def test_any_other_npm_failure_is_reported_as_one(tmp_path):
    runner = FakeRunner(outdated="npm ERR! code EACCES")
    code = global_tools.main(
        ["--yes"], run=runner, root=tmp_path, now=at(), which=lambda _name: "npm"
    )
    assert code == 1
    assert "npm outdated failed" in (tmp_path / global_tools.ARTIFACT).read_text(encoding="utf-8")


# --- the pass as a whole -------------------------------------------------------


def test_without_yes_it_reports_and_installs_nothing(tmp_path):
    runner = FakeRunner(outdated=outdated_payload(eslint=("10.0.1", "10.8.1")))
    code = global_tools.main([], run=runner, root=tmp_path, now=at(), which=lambda _name: "npm")
    assert code == 0
    assert runner.installed == []
    assert "dry run" in (tmp_path / global_tools.ARTIFACT).read_text(encoding="utf-8")


def test_with_yes_every_package_behind_is_installed_at_its_latest(tmp_path):
    runner = FakeRunner(
        outdated=outdated_payload(
            **{"eslint": ("10.0.1", "10.8.1"), "stylelint": ("17.3.0", "17.14.1")}
        )
    )
    code = global_tools.main(
        ["--yes"], run=runner, root=tmp_path, now=at(), which=lambda _name: "npm"
    )
    assert code == 0
    assert runner.installed == ["eslint@10.8.1", "stylelint@17.14.1"]


def test_one_package_failing_does_not_stop_the_others_and_is_reported(tmp_path):
    runner = FakeRunner(
        outdated=outdated_payload(
            **{"eslint": ("10.0.1", "10.8.1"), "stylelint": ("17.3.0", "17.14.1")}
        ),
        results={"eslint@10.8.1": (1, "npm ERR! code EACCES")},
    )
    code = global_tools.main(
        ["--yes"], run=runner, root=tmp_path, now=at(), which=lambda _name: "npm"
    )
    assert code == 1
    assert "stylelint@17.14.1" in runner.installed
    assert "FAILED  eslint" in (tmp_path / global_tools.ARTIFACT).read_text(encoding="utf-8")


def test_a_machine_with_no_npm_says_so_in_the_artifact_rather_than_crashing(tmp_path):
    """`pythonw.exe` sends a traceback nowhere at all, so the only place this can be
    reported is the file."""
    code = global_tools.main(
        ["--yes"], run=FakeRunner(), root=tmp_path, now=at(), which=lambda _name: None
    )
    assert code == 1
    assert "npm is not on PATH" in (tmp_path / global_tools.ARTIFACT).read_text(encoding="utf-8")


def test_every_exit_path_leaves_an_artifact(tmp_path):
    """The property that makes the job diagnosable at all: a windowless run's stdout
    goes nowhere, so a path that returns without writing reports nothing anywhere."""
    for kwargs in (
        {"run": FakeRunner(), "which": lambda _name: None},
        {"run": FakeRunner(outdated="npm ERR! ENOTFOUND"), "which": lambda _name: "npm"},
        {"run": FakeRunner(outdated=outdated_payload(x=("1", "2"))), "which": lambda _name: "npm"},
        {"run": FakeRunner(), "which": lambda _name: "npm"},
    ):
        target = tmp_path / str(id(kwargs["run"]))
        global_tools.main(["--yes"], root=target, now=at(), **kwargs)
        assert (target / global_tools.ARTIFACT).is_file()
