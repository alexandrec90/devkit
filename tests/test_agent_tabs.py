"""`scripts/agent_tabs.py`: what a paid session's terminal tab is made of.

These tests moved here with the code, out of `tests/test_agent_box.py`, when the tab tier
was split off `agent-box.py` -- see that module's docstring for why. They are the same
assertions: the window a tab joins, the semicolon escaping that stopped one click opening
two tabs, the profile lookup, and the two lines the operator reads as it opens.

Everything that acts takes a runner and is driven with a fake one; the argv builders are
pure and are driven directly.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from support import load_script

tabs = load_script("scripts/agent_tabs.py")
agent_models = load_script("scripts/agent_models.py")

CLAUDE = agent_models.Launch("claude")
CODEX = agent_models.Launch("codex")


class FakeRunner:
    """A `subprocess.run` stand-in that records argv and always succeeds."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, argv, **_kwargs):
        self.calls.append([str(a) for a in argv])
        return subprocess.CompletedProcess(argv, 0, "", "")


def _never(*_args, **_kwargs):
    raise AssertionError("nothing should have been spawned")


# --- the terminal ---------------------------------------------------------------------


def test_the_agent_tab_attaches_to_the_window_the_operator_is_looking_at():
    """`-w 0` is "most recently used window, create one only if there is none", which is
    the ask: a box belongs where the operator already is. `resume-sessions.py` forced
    `-w -1` until 2026-09-14 and now defaults to this too."""
    argv = tabs.wt_argv("agent/thing-0903", Path("C:/boxes/x"), "claude")
    assert argv[:3] == ["-w", "0", "new-tab"]
    assert "-NoExit" in argv
    assert argv[-1] == "claude"
    assert argv[argv.index("-d") + 1] == str(Path("C:/boxes/x"))


def test_the_kill_switchs_semicolon_does_not_open_a_second_tab():
    """The regression this file exists for: `wt` re-parses its own command line, so the
    `;` `agent_command` writes between the assignment and the agent used to end the tab's
    command there and start a second sub-command out of the rest -- two tabs per PR, the
    second one answering "The system cannot find the file specified" because it tried to
    launch `claude '<prompt>'` as an executable."""
    command = tabs.agent_command(CLAUDE, True, "fix it")
    argv = tabs.wt_argv("agent/thing-0903", Path("C:/boxes/x"), command)
    embedded = argv[argv.index("-Command") + 1]
    assert ";" in command
    assert embedded == command.replace(";", "\\;")
    assert ";" not in embedded.replace("\\;", "")


def test_a_command_with_no_semicolon_reaches_the_tab_untouched():
    """The ordinary case -- `spawn` with the harness running -- must not grow a backslash."""
    assert tabs.wt_argv("b", Path("C:/boxes/x"), "claude")[-1] == "claude"


def test_a_tab_opens_under_the_agent_profile_when_the_machine_has_one():
    """The `+` button beside an agent tab used to answer with the default profile in the
    default directory, and Duplicate Tab replayed a bare shell, because a tab built from
    an overridden command line has no profile of its own. `-p` is what gives it one."""
    argv = tabs.wt_argv("agent/thing-0903", Path("C:/boxes/x"), "claude", profile="Agent")
    assert argv[:5] == ["-w", "0", "new-tab", "-p", "Agent"]
    assert argv[argv.index("-d") + 1] == str(Path("C:/boxes/x"))


def test_a_machine_that_never_registered_the_profile_opens_the_tab_it_always_did():
    """The installer is a machine-level step; a checkout that has not run it must still
    open sessions, so no `-p` rather than one naming a profile that is not there."""
    assert "-p" not in tabs.wt_argv("b", Path("C:/boxes/x"), "claude")


def test_the_profile_is_read_from_the_machine_rather_than_guessed(monkeypatch, tmp_path):
    """`open_agent` is where the lookup happens, so a spawn on a machine that registered
    the profile picks it up with nothing passed down the call chain.

    Both machine lookups are stubbed, not just the profile one: `open_agent` returns
    before it spawns anything when `wt` is not on PATH, so a test that stubbed only the
    profile asserted against an empty call list on every non-Windows runner -- green
    here, red in CI, which is exactly the split the rehearsal exists to stop.
    """
    monkeypatch.setattr(tabs.shutil, "which", lambda name: "C:/wt.exe" if "wt" in name else None)
    monkeypatch.setattr(tabs.harness_switch, "hooks_are_off", lambda *_a: False)
    monkeypatch.setattr(tabs.wt_profile, "launch_name", lambda: "Agent")
    runner = FakeRunner()
    assert tabs.open_agent(CLAUDE, tmp_path, "agent/thing-0903", runner=runner) == 0
    assert runner.calls[0][runner.calls[0].index("-p") + 1] == "Agent"


