"""`scripts/fix_prompts.py`: what each shape of red tells its session."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fix_plan
import fix_prompts


def failure(**fields) -> fix_plan.Failure:
    base: dict[str, Any] = {
        "kind": fix_plan.PR,
        "project": "carameli",
        "number": 412,
        "title": "Adopt devkit v0.11.21 (10 vendored file(s))",
        "url": "https://github.com/x/carameli/pull/412",
        "head": "agent/auto/devkit-upgrade-v0-11-21-0917",
        "base": "main",
        "sha": "abc123",
        "reason": "1 check failing",
        "signature": (
            "scripts/hooks/tests/test_untested_symbols.py::test_every_public_symbol_is_named_by_a_test",
        ),
    }
    base.update(fields)
    return fix_plan.Failure(**base)


def red_main(**fields) -> fix_plan.Failure:
    base: dict[str, Any] = {
        "kind": fix_plan.BRANCH,
        "project": "devkit",
        "number": 0,
        "head": "",
        "base": "main",
        "sha": "fb17a31",
        "run_id": "35471200282",
        "workflow": "PR Gate",
        "url": "u/run",
        "reason": "",
        "signature": ("tests/test_new_project.py::" + fix_plan.RELEASE_TEST,),
    }
    base.update(fields)
    return failure(**base)


def test_the_pr_prompt_names_the_pr_the_fault_the_ids_the_logs_and_the_finish_line():
    text = fix_prompts.pr_prompt(failure())
    assert "fix pass reads it" in text
    for expected in (
        "#412",
        "carameli",
        "1 check failing",
        "test_every_public_symbol_is_named_by_a_test",
        fix_plan.EVIDENCE_DIR,
        "agent/auto/devkit-upgrade-v0-11-21-0917",
        "origin/main",
    ):
        assert expected in text


def test_the_upstream_prompt_names_every_project_and_pr_and_ends_in_the_ship_skill():
    group = (
        failure(project="carameli", number=412, url="u/412"),
        failure(project="roguelike", number=16, url="u/16"),
    )
    text = fix_prompts.upstream_prompt(group, "agent/fix-x-0918")
    assert "2 checkout(s) (carameli, roguelike)" in text
    assert "carameli #412 u/412" in text and "roguelike #16 u/16" in text
    assert "not in each consumer" in text
    assert "ship skill" in text and "agent/fix-x-0918" in text


def test_the_nightly_prompt_names_the_workflow_the_issue_and_the_fresh_branch():
    nightly = failure(
        kind=fix_plan.NIGHTLY, workflow="Nightly", number=7, url="u/7", signature=("tests/t.py::a",)
    )
    text = fix_prompts.nightly_prompt(nightly, "agent/fix-nightly-0918")
    assert "Nightly workflow in carameli" in text
    assert "issue #7 (u/7)" in text
    assert "origin/main" in text and "agent/fix-nightly-0918" in text
    assert "closes itself" in text


def test_a_conflicted_pr_gets_the_resolver_prompt_which_names_no_failure():
    """The gate cannot have run, and a resolver told "also fix the tests" fixes the
    wrong thing; whatever the gate says after the push is the next pass's business."""
    text = fix_prompts.pr_prompt(failure(signature=(fix_plan.CONFLICT, "tests/t.py::a")))
    assert "merge conflict with origin/main" in text
    assert "tests/t.py::a" not in text and "Failing" not in text
    assert "next pass" in text


def test_a_refused_commit_gets_the_prompt_for_its_own_worktree():
    refused = failure(kind=fix_plan.COMMIT, number=0, signature=("commit refused: secrets",))
    text = fix_prompts.pr_prompt(refused)
    assert "The commit stage refused the change on agent/auto/devkit-upgrade-v0-11-21-0917" in text
    assert "logs/ship-intent.md" in text and "fix pass commits" in text


def test_the_upstream_prompt_names_every_id_across_the_group():
    group = (failure(signature=("a::t",)), failure(project="x", signature=("b::u",)))
    text = fix_prompts.upstream_prompt(group, "agent/fix")
    assert "carameli #412 -- 1 check failing: a::t" in text
    assert "x #412 -- 1 check failing: b::u" in text


def test_the_branch_prompt_names_the_base_the_run_the_ids_and_the_fresh_branch():
    text = fix_prompts.branch_prompt(
        red_main(project="carameli", signature=("tests/t.py::a",)), "agent/fix-pr-gate-0919"
    )
    for expected in (
        "PR Gate workflow in carameli is red on origin/main itself",
        "fb17a31",
        "u/run",
        "tests/t.py::a",
        fix_plan.EVIDENCE_DIR,
        "agent/fix-pr-gate-0919",
        "ship skill",
    ):
        assert expected in text


def test_the_upstream_prompt_sends_the_session_at_the_ledger_only_when_the_backlog_is_in_it():
    backlog = red_main(
        kind=fix_plan.LEDGER, workflow="harness ledger", signature=("agent-report devkit [a] x2",)
    )
    with_it = fix_prompts.upstream_prompt((backlog, failure()), "agent/fix")
    assert "devkit ledger -- 1 open group(s) on the harness-defect ledger" in with_it
    assert "harness_triage.py --resolve-like" in with_it
    assert ".claude/skills/triage-harness/SKILL.md" in with_it
    assert fix_prompts.LEDGER_STEPS in with_it
    without = fix_prompts.upstream_prompt((failure(),), "agent/fix")
    assert "resolve-like" not in without


def test_the_upstream_prompt_reads_each_failure_with_what_its_gate_said():
    """devkit's own red main beside a consumer's shared vendored failure: one session,
    and each named the way the record names it, with its own reason."""
    group = (red_main(signature=("tests/t.py::a",)), failure(number=412, url="u/412"))
    text = fix_prompts.upstream_prompt(group, "agent/fix")
    assert "devkit origin/main -- PR Gate workflow failing on origin/main: tests/t.py::a" in text
    assert "carameli #412 -- 1 check failing: scripts/hooks/tests/" in text
    assert "one directory per failure" in text and "devkit origin/main u/run" in text
