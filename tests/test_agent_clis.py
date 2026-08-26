"""What the agent-CLI updater has to keep true.

Four properties, and each is a way this could be worse than leaving the CLIs alone:

1. **A running agent is never updated.** The updater replaces the binary a live session
   is executing. Everything else here is negotiable; this is not.
2. **Not knowing counts as running.** When the machine cannot be asked what is up, the
   safe answer is to skip -- an update that guesses is the failure this exists to avoid.
3. **Matching is exact.** `codex-app-server` is not `codex`; a prefix match would find a
   helper process forever and no CLI would ever be updated again.
4. **Offline is not a failure.** A laptop off the network at 04:30 is the ordinary case,
   and a job that reddens on it is a job whose alerts stop being read.

Nothing here spawns anything. Every function that would takes a runner, and the two
tests that go through `main` pass one.
"""

from __future__ import annotations

import subprocess

import pytest
from support import load_script

agent_clis = load_script("scripts/agent_clis.py")

CLAUDE = agent_clis.AGENTS[0]
CODEX = agent_clis.AGENTS[1]


class FakeRunner:
    """Answers by the verb in the argv, and records every spawn it was asked for."""

    def __init__(
        self,
        answers: dict[str, tuple[int, str, str]] | None = None,
        processes: tuple[int, str, str] | None = None,
    ):
        self.calls: list[list[str]] = []
        self.answers = answers or {}
        # Keyed off the lister rather than off one of its flags, so a test reads the same
        # on the POSIX runner, where the argv is `ps -e -o comm=` and shares no token
        # with `tasklist /FO CSV /NH`.
        self.processes = processes or (0, "", "")
        self.versions: list[str] = []

    def __call__(self, argv, timeout=0):
        argv = list(argv)
        self.calls.append(argv)
        verb = argv[-1]
        if argv[0] in {"tasklist", "ps"}:
            return subprocess.CompletedProcess(argv, *self.processes)
        if verb == "--version" and self.versions:
            return subprocess.CompletedProcess(argv, 0, self.versions.pop(0), "")
        code, out, err = self.answers.get(verb, (0, "", ""))
        return subprocess.CompletedProcess(argv, code, out, err)

    @property
    def verbs(self) -> list[str]:
        return [call[-1] for call in self.calls]


def tasklist(*names: str) -> str:
    return "".join(f'"{name}","1234","Console","1","10,000 K"\n' for name in names)


# --- who is running ------------------------------------------------------------


def test_the_process_lister_is_the_platform_s_own():
    assert agent_clis.process_argv(windows=True)[0] == "tasklist"
    assert agent_clis.process_argv(windows=False)[0] == "ps"


@pytest.mark.parametrize(
    "raw, expected",
    [("claude.exe", "claude"), (' "Codex.EXE" ', "codex"), ("/usr/bin/claude", "claude")],
)
def test_a_process_name_is_compared_by_its_bare_stem(raw, expected):
    assert agent_clis.normalise_process(raw) == expected


def test_a_process_name_containing_a_comma_survives_the_csv():
    """`tasklist /FO CSV` quotes its fields, and splitting on the comma would read
    `"Foo, Bar.exe"` as two processes -- neither of which is the one that is running."""
    payload = '"Foo, Bar.exe","1","Console","1","1 K"\n"claude.exe","2","Console","1","1 K"\n'
    assert agent_clis.parse_process_names(payload, windows=True) == {"foo, bar", "claude"}


def test_posix_process_names_come_from_plain_lines():
    assert agent_clis.parse_process_names("claude\n\ncodex\n", windows=False) == {
        "claude",
        "codex",
    }


def test_a_lister_that_failed_is_reported_as_unanswerable_not_as_nothing_running():
    runner = FakeRunner(processes=(1, "", "access denied"))
    assert agent_clis.running_processes(runner, windows=True) is None


def test_an_empty_answer_is_also_unanswerable():
    """A lister that exits 0 with no rows has not told us the machine is idle."""
    assert agent_clis.running_processes(FakeRunner(), windows=True) is None


def test_the_names_come_back_when_the_machine_answers():
    runner = FakeRunner(processes=(0, tasklist("claude.exe", "chrome.exe"), ""))
    assert agent_clis.running_processes(runner, windows=True) == {"claude", "chrome"}


def test_an_agent_with_a_live_process_is_running():
    assert agent_clis.is_running(CLAUDE, {"claude", "chrome"})


def test_not_knowing_what_is_running_counts_as_running():
    assert agent_clis.is_running(CLAUDE, None)


def test_a_helper_process_sharing_the_prefix_does_not_block_the_update():
    """`codex-app-server` outlives the sessions that spawn it. A prefix match here would
    mean codex is never updated again, and nothing would say why."""
    assert not agent_clis.is_running(CODEX, {"codex-app-server", "claude"})


