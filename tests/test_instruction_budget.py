"""Cover `scripts/instruction-budget.py` — the measurement half of instruction pruning.

Most of this exercises fixtures rather than devkit's own files, because the subject is
the *classifier*, not this repo's current prose. Three tests deliberately read the real
repo, and each is a claim that would otherwise rot silently:

- `test_the_line_cap_matches_authoring_md` — the module duplicates a number that lives
  in prose, and this is what stops the duplicate drifting.
- `test_the_vendored_set_is_read_not_guessed` — a stale MANIFEST reader would mark
  nothing vendored and quietly invite a cleanup edit that lands as drift downstream.
- `test_the_hot_budget_stays_under_its_ceiling` — the ratchet. Without it a pruning
  pass is a one-off and the tier grows straight back.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from support import REPO_ROOT, load_script

budget = load_script("scripts/instruction-budget.py")


# The measured hot total, rounded up to the next hundred.
# `.claude/rules/engineering.md` says coverage floors are ratchets and must never be
# raised to make a change pass; the same applies here. **This number only goes down.**
# Lower it after a pruning pass; if a change genuinely needs more hot prose, move
# something else down a tier to pay for it.
#
# The headroom is deliberately smaller than a paragraph: under 100 tok, a typo fix or a
# reworded sentence cannot redden `main`, and anything worth calling an addition still
# does. It is not a budget to spend. The first ceiling carried "a little headroom" of an
# unstated size and it bought nothing -- one paragraph added to `engineering.md` went
# straight through it, and because a PR gate is not re-run when its base moves, the
# ratchet went red on `main` rather than on the PR that grew the file. That red then
# stopped the nightly release, which is the backstop working: GitHub Free cannot require
# a PR to be current before merging (see `scripts/git_policy.py`), so nothing else here
# would have caught it.
HOT_CEILING = 5800


# --- the estimate -------------------------------------------------------------


def test_estimate_tokens_scales_with_length():
    assert budget.estimate_tokens("") == 0
    short = budget.estimate_tokens("x" * 100)
    assert short == pytest.approx(27, abs=1)
    assert budget.estimate_tokens("x" * 1000) == pytest.approx(short * 10, rel=0.05)


def test_tokens_from_chars_accepts_a_count_not_a_string():
    """Regression: `sections` accumulates lengths and has no string left to measure.

    Calling the string form with that integer raised `TypeError: object of type 'int'
    has no len()`. It fired on any file with an H2 — which is every hot file, and no
    fixture small enough for the bug to look like an edge case.
    """
    assert budget.tokens_from_chars(370) == 100
    assert budget.tokens_from_chars(0) == 0


# --- frontmatter and scoping --------------------------------------------------


SCOPED_RULE = """---
description: A scoped rule
paths:
  - src/**/*.py
---

# Body
"""

UNSCOPED_RULE = """---
description: A global rule
---

# Body

Rules may declare `paths:` in their frontmatter, like so:

