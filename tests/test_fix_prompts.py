"""`scripts/fix_prompts.py`: what each shape of red tells its session."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fix_plan
import fix_prompts
from support import REPO_ROOT, load_script


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
    text = fix_prompts.pr_prompt(failure(evidence="C:/ev/carameli-pr-412"))
    assert "ship skill" in text
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


def test_the_upstream_prompt_sends_a_pr_red_on_its_own_diff_to_that_pr():
    """devkit #387's upstream session found the cause was the PR's own diff and could
    only stop: nothing on a fresh branch off a green default lands on the PR."""
    text = fix_prompts.upstream_prompt((failure(),), "agent/fix")
    assert fix_prompts.OWN_DIFF in text
    assert "ship it with the ship skill from that worktree" in text
    assert text.index(fix_prompts.OWN_DIFF) < text.index(fix_prompts.STOP)


def every_prompt() -> list[str]:
    """One of each shape of prompt a fresh session can be sent with."""
    return [
        fix_prompts.pr_prompt(failure()),
        fix_prompts.pr_prompt(failure(signature=(fix_plan.CONFLICT,))),
        fix_prompts.pr_prompt(failure(kind=fix_plan.COMMIT, number=0, signature=("x refused",))),
        fix_prompts.upstream_prompt((failure(), red_main()), "agent/fix"),
        fix_prompts.branch_prompt(red_main(), "agent/fix"),
        fix_prompts.nightly_prompt(failure(kind=fix_plan.NIGHTLY, number=3), "agent/fix"),
    ]


def test_every_prompt_ends_on_the_one_narrow_exit():
    """A fresh session read "if it cannot be fixed, stop" as leave for any obstacle:
    devkit #387's stopped with the fix one worktree away. Every prompt now ends on the
    same exit, which names the three blockers that justify stopping and the obstacles
    that do not -- so a new prompt cannot quietly bring back a wider one."""
    for text in every_prompt():
        assert text.endswith(fix_prompts.STOP)
        assert text.count(fix_prompts.STOP) == 1


def test_every_prompt_opens_by_declaring_the_fixer_role():
    """The vendored rules are written for project sessions, and a fixer is told apart
    from one by this sentence alone -- the launcher's declaration, never an inference
    from what the prompt happens to name."""
    for text in every_prompt():
        assert text.startswith(fix_prompts.ROLE)
        assert text.count(fix_prompts.ROLE) == 1


def test_the_role_points_at_a_fixer_file_every_consumer_receives():
    """PR fixers run in consumer checkouts too, so the file has to be vendored; and the
    override is spelled in the sentence itself for a consumer that has not pulled it."""
    rel = ".claude/fixer.md"
    assert rel in fix_prompts.ROLE
    assert (REPO_ROOT / rel).is_file()
    sync = load_script("scripts/sync-devkit.py")
    assert rel in sync.MANIFEST
    for overridden in ("the harness is not your job", "and stop"):
        assert overridden in fix_prompts.ROLE
    # A fixer ships an intent like any session; the pass commits and pushes (FINISH).
    assert "commit" not in fix_prompts.ROLE and "push" not in fix_prompts.ROLE
    assert not set("\"'`") & set(fix_prompts.ROLE)


def test_the_fixer_file_says_a_choice_is_never_a_blocker_and_size_only_warns():
    """Every fixer reads this before its prompt's exit clause: it must not leave a design
    fork looking like a stop, nor send a session shaving a module to fit its size."""
    text = (REPO_ROOT / ".claude" / "fixer.md").read_text(encoding="utf-8")
    assert "Is the obstacle a choice?** Never a blocker" in text
    assert "`definitions` only warn" in text


def test_no_prompt_offers_the_open_exit():
    """The phrasing that let any obstacle count as a blocker, anywhere in the module."""
    source = Path(fix_prompts.__file__).read_text(encoding="utf-8")
    code = source.split("STOP = (", 1)[1]
    assert "what is in the way" not in code
    for text in every_prompt():
        assert "cannot be fixed" not in text and "cannot be done" not in text


def test_the_exit_names_its_blockers_what_is_not_one_and_asks_for_evidence():
    for expected in (
        "three blockers only",
        "quote the exact command",
        "only a person has",
        "outside this repository",
        "adding a worktree on another branch",
        "what you tried first",
    ):
        assert expected in fix_prompts.STOP
    # It crosses a wt command line with every other sentence here.
    assert not set("\"'`") & set(fix_prompts.STOP)


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
    assert "logs/ship-intent.refused.md" in text and "fix pass commits" in text


def test_the_upstream_prompt_names_every_id_across_the_group():
    group = (failure(signature=("a::t",)), failure(project="x", signature=("b::u",)))
    text = fix_prompts.upstream_prompt(group, "agent/fix")
    assert "carameli #412 -- 1 check failing: a::t" in text
    assert "x #412 -- 1 check failing: b::u" in text


def test_the_branch_prompt_names_the_base_the_run_the_ids_and_the_fresh_branch():
    text = fix_prompts.branch_prompt(
        red_main(project="carameli", signature=("tests/t.py::a",), evidence="C:/ev/x"),
        "agent/fix-pr-gate-0919",
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


def test_a_prompt_says_when_no_artifact_came_down_instead_of_naming_an_empty_directory():
    """Two sessions were told the logs were under logs/gate/ and spent turns finding
    the directory absent: the run had aged out, or uploaded nothing."""
    none = failure(evidence="", url="u/412")
    some = failure(evidence="C:/ev/carameli-pr-412")
    assert "No artifact came down from the run; read it at u/412" in fix_prompts.pr_prompt(none)
    assert fix_plan.EVIDENCE_DIR not in fix_prompts.pr_prompt(none)
    assert fix_plan.EVIDENCE_DIR in fix_prompts.pr_prompt(some)
    nightly = failure(kind=fix_plan.NIGHTLY, workflow="Nightly", number=7, url="u/7", evidence="")
    assert "No artifact came down" in fix_prompts.nightly_prompt(nightly, "agent/fix")
    assert "No artifact came down" in fix_prompts.branch_prompt(red_main(evidence=""), "agent/fix")
    assert "read the run at its URL" in fix_prompts.upstream_prompt((none,), "agent/fix")


def test_the_resolver_leaves_the_merge_for_the_pass_to_commit():
    """The first resolver the pass sent used --no-verify on its merge commit and flagged
    it itself. Now it commits nothing: the pass concludes the merge with the hooks
    running, which is the same protection with no prompt sentence to forget."""
    text = fix_prompts.pr_prompt(failure(signature=(fix_plan.CONFLICT,)))
    assert "leave the merge uncommitted" in text and "hooks running" in text
    assert "--no-verify" not in text


def test_every_prompt_ends_at_the_ship_skill_or_the_blocked_file_and_never_at_a_push():
    """A fixer fixes and stops. Merging the base in, pushing, opening the PR and reading
    the gate are one command each, and the pass runs them; a session's turn spent on
    any of them is the expensive way to run a command. The PR prompt used to say "merge
    origin/main in ... push" and the resolver used to commit and push itself."""
    for text in every_prompt():
        assert "ship skill" in text and "logs/fix-blocked.md" in text
        assert "the fix pass commits, pushes, opens or updates the PR" in text
        assert "git push" not in text and "Merge origin/main in, fix" not in text
        assert "bare git push" not in text


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


