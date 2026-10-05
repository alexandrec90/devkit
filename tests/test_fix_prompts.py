"""`scripts/fix_prompts.py`: what each shape of red tells its session."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fix_plan
import fix_prompts
import fix_reports
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


def test_the_nightly_prompt_names_the_red_runs_commit_beside_the_tip_it_was_cut_from():
    """41924a97: `failed-jobs.log` named no commit, and a fixer spent a `gh issue view`
    and a `gh pr list` learning it was older than its tree. c7979142: another slept in a
    loop waiting for the issue to close, which is the pass's to read."""
    tip = "b31b60c" + "b" * 33
    fields = {"kind": fix_plan.NIGHTLY, "workflow": "Nightly", "number": 69, "tip": tip}
    nightly = failure(**fields, run_id="36410261314", sha="59a4ef5" + "a" * 33)
    text = fix_prompts.nightly_prompt(nightly, "agent/fix-nightly-0928")
    assert "run 36410261314 at 59a4ef5aaaaa, which is not the tip" in text
    assert "whether origin/main already fixes it" in text
    assert "off origin/main at b31b60cbbbbb" in text
    assert "do not wait for it" in text
    at_tip = fix_prompts.nightly_prompt(failure(**fields, sha=tip), "agent/fix-nightly-0928")
    assert "which is not the tip" not in at_tip and "at b31b60cbbbbb" in at_tip


def _dependabot(*signature: str) -> fix_plan.Failure:
    return failure(
        kind=fix_plan.DEPENDABOT,
        number=0,
        title="Dependabot in ibkr_trader: 1 package(s) with alerts no PR answers",
        url="https://run/9",
        signature=signature,
    )


def test_the_dependabot_prompt_names_each_bump_and_where_the_evidence_is():
    alert = f"{fix_plan.ALERT_ENTRY}urllib3 >= 2.8.0"
    text = fix_prompts.dependabot_prompt(
        _dependabot(alert, "run sqlalchemy: dependency_file_content_not_changed"), "agent/fix-d"
    )
    assert text.startswith(fix_prompts.ROLE)
    assert "Bump each of these to at least the version named: urllib3 >= 2.8.0." in text
    assert "Update jobs also fail on: run sqlalchemy" in text
    assert f"{fix_plan.EVIDENCE_DIR}/{fix_plan.DEPENDABOT_EVIDENCE}" in text
    assert "Name every package you bumped" in text, "how the pass knows an alert is answered"
    assert "path dependency" not in text, "nothing to say about a sibling there is not"
    assert not set("`") & set(text)


def test_an_unfetchable_sibling_is_said_with_the_only_two_fixes_and_never_a_retry():
    """ibkr_trader: every Dependabot job died on `"data-lake" at /pyproject.toml`."""
    text = fix_prompts.dependabot_prompt(
        _dependabot(
            f"{fix_plan.ALERT_ENTRY}urllib3 >= 2.8.0", f"{fix_plan.UNFETCHABLE_ENTRY}data-lake"
        ),
        "agent/fix-d",
    )
    assert "dies on the path dependency data-lake" in text
    assert "re-running or retrying Dependabot changes nothing -- do neither" in text
    assert "bump the packages by hand in this repository" in text
    assert "give data-lake a source Dependabot can fetch" in text
    assert "move that ref to the sibling commit you locked against" in text
    assert "Update jobs also fail" not in text, "the sibling is said once, as the cause"


def test_a_conflicted_pr_gets_the_resolver_prompt_which_names_no_failure():
    """The gate cannot have run, and a resolver told "also fix the tests" fixes the
    wrong thing; whatever the gate says after the push is the next pass's business."""
    text = fix_prompts.pr_prompt(failure(signature=(fix_plan.CONFLICT, "tests/t.py::a")))
    assert "merge conflict with origin/main" in text
    assert "tests/t.py::a" not in text and "Failing" not in text
    assert "next pass" in text


def test_a_pr_prompt_says_when_its_tree_holds_what_an_earlier_session_left():
    """#422's resolver was sent into a tree with 21 files unstaged from an earlier
    session and told nothing of it (f7167792); the pass knew, and only printed it."""
    left = "the tree has uncommitted changes"
    for red in (failure(), failure(signature=(fix_plan.CONFLICT,))):
        text = fix_prompts.pr_prompt(red, "", left)
        assert left in text and "git status" in text and "earlier session" in text
        assert f"git log origin/{red.head}..HEAD" in text
        # 5f5c4b3c: told only to look, a fixer worked on a base three merges behind.
        assert f"git fetch origin {red.head} and git merge origin/{red.head}" in text
        assert "already up to date" not in text
    assert "earlier session" not in fix_prompts.pr_prompt(failure())
    assert "already up to date with its base" in fix_prompts.pr_prompt(failure())


def test_a_refused_commit_gets_the_prompt_for_its_own_worktree():
    refused = failure(kind=fix_plan.COMMIT, number=0, signature=("commit refused: secrets",))
    text = fix_prompts.pr_prompt(refused)
    assert "The commit stage refused the change on agent/auto/devkit-upgrade-v0-11-21-0917" in text
    assert "logs/ship-intent.refused.md" in text and "fix pass commits" in text