```yaml
paths:
  - src/**/*.py
```
"""


def test_frontmatter_returns_only_the_leading_block():
    assert "description: A scoped rule" in budget.frontmatter(SCOPED_RULE)
    assert "# Body" not in budget.frontmatter(SCOPED_RULE)
    assert budget.frontmatter("no frontmatter here") == ""


def test_rule_is_scoped_reads_frontmatter_only():
    """A `paths:` in the body is prose *about* frontmatter, not a declaration.

    Counting one would report an unscoped rule as scoped — the direction that hides
    cost, since the file would drop out of the hot total while still loading every
    session. `authoring.md` and this repo's own rules both contain such a block.
    """
    assert budget.rule_is_scoped(SCOPED_RULE) is True
    assert budget.rule_is_scoped(UNSCOPED_RULE) is False


# --- sections -----------------------------------------------------------------


def test_sections_splits_on_h2_in_document_order():
    text = "intro\n\n## Alpha\n\nbody\n\n## Beta\n\nmore body here\n"
    names = [name for name, _ in budget.sections(text)]
    assert names == ["(preamble)", "Alpha", "Beta"]


def test_sections_attributes_every_byte_to_some_section():
    text = "intro\n\n## Alpha\n\nbody\n\n## Beta\n\nmore\n"
    assert sum(cost for _, cost in budget.sections(text)) == budget.estimate_tokens(text)


def test_sections_ignores_h1_and_h3():
    text = "# Title\n\n## Real\n\n### Sub\n\nbody\n"
    assert [name for name, _ in budget.sections(text)] == ["(preamble)", "Real"]


# --- the vendored set ---------------------------------------------------------


def test_the_vendored_set_is_read_not_guessed():
    paths = budget.manifest_paths(REPO_ROOT)
    assert ".claude/rules/engineering.md" in paths
    assert ".claude/rules/authoring.md" in paths
    assert "CLAUDE.md" not in paths, "a project's own root file is never vendored"


def test_the_background_pipeline_warning_names_the_masked_task_status():
    rule = (REPO_ROOT / ".claude/rules/engineering.md").read_text(encoding="utf-8")
    assert "background task's completion status" in rule


def test_manifest_paths_is_empty_when_sync_devkit_is_missing(tmp_path: Path):
    assert budget.manifest_paths(tmp_path) == frozenset()


# --- the memory slug ----------------------------------------------------------


def test_slug_for_matches_the_directory_claude_code_creates():
    assert (
        budget.slug_for(Path(r"C:\Users\a\Desktop\vs_code\devkit"))
        == "C--Users-a-Desktop-vs-code-devkit"
    )


def test_slug_for_separates_a_mistyped_path_from_the_real_one():
    """The mechanism behind an orphaned memory directory: one typo, one dead slug."""
    assert budget.slug_for(Path("/w/ibkr_trader")) != budget.slug_for(Path("/w/ibrk_trader"))


# --- discovery and tiers ------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "CLAUDE.md").write_text("# Root\n\n## A\n\nbody\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "CLAUDE.md").write_text("# Subtree\n", encoding="utf-8")
    rules = tmp_path / ".claude" / "rules"
    rules.mkdir(parents=True)
    (rules / "scoped.md").write_text(SCOPED_RULE, encoding="utf-8")
    (rules / "global.md").write_text(UNSCOPED_RULE, encoding="utf-8")
    skill = tmp_path / ".claude" / "skills" / "thing"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
    return tmp_path


def _tier(docs, name: str) -> str:
    return next(doc.tier for doc in docs if doc.path.name == name or doc.rel.endswith(name))


def test_discover_assigns_the_tier_that_decides_the_cost(repo: Path):
    docs = budget.discover(repo, frozenset())
    assert _tier(docs, "CLAUDE.md") == "hot"
    assert _tier(docs, "src/CLAUDE.md") == "lazy"
    assert _tier(docs, "global.md") == "hot"
    assert _tier(docs, "scoped.md") == "lazy"
    assert _tier(docs, "SKILL.md") == "on-demand"


def test_discover_marks_vendored_files(repo: Path):
    docs = budget.discover(repo, frozenset({".claude/rules/global.md"}))
    by_name = {doc.path.name: doc for doc in docs}
    assert by_name["global.md"].vendored is True
    assert by_name["scoped.md"].vendored is False


def test_discover_skips_worktrees_and_dependency_trees(repo: Path):
    """A box holds a copy of every instruction file; counting them multiplies the total."""
    box = repo / ".worktrees" / "a-box"
    box.mkdir(parents=True)
    (box / "CLAUDE.md").write_text("# Copy\n", encoding="utf-8")
    nested = repo / "node_modules" / "pkg"
    nested.mkdir(parents=True)
    (nested / "CLAUDE.md").write_text("# Vendor\n", encoding="utf-8")

    found = {doc.rel for doc in budget.discover(repo, frozenset())}
    assert not any(".worktrees" in rel or "node_modules" in rel for rel in found)


def test_hot_total_counts_only_the_hot_tier(repo: Path):
    docs = budget.discover(repo, frozenset())
    expected = sum(doc.tokens for doc in docs if doc.tier == "hot")
    assert budget.hot_total(docs) == expected
    assert budget.hot_total(docs) < sum(doc.tokens for doc in docs)


# --- findings -----------------------------------------------------------------


def test_find_oversized_flags_a_file_over_the_line_cap(tmp_path: Path):
    doc = budget.Doc(
        path=tmp_path / "CLAUDE.md",
        tier="hot",
        tokens=10,
        lines=budget.LINE_CAP + 1,
        vendored=False,
    )
    kinds = [finding.kind for finding in budget.find_oversized([doc])]
    assert "over-line-cap" in kinds


def test_find_oversized_flags_a_fat_unscoped_rule_but_not_a_scoped_one(tmp_path: Path):
    rules = tmp_path / "rules"
    rules.mkdir()
    fat = budget.UNSCOPED_RULE_TOKENS + 1
    unscoped = budget.Doc(path=rules / "a.md", tier="hot", tokens=fat, lines=10, vendored=False)
    scoped = budget.Doc(path=rules / "b.md", tier="lazy", tokens=fat, lines=10, vendored=False)

    assert "fat-unscoped-rule" in [f.kind for f in budget.find_oversized([unscoped])]
    assert "fat-unscoped-rule" not in [f.kind for f in budget.find_oversized([scoped])], (
        "a scoped rule costs nothing in a session that never touches its paths"
    )


def test_every_finding_names_a_remedy(tmp_path: Path):
    """A report that says only what is wrong is one nobody can act on."""
    doc = budget.Doc(path=tmp_path / "CLAUDE.md", tier="hot", tokens=10, lines=9999, vendored=False)
    for finding in budget.find_oversized([doc]):
        assert finding.remedy.strip()


def test_find_stale_memory_reports_old_files_and_skips_the_index(tmp_path: Path):
    now = time.time()
    old = tmp_path / "ancient.md"
    old.write_text("x", encoding="utf-8")
    fresh = tmp_path / "recent.md"
    fresh.write_text("x", encoding="utf-8")
    index = tmp_path / "MEMORY.md"
    index.write_text("x", encoding="utf-8")

    ancient = now - (budget.STALE_MEMORY_DAYS + 5) * 86400
    for path in (old, index):
        import os

        os.utime(path, (ancient, ancient))

    subjects = {finding.subject for finding in budget.find_stale_memory(tmp_path, now)}
    assert subjects == {"ancient.md"}, "MEMORY.md is the hot index, pruned with its entries"


def test_find_orphan_memory_names_slugs_no_checkout_produces(tmp_path: Path):
    for name, files in (("live-slug", ["a.md"]), ("dead-slug", ["b.md"]), ("empty-slug", [])):
        memory = tmp_path / name / "memory"
        memory.mkdir(parents=True)
        for item in files:
            (memory / item).write_text("x", encoding="utf-8")

    findings = budget.find_orphan_memory(tmp_path, live={"live-slug"})
    subjects = {finding.subject for finding in findings}
    assert subjects == {"dead-slug"}, "a live slug is recallable; an empty one costs nothing"


def test_a_slug_differing_only_in_case_is_not_orphaned(tmp_path: Path):
    """Regression: Windows hands back the drive letter in whichever case the shell used.

    `c--Users-...-carameli` and `C--Users-...-carameli` are one checkout, and only the
    lowercase directory had ever been written. A case-sensitive comparison reported it
    as a slug "no live checkout produces", with `delete` as the stated remedy -- 37 live
    memories, plus 13 more under another project.
    """
    memory = tmp_path / "c--Users-a-vs-code-carameli" / "memory"
    memory.mkdir(parents=True)
    (memory / "held.md").write_text("x", encoding="utf-8")

    findings = budget.find_orphan_memory(tmp_path, live={"C--Users-a-vs-code-carameli"})
    assert findings == [], "the drive letter's case does not make a second checkout"


def test_workspace_of_looks_through_the_box_store(tmp_path: Path):
    """`.worktrees/` holds copies of checkouts, so it is never the workspace itself."""
    workspace = tmp_path / "vs_code"
    checkout = workspace / "devkit"
    box = workspace / ".worktrees" / "devkit--topic-0827"

    assert budget.workspace_of(checkout) == workspace
    assert budget.workspace_of(box) == workspace, "a box's parent is the store, not the workspace"


def test_live_slugs_covers_the_workspace_directory_itself(tmp_path: Path):
    """The parent is a working directory too, not merely the thing checkouts sit in.

    A multi-root workspace has its own `CLAUDE.md` governing sessions opened at that
    level, so its memories are recallable. Building the live set from the checkout and
    its *siblings* alone left the workspace slug out and reported it as orphaned.
    """
    workspace = tmp_path / "vs_code"
    (workspace / "devkit").mkdir(parents=True)
    (workspace / "carameli").mkdir()

    slugs = budget.live_slugs(workspace / "devkit")

    assert budget.slug_for(workspace) in slugs, "sessions run at the workspace root"
    assert budget.slug_for(workspace / "devkit") in slugs
    assert budget.slug_for(workspace / "carameli") in slugs


def test_live_slugs_from_a_box_still_sees_the_real_checkouts(tmp_path: Path):
    """Regression: run from an ephemeral box, the parent is `.worktrees`, not the
    workspace — so every real checkout, devkit's own included, read as orphaned. The
    finding's remedy is `delete`, and devkit's memory directory is the biggest here."""
    workspace = tmp_path / "vs_code"
    (workspace / "devkit").mkdir(parents=True)
    box = workspace / ".worktrees" / "devkit--topic-0827"
    box.mkdir(parents=True)

    slugs = budget.live_slugs(box)

    assert budget.slug_for(workspace / "devkit") in slugs, "the box is a copy of a checkout"
    assert budget.slug_for(workspace) in slugs
    assert budget.slug_for(box) in slugs, "a live box is a working directory of its own"


# --- rendering and the entry point --------------------------------------------


def test_render_marks_vendored_files_as_uneditable_here(repo: Path):
    docs = budget.discover(repo, frozenset({".claude/rules/global.md"}))
    report = budget.render(docs, [])
    assert "[vendored: edit in devkit only]" in report


def test_render_says_none_rather_than_leaving_the_section_empty(repo: Path):
    assert "None." in budget.render(budget.discover(repo, frozenset()), [])


def test_render_carries_every_field_of_a_finding(repo: Path):
    """All four fields reach the artifact — a dropped remedy is a report nobody can act on."""
    finding = budget.Finding(
        kind="stale-memory",
        subject="ancient.md",
        detail="untouched for 90 days",
        remedy="verify the fact still holds",
    )
    report = budget.render(budget.discover(repo, frozenset()), [finding])
    for field in (finding.kind, finding.subject, finding.detail, finding.remedy):
        assert field in report


def test_memory_root_resolves_under_the_given_home(tmp_path: Path):
    """Taking `home` as an argument is what lets the memory pass be tested at all."""
    assert budget.memory_root(tmp_path) == tmp_path / ".claude" / "projects"
    assert budget.memory_root() == Path.home() / ".claude" / "projects"


def test_collect_notes_a_root_with_no_memory_directory(repo: Path, tmp_path: Path):
    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    _, _, note = budget.collect(repo, REPO_ROOT, home=home)
    assert "nothing to prune" in note.lower()


def test_main_writes_its_artifact_and_reports_clean(repo: Path, capsys):
    assert budget.main(["--root", str(repo), "--devkit", str(REPO_ROOT)]) == 0
    assert (REPO_ROOT / "logs" / "instruction-budget.log").is_file()
    assert "hot:" in capsys.readouterr().out


def test_check_exits_nonzero_only_when_there_are_findings(repo: Path):
    assert budget.main(["--root", str(repo), "--devkit", str(REPO_ROOT), "--check"]) == 0
    (repo / "CLAUDE.md").write_text("\n".join(["line"] * (budget.LINE_CAP + 1)), encoding="utf-8")
    assert budget.main(["--root", str(repo), "--devkit", str(REPO_ROOT), "--check"]) == 1


# --- the claims about this repo -----------------------------------------------


def test_the_line_cap_matches_authoring_md():
    """The module duplicates a number owned by prose; this checks the duplicate."""
    text = (REPO_ROOT / ".claude" / "rules" / "authoring.md").read_text(encoding="utf-8")
    assert f"**{budget.LINE_CAP} lines**" in text, (
        f"instruction-budget.LINE_CAP is {budget.LINE_CAP}, which authoring.md no longer states"
    )


def test_the_hot_budget_stays_under_its_ceiling():
    """The ratchet. Lower `HOT_CEILING` after a pruning pass; never raise it.

    Without this a pruning pass is a one-off: the tier grows straight back, and the
    next session pays for it on every API call with nothing red anywhere.
    """
    docs = budget.discover(REPO_ROOT, budget.manifest_paths(REPO_ROOT))
    total = budget.hot_total(docs)
    hot = sorted((d for d in docs if d.tier == "hot"), key=lambda d: -d.tokens)
    assert total <= HOT_CEILING, (
        f"always-loaded instruction tier is {total} tok, ceiling is {HOT_CEILING}. "
        "Move a section to a lazy tier rather than raising the ceiling. Largest: "
        + ", ".join(f"{d.rel} ({d.tokens})" for d in hot[:3])
    )
