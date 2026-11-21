"""`scripts/install-codex-context.py`: the writer for what `codex_context` reports."""

from __future__ import annotations

from support import load_script

inst = load_script("scripts/install-codex-context.py")
ctx = load_script("scripts/codex_context.py")

# This machine's config on 2026-11-21: top-level keys, then tables.
CONFIG = (
    'model = "gpt-6-astra"\n'
    "[projects.'c:\\users\\alexa']\n"
    'trust_level = "trusted"\n'
    "\n"
    "[tui]\n"
    "screen_reader_detection_done = true\n"
)


def _home(tmp_path, config=CONFIG, agents=None):
    home = tmp_path / ".codex"
    home.mkdir()
    (home / "config.toml").write_text(config, encoding="utf-8")
    if agents is not None:
        (home / "AGENTS.md").write_text(agents, encoding="utf-8")
    return home


def test_yes_writes_what_the_report_says_is_missing_and_the_report_goes_quiet(tmp_path):
    """bdd4dfd3, 4dd922ff: reported every day from 2026-10-04, written by nothing."""
    home = _home(tmp_path)
    assert len(ctx.report_lines(home)) == 2
    assert inst.main(["--check", "--home", str(home)]) == 1
    assert inst.main(["--yes", "--home", str(home)]) == 0
    assert ctx.report_lines(home) == []
    assert inst.main(["--check", "--home", str(home)]) == 0


def test_the_setting_lands_above_the_first_table(tmp_path):
    """Below `[tui]` it would be `tui.project_doc_fallback_filenames`, read by nothing."""
    text = inst.with_fallback(CONFIG)
    lines = text.splitlines()
    assert lines.index(inst.FALLBACK_LINE) < lines.index("[projects.'c:\\users\\alexa']")
    assert inst.with_fallback("") == inst.FALLBACK_LINE + "\n"
    assert inst.with_fallback("a = 1") == "a = 1\n" + inst.FALLBACK_LINE + "\n"


def test_an_operators_values_are_kept(tmp_path):
    home = _home(
        tmp_path,
        config='project_doc_fallback_filenames = ["AGENTS.md", "CLAUDE.md"]\n',
        agents="Read .claude/rules my own way.\n",
    )
    assert inst.plan(home, uninstall=False) == []
    assert inst.plan(home, uninstall=True) == [], "nothing here is this installer's"


def test_the_bridge_is_appended_after_what_agents_md_already_says(tmp_path):
    text = inst.with_bridge("# No hooks\n\nPolicy.\n")
    assert text.startswith("# No hooks\n\nPolicy.\n\n")
    assert text.endswith(inst.BRIDGE)
    assert inst.with_bridge("") == inst.BRIDGE


def test_uninstall_takes_back_exactly_what_yes_wrote(tmp_path):
    home = _home(tmp_path, agents="# No hooks\n")
    assert inst.main(["--yes", "--home", str(home)]) == 0
    assert inst.main(["--uninstall", "--yes", "--home", str(home)]) == 0
    assert (home / "config.toml").read_text(encoding="utf-8") == CONFIG
    assert (home / "AGENTS.md").read_text(encoding="utf-8") == "# No hooks\n"


def test_the_removers_answer_none_when_nothing_is_theirs():
    assert inst.without_fallback(CONFIG) is None
    assert inst.without_bridge("# No hooks\n") is None
    assert inst.without_fallback(inst.with_fallback(CONFIG)) == CONFIG


def test_check_names_the_missing_file_and_build_parser_spells_every_verb(tmp_path, capsys):
    home = _home(tmp_path, agents="Read .claude/rules.\n")
    assert inst.run_check(home) == 1
    assert "config.toml" in capsys.readouterr().err
    for argv in (["--check"], ["--uninstall", "--yes"], ["--yes"]):
        inst.build_parser().parse_args(argv)


def test_a_dry_run_changes_nothing(tmp_path):
    home = _home(tmp_path)
    assert inst.main(["--home", str(home)]) == 0
    assert (home / "config.toml").read_text(encoding="utf-8") == CONFIG
    assert not (home / "AGENTS.md").exists()


def test_no_codex_here_is_left_alone(tmp_path):
    absent = tmp_path / "nope"
    assert inst.main(["--check", "--home", str(absent)]) == 2
    assert inst.main(["--yes", "--home", str(absent)]) == 2
    assert not absent.exists()