def test_a_tab_that_cannot_join_the_operators_window_says_why_as_it_opens(monkeypatch, capsys):
    """The elevation mismatch, reported in the one place it reads as a cause.

    `wt_profile.py` owns what the mismatch is; what this pins is that `open_agent` prints
    it. Without it a spawn on a machine whose windows are elevated opens a window of its
    own and says only "opening claude in ..." -- which is how the same defect got
    reported twice as "the task ignores the terminal I have open".
    """
    monkeypatch.setattr(tabs.shutil, "which", lambda name: "C:/wt.exe" if "wt" in name else None)
    monkeypatch.setattr(tabs.harness_switch, "hooks_are_off", lambda *_a: False)
    monkeypatch.setattr(tabs.wt_profile, "launch_name", lambda: "Agent")
    monkeypatch.setattr(tabs.wt_profile, "launch_note", lambda: tabs.wt_profile.ELEVATION_NOTE)
    assert tabs.open_agent(CLAUDE, Path("C:/boxes/x"), "agent/x", runner=FakeRunner()) == 0
    out = capsys.readouterr().out
    assert "opening claude" in out and "elevate" in out


def test_nothing_is_said_about_windows_when_the_tab_will_land_in_one(monkeypatch, capsys):
    """The other half, and the one that decides whether the note is bearable: on a
    machine with no mismatch every spawn prints the line it always did."""
    monkeypatch.setattr(tabs.shutil, "which", lambda name: "C:/wt.exe" if "wt" in name else None)
    monkeypatch.setattr(tabs.harness_switch, "hooks_are_off", lambda *_a: False)
    monkeypatch.setattr(tabs.wt_profile, "launch_name", lambda: "Agent")
    monkeypatch.setattr(tabs.wt_profile, "launch_note", lambda: "")
    assert tabs.open_agent(CLAUDE, Path("C:/boxes/x"), "agent/x", runner=FakeRunner()) == 0
    printed = capsys.readouterr().out.splitlines()
    assert len(printed) == 1 and printed[0].startswith("opening claude in ")


def test_a_semicolon_in_the_title_or_the_directory_is_escaped_too():
    """Both are legal in a Windows path and in a git branch name, and both are strings
    this module is handed rather than writes."""
    argv = tabs.wt_argv("agent/odd;name", Path("C:/box;es/x"), "claude")
    assert argv[argv.index("--title") + 1] == "agent/odd\\;name"
    assert ";" not in argv[argv.index("-d") + 1].replace("\\;", "")


def test_the_kill_switch_is_exported_into_the_tab_when_it_is_on():
    """Claude reads `env` out of the user settings file. Codex reads no settings file of
    ours, so without this a Codex session in a box would run hooks the operator had
    switched off everywhere else."""
    assert tabs.agent_command(CODEX, True).startswith("$env:DEVKIT_HOOKS_OFF='1'; ")
    assert tabs.agent_command(CODEX, True).endswith("codex")


def test_nothing_is_exported_when_the_harness_is_running():
    assert tabs.agent_command(CLAUDE, False) == "claude"


def test_asking_for_no_agent_opens_no_terminal(capsys):
    assert (
        tabs.open_agent(agent_models.Launch("none"), Path("C:/boxes/x"), "agent/x", runner=_never)
        == tabs.EXIT_OK
    )
    assert "no agent requested" in capsys.readouterr().out


def test_a_machine_without_windows_terminal_is_told_what_to_type(monkeypatch, capsys):
    monkeypatch.setattr(tabs.shutil, "which", lambda _name: None)
    monkeypatch.setattr(tabs.harness_switch, "hooks_are_off", lambda *_a: False)
    assert tabs.open_agent(CLAUDE, Path("C:/boxes/x"), "agent/x", runner=_never) == tabs.EXIT_OK
    assert "run this yourself" in capsys.readouterr().out


# --- what the agent is told ---------------------------------------------------------


def test_a_prompt_reaches_powershell_as_a_single_quoted_literal():
    """Single quotes because PowerShell expands `$` and backticks inside double ones."""
    command = tabs.agent_command(CLAUDE, False, "fix $env:PATH and `x`")
    assert command == "claude 'fix $env:PATH and `x`'"


def test_an_apostrophe_in_a_prompt_is_doubled_not_escaped():
    assert tabs.ps_quote("it's") == "'it''s'"