def test_every_pr_shape_names_a_cache_an_elevated_session_locked_and_the_way_round_it():
    """cadb6ba8: sports_betting #48's fixer reused a tree an elevated session had run
    pytest in, found its `.pytest_cache` by `WinError 5`, and worked out
    `-p no:cacheprovider` alone. The prompt says so up front, in every shape."""
    locked = {".pytest_cache": "PYTEST_ADDOPTS=-p no:cacheprovider"}
    shapes = (
        failure(),
        failure(signature=(fix_plan.CONFLICT,)),
        failure(kind=fix_plan.COMMIT, number=0, signature=("x refused",)),
    )
    for red in shapes:
        text = fix_prompts.pr_prompt(red, "", "", locked)
        assert "An elevated session left .pytest_cache" in text
        assert "PYTEST_ADDOPTS=-p no:cacheprovider" in text
        assert text.endswith(fix_prompts.STOP)
        assert "elevated session" not in fix_prompts.pr_prompt(red)
        assert "elevated session" not in fix_prompts.pr_prompt(red, "", "", {})


def test_a_pr_prompt_quotes_a_refusal_already_standing_in_its_tree(tmp_path):
    """d609d34d: carameli #395's real blocker was an earlier fixer's intent the commit
    stage had refused (detect-secrets, `.secrets.baseline` unstaged), recorded in the
    tree's `ship-state.json`, while the prompt named only the red check -- five calls to
    find. The fixer's own work ships through that same stage, so the prompt says so."""
    state = tmp_path / "logs" / "ship-state.json"
    state.parent.mkdir()
    output = "detect-secrets.......Failed\n- hook id: detect-secrets\n.secrets.baseline unstaged\n"
    state.write_text(
        json.dumps({"stage": "refused", "step": "commit", "output": output}), encoding="utf-8"
    )
    refusal = fix_prompts.standing_refusal(tmp_path)
    assert refusal.startswith("commit: ") and "detect-secrets" in refusal
    for red in (failure(), failure(signature=(fix_plan.CONFLICT,))):
        text = fix_prompts.pr_prompt(red, refusal)
        assert "refused" in text and ".secrets.baseline unstaged" in text
    assert "ship-state" not in fix_prompts.pr_prompt(failure())
    # A shipped state, no state, or an unreadable one: nothing to quote.
    state.write_text(json.dumps({"stage": "shipped"}), encoding="utf-8")
    assert fix_prompts.standing_refusal(tmp_path) == ""
    state.write_text("{", encoding="utf-8")
    assert fix_prompts.standing_refusal(tmp_path) == ""
    assert fix_prompts.standing_refusal(tmp_path / "nowhere") == ""


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


def test_a_prompt_says_when_the_failed_job_never_started_a_step():
    """carameli PR Gate run 37363944090: "Failing: see the run" and "no artifact came
    down" gave its fixer no hint that the one red job had never been given a runner."""
    sig = ("Backend unit + integration (never started a step)",)
    text = fix_prompts.pr_prompt(failure(evidence="", url="u/412", signature=sig))
    assert "Backend unit + integration (never started a step)" in text
    assert "No job that failed ever started a step, so there is no log" in text
    assert "a runner was never acquired" in text
    assert "annotations at u/412" in text
    assert "No artifact came down" not in text


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


def test_the_upstream_prompt_names_the_triage_log_at_the_path_it_is_placed():
    """ "in that directory's harness-triage.log" read as `logs/gate/`, which holds one
    directory per failure: both ledger sessions of the second supervised run opened the
    wrong path first. The log is placed under the failure's own evidence slot."""
    backlog = red_main(
        kind=fix_plan.LEDGER,
        workflow="harness ledger",
        signature=("agent-report devkit [a] x2",),
        # Built, not spelled: a `C:\` literal is one file name on the Linux gate.
        evidence=str(Path("ws") / ".worktrees" / "gate" / "devkit-ledger-main"),
    )
    text = fix_prompts.upstream_prompt((backlog, failure()), "agent/fix")
    assert "logs/gate/devkit-ledger-main/harness-triage.log" in text
    assert "that directory" not in text


def test_a_ledger_sweep_is_scoped_to_the_groups_it_was_sent_with():
    """0927-3 re-listed the ledger after its nine groups, took on one filed while it ran,
    and spent 18k output tokens and a 700-test run on it -- 14% of the round's largest
    session, for work the next pass would have sent a fresh session at anyway."""
    assert "filed after this session started" in fix_prompts.LEDGER_STEPS


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