def test_every_prompt_names_the_friction_channel_the_pass_files_from():
    """The engineering rule told sessions to report what the harness did to them, and
    the report went into a chat nothing read. Each line of `logs/friction.md` is filed
    on the harness-defect ledger instead, so every prompt has to say the file exists."""
    for text in every_prompt():
        assert "logs/friction.md" in text and "the pass files each for the devkit session" in text


def test_the_devkit_session_is_told_to_take_over_an_escalated_problem():
    """What replaced "needs a human": the devkit session fixes what failed the fixers and
    the problem itself, and names its branch so a resolution can be checked."""
    for kind in ("fixers-exhausted", "blind-evidence", "fixer-blocked"):
        assert kind in fix_prompts.LEDGER_STEPS
    assert "--pr BRANCH" in fix_prompts.LEDGER_STEPS
    assert "`" not in fix_prompts.LEDGER_STEPS and '"' not in fix_prompts.LEDGER_STEPS, (
        "it crosses a wt command line"
    )


def test_the_resolver_checks_every_file_for_a_conflict_marker():
    """A resolver grepped only `*.py` and left a marker in `.claude/rules/session-scope.md`;
    the commit stage refused it a pass later."""
    text = fix_prompts.pr_prompt(failure(signature=(fix_plan.CONFLICT,)))
    assert "git diff --check" in text and "not only the code" in text


