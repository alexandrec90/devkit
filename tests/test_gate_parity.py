"""What `.github/workflows/pr-gate.yml` has to hold, now that it is the only gate.

This file used to hold the gate to `scripts/precommit/run_push_gate.py`, the
`devkit-push-gate` hook that ran the PR gate's commands before every push, step for step
with a written reason for each difference. No hook runs it any more: the owner's decision
(2026-11-26) is that nothing gates a commit or a push on this machine, and every check
runs after the merge, in CI on the default branch, whose red the fix pass hands a fixer.
So the parity ledger went with the hook, and what is left is the gate's own shape.

Cheap by construction -- it parses files and launches nothing.
"""

from __future__ import annotations

import re

import pytest
from support import REPO_ROOT, checks_a_red_check_skips, load_script

gate = load_script("scripts/precommit/run_push_gate.py")
sync = load_script("scripts/sync-devkit.py")

PR_GATE = REPO_ROOT / ".github" / "workflows" / "pr-gate.yml"
TEMPLATE = REPO_ROOT / "templates" / "core" / "dot-github" / "workflows" / "pr-gate.yml.tmpl"
# A mustache control line: `{{#postgres}}` and its close, which only the generator reads.
CONTROL = re.compile(r"^\{\{[#^/]\w+\}\}\n", re.M)


def _yaml():
    # Same reason as `test_self_hosting.py`: PyYAML arrives with pre-commit in the dev
    # group rather than on its own, so a partially-provisioned checkout can be without it.
    return pytest.importorskip("yaml")


@pytest.mark.parametrize("workflow", [PR_GATE, TEMPLATE], ids=["devkit", "template"])
def test_a_pr_runs_the_gate_only_when_it_waits_for_one(workflow):
    """A direct project's PR merges as it opens and is checked by the run on the default
    branch after it, so a run on the PR was the same work twice, burning the account's
    concurrent jobs until the merge cancelled it. Every job carries
    `sync-devkit.PR_GATE_RUNS_IF`, and the trigger hears `labeled`: the rewrite `--pull`
    applies to a consumer's gate finds nothing left to do here."""
    text = CONTROL.sub("", workflow.read_text(encoding="utf-8"))
    assert sync.gate_pr_runs(text) == (text, []), workflow.name


def test_the_condition_lets_through_every_pr_the_queue_waits_on():
    """Each kind of PR `merge-dependabot-prs.py` will not merge without a gate is named
    in the condition, and nothing reads the default branch's runs as a PR's."""
    condition = sync.PR_GATE_RUNS_IF
    assert condition.startswith("${{ github.event_name != 'pull_request'")
    for needle in (
        "user.type == 'Bot'",  # Dependabot, before classify's label (raises no event)
        "head.repo.full_name != github.repository",  # a fork's, which the queue passes over
        "startsWith(github.head_ref, 'release/')",  # release-pipeline.py reads its red
        "labels.*.name, 'automerge'",  # waits for its gate, money PRs included
        "labels.*.name, 'hold'",  # waits for a person, who reads the gate
        "body, 'hold: yes'",  # merger.HOLD_LINE, the held ship intent's trailer
    ):
        assert needle in condition, needle
    assert sync.PR_GATE_EVENT_TYPES.endswith("labeled]")


def _run_steps() -> list[tuple[str, str, str]]:
    """Every `run:` step in the PR gate as `(job id, step key, the block's text)`."""
    parsed = _yaml().safe_load(PR_GATE.read_text(encoding="utf-8"))
    steps = []
    for job_id, job in parsed["jobs"].items():
        for step in job.get("steps", []):
            script = step.get("run")
            if script is None:
                continue
            first = next(line.strip() for line in script.splitlines() if line.strip())
            steps.append((job_id, step.get("name") or first, script))
    return steps


def test_the_workflow_scan_is_not_vacuous():
    """A parse that found nothing would pass every assertion in this file."""
    steps = _run_steps()
    assert len(steps) >= 5, f"only {len(steps)} run steps found in {PR_GATE.name}"
    assert {job for job, _, _ in steps} >= {
        "test",
        "pre-commit",
        "generated-project",
        "upgrade-rehearsal",
    }


def test_ci_runs_every_hook_over_the_whole_repo():
    """The formatters and fixers no longer run at commit time, so CI's `--all-files` run
    of the commit stage is where they are checked, and the tidy job is what repairs them
    after a merge (`tidy-main.py`)."""
    runs = [script for job, _, script in _run_steps() if job == "pre-commit"]
    assert any("pre-commit run --all-files" in script for script in runs), runs


def test_the_shards_run_every_test_file_exactly_once():
    """A file no shard names is a test CI never runs, with nothing red to say so."""
    parsed = _yaml().safe_load(PR_GATE.read_text(encoding="utf-8"))
    runs = {
        job: step["run"]
        for job in ("test", "suite")
        for step in parsed["jobs"][job]["steps"]
        if "run-tests.py" in str(step.get("run", ""))
    }
    generator = 'tests/test_new_project.py"'
    assert f'$0 == "{generator} || NR % 3 == 0' in runs["test"]
    assert f'$0 != "{generator} && NR % 3 == ${{{{ matrix.shard }}}}' in runs["suite"]
    assert parsed["jobs"]["suite"]["strategy"]["matrix"]["shard"] == [1, 2]
    nested = [p for p in (REPO_ROOT / "tests").rglob("test_*.py") if p.parent.name != "tests"]
    assert nested == [], "the shards list tests/test_*.py alone"


