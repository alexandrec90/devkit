"""Free coupling between `.github/workflows/pr-gate.yml` and the gate that runs before a push.

`scripts/precommit/run_push_gate.py` exists so a failure is read from `logs/` a minute
after `git push` instead of from a workflow artifact ten minutes after it. That payoff is
proportional to how much of CI the hook actually reproduces — and nothing measured it.
`tests/test_run_push_gate.py::test_the_steps_are_the_gates_in_cis_order` asserts the three
steps against **hardcoded literals**; it never opens the workflow, so a fourth job in
`pr-gate.yml` would be a check that only ever runs in CI, silently.

The rule here is not "the two must be identical". They must not be: `generated-project`
renders five presets and runs each one's full suite, which is minutes of work nobody wants
on every push. The rule is that **every difference is written down**. A `run:` step is
either matched by a push-gate step or listed in `EXEMPT` with a reason, the same way
`.claude/rules/engineering.md` allows a suppressed lint finding only with the claim spelled
out beside it. Adding a CI step then costs one deliberate decision instead of zero.

Cheap by construction — it parses two files and launches nothing — so it runs in every
gate, and fails the moment the two drift in either direction.
"""

from __future__ import annotations

import pytest
from support import REPO_ROOT, load_script

gate = load_script("scripts/precommit/run_push_gate.py")

PR_GATE = REPO_ROOT / ".github" / "workflows" / "pr-gate.yml"

# Keyed by (job id, step key), where the step key is the step's `name` when it has one and
# the first line of its `run:` block when it does not. The value is why the push gate does
# not reproduce it — a sentence a reader can disagree with, not a shrug.
EXEMPT: dict[tuple[str, str], str] = {
    ("test", "Format"): (
        "The push gate cannot reproduce this one, and the workflow says why in its own "
        "comment: `lint-all.py` auto-fixes, so running it first repairs the drift and then "
        "reports clean. The local cover is a different mechanism — `.pre-commit-config.yaml`"
        "'s commit-stage `ruff-format` hook formats staged Python before it is ever "
        "committed. That leaves a real hole for a blob committed past the hook (`SKIP=`, a "
        "merge, a file the hook's `types_or` does not match), which CI catches and the push "
        "gate does not."
    ),
    ("test", "Lint"): (
        "`ruff check .` runs inside `scripts/lint-all.py`, which is push-gate step 1 — the "
        "workflow spells it a second time only so a lint failure is one step rather than "
        "buried in the wrapper's artifact."
    ),
    ("pre-commit", "Run every hook over the whole repo"): (
        "`--all-files` has no local equivalent: pre-commit's commit stage sees the staged "
        "diff, so actionlint, detect-secrets and check-yaml pass locally against a diff and "
        "can still fail in CI against the repo. Reproducing it would mean running every "
        "pinned third-party hook over the whole tree on each push."
    ),
    ("generated-project", "pip install ruff pytest uv mypy"): (
        "Provisioning for the generated-project job, not a check."
    ),
    ("generated-project", "Configure git identity"): (
        "Runner setup — the generator makes an initial commit and a bare runner has no "
        "identity. A workstation already has one."
    ),
    ("generated-project", "Generate a project of each shape and run its checks"): (
        "Deliberately CI-only, and the one exemption that is a cost decision rather than a "
        "mechanism gap: five presets rendered and each one's suite run is minutes, on a "
        "check that can only break when `templates/` or the MANIFEST changes. Note this "
        "block *names* the push gate's own wrappers — it runs them inside each generated "
        "tree, not against devkit — which is why the exemption is consulted before the "
        "coverage match below rather than after it."
    ),
}


# The mirror of EXEMPT, and it needs the same discipline for the opposite drift: a step
# the hook runs and CI does not is one the merge is not gated on, so a green push says
# more than the PR does. Keyed by `run_push_gate.Step.name`.
LOCAL_ONLY: dict[str, str] = {
    "posix rehearsal": (
        "The only step whose value is a function of the platform it runs on. It fakes the "
        "host's platform so a Windows workstation exercises the branches CI's "
        "`ubuntu-latest` runner takes; on that runner it would fake POSIX on POSIX, which "
        "is `run-tests.py` a second time under a different name. CI does not need it "
        "because CI *is* the thing being rehearsed -- a test this catches locally is a "
        "test the existing `devkit tests` step already fails on in CI, ten minutes later."
    ),
}


def _yaml():
    # Same reason as `test_self_hosting.py`: PyYAML arrives with pre-commit in the dev
    # group rather than on its own, so a partially-provisioned checkout can be without it.
    return pytest.importorskip("yaml")


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


def _signature(step) -> str:
    """The token that identifies a push-gate step inside a shell command.

    Derived from the step's own argv rather than written out again here, so a renamed
    wrapper cannot leave this file matching the old path. Every step happens to carry
    exactly one path-shaped argument, which `test_every_push_gate_step_has_one_signature`
    holds them to — a step with none would match nothing and a step with two would need a
    rule about which one counts.
    """
    paths = [arg for arg in step.argv if "/" in arg]
    return paths[0] if paths else ""


def test_every_push_gate_step_has_one_signature():
    """Guards the guard: the matching below is only as good as this derivation."""
    for step in gate.STEPS:
        paths = [arg for arg in step.argv if "/" in arg]
        assert len(paths) == 1, f"{step.name!r} carries {paths}; the signature is ambiguous"