def test_every_prompt_names_the_ratchets_and_forbids_a_question():
    for text in every_prompt():
        assert "structure_check.py" in text and "untested_symbols.py" in text
        assert "never ask a question" in text
        # A fixer ended on a question whose "(Recommended)" option was the answer.
        assert "the option you would recommend is the decision" in text
        # `STOP` once listed "a decision only a person has" as a blocker; a decision is
        # never one -- the sweep that stopped on one had its recommendation in hand.
        assert "never a decision, which is yours" in text and "a decision or" not in text
        # A missing .venv came back session after session, each fixing only its own tree.
        assert "Fix causes, not instances" in text and "the provisioner" in text
        # A rule file said it and three sessions still lost turns: the prompt says it too.
        assert "never a shell heredoc" in text


def test_a_red_gate_prompt_names_the_readable_failures_and_that_the_gate_is_linux():
    """Two sweeps hand-parsed junit XML, and one shipped a Linux-only fix it could not
    check here and said nothing about it."""
    with_evidence = fix_prompts.pr_prompt(failure(evidence="C:/ev/carameli-pr-412"))
    assert "failures.txt" in with_evidence and "ran on Linux" in with_evidence
    assert "ran on Linux" in fix_prompts.pr_prompt(failure(evidence=""))
    assert ".venv interpreter" in fix_prompts.FINISH
    assert "--pr" in fix_prompts.LEDGER_STEPS and "any repository" in fix_prompts.LEDGER_STEPS


def test_the_skill_the_ledger_sweep_follows_never_sends_it_to_the_user():
    """`LEDGER_STEPS` sends the devkit session to this skill, which told it to ask the user
    about "a fix needing a decision only the user can make" -- and it did, with nobody
    there. It now says to decide, and why. On 2026-09-26 a dispatched session followed the
    skill's "Ask it, get the answer, fix it" into an `AskUserQuestion` nobody answered,
    while the prompt that sent it said never to ask."""
    skill = (REPO_ROOT / ".claude" / "skills" / "triage-harness" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "triage-harness/SKILL.md" in fix_prompts.LEDGER_STEPS
    assert "never ask a question" in fix_prompts.upstream_prompt((failure(),), "a/b")
    assert "Decide everything, and never ask" in skill
    assert "Ask it, get the answer" not in skill and "stay the user's call" not in skill
    assert "Fix the cause, never the instance" in skill and "RECURRED" in skill


def test_the_devkit_session_cuts_sibling_trees_with_the_verb_that_marks_them():
    """Told to copy the mark by hand, a sweep did not; the verb now does it
    (`fix_reports.inherit_origin`), so the prompt names the verb, not the chore."""
    steps = fix_prompts.LEDGER_STEPS
    assert "scripts/agent-worktree.py new" in steps and "merges once green" in steps
    assert "gets a copy" not in steps
