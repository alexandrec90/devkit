"""devkit must actually run the utilities it ships.

A harness that is only ever wired downstream is a harness nobody tests. Every check
here exists because the absence of the thing it asserts was, at some point, real:
devkit shipped hooks with no `.claude/settings.json` to fire them, a manifest describing
a different project, and a Stop hook invoking a lint runner this repo did not have.

These are contract tests, not style preferences — each one fails loudly if devkit drifts
back into shipping a utility it does not itself use.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import tomllib

import pytest
from support import (
    REPO_ROOT,
    TEMPLATES,
    devkit_project,
    gh_steps_without_repo_context,
    harness_config,
    load_script,
)

SETTINGS = REPO_ROOT / ".claude" / "settings.json"
TEMPLATE_SETTINGS = TEMPLATES / "core" / "dot-claude" / "settings.json.tmpl"

# Hook commands are written as `${CLAUDE_PROJECT_DIR:-.}/<path>`; this pulls the paths.
HOOK_PATH_RE = re.compile(r"\$\{CLAUDE_PROJECT_DIR:-\.\}/([^\"]+?\.(?:py|sh))")


def _settings() -> dict:
    """devkit's own settings, parsed strictly.

    Strictly on purpose: Claude Code does not accept comments in `settings.json`, and an
    unparseable file does not warn — it silently disables every hook in it, which is the
    exact failure this whole module exists to prevent.
    """
    return json.loads(SETTINGS.read_text(encoding="utf-8"))


def test_devkit_has_settings_wiring_its_own_hooks():
    assert SETTINGS.exists(), "devkit ships hook scripts but has nothing to fire them"
    hooks = _settings()["hooks"]
    # The events that carry devkit's own utilities. SessionStart provisions the venv,
    # PostToolUse auto-formats on edit, Stop runs pre-stop verification; without these
    # three the harness is inert no matter what else the file says.
    for event in ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"):
        assert event in hooks, f"{event} is not wired in devkit's own settings"


def test_every_hook_devkit_wires_actually_exists():
    """A hook command pointing at a missing script fires every turn and fails silently."""
    referenced = HOOK_PATH_RE.findall(SETTINGS.read_text(encoding="utf-8"))
    assert referenced, "no hook scripts referenced — the regex or the file shape changed"
    for rel in referenced:
        assert (REPO_ROOT / rel).exists(), f"{rel} is wired as a hook but does not exist"


def test_devkit_wires_every_hook_event_the_template_does():
    """devkit must not fall behind the settings it generates for other projects.

    The template is the specification of "what a project gets". If it grows a hook event,
    devkit should have it too — otherwise the repo authoring the harness is the one repo
    running an older version of it.
    """
    template_hooks = set(re.findall(r'"(\w+)": \[', TEMPLATE_SETTINGS.read_text(encoding="utf-8")))
    # `hooks` itself is not an event, and neither is the permissions `allow` list.
    template_events = {name for name in template_hooks if name not in {"hooks", "allow"}}
    missing = template_events - set(_settings()["hooks"])
    assert not missing, f"the template wires {sorted(missing)} but devkit does not"


# --- the lint runner / Stop hook contract -------------------------------------


def _lint_runner_help(cwd, script="scripts/lint-all.py") -> str:
    result = subprocess.run(
        [sys.executable, script, "--help"], cwd=cwd, capture_output=True, text=True
    )
    assert result.returncode == 0, f"{script} --help failed:\n{result.stderr}"
    return result.stdout


def test_lint_runner_accepts_every_flag_the_stop_hook_passes():
    """Tier 1 passes fixed flags; argparse rejecting one is a permanent lint failure.

    `stop.py` is vendored byte-identical and cannot introspect the lint runner, so the
    flags it sends are a contract. When they disagree, argparse exits 2 and the Stop hook
    reports a lint failure whose body is a usage message — on every single stop, with
    nothing in the source tree that can fix it. That is exactly what `--no-secrets` did.
    """
    stop = load_script("scripts/hooks/stop.py")
    spec = stop._command_for(stop.CHECK_LINT)
    assert spec is not None, "devkit has no lint runner for its own Stop hook to invoke"
    argv, _cwd, _artifact = spec
    help_text = _lint_runner_help(REPO_ROOT)
    for flag in [a for a in argv if a.startswith("--")]:
        assert flag in help_text, f"stop.py passes {flag} but scripts/lint-all.py rejects it"


def test_lint_runner_accepts_the_paths_flag_ship_probes_for():
    """`ship.py` is vendored byte-identical and hands the lint runner the branch diff.

    Without `--paths` the gate falls back to `--changed`, whose set is the working tree
    versus HEAD -- and ship refuses to run at all unless that is clean. So the fallback
    lints nothing and reports LINT PASSED for it, which is what shipped for as long as
    `--changed` was the only scope ship could ask for.
    """
    assert "--paths" in _lint_runner_help(REPO_ROOT)


def test_stop_hook_finds_devkits_own_lint_runner():
    stop = load_script("scripts/hooks/stop.py")
    assert stop.LINT_ALL.exists(), "the Stop hook's Tier 1 script is missing from devkit"


# --- the manifest describes devkit, not some other project --------------------


def test_manifest_paths_exist_in_this_repo():
    """`.devkit.toml` used to be a copy of carameli's, naming `app/` and a DB.

    Harmless as documentation, wrong as configuration: the hooks read it to decide which
    directories to lint and test, and devkit now runs those hooks on itself.
    """
    cfg = harness_config.load(REPO_ROOT)
    for label, path in (("app", cfg.app_dir), ("tests", cfg.tests_dir), ("unit", cfg.unit_tests)):
        assert (REPO_ROOT / path).is_dir(), f"[paths] {label} = {path!r} is not a directory here"


def test_manifest_declares_no_infra_devkit_does_not_have():
    cfg = harness_config.load(REPO_ROOT)
    assert not cfg.db.enabled, "devkit has no database or compose stack"
    assert not cfg.frontend.enabled, "devkit has no frontend"
    assert not (REPO_ROOT / "docker-compose.yml").exists()


def test_manifest_env_prefix_is_devkits_own():
    """A borrowed prefix silently shares control vars with another project's shell."""
    cfg = harness_config.load(REPO_ROOT)
    assert cfg.env_prefix == "DEVKIT", f"env_prefix is {cfg.env_prefix!r}"