def test_a_session_with_no_prompt_is_unchanged():
    """`spawn` and `attach` hand over a box with no topic, and must keep doing so."""
    assert tabs.agent_command(CODEX, False) == "codex"


def test_the_hooks_off_prefix_survives_a_prompt(monkeypatch):
    command = tabs.agent_command(CLAUDE, True, "do the thing")
    assert command.startswith("$env:")
    assert command.endswith("claude 'do the thing'")


# --- the other way a session opens ---------------------------------------------------


def test_the_background_argv_passes_the_prompt_as_one_argument():
    """No shell in this mode, so no quoting -- and the words handed over are the same
    ones the tab mode hands over."""
    assert tabs.background_argv("claude", CLAUDE, "do a; b") == ["claude", "--bg", "do a; b"]


def test_the_background_launch_runs_the_resolved_exe_in_the_box(monkeypatch, tmp_path):
    seen = {}

    def runner(argv, **kwargs):
        seen.update(argv=argv, kwargs=kwargs)
        return subprocess.CompletedProcess(argv, 0, "session abc123", "")

    # `which` resolves to an absolute path, and that resolved path is what must be
    # spawned: the argv is handed to `subprocess.run` with no shell, so the bare name
    # would be looked up a second time -- against the child's PATH, not this one's.
    resolved = r"C:\bin\claude.exe"
    monkeypatch.setattr(tabs.shutil, "which", lambda _cli: resolved)
    code = tabs.launch_background(CLAUDE, tmp_path, "fix #412", False, runner)
    assert code == tabs.EXIT_OK
    assert seen["argv"] == [resolved, "--bg", "fix #412"]
    assert seen["kwargs"]["cwd"] == str(tmp_path)
    assert tabs.harness_switch.HOOKS_OFF_ENV not in seen["kwargs"]["env"]


def test_the_background_launch_carries_the_hooks_switch_as_an_env_var(monkeypatch, tmp_path):
    """There is no shell in this mode, so the `$env:` prefix the tab uses has nowhere to
    go -- the switch has to reach the child through its environment or not at all."""
    seen = {}

    def runner(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(tabs.shutil, "which", lambda _cli: "claude")
    tabs.launch_background(CLAUDE, tmp_path, "p", True, runner)
    switch = tabs.harness_switch
    assert seen["env"][switch.HOOKS_OFF_ENV] == switch.HOOKS_OFF_VALUE


def test_a_cli_that_is_not_on_path_is_reported_rather_than_spawned(monkeypatch, tmp_path, capsys):
    def explode(*_a, **_k):
        raise AssertionError("nothing should be spawned")

    monkeypatch.setattr(tabs.shutil, "which", lambda _cli: None)
    assert tabs.launch_background(CLAUDE, tmp_path, "p", False, explode) == tabs.EXIT_FAILED
    assert "not on PATH" in capsys.readouterr().out


def test_a_background_session_that_failed_to_start_is_a_failure(monkeypatch, tmp_path):
    def runner(argv, **_kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "no credit")

    monkeypatch.setattr(tabs.shutil, "which", lambda _cli: "claude")
    assert tabs.launch_background(CLAUDE, tmp_path, "p", False, runner) == tabs.EXIT_FAILED


# --- the two halves the resume task shares ------------------------------------------


def test_the_tab_clause_is_the_part_a_batch_can_repeat():
    """`resume-sessions.py` opens several tabs in one `wt` invocation and supplies its own
    `-w` once, ahead of all of them, so everything that differs per tab is `tab_argv` and
    `wt_argv` is that plus the window. Two copies of the clause is what this replaced."""
    clause = tabs.tab_argv("t", Path("C:/w"), "claude", profile="Agent")
    assert clause[0] == "new-tab" and "-w" not in clause
    assert tabs.wt_argv("t", Path("C:/w"), "claude", profile="Agent") == [
        "-w",
        tabs.WT_WINDOW,
        *clause,
    ]


def test_windows_terminal_is_looked_up_once_under_both_of_its_names(monkeypatch):
    """`wt.exe` on a normal install and `wt` on a shim; "" is every POSIX runner, which is
    why neither launcher may treat a missing terminal as a failure."""
    monkeypatch.setattr(tabs.shutil, "which", lambda name: "C:/wt.exe" if name == "wt" else None)
    assert tabs.find_terminal() == "C:/wt.exe"
    monkeypatch.setattr(tabs.shutil, "which", lambda _name: None)
    assert tabs.find_terminal() == ""
