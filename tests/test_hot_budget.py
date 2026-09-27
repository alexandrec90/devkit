"""`scripts/hot-budget.py`: the instruction-tier ratchet, checked at commit time."""

from __future__ import annotations

from support import REPO_ROOT, load_script

budget = load_script("scripts/instruction-budget.py")
hot_budget = load_script("scripts/hot-budget.py")
HOT_CEILING = hot_budget.HOT_CEILING


def test_the_commit_stage_names_an_over_budget_tier_and_what_to_move(tmp_path):
    """c5dae5d1: the ceiling was first met in CI, three PRs running, and each took a
    fixer. `hot-budget.py` is the same verdict at commit time."""
    fat = budget.Doc(path=tmp_path / "a.md", tier="hot", tokens=70, lines=1, vendored=False)
    lazy = budget.Doc(path=tmp_path / "b.md", tier="lazy", tokens=900, lines=1, vendored=False)
    assert hot_budget.verdict([fat, lazy], ceiling=70) == (0, "instruction budget: hot 70/70 tok")
    code, said = hot_budget.verdict([fat, lazy], ceiling=69)
    assert code == 1 and "hot 70/69" in said and "never raise it" in said
    assert "(70)" in said and "(900)" not in said, "only the hot tier is named"


def test_the_commit_stage_check_reads_this_repos_tier(capsys):
    assert hot_budget.main() == 0, capsys.readouterr().out
    assert f"/{HOT_CEILING} tok" in capsys.readouterr().out


def test_the_commit_stage_hook_checks_the_hot_tier_on_every_markdown_change():
    config = (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    hook = config.split("- id: instruction-budget", 1)[1].split("\n\n", 1)[0]
    assert "entry: scripts/hot-budget.py" in hook
    assert r"files: \.md$" in hook and "pass_filenames: false" in hook