# --- toolchain provisioning ----------------------------------------------------


def test_pyproject_lets_session_start_provision_devkit():
    """`session-start.sh` detects the dependency model from files on disk.

    Its chain is `uv.lock` -> `requirements-dev.txt` -> `pyproject.toml` -> warn and
    skip. devkit had none of the three, so every remote session hit the final branch and
    left an empty `.venv` with no ruff, mypy or pytest — while the script it ends by
    recommending, `scripts/lint-all.py`, needs all three.
    """
    assert (REPO_ROOT / "uv.lock").exists(), "no lock: provisioning is not reproducible"
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    # A virtual project: devkit is scripts and templates, not an installable
    # distribution, and `uv sync` fails outright trying to build a wheel without this.
    assert data["tool"]["uv"]["package"] is False
    dev = data["dependency-groups"]["dev"]
    for tool in ("ruff", "mypy", "pytest"):
        assert any(tool in spec for spec in dev), f"{tool} missing from the dev group"


def test_pytest_scope_keeps_the_vendored_tier_separate():
    """`scripts/hooks/tests/` ships into every project and runs as its own step."""
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    testpaths = data["tool"]["pytest"]["ini_options"]["testpaths"]
    assert testpaths == ["tests"], f"testpaths is {testpaths!r}"


def test_tests_reading_the_live_workspace_are_marked_to_skip_without_it():
    """The registry sits *beside* the checkout, so CI never has it.

    Five tests landed asserting against `alex-projects.code-workspace` by path, and
    the PR gate went red with a FileNotFoundError naming a directory no runner could
    have had. `support.needs_live_workspace` is the fix — it keeps the workstation
    coverage and skips where the file cannot exist. This keeps the sixth such test
    from being written without it.
    """
    registry = devkit_project.DEFAULT_WORKSPACE.name
    offenders = []
    for path in sorted((REPO_ROOT / "tests").glob("test_*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
                continue
            reads_it = any(
                (isinstance(n, ast.Name) and n.id == "LIVE_WORKSPACE")
                or (isinstance(n, ast.Constant) and n.value == registry)
                for n in ast.walk(node)
            )
            marked = any(
                isinstance(d, ast.Name) and d.id == "needs_live_workspace"
                for d in node.decorator_list
            )
            if reads_it and not marked:
                offenders.append(f"{path.name}::{node.name}")
    assert not offenders, f"these read {registry} without @needs_live_workspace: " + ", ".join(
        offenders
    )


# --- copies that must not drift -----------------------------------------------


def test_notify_scripts_are_byte_identical_to_the_template_copies():
    """devkit's task wrappers are copies, not forks — nothing renders them.

    They carry no placeholders, so there is no reason for the two to differ, and a silent
    divergence means the fix you made locally never reaches a generated project.
    """
    for name in ("notify.py", "notify-wrap.py"):
        ours = (REPO_ROOT / "scripts" / name).read_bytes()
        theirs = (TEMPLATES / "core" / "scripts" / name).read_bytes()
        assert ours == theirs, f"scripts/{name} has drifted from templates/core/scripts/{name}"


def test_setup_action_template_matches_devkits():
    """The composite action is a template again, so the two copies must not drift.

    It was vendored in v0.7.0 on the argument that its one variable — the Python
    version — had moved to the caller. Two consumers disproved that on the first pull
    that reached them: apt-finder's copy opened with a step cloning its private sibling
    `data-lake` (deleted by the pull, every job then dying on `Distribution not found`),
    and carameli installs pip-tools compiled locks with `uv pip install --system`, which
    `uv sync --all-extras --all-groups` cannot do — there is no `uv.lock` to sync from.

    What varies between projects is *how a project installs*, not which interpreter it
    installs. So this file went back to `templates/`, and is held to devkit's own copy
    the same way `notify.py` is: nothing renders either, so a divergence is a fix that
    silently never reaches a generated project.
    """
    ours = (REPO_ROOT / ".github" / "actions" / "setup-python-env" / "action.yml").read_bytes()
    theirs = (
        TEMPLATES / "core" / "dot-github" / "actions" / "setup-python-env" / "action.yml"
    ).read_bytes()
    assert ours == theirs, (
        ".github/actions/setup-python-env/action.yml has drifted from its templates/ copy"
    )


def test_the_setup_action_is_not_vendored():
    """The ratchet on the paragraph above.

    Re-adding the path to `MANIFEST` would silently replace apt-finder's and carameli's
    install steps again, and the symptom is a red gate in someone else's repository —
    never here. So the exclusion is asserted rather than left to the comment.
    """
    sync_devkit = load_script("scripts/sync-devkit.py")
    assert ".github/actions/setup-python-env/action.yml" not in sync_devkit.MANIFEST, (
        "the composite action is project-owned: it carries how a project installs, "
        "which differs per project. See test_setup_action_template_matches_devkits."
    )


# `vendored` near the composite action is allowed only as a denial; anything else is
# the v0.7.0 claim outliving the commit that stopped making it true. `un-vendored` and
# `vendoring` are the history, which stays sayable.
_UNNEGATED_VENDORED = re.compile(r"(?<!never )(?<!not )(?<!no longer )(?<!un-)vendored")
_NAMES_THE_ACTION = re.compile(r"setup-python-env")
# The action's own two copies are entirely about themselves, so every line is in scope.
_SETUP_ACTION_COPIES = (
    ".github/actions/setup-python-env/action.yml",
    "templates/core/dot-github/actions/setup-python-env/action.yml",
)
# Elsewhere the claim rode along beside a `uses:` line or a path in prose, and these
# files legitimately describe *other* things as vendored (the auto-merge workflow, the
# hook tier). So the window is the naming line plus what continues it.
_MENTIONS_THE_ACTION = (
    "templates/core/README.md.tmpl",
    "templates/core/dot-github/workflows/pr-gate.yml.tmpl",
    "templates/core/dot-github/workflows/nightly.yml.tmpl",
    ".github/workflows/pr-gate.yml",
    ".github/workflows/nightly.yml",
)
_WINDOW = 5


def _vendored_claims_about_the_action(rel: str) -> list[str]:
    lines = (REPO_ROOT / rel).read_text(encoding="utf-8").splitlines()
    in_scope = set(range(len(lines)))
    if rel in _MENTIONS_THE_ACTION:
        named = [i for i, line in enumerate(lines) if _NAMES_THE_ACTION.search(line)]
        in_scope = {j for i in named for j in range(i, min(i + _WINDOW, len(lines)))}
    return [
        f"{rel}:{i + 1}: {lines[i].strip()}"
        for i in sorted(in_scope)
        if _UNNEGATED_VENDORED.search(lines[i])
    ]


def test_nothing_still_calls_the_setup_action_vendored():
    """The tier moved in `4ef8866`; the prose describing it did not.

    Un-vendoring edited `MANIFEST` and added a `templates/` copy without changing a
    word of the file, so both copies, five call sites across the gate and nightly
    templates, and the README handed to every new project all went on saying the action
    is "vendored byte-identical from devkit" and that editing it "shows up as drift".
    Nothing was red — the claim is prose, and `test_the_setup_action_is_not_vendored`
    guards only the tier itself.

    It cost a review. A reader took the file at its word, concluded the hard-coded
    `uv sync --all-extras --all-groups` was drift-gated and therefore unfixable from
    inside a consumer's repo, and filed for a `sync-args` input — a seam for a problem
    the un-vendoring had already solved more completely, since what differed between
    the two projects that disproved vendoring was the *shape* of the install, not its
    flags. Prose that outlives its subject bills the next reader for the discovery.
    """
    claims = [
        c
        for rel in _SETUP_ACTION_COPIES + _MENTIONS_THE_ACTION
        for c in _vendored_claims_about_the_action(rel)
    ]
    assert not claims, (
        "these lines say the composite action is vendored; it is a `templates/` copy "
        "the project owns and may edit:\n  " + "\n  ".join(claims)
    )


def test_the_setup_action_says_which_tier_it_is_in():
    """The ratchet above passes on silence, and silence is how the drift started.

    Deleting the stale sentence satisfies it while leaving a reader no way to tell
    whether rewriting the install step is supported or reported as drift — which is the
    only question anyone opens this file to answer.
    """
    for rel in (*_SETUP_ACTION_COPIES, "templates/core/README.md.tmpl"):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "templates/" in text, (
            f"{rel} no longer says the composite action comes from `templates/`, so "
            "nothing tells a project it owns the file and may edit the install step"
        )


WORKFLOWS = REPO_ROOT / ".github" / "workflows"
TEMPLATE_WORKFLOWS = TEMPLATES / "core" / "dot-github" / "workflows"
ACTIONS = REPO_ROOT / ".github" / "actions"
TEMPLATE_ACTIONS = TEMPLATES / "core" / "dot-github" / "actions"
# `uses: owner/repo@ref`, with an optional leading `- `. Local composite actions
# (`./.github/actions/...`) carry no ref and are deliberately not matched.
USES_RE = re.compile(r"uses:\s+([\w.-]+/[\w.-]+)@(\S+)")


def _yaml():
    # PyYAML arrives with pre-commit in the dev group rather than on its own, so a
    # partially-provisioned checkout can be without it. Skipping beats a spurious red.
    return pytest.importorskip("yaml")


def test_devkit_runs_the_dependency_updates_it_ships():
    """The generator hands every project a Dependabot config; devkit must carry one too.

    Same reason as every other check in this file: a config that is only ever rendered
    downstream is a config nobody has run. devkit's ecosystems are the subset it has —
    no npm, no Dockerfile — but `uv` and the actions its own workflows pin must be there
    or its toolchain and CI pins go stale exactly as silently as the templates' would.
    """
    yaml = _yaml()
    config = REPO_ROOT / ".github" / "dependabot.yml"
    assert config.exists(), "devkit ships dependency updates but does not run them"
    parsed = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert parsed["version"] == 2
    ecosystems = {u["package-ecosystem"] for u in parsed["updates"]}
    assert ecosystems == {"uv", "github-actions"}, (
        f"devkit declares {sorted(ecosystems)}; it has a uv lock and workflows, and "
        f"nothing else Dependabot can read (no package.json, no Dockerfile)"
    )


# Entry points that need the project's tooling importable, and what each one needs.
# `lint-all.py` and `run-tests.py` are here precisely because they degrade a missing
# tool to a skip: run either unprovisioned and it reports success having checked
# nothing, which is worse than the hard failure `python -m pytest` gives.
RUNNER_REQUIREMENTS = {
    "python -m pytest": ("pytest",),
    "pytest": ("pytest",),
    "scripts/run-tests.py": ("pytest",),
    "scripts/lint-all.py": ("ruff", "mypy"),
}


def _unprovisioned_tools(run: str) -> set[str]:
    """Tools a `run:` block invokes without `uv run` in front of them."""
    needed: set[str] = set()
    for line in run.splitlines():
        for token, tools in RUNNER_REQUIREMENTS.items():
            pos = line.find(token)
            # `uv run` earlier on the same line means the venv supplies the tool.
            if pos != -1 and "uv run" not in line[:pos]:
                needed.update(tools)
    return needed


def test_every_workflow_job_provisions_the_tools_it_runs():
    """A job that runs pytest/ruff/mypy must install them, by venv or by pip.

    `release.yml` shipped calling `python -m pytest -q` after a bare
    `actions/setup-python`, with no `uv sync` and no `pip install` — so the release
    gate died on "No module named pytest" the first time anyone cut a tag with it.
    It failed closed, which is the good version of this bug; the bad version is the
    same mistake in front of `lint-all.py`, which skips linters it cannot import and
    would have reported a green gate having checked nothing.

    Note that `./.github/actions/setup-python-env` deliberately does NOT count as
    provisioning a *bare* invocation: it syncs into `.venv`, which only `uv run`
    reaches. Using the action and then calling `python -m pytest` directly is the
    same failure with an extra step.
    """
    yaml = _yaml()
    offenders: list[str] = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job_name, job in (parsed.get("jobs") or {}).items():
            steps = job.get("steps") or []
            installed = " ".join(
                s.get("run", "") for s in steps if "pip install" in (s.get("run") or "")
            )
            for step in steps:
                for tool in sorted(_unprovisioned_tools(step.get("run") or "")):
                    if tool not in installed:
                        offenders.append(
                            f"{path.name}::{job_name}::{step.get('name', '<unnamed>')} "
                            f"runs {tool} without `uv run` and without pip-installing it"
                        )
    assert not offenders, "\n".join(offenders)


def test_release_stages_the_tag_before_gates_and_publishes_it_afterward():
    """The fallback/tag invariant otherwise makes phase 2 block itself.

    The full suite contains a test comparing ``FALLBACK_DEVKIT_REF`` with the newest
    visible tag. After the phase-1 bump merges but before phase 2 publishes its tag,
    that test is necessarily red. The candidate therefore has to exist locally for
    verification, while the push must stay after every gate so a red commit is never
    released.
    """
    yaml = _yaml()
    parsed = yaml.safe_load((WORKFLOWS / "release.yml").read_text(encoding="utf-8"))
    steps = parsed["jobs"]["release"]["steps"]
    names = [step.get("name", "") for step in steps]

    staged = names.index("Stage the release tag locally")
    suite = names.index("Full suite")
    lint = names.index("Lint")
    published = names.index("Publish the release tag")
    assert staged < suite < lint < published

    assert "git tag" in steps[staged]["run"]
    assert "git push" not in steps[staged]["run"]
    assert "git push origin" in steps[published]["run"]


def test_a_refused_release_pr_does_not_fail_the_prepare_phase():
    """`gh pr create` is the last thing prepare does, and the one that can be vetoed.

    "Allow GitHub Actions to create and approve pull requests" is a repo setting that
    `permissions:` cannot override and a free-tier private repo may not be able to turn
    on. It was off here for v0.7.0: the run bumped FALLBACK_DEVKIT_REF, committed it and
    pushed `release/v0.7.0` — then died on the PR call and reported the whole release as
    failed, with the one command that would finish it nowhere in the output.

    So the call must be conditional, and its failure must leave the command behind in
    the step summary.
    """
    yaml = _yaml()
    parsed = yaml.safe_load((WORKFLOWS / "release.yml").read_text(encoding="utf-8"))
    steps = parsed["jobs"]["release"]["steps"]
    prepare = [s for s in steps if "gh pr create" in (s.get("run") or "")]
    assert len(prepare) == 1, "expected exactly one step opening the release PR"
    run = prepare[0]["run"]

    # Bare `gh pr create` on its own line aborts the job under `bash -e`.
    assert "if gh pr create" in run, "the PR call must be conditional, not fatal"
    assert "GITHUB_STEP_SUMMARY" in run, "a refused PR must leave its command in the summary"
    # Twice: the attempt, and the copy echoed into the summary for someone to paste.
    # A summary saying only "this failed" is what made the first one expensive.
    assert run.count("gh pr create") >= 2, "the summary must carry the command, not just the news"


# `gh pr` verbs that WRITE. `list` and `view` are deliberately absent: they read, and
# the default read-only token serves them.
GH_PR_WRITES = ("create", "edit", "merge", "comment", "review", "close", "reopen", "ready")


def test_workflow_jobs_declare_the_gh_scopes_they_use():
    """A job that writes to a PR needs `pull-requests: write`, or `gh` 403s at the end.

    `release.yml` declared only `contents: write` while its prepare phase ends in
    `gh pr create`. The release therefore ran the full suite, ran lint, bumped
    FALLBACK_DEVKIT_REF, committed it and pushed the branch — and then died on:

        pull request create failed: GraphQL: Resource not accessible by integration

    Everything expensive had already succeeded, so the failure looked like a release
    that had gone wrong when in fact only the last call had, and the branch was sitting
    on the remote waiting for a PR someone had to open by hand.

    Checked against the *effective* permissions — a job-level block overrides the
    workflow-level one — because that is what the token is actually minted from.
    """
    yaml = _yaml()
    offenders: list[str] = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
        top = parsed.get("permissions")
        for job_name, job in (parsed.get("jobs") or {}).items():
            effective = job.get("permissions", top)
            granted = effective.get("pull-requests") if isinstance(effective, dict) else effective
            for step in job.get("steps") or []:
                run = step.get("run") or ""
                if not any(f"gh pr {verb}" in run for verb in GH_PR_WRITES):
                    continue
                if granted != "write":
                    offenders.append(
                        f"{path.name}::{job_name}::{step.get('name', '<unnamed>')} writes to a "
                        f"PR with `gh pr` but its permissions grant pull-requests={granted!r}"
                    )
    assert not offenders, "\n".join(offenders)


def test_devkit_automerge_waits_on_devkits_own_ci_workflow():
    """`workflow_run` matches a workflow by title. A rename makes the merge job inert.

    Nothing goes red when it does: an unmatched `workflow_run` produces no run at all,
    so every Dependabot PR just sits open with an `automerge` label nobody acts on.
    """
    yaml = _yaml()
    automerge = WORKFLOWS / "dependabot-automerge.yml"
    assert automerge.exists(), "devkit has Dependabot but nothing to merge its PRs"
    parsed = yaml.safe_load(automerge.read_text(encoding="utf-8"))
    # PyYAML is YAML 1.1, where an unquoted `on` key parses as the boolean True.
    triggers = parsed.get("on", parsed.get(True))
    watched = triggers["workflow_run"]["workflows"]
    titles = {
        yaml.safe_load(p.read_text(encoding="utf-8"))["name"]
        for p in WORKFLOWS.glob("*.yml")
        if p != automerge
    }
    for name in watched:
        assert name in titles, (
            f"dependabot-automerge.yml waits on a workflow named {name!r}, "
            f"but devkit's workflows are {sorted(titles)}"
        )


def test_the_failure_reporter_watches_a_workflow_devkit_actually_has():
    """Same trap as the automerge title, with a quieter symptom.

    A renamed nightly still fails visibly in its own Actions tab, so nothing looks
    broken -- while the reporter that was meant to carry that failure into an issue
    simply never fires. devkit is the repo where this must hold first: it ships the file
    to everyone else, so a title mismatch here is one it would ship inert.
    """
    yaml = _yaml()
    reporter = WORKFLOWS / "scheduled-failure-issue.yml"
    assert reporter.exists(), "devkit does not run the failure reporter it vendors"
    parsed = yaml.safe_load(reporter.read_text(encoding="utf-8"))
    # PyYAML is YAML 1.1, where an unquoted `on` key parses as the boolean True.
    triggers = parsed.get("on", parsed.get(True))
    watched = triggers["workflow_run"]["workflows"]
    titles = {
        yaml.safe_load(p.read_text(encoding="utf-8"))["name"]
        for p in WORKFLOWS.glob("*.yml")
        if p != reporter
    }
    for name in watched:
        assert name in titles, (
            f"scheduled-failure-issue.yml waits on a workflow named {name!r}, "
            f"but devkit's workflows are {sorted(titles)}"
        )


def test_the_failure_reporter_can_write_the_issues_it_exists_to_write():
    """`issues: write` is the whole file. Without it every run dies on the first `gh`
    call -- and it dies in a workflow nobody watches, reporting a failure about a
    failure nobody was going to see either."""
    yaml = _yaml()
    parsed = yaml.safe_load((WORKFLOWS / "scheduled-failure-issue.yml").read_text(encoding="utf-8"))
    assert parsed["permissions"].get("issues") == "write", parsed["permissions"]


def test_devkit_automerge_tells_gh_which_repo_it_is_acting_on():
    """The classify job checks nothing out, so `gh` has no git remote to infer from.

    Without `GH_REPO` the very first `gh label create` dies on "fatal: not a git
    repository" — an error that points at git and not at the missing repo argument,
    and one that fires on every Dependabot PR rather than on anything about the bump.
    """
    yaml = _yaml()
    parsed = yaml.safe_load((WORKFLOWS / "dependabot-automerge.yml").read_text(encoding="utf-8"))
    offenders = gh_steps_without_repo_context(parsed)
    assert not offenders, f"these steps run `gh` with no repo to resolve: {offenders}"


def _concurrency_block(text: str) -> list[str]:
    """The `concurrency:` mapping's lines, comments and blanks dropped.

    Read as text rather than parsed: `pr-gate.yml.tmpl` carries `{{#postgres}}`
    control lines that are not valid YAML until the generator renders them, so a
    template and a real workflow can only be compared before parsing.
    """
    lines = text.splitlines()
    body = []
    for line in lines[lines.index("concurrency:") + 1 :]:
        if line and not line.startswith((" ", "\t")):
            break
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            body.append(stripped)
    return body


def test_devkit_gate_is_hardened_the_way_the_gate_it_ships_is():
    """The template's gate and devkit's own must not diverge on the basics.

    Same reason as every other check in this file. All three of these were absent
    here while being written into the gate every generated project gets: superseded
    runs kept burning minutes, and the workflow ran with whatever permissions the
    repo default happened to grant.
    """
    yaml = _yaml()
    parsed = yaml.safe_load((WORKFLOWS / "pr-gate.yml").read_text(encoding="utf-8"))
    assert parsed["permissions"] == {"contents": "read"}
    # PyYAML is YAML 1.1, where an unquoted `on` key parses as the boolean True.
    assert "workflow_dispatch" in parsed.get("on", parsed.get(True))

    ours = _concurrency_block((WORKFLOWS / "pr-gate.yml").read_text(encoding="utf-8"))
    theirs = _concurrency_block(
        (TEMPLATE_WORKFLOWS / "pr-gate.yml.tmpl").read_text(encoding="utf-8")
    )
    assert ours == theirs, "devkit's gate and the shipped gate disagree on concurrency"
    assert "github.ref" in ours[0], f"concurrency group is not per-ref: {ours[0]}"


def test_devkit_gate_is_titled_and_filed_the_way_every_consumer_expects():
    """Two conventions the vendored auto-merge workflow depends on, asserted here.

    `dependabot-automerge.yml` is vendored byte-identical, so it names the gate it
    waits on by a title it cannot parameterise: `PR Gate`. devkit titled its own
    workflow `CI`, which meant the file it ships to everyone else would have been
    inert in devkit alone — and inert quietly, since an unmatched `workflow_run`
    produces no run at all.

    The path is asserted for the same class of reason: `sync-devkit.py` bumps the
    drift job's `ref:` by rewriting `PR_GATE_FILE`, and a devkit whose own gate is
    somewhere else is a devkit that cannot rehearse its own release steps.
    """
    sync = load_script("scripts/sync-devkit.py")
    gate = REPO_ROOT / sync.PR_GATE_FILE
    assert gate.is_file(), f"devkit's own gate is not at {sync.PR_GATE_FILE}"
    assert _yaml().safe_load(gate.read_text(encoding="utf-8"))["name"] == "PR Gate"


def test_every_local_composite_action_referenced_by_a_workflow_exists():
    """`uses: ./path` resolves from the workspace, and a wrong path fails at run time.

    Not at parse time and not in any linter: the job simply errors on the runner with
    "Can't find 'action.yml'". Cheap to assert, and the same class of silent-until-CI
    breakage as a hook command pointing at a missing script.
    """
    referenced = set()
    for path in WORKFLOWS.glob("*.yml"):
        referenced |= set(re.findall(r"uses:\s+\./(\S+)", path.read_text(encoding="utf-8")))
    assert referenced, "no local composite actions referenced — the layout changed"
    for rel in referenced:
        assert (REPO_ROOT / rel / "action.yml").is_file(), f"{rel}/action.yml does not exist"


# The GitHub Actions files devkit vendors rather than renders, and why each qualifies.
#
# `setup-python-env/action.yml` was here and is not any more — see
# `test_setup_action_template_matches_devkits` for the two consumers that disproved
# "its one variable moved to the caller". Qualifying means *nothing* in the file varies
# per project, and how a project installs its dependencies always does.
VENDORED_CI_FILES = {
    ".github/workflows/dependabot-automerge.yml": "no branch filter, one fixed gate title",
    ".github/workflows/scheduled-failure-issue.yml": (
        "one fixed nightly title, and the assignee is read from `repository_owner` at "
        "run time rather than written down"
    ),
}


def test_the_shared_ci_files_are_vendored_and_not_also_rendered():
    """A file cannot be in both tiers: whichever runs last wins, silently.

    The generator renders `templates/` and *then* vendors the MANIFEST, so a path in
    both would take the vendored bytes on a new project and the rendered bytes on
    every `--pull` that predates the template's removal — two projects generated a
    week apart disagreeing, with nothing reporting it.

    These two moved out of `templates/` because a template is a one-shot copy:
    `--pull` never looks at it again, which is how carameli's auto-merge workflow came
    to be missing `issues: write`, `--force` on `gh label create`, and `GH_REPO` --
    three fixes made here after it was generated, none of which could ever reach it.
    """
    manifest = load_script("scripts/sync-devkit.py").MANIFEST
    for rel, why in VENDORED_CI_FILES.items():
        assert rel in manifest, f"{rel} is not vendored ({why})"
        assert (REPO_ROOT / rel).is_file(), f"{rel} is in the MANIFEST but not in devkit"
        stem = rel.split("/")[-1]
        leftover = sorted(p for p in (TEMPLATES / "core" / "dot-github").rglob(f"{stem}.tmpl"))
        assert not leftover, f"{rel} is vendored but still rendered from {leftover}"


def test_the_vendored_setup_action_still_installs_the_environment():
    """Vendoring only helps if the thing being vendored is the whole provisioning step.

    `uv sync --all-extras --all-groups` is the entire point of having one action: both
    flags, always, because each is a no-op in one of the two shapes it serves (devkit
    declares a PEP 735 group and no extras, a generated project the reverse).
    """
    body = (REPO_ROOT / ".github" / "actions" / "setup-python-env" / "action.yml").read_text(
        encoding="utf-8"
    )
    assert "run: uv sync --all-extras --all-groups" in body, body
    assert "using: composite" in body


def test_devkit_passes_its_python_version_to_the_action_it_vendors():
    """The action's default cannot be any project's version, devkit's included.

    Every generated gate passes `python-version` explicitly at each call site for that
    reason. devkit's own version happens to equal the default, which is exactly why
    leaving it implicit here would be wrong — the convention would be untested in the
    one repo that ships it, and the first consumer to move off 3.12 would find out by
    provisioning the wrong interpreter with nothing red.
    """
    yaml = _yaml()
    parsed = yaml.safe_load((WORKFLOWS / "pr-gate.yml").read_text(encoding="utf-8"))
    implicit = [
        f"{job_name} / {step.get('name', step.get('uses'))}"
        for job_name, job in parsed["jobs"].items()
        for step in job.get("steps") or []
        if step.get("uses") == "./.github/actions/setup-python-env"
        and not (step.get("with") or {}).get("python-version")
    ]
    assert not implicit, f"these steps rely on the vendored action's default: {implicit}"


def _action_pins(*globbed: list) -> dict[str, set[str]]:
    """Every `uses: owner/repo@ref` across the given paths, as action -> {refs}."""
    pins: dict[str, set[str]] = {}
    for path in (p for group in globbed for p in group):
        for action, ref in USES_RE.findall(path.read_text(encoding="utf-8")):
            pins.setdefault(action, set()).add(ref)
    return pins


def test_workflow_action_pins_match_the_templates():
    """Dependabot bumps devkit's workflows; it cannot see the ones under `templates/`.

    The github-actions ecosystem only scans `.github/workflows/`, so a bump lands here
    and the rendered PR gate keeps pinning the old major indefinitely — invisible,
    because every generated project's gate goes on passing. This is the reminder to
    carry the bump across; it is not a rule that the two must always agree, so if a
    template genuinely needs a different pin, record the reason and adjust this test.

    `.github/actions/` is in scope for the same reason it is out of Dependabot's:
    the ecosystem does not scan composite actions either, so `setup-python-env` sat a
    major behind the workflow calling it, in both trees at once. That one is now
    vendored rather than rendered, so a bump to devkit's copy reaches consumers on
    their next `--pull` — which shrinks what this test guards to the templates, and
    is the argument for moving more of the gate the same way.
    """
    ours = _action_pins(WORKFLOWS.glob("*.yml"), ACTIONS.glob("*/action.yml"))
    theirs = _action_pins(TEMPLATE_WORKFLOWS.glob("*.tmpl"))

    assert ours, "no pinned actions found in devkit's workflows — the regex or layout changed"
    for action, refs in theirs.items():
        if action not in ours:
            continue
        assert refs == ours[action], (
            f"{action} is pinned {sorted(ours[action])} in devkit's own workflows but "
            f"{sorted(refs)} in templates/ — carry the bump across"
        )


@pytest.mark.parametrize("tree", ["ours", "theirs"])
def test_each_action_is_pinned_to_one_ref_within_a_tree(tree):
    """A workflow and the composite action it calls must not disagree about a major.

    They are read by different jobs of the same gate, so nothing goes red when they
    split: one job installs Python with v7 and the next with v6, and the only symptom
    is whichever behaviour changed between them. Dependabot cannot close this on its
    own — it bumps the workflow and never sees `.github/actions/`.
    """
    pins = (
        _action_pins(WORKFLOWS.glob("*.yml"), ACTIONS.glob("*/action.yml"))
        if tree == "ours"
        else _action_pins(TEMPLATE_WORKFLOWS.glob("*.tmpl"))
    )
    assert pins, f"no pinned actions found in the {tree!r} tree — the layout changed"
    split = {action: sorted(refs) for action, refs in pins.items() if len(refs) > 1}
    assert not split, f"pinned to more than one ref in the same tree: {split}"


def _tasks_json() -> dict:
    """devkit's canonical copy of the SHARED task block.

    These four checks used to read `devkit/.vscode/tasks.json`, which no longer
    exists — devkit now owns zero project-level tasks, which is the only honest
    position for the repo that prescribes the rule. They are repointed here rather
    than deleted because the rules they enforce (a `detail` on everything, `process`
    type, one real token per picker branch, no `${input:typo}`) were never about
    devkit's two tasks; they are the conventions CLAUDE.md states for VS Code tasks
    generally, and the shared block is now where nearly all of them live.

    The move also fixes a check that had been asserting against the wrong file for as
    long as it existed: see `test_check_style_tasks_go_through_the_wrapper...` below.
    """
    raw = devkit_project.CANONICAL_TASKS.read_text(encoding="utf-8")
    return devkit_project.devkit_jsonc.loads(raw)


def test_vscode_tasks_are_valid_and_labelled():
    """Every task carries a `detail` — it is the only place a one-click action can state
    its blast radius, which is CLAUDE.md's rule for this file."""
    tasks = _tasks_json()["tasks"]
    assert tasks, "no tasks defined"
    for task in tasks:
        assert task.get("detail"), f"task {task['label']!r} has no detail"
        assert task.get("type") == "process", f"task {task['label']!r} is not type=process"


def test_devkit_ships_no_project_level_tasks():
    """devkit prescribes the bar for a project task; it must also clear it.

    Every action devkit needs is in the shared block and takes `--project`. Its own
    two tasks were the last holdouts and neither survived the bar: "Test: Harness Hook
    Tests" was generic (identical in every consumer, and duplicated a third time as
    carameli's `--target=hook-tests`), and "Format: Check Only" ran `ruff format
    --check` over files `lint-all.py` had just reformatted in place at
    `scripts/lint-all.py`'s auto-fix pass — it could only ever fail on files nothing
    had linted or edited, which is CI's job and CI already does it.
    """
    assert not (REPO_ROOT / ".vscode" / "tasks.json").exists(), (
        "devkit has re-grown a project-level tasks.json; the shared block in "
        f"{devkit_project.CANONICAL_TASKS.name} is where a task belongs"
    )


# Both of these carry an empty option, and both are exempt for the SAME real reason:
# they feed `devkit_project.py`, which strips empty arguments before exec
# (`plan_command`'s `[a for a in extra if a]`, pinned by
# `tests/test_devkit_project.py`), so the empty branch cannot reach argparse at all.
#
# That is worth distinguishing from the exemption this list replaced. `testSuite` was
# carameli's picker feeding `run-tests.py` DIRECTLY, and it survived only because that
# one script happens to use `parse_known_args`, which swallows a stray token instead of
# rejecting it — an accident of one script, not a safe pattern.
#
# `e2eMode` is the same picker's neighbour and had the same accidental exemption
# (`run-e2e.py` tests membership rather than parsing). Hoisting the task put the
# dispatcher in front of it, which is what turned the accident into the real reason.
#
# Listed rather than exempted silently so the deviation stays visible, and so nothing
# joins it on the weaker justification.
_EMPTY_OPTION_ALLOWED: frozenset[str] = frozenset({"testScope", "e2eMode"})


def test_every_picker_option_supplies_a_real_token():
    """CLAUDE.md's rule for this file: a `${input:...}` picker must produce one real
    token in every branch. An empty string does not vanish from the args array — it
    reaches argparse as a stray positional and the task fails. This is why sweep.py
    and new-project.py both carry a `--dry-run` that is redundant with their default.
    """
    for spec in _tasks_json().get("inputs", []):
        if spec.get("type") != "pickString" or spec["id"] in _EMPTY_OPTION_ALLOWED:
            continue
        for option in spec["options"]:
            # A bare string option is its own value.
            value = option if isinstance(option, str) else option["value"]
            assert value != "", f"input {spec['id']!r} has an empty option; give it a real flag"


def test_every_referenced_input_is_defined():
    """A `${input:typo}` is not an error in VS Code — it prompts for an *undefined*
    input and passes the literal through, so the task fails somewhere further down."""
    data = _tasks_json()
    defined = {spec["id"] for spec in data.get("inputs", [])}
    for task in data["tasks"]:
        for arg in task.get("args", []):
            for referenced in re.findall(r"\$\{input:([^}]+)\}", arg):
                assert referenced in defined, (
                    f"task {task['label']!r} references undefined input {referenced!r}"
                )


def _check_tasks() -> list[dict]:
    return [task for task in _tasks_json()["tasks"] if "--check" in task.get("args", [])]


def test_check_style_tasks_go_through_the_wrapper_that_preserves_exit_codes():
    """A `--check` task's whole output is its exit code, so the wrapper must pass it on.

    `sweep.py --check` prints the same table the read-only sweep does; the only thing
    distinguishing "nothing stranded" from "three checkouts need action" is 0 vs 1. Wrap
    it in anything that returns its own status and the task goes green over stranded
    work — a silent failure that looks exactly like success, which is the same shape as
    the Stop-hook dispatch bug this file exists to catch.
    """
    tasks = _check_tasks()
    # This assertion is why the file this test reads had to change. Its whole subject
    # is `sweep.py --check`, but it used to read devkit's own tasks.json, which has
    # never contained a sweep task — it was matching "Format: Check Only" and passing
    # on the strength of a task that had nothing to do with what it documents. Reading
    # the shared block is the first time it asserts against Ship: Check Workspace.
    assert tasks, "no --check task defined; this test is asserting against nothing"
    for task in tasks:
        assert task["args"][0] == "scripts/notify-wrap.py", (
            f"task {task['label']!r} runs --check without notify-wrap.py, "
            f"whose exit-code propagation is what makes the result readable"
        )


@pytest.mark.parametrize("code", [0, 1, 2])
def test_notify_wrap_propagates_the_wrapped_exit_code(monkeypatch, code):
    """The property the test above depends on, asserted rather than assumed.

    Covers sweep.py --check's full range: 0 clean, 1 needs action, 2 blocked. `notify`
    is stubbed because the real one raises a Windows toast per run, which a test suite
    has no business doing — the assertion is the return value, not the notification.
    """
    notify_wrap = load_script("scripts/notify-wrap.py")
    monkeypatch.setattr(notify_wrap, "notify", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        sys,
        "argv",
        ["notify-wrap.py", "T", "--", sys.executable, "-c", f"raise SystemExit({code})"],
    )
    assert notify_wrap.main() == code