def test_every_prompt_gives_the_spelling_that_files_a_line_settled_by_its_branch():
    """407df645, 6a3312bf, a8142f2a: a fixer that fixed its own friction left it open,
    and the next pass sent a second fixer at it. The spelling is what the pass matches."""
    spelling = "fixed on this branch"
    assert fix_reports.fixed_here(f"the evidence was rewritten; {spelling}")
    for text in every_prompt():
        assert spelling in text


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
        # 0929-6 ran mypy on its scripts only, "the linter" being unnamed, and #467 went
        # red on the test file it added: a second fixer for one annotation.
        assert "python scripts/lint-all.py over every file you changed, the tests" in text
        # 2026-10-02: sports_betting's fixer handed --paths only its `*.py` files, and
        # dotenv-linter's finding in the `.env.example` it had staged went red in CI: a
        # second fixer for one pair of quotes. --changed leaves nothing to choose.
        assert "python scripts/lint-all.py --changed" in text and "*.py" not in text
        # afd00d21: "so no file type is left out" read as every type being linted, and a
        # fixer found its CLAUDE.md and workflow edits skipped. The scope is whole; the
        # linters are not, and the prompt says which half it promises.
        assert "no file type is left out" not in text
        assert "no changed file is left out of its scope" in text
        assert "names the changed files it has none for" in text
        # A resolver took main's wording in a rule, ran the two ratchets it was named,
        # and left #398 13 tokens over the hot-tier ceiling: a third session to fix it.
        assert "python scripts/hot-budget.py" in text
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
        # b935e421: a fixer moving a class to the end of a file reached for `cat >>`.
        assert "An append is an Edit" in text
        # 79ce2440: a fixer found its nightly already green at the tip, changed nothing,
        # wrote no intent -- and the pass, seeing no outcome file, filed it as dead.
        assert "already fixed" in text and "with neither an intent nor" in text


def test_the_nothing_to_change_outcome_is_the_one_the_pass_sets_aside():
    """The prompt sends an already-fixed failure through the ship skill; that only holds
    if the skill says so and the pass reads an intent over a clean tree as done."""
    skill = " ".join((REPO_ROOT / ".claude/skills/ship/SKILL.md").read_text("utf-8").split())
    assert "a failure already fixed where the tree was cut" in skill
    assert "ship-intent.md" in fix_prompts.FINISH
    assert fix_prompts.INTENT_FILE in fix_reports.OUTCOME_FILES
    fixer = " ".join((REPO_ROOT / ".claude/fixer.md").read_text("utf-8").split())
    assert "already fixed" in fixer and "the ship skill" in fixer


def test_a_red_gate_prompt_names_the_readable_failures_and_that_the_gate_is_linux(tmp_path):
    """Two sweeps hand-parsed junit XML, and one shipped a Linux-only fix it could not
    check here and said nothing about it."""
    (tmp_path / "failures.txt").write_text("FAILED x\n", encoding="utf-8")
    with_evidence = fix_prompts.pr_prompt(failure(evidence=str(tmp_path)))
    assert "failures.txt" in with_evidence and "ran on Linux" in with_evidence
    assert "ran on Linux" in fix_prompts.pr_prompt(failure(evidence=""))
    assert ".venv interpreter" in fix_prompts.FINISH
    assert "--pr" in fix_prompts.LEDGER_STEPS and "any repository" in fix_prompts.LEDGER_STEPS


def test_who_made_a_reused_tree_reaches_every_pr_shape():
    """df43b14e: a fixer in an operator's elevated tree spent its turns working out whose
    it was; `fix_trees.provenance` says, and every shape a reused tree gets carries it."""
    made = " This tree was not cut for you: someone made it."
    shapes = (
        failure(),
        failure(signature=(fix_plan.CONFLICT,)),
        failure(kind=fix_plan.COMMIT, number=0, signature=("commit refused",)),
    )
    for red in shapes:
        assert made in fix_prompts.pr_prompt(red, made=made)
        assert "not cut for you" not in fix_prompts.pr_prompt(red)


def test_every_prompt_names_the_runner_that_adds_the_contract_tests():
    """54bb72df: #467's fixer ran the tests its files named and missed the one that
    reads every module; a second fixer was sent for it."""
    run_tests = load_script("scripts/run-tests.py")
    assert "tests/test_scheduled_jobs.py" in run_tests.CONTRACT_TESTS
    for text in every_prompt():
        assert "python scripts/run-tests.py with no arguments" in text
        assert "the contract tests that read every module" in text


def test_every_prompt_says_a_runner_that_runs_the_suite_bare_is_given_files():
    """27a0245d: carameli's own run-tests.py runs the whole suite in its app container
    when bare, so the runner sentence was true of devkit only; its fixer, told nothing
    else, ran all 2,331 vendored hook tests where one file was asked for."""
    for text in every_prompt():
        assert "where its --help says so" in text
        assert "given the test files for what you changed" in text
        assert "a whole test directory is the gate's to run" in text


def test_a_prompt_names_the_log_that_came_down_when_no_junit_report_failed(tmp_path):
    """8f622cc6: devkit's suite reports only in `test-failures.log`, and a fixer told to
    read `failures.txt` first went looking for a file nothing had written."""
    (tmp_path / "test-failures").mkdir()
    (tmp_path / "test-failures" / "test-failures.log").write_text("FAILED", encoding="utf-8")
    text = fix_prompts.pr_prompt(failure(evidence=str(tmp_path)))
    assert "test-failures/test-failures.log there first" in text
    assert "failures.txt (" not in text


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