# --- versions ------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("1.2.3 (Claude Code)", "1.2.3"),
        ("codex-cli 0.49.0", "0.49.0"),
        ("2.0.0-beta.4", "2.0.0-beta.4"),
        ("no version here", ""),
    ],
)
def test_the_version_is_read_out_of_whatever_the_cli_prints(text, expected):
    assert agent_clis.version_of(text) == expected


def test_a_version_probe_that_failed_claims_nothing():
    runner = FakeRunner({"--version": (1, "", "boom")})
    assert agent_clis.installed_version("claude", runner) == ""


def test_a_cli_that_answers_on_stderr_is_still_read():
    runner = FakeRunner({"--version": (0, "", "1.4.0")})
    assert agent_clis.installed_version("claude", runner) == "1.4.0"


@pytest.mark.parametrize(
    "text", ["getaddrinfo ENOTFOUND", "Connection refused", "request timed out"]
)
def test_a_network_failure_is_recognised_as_one(text):
    assert agent_clis.looks_offline(text)


def test_a_real_updater_defect_is_not_mistaken_for_a_missing_network():
    assert not agent_clis.looks_offline("EACCES: permission denied, rename '/usr/bin/claude'")


def test_the_error_is_taken_from_the_last_line_the_cli_wrote():
    assert agent_clis.last_line("downloading...\n\nfatal: disk full\n") == "fatal: disk full"


def test_no_output_leaves_no_error_line():
    assert agent_clis.last_line("") == ""


# --- one agent -----------------------------------------------------------------


def test_both_clis_spell_their_own_updater_the_same_way():
    assert agent_clis.update_argv("claude") == ["claude", "update"]
    assert agent_clis.doctor_argv("codex") == ["codex", "doctor"]


def test_a_version_that_moved_is_reported_as_an_update():
    runner = FakeRunner()
    runner.versions = ["1.0.0", "1.1.0"]
    outcome = agent_clis.update_one(CLAUDE, "claude", runner)
    assert (outcome.status, outcome.before, outcome.after) == (agent_clis.UPDATED, "1.0.0", "1.1.0")


def test_an_updater_that_reinstalled_the_same_release_is_current_not_updated():
    """`codex update` exits 0 and reinstalls when there is nothing newer, so the exit
    code alone would report a move every single night."""
    runner = FakeRunner()
    runner.versions = ["0.49.0", "0.49.0"]
    outcome = agent_clis.update_one(CODEX, "codex", runner)
    assert outcome.status == agent_clis.CURRENT
    assert outcome.ok


def test_an_updater_that_could_not_reach_the_network_is_offline_not_failed():
    runner = FakeRunner({"update": (1, "", "getaddrinfo ENOTFOUND registry")})
    outcome = agent_clis.update_one(CODEX, "codex", runner)
    assert outcome.status == agent_clis.OFFLINE
    assert outcome.ok


def test_an_updater_that_broke_is_a_failure_carrying_its_last_line():
    runner = FakeRunner({"update": (1, "", "EACCES: permission denied")})
    outcome = agent_clis.update_one(CLAUDE, "claude", runner)
    assert outcome.status == agent_clis.FAILED
    assert not outcome.ok
    assert "EACCES" in outcome.detail


def test_the_doctor_report_is_trimmed_to_its_head():
    runner = FakeRunner({"doctor": (0, "\n".join(f"line {n}" for n in range(100)), "")})
    assert len(agent_clis.doctor_text("claude", runner, lines=3).splitlines()) == 3


def test_a_doctor_with_nothing_to_say_contributes_nothing():
    assert agent_clis.doctor_text("claude", FakeRunner()) == ""


@pytest.mark.parametrize(
    "outcome, fragment",
    [
        (
            agent_clis.Outcome("claude", agent_clis.UPDATED, "1.0", "1.1"),
            "updated claude 1.0 -> 1.1",
        ),
        (agent_clis.Outcome("codex", agent_clis.ABSENT, detail="not on PATH"), "absent  codex"),
        (agent_clis.Outcome("codex", agent_clis.FAILED, detail="boom"), "FAILED  codex"),
    ],
)
def test_every_outcome_has_a_line_of_its_own(outcome, fragment):
    assert fragment in agent_clis.describe(outcome)


# --- the pass ------------------------------------------------------------------


def test_no_selection_means_every_agent():
    assert agent_clis.select_agents([]) == agent_clis.AGENTS


def test_a_selection_is_taken_by_name_and_stays_in_agents_order():
    assert agent_clis.select_agents(["codex", "CLAUDE"]) == agent_clis.AGENTS


def test_a_name_nothing_answers_to_selects_nothing():
    assert agent_clis.select_agents(["gemini"]) == ()


