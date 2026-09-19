"""Tests for `codex_context.py` -- the two machine-local files Codex reads a repo through.

Everything here is `tmp_path` standing in for `CODEX_HOME`. Nothing reads the real one:
the whole point of the module is that the machine running these tests may be the
misconfigured one, and a suite that went red on that would be reporting the finding in
the place that cannot act on it.
"""

from __future__ import annotations

from support import load_script

cc = load_script("scripts/codex_context.py")


def _home(tmp_path, config: str | None = None, agents: str | None = None):
    """A `CODEX_HOME` holding whichever of the two files the caller named."""
    if config is not None:
        (tmp_path / cc.CONFIG_NAME).write_text(config, encoding="utf-8")
    if agents is not None:
        (tmp_path / cc.AGENTS_NAME).write_text(agents, encoding="utf-8")
    return tmp_path


WIRED_CONFIG = 'model = "gpt-5"\nproject_doc_fallback_filenames = ["CLAUDE.md"]\n'
WIRED_AGENTS = "# Rules\n\nOn entering a repo, list `.claude/rules/` and read the frontmatter.\n"


def test_a_fully_wired_codex_says_nothing(tmp_path):
    assert cc.report_lines(_home(tmp_path, WIRED_CONFIG, WIRED_AGENTS)) == []


def test_a_machine_that_does_not_run_codex_says_nothing(tmp_path):
    """No `CODEX_HOME` directory is the ordinary state of a Claude-only workstation, and
    a prerequisite reported to someone without the tool is the line that teaches them to
    skip the rest."""
    assert cc.report_lines(tmp_path / "no-such-home") == []


def test_a_config_without_the_fallback_is_reported_with_the_setting(tmp_path):
    """Without it Codex reads no project instructions at all: devkit repos carry
    `CLAUDE.md` and deliberately no `AGENTS.md`, so there is nothing for its default
    lookup to find."""
    (line,) = cc.report_lines(_home(tmp_path, 'model = "gpt-5"\n', WIRED_AGENTS))
    assert cc.FALLBACK_SETTING in line
    assert 'project_doc_fallback_filenames = ["CLAUDE.md"]' in line


def test_an_agents_file_that_lost_the_bridge_is_reported(tmp_path):
    """The regression. On 2026-09-18 this file held the no-hooks policy and nothing
    else: the bridge paragraph was gone, README still asserted it, and nothing in git
    had changed -- so no gate anywhere had anything to say."""
    only_hooks = "# No coding-agent hooks\n\nNever wire one.\n"
    (line,) = cc.report_lines(_home(tmp_path, WIRED_CONFIG, only_hooks))
    assert cc.BRIDGE_MARKER in line and "README.md" in line


def test_a_missing_agents_file_reads_the_same_as_one_without_the_bridge(tmp_path):
    """Inside a `CODEX_HOME` that exists, absent and present-but-empty cost Codex the
    same thing, so they are one finding rather than two states to reason about."""
    absent = cc.report_lines(_home(tmp_path, WIRED_CONFIG))
    assert len(absent) == 1 and cc.BRIDGE_MARKER in absent[0]


def test_both_files_wrong_is_two_lines(tmp_path):
    """One line per file: the fixes are different edits to different files, and a single
    merged line would have the reader guessing which half they had done."""
    assert len(cc.report_lines(_home(tmp_path, "", ""))) == 2


def test_codex_home_follows_the_environment_variable(tmp_path, monkeypatch):
    """A machine that has moved `CODEX_HOME` must not be checked against a directory it
    does not use -- the same reason `worktree_tiers.home_of` reads the variable, which
    is where this borrows the answer from rather than spelling it a second time."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "elsewhere"))
    assert cc.codex_home() == tmp_path / "elsewhere"

    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert cc.codex_home().name == ".codex"


def test_it_never_writes_to_the_home_it_reads(tmp_path):
    """Restoring either file is an edit to the operator's own home directory. A reporter
    that fixed it would be writing user configuration from a status pass."""
    home = _home(tmp_path, "", "")
    before = sorted(p.name for p in home.iterdir())

    cc.report_lines(home)

    assert sorted(p.name for p in home.iterdir()) == before