def test_lint_runs_before_the_test_tier_in_its_job():
    """Lint auto-fixes, so a test step after it in the same job sees what it repaired;
    before it, the later step reports clean on a tree the test never saw."""
    test_job = [key for job, key, _ in _run_steps() if job == "test"]
    lint = test_job.index("Lint + typecheck via the wrapper devkit ships")
    tier = "devkit tests (generator, and a third of the rest)"
    assert lint < test_job.index(tier), (
        f"{PR_GATE.name}'s test job runs {tier!r} before lint-all.py, which auto-fixes -- "
        f"whatever it repairs, the later step then reports clean. Job order: {test_job}"
    )


def test_ci_never_claims_a_release_prepare():
    """`run_push_gate.py` is still published, for the consumer configs generated while it
    was wired, and it sets `RELEASE_PREPARE_ENV` on a release prepare to excuse
    `test_fallback_devkit_ref_tracks_the_newest_tag`. CI must not: judging exactly that red
    is what `release-pipeline.py`'s `gate_verdict` is for, and a release PR that went
    green would sail past it."""
    assert gate.RELEASE_PREPARE_ENV not in PR_GATE.read_text(encoding="utf-8"), (
        f"{PR_GATE.name} sets {gate.RELEASE_PREPARE_ENV}, which would make the "
        "release PR green and defeat the expected-red check that gates every release"
    )


def test_a_red_vendored_suite_in_devkits_own_gate_uploads_what_failed():
    """245a2eb9: the hook-script step fails before `run-tests.py` ever writes
    `logs/test-failures.log`, so devkit's gate uploaded nothing for it, and #407's
    fixer read the failing ids out of `gh run view --log-failed`. The generated gate
    was fixed first; this is devkit's own. Each job that runs tests uploads the report its
    own runner writes, under a name no other job takes."""
    jobs = _yaml().safe_load(PR_GATE.read_text(encoding="utf-8"))["jobs"]
    hook_runs = [step.get("run", "") for step in jobs["hook-tests"]["steps"]]
    assert any(
        "pytest scripts/hooks/tests/ -q --junit-xml=logs/junit-hooks.xml" in run
        for run in hook_runs
    )
    uploads = {
        job: step["with"]
        for job in ("test", "suite", "hook-tests")
        for step in jobs[job]["steps"]
        if step.get("if") == "failure()" and step.get("name") == "Upload test artifact"
    }
    assert {job: upload["path"] for job, upload in uploads.items()} == {
        "test": "logs/test-failures.log",
        "suite": "logs/test-failures.log",
        "hook-tests": "logs/junit-hooks.xml",
    }
    names = [upload["name"] for upload in uploads.values()]
    assert len(set(names)) == len(names)
    assert all(name.startswith("test-failures") for name in names)


def test_a_red_check_does_not_hide_the_checks_after_it():
    """f07541c7: the hook-script step failed, the `run-tests.py` step after it was
    skipped, and the fixer was sent with `junit-hooks.xml` alone -- a third red
    (`test_install_tray`) surfaced only from a local run. f872ef7c: a red mypy skipped
    both test tiers, so main's log named 2 of its 4 reds. Every check runs whenever the
    environment did, so one gate cycle names every red -- in the nightly too, whose
    failures reach a fixer the same way."""
    for workflow in (PR_GATE, PR_GATE.with_name("nightly.yml")):
        parsed = _yaml().safe_load(workflow.read_text(encoding="utf-8"))
        assert not checks_a_red_check_skips(parsed), (
            f"{workflow.name}: a red check skips the one after it, so that check's "
            f"failures reach the fix pass a gate cycle late: {checks_a_red_check_skips(parsed)}"
        )


def test_the_gate_lints_with_the_ruff_a_generated_project_pins():
    """v0.11.100 was lint-clean under devkit's ruff and failed B008 under the one every
    consumer's pre-commit pins, in seven adoption PRs at once."""
    config = REPO_ROOT / "templates" / "core" / "dot-pre-commit-config.yaml.tmpl"
    pinned = re.search(r"ruff-pre-commit\n\s+rev: v(\S+)", config.read_text(encoding="utf-8"))
    assert pinned, "the template pins no ruff-pre-commit rev"
    steps = _yaml().safe_load(PR_GATE.read_text(encoding="utf-8"))["jobs"]["test"]["steps"]
    runs = [step.get("run") for step in steps]
    assert f"uvx ruff@{pinned[1]} check ." in runs


def test_the_skipped_check_check_is_not_vacuous():
    """Two tiers with no `if:` are what f07541c7 shipped, and a lint then a tier with no
    `if:` what f872ef7c did; the helper must name the second of each, and accept it once
    it runs after the first one's failure."""
    lint = {"name": "lint", "run": "uv run python scripts/lint-all.py"}
    hooks = {"name": "hooks", "run": "pytest scripts/hooks/tests/ -q"}
    suite = {"name": "suite", "run": "python scripts/run-tests.py"}
    assert checks_a_red_check_skips({"jobs": {"t": {"steps": [hooks, suite]}}}) == ["t / suite"]
    assert checks_a_red_check_skips({"jobs": {"t": {"steps": [lint, hooks]}}}) == ["t / hooks"]
    fixed = [
        {**hooks, "id": "hooks"},
        {**suite, "if": "success() || steps.hooks.outcome == 'failure'"},
    ]
    assert checks_a_red_check_skips({"jobs": {"t": {"steps": fixed}}}) == []
    after_any = "${{ !cancelled() && steps.env.outcome == 'success' }}"
    steps = [lint, {**hooks, "if": after_any}, {**suite, "if": after_any}]
    assert checks_a_red_check_skips({"jobs": {"t": {"steps": steps}}}) == []
    # One check, or both in one `run:` block, has nothing to skip.
    assert checks_a_red_check_skips({"jobs": {"t": {"steps": [suite]}}}) == []