def test_a_running_agent_is_skipped_and_its_updater_is_never_spawned():
    """The one property this module exists for."""
    runner = FakeRunner()
    report = agent_clis.run_pass(
        [CLAUDE], yes=True, run=runner, which=lambda name: name, processes={"claude"}
    )
    assert "update" not in runner.verbs
    assert report.outcomes[0].status == agent_clis.SKIPPED
    assert report.failures == 0


def test_an_unanswerable_machine_skips_every_agent():
    runner = FakeRunner()
    report = agent_clis.run_pass(
        agent_clis.AGENTS, yes=True, run=runner, which=lambda name: name, processes=None
    )
    # `processes=None` asks the machine, and this FakeRunner answers nothing.
    assert "update" not in runner.verbs
    assert {outcome.status for outcome in report.outcomes} == {agent_clis.SKIPPED}


def test_an_agent_that_is_not_installed_is_absent_rather_than_failed():
    report = agent_clis.run_pass(
        [CODEX], yes=True, run=FakeRunner(), which=lambda _name: None, processes=set()
    )
    assert report.outcomes[0].status == agent_clis.ABSENT
    assert report.failures == 0


def test_without_yes_nothing_is_updated():
    runner = FakeRunner()
    runner.versions = ["1.0.0"]
    report = agent_clis.run_pass([CLAUDE], run=runner, which=lambda name: name, processes=set())
    assert "update" not in runner.verbs
    assert report.outcomes[0].status == agent_clis.SKIPPED


def test_an_idle_agent_is_updated():
    runner = FakeRunner()
    runner.versions = ["1.0.0", "1.1.0"]
    report = agent_clis.run_pass(
        [CLAUDE], yes=True, run=runner, which=lambda name: name, processes=set()
    )
    assert "update" in runner.verbs
    assert report.outcomes[0].status == agent_clis.UPDATED
    assert "updated claude" in report.lines[0]


def test_the_doctor_runs_unasked_only_after_a_failure():
    """Recorded on failure because that is when it is evidence; recorded always would
    put a wall of prose in the artifact every night and bury the one line that matters."""
    runner = FakeRunner({"update": (1, "", "EACCES"), "doctor": (0, "install is broken", "")})
    report = agent_clis.run_pass(
        [CLAUDE], yes=True, run=runner, which=lambda name: name, processes=set()
    )
    assert "doctor" in runner.verbs
    assert any("install is broken" in line for line in report.lines)


def test_a_healthy_pass_does_not_run_the_doctor():
    runner = FakeRunner()
    runner.versions = ["1.0.0", "1.0.0"]
    agent_clis.run_pass([CLAUDE], yes=True, run=runner, which=lambda name: name, processes=set())
    assert "doctor" not in runner.verbs


def test_the_doctor_can_be_asked_for_explicitly():
    runner = FakeRunner({"doctor": (0, "all good", "")})
    runner.versions = ["1.0.0", "1.0.0"]
    report = agent_clis.run_pass(
        [CLAUDE], yes=True, doctor=True, run=runner, which=lambda name: name, processes=set()
    )
    assert any("all good" in line for line in report.lines)


def test_a_pass_over_no_agents_still_says_something():
    report = agent_clis.run_pass([], yes=True, run=FakeRunner(), processes=set())
    assert report.lines == ("  no agent CLI to update",)
    assert report.summary == "nothing to do"


def test_the_summary_counts_each_status_once():
    report = agent_clis.Report(
        (
            agent_clis.Outcome("claude", agent_clis.UPDATED),
            agent_clis.Outcome("codex", agent_clis.SKIPPED),
        ),
        (),
    )
    assert report.summary == "1 updated, 1 skipped"
    assert report.failures == 0


def test_an_agent_carries_the_label_the_report_names_it_by():
    assert agent_clis.Agent("claude", "Claude Code").label == "Claude Code"


# --- the command line ----------------------------------------------------------


def test_a_failed_update_is_the_exit_code(capsys):
    runner = FakeRunner({"update": (1, "", "EACCES")}, processes=(0, tasklist("chrome.exe"), ""))
    assert agent_clis.main(["--yes"], run=runner, which=lambda name: name) == 1
    assert "agent CLIs:" in capsys.readouterr().out


def test_a_pass_with_nothing_to_do_exits_clean(capsys):
    runner = FakeRunner(processes=(0, tasklist("chrome.exe"), ""))
    assert agent_clis.main([], run=runner, which=lambda _name: None) == 0


def test_an_unknown_agent_is_rejected_rather_than_silently_updating_both():
    runner = FakeRunner()
    with pytest.raises(SystemExit):
        agent_clis.main(["--agent", "gemini"], run=runner, which=lambda name: name)


def test_a_spawn_that_cannot_happen_is_a_returncode_not_a_traceback():
    """`run_command` is the one function here that really spawns; an unattended job has
    nowhere to send a traceback, so every failure has to arrive as a result."""
    result = agent_clis.run_command(["definitely-not-a-real-binary-xyz"], timeout=5)
    assert result.returncode == 1
    assert result.stderr