def test_the_workflow_scan_is_not_vacuous():
    """A parse that found nothing would pass every assertion in this file."""
    steps = _run_steps()
    assert len(steps) >= 5, f"only {len(steps)} run steps found in {PR_GATE.name}"
    assert {job for job, _, _ in steps} >= {"test", "pre-commit", "generated-project"}


def test_every_ci_step_is_reproduced_locally_or_exempt_with_a_reason():
    """The check this file exists for. A new `run:` step in the PR gate is a check the
    push gate does not have until someone either adds it there or writes down why not."""
    signatures = [_signature(step) for step in gate.STEPS]
    orphans = []
    for job_id, key, script in _run_steps():
        if (job_id, key) in EXEMPT:
            continue
        if not any(signature in script for signature in signatures):
            orphans.append(f"{job_id} / {key}")
    assert not orphans, (
        "these PR-gate steps run only in CI, so a failure in them costs a round trip "
        "through GitHub: "
        + "; ".join(orphans)
        + f". Add the command to run_push_gate.STEPS, or add an entry to EXEMPT in "
        f"{__file__} saying why the push gate cannot or should not run it."
    )


def test_the_coverage_match_actually_matches_something():
    """The other half of the guard: if `_signature` broke, every step would look like an
    orphan — but every step is also listable in EXEMPT, so the test above could be made to
    pass by exempting the lot. This one fails if the gate stops covering anything."""
    shared = [step for step in gate.STEPS if step.name not in LOCAL_ONLY]
    signatures = [_signature(step) for step in shared]
    covered = [
        f"{job}/{key}"
        for job, key, script in _run_steps()
        if (job, key) not in EXEMPT and any(s in script for s in signatures)
    ]
    assert len(covered) == len(shared), (
        f"the push gate has {len(shared)} steps CI should also run, but only {covered} "
        f"in CI match them"
    )


def test_every_push_gate_step_is_a_check_ci_also_runs():
    """The reverse drift, which is quieter: a local-only step is one the merge is not
    actually gated on, so a green push says more than the PR does.

    `LOCAL_ONLY` is the escape hatch, and it is deliberately as expensive to use as
    `EXEMPT`: a sentence a reader can disagree with, not a shrug.
    """
    scripts = [script for job, key, script in _run_steps() if (job, key) not in EXEMPT]
    for step in gate.STEPS:
        if step.name in LOCAL_ONLY:
            continue
        signature = _signature(step)
        assert any(signature in script for script in scripts), (
            f"push-gate step {step.name!r} runs {signature}, which no non-exempt step of "
            f"{PR_GATE.name} runs — either CI dropped it, or the hook grew a local-only "
            f"check that needs an entry in LOCAL_ONLY in {__file__} saying why CI should "
            f"not run it too"
        )


def test_no_local_only_entry_outlives_its_step():
    """A stale entry silently re-exempts whatever step later takes that name."""
    names = {step.name for step in gate.STEPS}
    stale = sorted(name for name in LOCAL_ONLY if name not in names)
    assert not stale, f"LOCAL_ONLY names push-gate steps that no longer exist: {stale}"


def test_every_local_only_entry_carries_a_reason():
    for name, reason in LOCAL_ONLY.items():
        assert len(reason.split()) >= 8, f"{name} is local-only without saying why"


def test_a_local_only_step_is_not_also_in_ci():
    """The two ledgers must not disagree. An entry here for a step CI does run would be a
    claim nobody checks, and the reader would believe the wrong one of the two."""
    scripts = [script for job, key, script in _run_steps() if (job, key) not in EXEMPT]
    for step in gate.STEPS:
        if step.name not in LOCAL_ONLY:
            continue
        signature = _signature(step)
        assert not any(signature in script for script in scripts), (
            f"{step.name!r} is listed in LOCAL_ONLY but {PR_GATE.name} runs {signature} — "
            f"drop the entry"
        )


def test_no_exemption_outlives_the_step_it_excuses():
    """A stale entry silently re-exempts whatever step later takes that name."""
    present = {(job, key) for job, key, _ in _run_steps()}
    stale = sorted(f"{job} / {key}" for job, key in EXEMPT if (job, key) not in present)
    assert not stale, f"EXEMPT names steps {PR_GATE.name} no longer has: {'; '.join(stale)}"


def test_every_exemption_carries_a_reason():
    """The whole mechanism is the reason. An empty one is an undocumented difference with
    extra steps."""
    for key, reason in EXEMPT.items():
        assert len(reason.split()) >= 8, f"{key} is exempt without saying why"


def test_lint_runs_before_the_test_tiers_in_both_gates():
    """The one ordering both gates genuinely share, and the only one either depends on:
    lint auto-fixes, so anything that could rewrite a file has to come after it.

    Their *test* tiers are deliberately not compared. CI runs the vendored hook tests
    before devkit's own suite so a broken vendored tier still reports when devkit's suite
    is red; the push gate runs them the other way and stops at the first failure, which is
    what keeps the artifact under `logs/` the one the refusal points at.
    """
    names = [step.name for step in gate.STEPS]
    assert names[0] == "lint", f"push gate runs {names[0]!r} before lint; the order is {names}"

    test_job = [key for job, key, _ in _run_steps() if job == "test"]
    lint = test_job.index("Lint + typecheck via the wrapper devkit ships")
    for tier in ("Hook-script tests (vendored tier)", "devkit tests (generator, ports, renderer)"):
        assert lint < test_job.index(tier), (
            f"{PR_GATE.name}'s test job runs {tier!r} before lint-all.py, which auto-fixes — "
            f"whatever it repairs, the later step then reports clean. Job order: {test_job}"
        )
