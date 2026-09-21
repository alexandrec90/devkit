#!/usr/bin/env python3
"""Opening one agent session in a Windows Terminal tab. The one answer, for four callers.

`agent-box.py spawn` and `attach` open one in a box; `agent-worktree.py new` opens one in
a plain agent-CLI worktree; `fix-prs.py` opens one per red PR. All four have to agree on
which window the tab lands in, what its command line looks like, and what the operator is
told about the session they just paid for -- so all four call `open_agent` here.

**This module is `agent-box.py`'s owed split, cut.** It lived there because `spawn` was
the first caller, and the other three reached it by loading a hyphenated filename through
`_loader.load_by_path` -- a second module object per importer, which
`tests/test_preview_task.py` has already been broken by once. Three baseline records in
`.devkit-structure.txt` deferred this split while agent-box.py grew past `file_lines`
three times, each naming the same seam: the box *verbs* against the tab tier the other
scripts load it for. `.claude/rules/engineering.md` calls a fourth raise a defect report
rather than a raise, so the split is here, and the underscore in the name is half the
point -- every caller now does a plain `import agent_tabs`.

Two decisions live here rather than where they are used, and both have cost something:

- **`-w 0`**, the most recently used window, creating one only when there is none. A tab
  belongs beside the work that asked for it. `resume-sessions.py` forced `-w -1` until
  2026-09-14 and opened a stray window every time.
- **Every string that reaches `wt` has its semicolons escaped**, in `wt_argv` and nowhere
  else. `wt` re-parses its own command line and reads `;` as a tab separator, so one
  owner for that hazard is the whole safety property -- the kill switch's own `;` went
  unescaped for a release when a caller escaped its prompt and assumed that was all.

`wt_profile.py` owns which profile the tab opens under and why a stray window is usually
an elevation mismatch. `agent_models.py` owns which model and effort it opens at.

The argv builders are pure; `open_agent` takes a runner. Tested in
`tests/test_agent_tabs.py`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "precommit"))
import agent_models
import wt_profile

# Resolved by the second insert above; `scripts/precommit/` is not a package. Used for the
# one neighbour whose name has a hyphen in it and so cannot be a plain import.
from _loader import load_by_path

REPO_ROOT = Path(__file__).resolve().parents[1]

harness_switch = load_by_path("harness_switch", REPO_ROOT / "scripts" / "harness-switch.py")

# See the module docstring.
WT_WINDOW = "0"

EXIT_OK = 0
EXIT_FAILED = 1


def agent_command(launch: agent_models.Launch, hooks_off: bool, prompt: str = "") -> str:
    """The one command line the terminal tab runs.

    A string rather than an argv because `wt` hands everything after `-Command` to
    PowerShell as a single line anyway, and the environment assignment has to be part of
    it: `wt` has no way to set a variable for the child it spawns.

    `prompt` is the session's opening instruction, and it is optional because the callers
    want opposite things. `spawn` and `attach` hand over a box with no topic -- the person
    who asked for it is about to type one. `fix-prs.py` already knows the whole job (which
    PR, what is wrong with it, where it has to end up), and a session that starts by
    rediscovering that is the cost that task exists to remove.
    """
    prefix = (
        f"$env:{harness_switch.HOOKS_OFF_ENV}='{harness_switch.HOOKS_OFF_VALUE}'; "
        if hooks_off
        else ""
    )
    # Quoted through, flag names included: a quoted literal is still one argument to
    # PowerShell, and quoting the lot means no future flag value can be read as a
    # `$`-expansion on its way to the agent.
    tail = [*launch.flags(), *([prompt] if prompt else [])]
    return " ".join([prefix + launch.cli, *(ps_quote(part) for part in tail)])


def ps_quote(text: str) -> str:
    """`text` as a PowerShell single-quoted literal.

    Single quotes rather than double: PowerShell expands `$` and backticks inside a
    double-quoted string, so a prompt naming `$env:` -- or a branch with a backtick in
    it -- would be rewritten on its way to the agent. Doubling is how a single quote is
    escaped inside one.
    """
    return "'" + str(text).replace("'", "''") + "'"


def find_terminal() -> str:
    """Path to wt.exe, or "" when Windows Terminal is not installed -- every POSIX box."""
    return shutil.which("wt.exe") or shutil.which("wt") or ""


def tab_argv(title: str, cwd: Path, command: str, profile: str = "") -> list[str]:
    """One `new-tab` clause, escaping the semicolons wt reads as tab separators.

    Separate from `wt_argv` because `resume-sessions.py` opens several tabs in one `wt`
    invocation and so supplies its own `-w` once, ahead of all of them. Everything that
    differs per tab is here, which is what makes the two callers one implementation.

    `profile` is the Windows Terminal profile it opens under; `""` is the tab this built
    before there was one, inheriting the default profile. `wt_profile.py` owns why.
    """
    return [
        "new-tab",
        *(["-p", profile] if profile else []),
        "--title",
        title.replace(";", "\\;"),
        "-d",
        str(cwd).replace(";", "\\;"),
        # -NoExit for `resume-sessions.py`'s reason: an agent that dies on startup still
        # leaves its error on screen instead of closing the tab it printed it in.
        "pwsh.exe",
        "-NoLogo",
        "-NoExit",
        "-Command",
        command.replace(";", "\\;"),
    ]


def wt_argv(title: str, cwd: Path, command: str, profile: str = "") -> list[str]:
    """One whole `wt` command line, for the callers that open exactly one tab."""
    return ["-w", WT_WINDOW, *tab_argv(title, cwd, command, profile)]


def open_agent(
    launch: agent_models.Launch,
    box: Path,
    branch: str,
    runner=subprocess.run,
    prompt: str = "",
    title: str = "",
) -> int:
    """Open one agent tab in `box`.

    Nothing here says `box` to the operator: every caller hands this a worktree, and only
    some of them are boxes -- `agent-worktree.py` cuts a plain one with no lease, no port
    and no reaper, and shares this anyway for the module docstring's reason.

    `launch` is which agent, at which model, at what effort -- `agent_models.Launch` owns
    it. What it chose is printed rather than left to the tab: a session at `max` costs
    several times one at the default, and the terminal is the only place that choice is
    recorded once the quick-pick has closed.

    `title` overrides the tab's name, which the branch answers for a box cut to do one
    thing. `fix-prs.py` passes the PR instead: several of its tabs can be open at once, on
    branches whose names all begin `agent/`, and a strip of tabs that agree for their
    first eleven characters names nothing.
    """
    if launch.agent == "none":
        print(f"no agent requested; the worktree is at {box}")
        return EXIT_OK
    terminal = find_terminal()
    command = agent_command(launch, harness_switch.hooks_are_off(), prompt)
    if not terminal:
        print(f"Windows Terminal not found; run this yourself:\n  cd {box}\n  {command}")
        return EXIT_OK
    argv = wt_argv(title or branch, box, command, wt_profile.launch_name())
    print(f"opening {launch.cli}{launch.describe()} in {box}{wt_profile.launch_note()}")
    done = runner([terminal, *argv], check=False)
    return EXIT_OK if done.returncode == 0 else EXIT_FAILED


def background_argv(exe: str, launch: agent_models.Launch, prompt: str) -> list[str]:
    """`claude --bg <prompt>`, as an argv rather than a command line.

    No shell here, so no quoting: the prompt is one argument. That is the one thing the
    background mode has strictly better than the tab, and it is why its caller flattens
    the prompt anyway -- the two modes must hand the agent the same words, or a report
    about one says nothing about the other. The model flags come from the same `Launch`
    for that same reason: the two must differ in where you read them and nowhere else.
    """
    return [exe, "--bg", *launch.flags(), prompt]


def launch_background(
    launch: agent_models.Launch, tree: Path, prompt: str, hooks_off: bool, runner=subprocess.run
) -> int:
    """Start a detached session and print the id that reads it back.

    The other way a session opens, and here rather than in `fix-prs.py` because it is the
    same question this module's `open_agent` answers: what does the CLI get told, and
    what is the operator told back. Only Claude Code has one -- `codex exec` streams into
    the terminal that started it and leaves nothing to reattach to.
    """
    cli = launch.cli
    exe = shutil.which(cli)
    if not exe:
        print(f"agent-tabs: {cli} is not on PATH; run this yourself:\n  cd {tree}\n  {cli} --bg")
        return EXIT_FAILED
    env = dict(os.environ)
    if hooks_off:
        env[harness_switch.HOOKS_OFF_ENV] = harness_switch.HOOKS_OFF_VALUE
    done = runner(
        background_argv(exe, launch, prompt), cwd=str(tree), capture_output=True, text=True, env=env
    )
    sys.stdout.write(done.stdout or "")
    sys.stderr.write(done.stderr or "")
    if done.returncode != 0:
        return EXIT_FAILED
    print("  read it back with `claude agents`, `claude logs <id>`, `claude attach <id>`")
    return EXIT_OK
