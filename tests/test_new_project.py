"""Tests for the project generator.

The important ones are at the bottom: they render the whole template tree for
several feature combinations and then **parse every generated file** with a real
parser. A template that renders without raising can still emit a JSON file with a
trailing comma or a compose file with a dangling `volumes:` key, and that failure
would otherwise surface only when a human ran `docker compose up` in a brand-new
repo.
"""

import argparse
import itertools
import json
import os
import re
import sys
import tomllib
from pathlib import Path

import pytest
from support import (
    REPO_ROOT,
    TEMPLATES,
    devkit_ports,
    devkit_project,
    gh_steps_without_repo_context,
    harness_config,
    load_script,
    vendor_manifest,
)

new_project = load_script("scripts/new-project.py")
GeneratorError = new_project.GeneratorError


def _hoisted_task_labels() -> frozenset[str]:
    """Every label the shared workspace block already defines.

    DERIVED, not listed. A hand-maintained set is the same duplication this whole
    arrangement exists to remove: it went stale immediately last time (it still named
    "Test: Run Suite — free" long after the workspace task had been renamed to "Test:
    Run Suite", so the check was guarding a label nothing could emit). Reading
    devkit's canonical block instead means every task hoisted from now on becomes
    forbidden in the template automatically, with nothing to remember.
    """
    text = devkit_project.CANONICAL_TASKS.read_text(encoding="utf-8")
    block = devkit_project.devkit_jsonc.loads(text)
    return frozenset(task["label"] for task in block.get("tasks", []) if task.get("label"))


HOISTED_TASK_LABELS = _hoisted_task_labels()


def make_args(**overrides):
    """The argparse namespace `plan()` expects, with the CLI's own defaults."""
    base = {
        "name": "demo_project",
        "description": "A demo.",
        "display_name": "",
        "parent": "",
        "github_owner": "alexandrec90",
        "python_version": "3.12",
        "default_branch": "main",
        "devkit_ref": "v0.1.0",
        "db_url_scheme": "postgresql+psycopg",
        "src_layout": False,
        "preset": None,
        "worktree": True,
        "remote": True,
        "register": True,
        "dry_run": True,
        **{f: False for f in new_project.FEATURES},
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def registry():
    return devkit_ports.load(REPO_ROOT)


# --------------------------------------------------------------------------
# Naming and path conventions
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [("sports-betting", "sports_betting"), ("IBKR_Trader", "ibkr_trader"), ("a.b", "a_b")],
)
def test_slugify_package_produces_an_importable_name(name, expected):
    assert new_project.slugify_package(name) == expected


@pytest.mark.parametrize("name", ["-leading", "has space", "has/slash", "", "has:colon"])
def test_invalid_names_are_rejected(name):
    # The name becomes a directory, a Python package, AND COMPOSE_PROJECT_NAME.
    # Docker rejects some of these only at `up` time, long after generation.
    with pytest.raises(GeneratorError, match="invalid project name"):
        new_project.validate_name(name)


@pytest.mark.parametrize("name", ["sports_betting", "carameli", "ibkr-trader", "x1"])
def test_valid_names_are_accepted(name):
    assert new_project.validate_name(name) == name


@pytest.mark.parametrize(
    "source,expected",
    [
        ("dot-gitignore.tmpl", ".gitignore"),
        ("dot-claude/settings.json.tmpl", ".claude/settings.json"),
        ("dot-github/workflows/pr-gate.yml.tmpl", ".github/workflows/pr-gate.yml"),
        ("dot-github/dependabot.yml.tmpl", ".github/dependabot.yml"),
        ("scripts/notify.py", "scripts/notify.py"),
        ("Dockerfile.tmpl", "Dockerfile"),
    ],
)
def test_dot_and_tmpl_conventions_apply_to_every_path_segment(source, expected):
    # `dot-` exists because a literal `.gitignore` inside templates/ would take
    # effect on devkit's own repo and make git ignore the templates themselves.
    assert Path(new_project._destination_name(Path(source))).as_posix() == expected


def test_no_template_ships_a_literal_dotfile():
    # Guards the reason `dot-` exists — a regression here silently breaks devkit's
    # own repo rather than the generated one, which is much harder to notice.
    offenders = [
        p.relative_to(TEMPLATES).as_posix()
        for p in TEMPLATES.rglob("*")
        if any(part.startswith(".") for part in p.relative_to(TEMPLATES).parts)
    ]
    assert offenders == []


# --------------------------------------------------------------------------
# Feature selection
# --------------------------------------------------------------------------


def test_feature_directories_are_only_included_when_enabled():
    off = {f: False for f in new_project.FEATURES}
    core_only = {d for _, d in new_project.iter_template_files(off)}
    assert Path("docker-compose.yml") not in core_only
    assert Path("CLAUDE.md") in core_only

    with_docker = {d for _, d in new_project.iter_template_files({**off, "docker": True})}
    assert Path("docker-compose.yml") in with_docker


def test_archive_feature_brings_its_rule_file():
    off = {f: False for f in new_project.FEATURES}
    files = {d.as_posix() for _, d in new_project.iter_template_files({**off, "archive": True})}
    assert ".claude/rules/data-lake.md" in files


def test_archive_rule_renders_required_frontmatter(tmp_path):
    root = generate(tmp_path, {"archive": True})
    text = (root / ".claude" / "rules" / "data-lake.md").read_text(encoding="utf-8")
    assert text.startswith("---\ndescription: Data-lake storage")
    assert '\npaths:\n  - "demo_project/archive/**/*.py"\n---\n' in text


def test_alembic_implies_postgres_via_the_cli(tmp_path):
    result = new_project.main(
        ["demo_project", "--with-alembic", "--parent", str(tmp_path), "--no-remote"]
    )
    assert result == 0  # dry run


@pytest.mark.parametrize("preset", sorted(new_project.PRESETS))
def test_every_preset_names_only_real_features(preset):
    # A typo'd feature in a preset would silently do nothing — `setattr` on an
    # argparse namespace happily creates a new attribute no template ever reads.
    assert set(new_project.PRESETS[preset]) <= set(new_project.FEATURES)


@pytest.mark.parametrize("preset", sorted(new_project.PRESETS))
def test_every_preset_generates(tmp_path, preset):
    assert (
        new_project.main(
            ["demo_project", "--preset", preset, "--parent", str(tmp_path), "--no-remote"]
        )
        == 0
    )


def test_explicit_flags_add_to_a_preset_rather_than_replacing_it(tmp_path):
    args = make_args(parent=str(tmp_path), preset="data", redis=True)
    for feature in new_project.PRESETS["data"]:
        setattr(args, feature, True)
    the_plan = new_project.plan(args, registry())
    assert the_plan.context["archive"] is True  # from the preset
    assert the_plan.context["redis"] is True  # from the explicit flag


def test_worktree_env_only_offsets_services_the_project_uses():
    args = make_args(postgres=True, docker=True, app_service=True)
    the_plan = new_project.plan(args, registry())
    # No redis feature -> no REDIS_HOST_PORT to get wrong later.
    assert "REDIS_HOST_PORT" not in the_plan.worktree_env
    assert "DB_HOST_PORT" in the_plan.worktree_env
    assert the_plan.worktree_env["COMPOSE_PROJECT_NAME"] == "demo_project-b"


def test_worktree_gets_a_different_slot_than_the_primary():
    the_plan = new_project.plan(make_args(postgres=True), registry())
    assert the_plan.context["slot"] != the_plan.context["worktree_slot"]


def test_generating_into_a_non_empty_directory_is_refused(tmp_path):
    target = tmp_path / "demo_project"
    target.mkdir()
    (target / "existing.txt").write_text("keep me")
    with pytest.raises(GeneratorError, match="already exists and is not empty"):
        new_project.plan(make_args(parent=str(tmp_path)), registry())


def test_generating_into_an_empty_existing_directory_is_allowed(tmp_path):
    (tmp_path / "demo_project").mkdir()
    assert new_project.plan(make_args(parent=str(tmp_path)), registry())


# --------------------------------------------------------------------------
# The real check: render everything, then parse everything
# --------------------------------------------------------------------------

FEATURE_MATRIX = [
    pytest.param({}, id="bare"),
    pytest.param({"postgres": True, "app_service": True}, id="service"),
    pytest.param({"postgres": True, "redis": True, "app_service": True}, id="service+redis"),
    pytest.param({"postgres": True, "alembic": True, "app_service": True}, id="service+alembic"),
    pytest.param({"postgres": True, "archive": True}, id="data"),
    pytest.param({"frontend": True, "postgres": True, "app_service": True}, id="fullstack"),
    pytest.param(
        {f: True for f in new_project.FEATURES},
        id="everything",
    ),
]


def generate(tmp_path: Path, features: dict) -> Path:
    """Render the tree for one feature set into tmp_path and return the root."""
    args = make_args(parent=str(tmp_path), **features)
    if args.alembic:
        args.postgres = True
    if args.app_service or args.postgres or args.redis or args.frontend:
        args.docker = True
    the_plan = new_project.plan(args, registry())
    the_plan.root.mkdir(parents=True, exist_ok=True)
    new_project.render_tree(the_plan, dry_run=False)
    new_project.write_package(the_plan, dry_run=False)
    # Both tiers, because a real project is both. See `support.vendor_manifest`.
    vendor_manifest(the_plan.root)
    return the_plan.root


@pytest.mark.parametrize("features", FEATURE_MATRIX)
def test_every_feature_combination_renders(tmp_path, features):
    root = generate(tmp_path, features)
    assert (root / "CLAUDE.md").exists()
    assert (root / ".devkit.toml").exists()


@pytest.mark.parametrize("features", FEATURE_MATRIX)
def test_no_unrendered_template_tag_survives(tmp_path, features):
    # The failure this catches: an inline `{{#flag}}` or a typo'd `{{ var }}` that
    # ends up copied verbatim into a generated config file.
    root = generate(tmp_path, features)
    for path in root.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="ignore")
            leftovers = re.findall(r"\{\{[#^/]?\s*[A-Za-z_][A-Za-z0-9_]*\s*\}\}", text)
            assert leftovers == [], f"{path.relative_to(root)} kept {leftovers}"


@pytest.mark.parametrize("features", FEATURE_MATRIX)
def test_generated_env_example_has_no_stripped_section_scars(tmp_path, features):
    """A blank line *before* a `{{#flag}}` block survives the block being stripped.

    So the convention in `dot-env.example.tmpl` is that every optional section carries
    its own separating blank INSIDE its block — otherwise a project with docker,
    postgres and archive all off renders three blank lines in a row where they used
    to be. dotenv-linter reports that as ExtraBlankLine, and since `lint-all.py` now
    runs it, a regression here fails the project's gate rather than just looking
    untidy. Asserted directly so the reason survives without the linter installed.
    """
    body = (generate(tmp_path, features) / ".env.example").read_text(encoding="utf-8")
    lines = body.splitlines()
    doubles = [i + 1 for i, (a, b) in enumerate(itertools.pairwise(lines)) if not a and not b]
    assert not doubles, f"consecutive blank lines at {doubles} in:\n{body}"


@pytest.mark.parametrize("features", FEATURE_MATRIX)
def test_generated_toml_parses(tmp_path, features):
    root = generate(tmp_path, features)
    for name in (".devkit.toml", "pyproject.toml", "ruff.toml"):
        with (root / name).open("rb") as fh:
            tomllib.load(fh)


# Rules a generated project must not enforce. None of them can report a defect -- each
# enforces a preferred spelling of code that already behaves identically -- so a finding
# is guaranteed to cost a turn and catch nothing.
#
# E501 is the reason this test exists. Nobody ever selected it: `"E"` is a *family
# prefix* and line-too-long is one of its 19 members, so it rode in with the family and
# was then suppressed one directory at a time, in every project generated from the
# template, for years. Family prefixes are the mechanism, so the pin has to survive a
# new family being added here or an existing one growing a member in a `ruff` release.
COSMETIC_RULES = (
    "E101", "E401", "E501", "E701", "E702", "E703", "E731", "E741", "E742", "E743",
    "I001", "N801", "N802", "N803", "SIM108", "T201", "UP007", "UP035",
)  # fmt: skip


def _selects(selector: str, rule: str) -> bool:
    """Whether a ruff selector covers a rule code, the way ruff resolves it.

    Not a plain `startswith`. A selector is `<linter><digits>`, and the *linter* part
    must match exactly -- `S` is flake8-bandit and does not select `SIM108` from
    flake8-simplify, however much it looks like a prefix. Only the numeric part is
    matched as a prefix, which is what makes `E5` select `E501`.

    Verified against ruff directly: `--select S` reports nothing on a SIM108 violation,
    `--select SIM` reports it, and `--select E5` reports a long line.
    """
    head = "".join(c for c in selector if not c.isdigit())
    tail = selector[len(head) :]
    rule_head = "".join(c for c in rule if not c.isdigit())
    return head == rule_head and rule[len(rule_head) :].startswith(tail)


def enforced(config: dict, rule: str) -> bool:
    """True when `rule` would actually fire under a rendered `ruff.toml`.

    Checked by reachability rather than by looking the code up in `ignore`: a rule is
    equally disabled by dropping its family from `select`, and asserting on one spelling
    would pass a config that re-enabled the rule through the other.
    """
    lint = config.get("lint", {})
    selected = any(_selects(s, rule) for s in lint.get("select", []))
    ignored = any(_selects(s, rule) for s in lint.get("ignore", []))
    return selected and not ignored


@pytest.mark.parametrize("features", FEATURE_MATRIX)
def test_generated_projects_do_not_enforce_cosmetic_rules(tmp_path, features):
    root = generate(tmp_path, features)
    with (root / "ruff.toml").open("rb") as fh:
        config = tomllib.load(fh)
    live = [rule for rule in COSMETIC_RULES if enforced(config, rule)]
    assert not live, (
        f"{live} would be enforced in a new project. Lint is for correctness and "
        "security (.claude/rules/engineering.md); a style-only rule that blocks a commit "
        "costs a turn and catches nothing. Fix it in templates/core/ruff.toml.tmpl -- "
        "drop the family from `select` or add the code to `ignore`, not a per-file "
        "exemption."
    )


def test_the_cosmetic_rule_guard_can_actually_fail():
    """The guard is only worth having if it is not vacuous. A rule that is genuinely
    enforced must be reported -- otherwise a config change that re-enables the whole
    family would slip through as silently as E501 originally did."""
    assert enforced({"lint": {"select": ["E", "F"]}}, "E501")
    assert not enforced({"lint": {"select": ["E"], "ignore": ["E501"]}}, "E501")
    assert not enforced({"lint": {"select": ["E", "F"]}}, "I001")


def test_a_selector_does_not_leak_across_linters():
    """`S` is flake8-bandit and `SIM` is flake8-simplify, so `select = ["S"]` must not
    read as enabling SIM108 -- a naive `startswith` says it does, and that made this
    guard report a violation the config did not actually have."""
    assert not _selects("S", "SIM108")
    assert _selects("SIM", "SIM108")
    assert _selects("S", "S603")
    # Only the numeric part is a prefix match, which is what makes `E5` cover `E501`.
    assert _selects("E5", "E501")
    assert not _selects("E7", "E501")
    assert _selects("E", "E501")


@pytest.mark.parametrize("features", FEATURE_MATRIX)
def test_generated_harness_manifest_is_readable_by_harness_config(tmp_path, features):
    # The generated seam must load in the *actual* loader the vendored hooks use —
    # not merely be valid TOML. A schema mismatch here would silently degrade every
    # new project to neutral defaults, which looks like "the harness does nothing".
    root = generate(tmp_path, features)
    config = harness_config.load(root)
    assert config.env_prefix == "DEMO_PROJECT"
    assert config.db.enabled is bool(features.get("postgres"))
    # The frontend tier ships off even when the feature is on: the feature wires the
    # compose service but scaffolds no frontend/ tree, and an enabled tier whose
    # `src` prefix matches nothing is inert without ever saying so. The paths are
    # still rendered, ready for the project to flip once it has a frontend.
    assert config.frontend.enabled is False
    if features.get("frontend"):
        assert config.frontend.src == "frontend/src/"
        assert config.frontend.test_cmd == ("run", "test:run")


@pytest.mark.parametrize("features", FEATURE_MATRIX)
def test_generated_json_parses(tmp_path, features):
    root = generate(tmp_path, features)
    json.loads((root / ".claude" / "settings.json").read_text(encoding="utf-8"))
    # tasks.json is JSONC — strip the line comments the way VS Code does.
    tasks = (root / ".vscode" / "tasks.json").read_text(encoding="utf-8")
    stripped = "\n".join(line for line in tasks.splitlines() if not line.lstrip().startswith("//"))
    parsed = json.loads(stripped)
    assert parsed["version"] == "2.0.0"
    # NOT `assert parsed["tasks"]`. An empty task list is the template's intended
    # state now: every generic action lives once in the workspace and takes
    # `--project`, so a preset with no alembic tier legitimately emits none. The file
    # is still worth rendering for the policy comment that stops the next author
    # re-adding them. What replaces this assertion is the pair below — nothing
    # hoisted may come back, and the scripts the workspace calls must still exist.
    assert isinstance(parsed["tasks"], list)


@pytest.mark.parametrize("features", FEATURE_MATRIX)
def test_every_task_has_a_label_and_a_detail(tmp_path, features):
    # The convention CLAUDE.md states: `detail` is the second line of the quick-pick
    # and the only place a one-click action can declare its cost or blast radius.
    root = generate(tmp_path, features)
    tasks = (root / ".vscode" / "tasks.json").read_text(encoding="utf-8")
    stripped = "\n".join(line for line in tasks.splitlines() if not line.lstrip().startswith("//"))
    for task in json.loads(stripped)["tasks"]:
        assert task.get("label"), task
        assert task.get("detail"), f"{task['label']} has no detail"
        assert re.match(r"^[A-Z][A-Za-z]*: [A-Z]", task["label"]), (
            f"{task['label']} breaks the 'Domain: Title Case Action' convention"
        )


@pytest.mark.parametrize("features", FEATURE_MATRIX)
def test_every_task_input_referenced_is_defined(tmp_path, features):
    # A `${input:foo}` with no matching entry makes VS Code fail the task at click
    # time with an opaque error — and only for the feature combination that emits it.
    root = generate(tmp_path, features)
    text = (root / ".vscode" / "tasks.json").read_text(encoding="utf-8")
    stripped = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//"))
    parsed = json.loads(stripped)
    defined = {i["id"] for i in parsed.get("inputs", [])}
    referenced = set(re.findall(r"\$\{input:([A-Za-z_][A-Za-z0-9_]*)\}", json.dumps(parsed)))
    assert referenced <= defined, f"undefined inputs: {referenced - defined}"


@pytest.mark.parametrize("features", FEATURE_MATRIX)
def test_generated_projects_do_not_ship_the_generic_tasks(tmp_path, features):
    """Test/lint tasks are defined once at workspace level and take --project.

    Shipping copies here is what produced the drift this template was changed to
    stop — the same task existed at user, workspace and project level with different
    defaults in each. A regression would recreate it on every new project.
    """
    root = generate(tmp_path, features)
    text = (root / ".vscode" / "tasks.json").read_text(encoding="utf-8")
    stripped = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//"))
    labels = {task["label"] for task in json.loads(stripped)["tasks"]}
    assert not (labels & HOISTED_TASK_LABELS), (
        f"generic tasks are back in the template: {labels & HOISTED_TASK_LABELS}"
    )


@pytest.mark.parametrize("features", FEATURE_MATRIX)
def test_generated_projects_ship_no_tasks_at_all(tmp_path, features):
    """The template is empty now, for EVERY preset — including alembic ones.

    Stronger than the label check above, and it has to be: that one only catches a task
    whose label collides with a hoisted one, so a project-level task under a NEW name
    would pass it. "DB: New Migration" was the last entry here and went up once carameli
    and ibkr_trader grew their `scripts/db-revision.py` entrypoints.

    The reason to keep this file empty rather than merely tidy is that a task defined in
    a repo is rendered once per WORKTREE, and every project gets a parallel `-b` checkout
    — so one entry here becomes two indistinguishable quick-pick rows the moment the
    worktree exists. A generated project that needs a one-click migration joins
    `DB_PROJECTS` and the `dbCheckout` picker instead.
    """
    root = generate(tmp_path, features)
    text = (root / ".vscode" / "tasks.json").read_text(encoding="utf-8")
    stripped = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//"))
    parsed = json.loads(stripped)
    assert parsed["tasks"] == [], f"the template grew a task back: {parsed['tasks']}"
    assert parsed["inputs"] == [], f"the template grew an input back: {parsed['inputs']}"


@pytest.mark.parametrize("features", FEATURE_MATRIX)
def test_generated_projects_still_satisfy_the_shared_task_contract(tmp_path, features):
    """The other half: the workspace tasks call these paths, so they must exist.

    Dropping the task shims is only safe while the scripts behind them stay put —
    `devkit_project.ACTIONS` addresses them by path with the checkout as cwd.

    A project gets each one through one of devkit's two channels: rendered from
    `templates/`, or vendored via `sync-devkit.py`'s MANIFEST. `generate()` only
    renders, so a template-only check would fail on the vendored ones and a
    MANIFEST-only check would fail on the rendered ones — the contract is the union.

    Project-SCOPED actions (`Action.projects`) are exempt, and that exemption is the
    point of the field. They are the tasks hoisted out of carameli's and ibkr_trader's
    own `.vscode/tasks.json` — a Playwright suite, an IBKR backtest — which a freshly
    generated project has no business shipping a script for. Without the exemption,
    consolidating those tasks would have made the generator's own contract
    unsatisfiable by every preset it can emit.
    """
    root = generate(tmp_path, features)
    manifest = set(load_script("scripts/sync-devkit.py").MANIFEST)
    for name, action in devkit_project.ACTIONS.items():
        if action.owner != devkit_project.PROJECT or action.projects:
            continue
        assert (root / action.script).is_file() or action.script in manifest, (
            f"action {name!r} wants {action.script}, which a generated project gets "
            "from neither templates/ nor the vendoring MANIFEST"
        )


def test_the_generator_writes_no_per_project_workspace_file(tmp_path):
    """It registers in the shared workspace instead.

    A per-project `.code-workspace` was invisible to `sweep.py`, which reads only
    `alex-projects.code-workspace` — so a scaffolded project could strand work with
    nothing reporting it.
    """
    generate(tmp_path, {})
    assert not list(tmp_path.glob("**/*.code-workspace"))


def test_registration_is_a_warning_not_a_failure(tmp_path, capsys, monkeypatch):
    """The project is already written by then; a broken registry is fixable by hand."""
    args = make_args(parent=str(tmp_path))
    the_plan = new_project.plan(args, registry())
    broken = tmp_path / "broken.code-workspace"
    broken.write_text('{"tasks": {}}', encoding="utf-8")
    monkeypatch.setattr(devkit_project, "DEFAULT_WORKSPACE", broken)
    new_project.register_in_workspace(the_plan, dry_run=False)
    assert "WARNING" in capsys.readouterr().out


def _recording_register(monkeypatch) -> list[list[str]]:
    """Record what `register_in_workspace` would write, without a workspace file."""
    calls: list[list[str]] = []

    def fake(text: str, names: list[str]) -> str:
        calls.append(names)
        return text

    monkeypatch.setattr(devkit_project, "register", fake)
    return calls


def test_no_register_never_opens_the_workspace_file(tmp_path, capsys, monkeypatch):
    """The RELEASING.md acceptance test renders a probe it then deletes.

    Registering that probe edits the *real* workspace file — `folders` plus every
    scope picker — and the edit outlives the directory, so `sweep.py` inherits a
    registered checkout under a temp path that no longer exists.
    """
    args = make_args(parent=str(tmp_path), register=False)
    the_plan = new_project.plan(args, registry())
    absent = tmp_path / "nowhere" / "registry.code-workspace"
    monkeypatch.setattr(devkit_project, "DEFAULT_WORKSPACE", absent)
    calls = _recording_register(monkeypatch)

    new_project.register_in_workspace(the_plan, dry_run=False)

    assert calls == []
    assert not absent.exists()
    out = capsys.readouterr().out
    assert "--no-register" in out
    # The discriminator: registering against a path that cannot be read warns. A
    # silent run therefore proves the file was never opened, not merely unchanged.
    assert "WARNING" not in out


def test_registration_is_still_the_default(tmp_path, monkeypatch):
    """The flag is opt-out: a real project must stay visible to `sweep.py`."""
    args = make_args(parent=str(tmp_path))
    the_plan = new_project.plan(args, registry())
    workspace = tmp_path / "registry.code-workspace"
    workspace.write_text('{"folders": []}', encoding="utf-8")
    monkeypatch.setattr(devkit_project, "DEFAULT_WORKSPACE", workspace)
    calls = _recording_register(monkeypatch)

    new_project.register_in_workspace(the_plan, dry_run=False)

    assert calls == [[the_plan.name, the_plan.worktree]]


def test_the_no_register_flag_is_reachable_from_the_cli(tmp_path, capsys, monkeypatch):
    """RELEASING.md's acceptance test passes it; a renamed dest breaks that silently."""
    monkeypatch.setattr(
        devkit_project, "DEFAULT_WORKSPACE", tmp_path / "nowhere" / "ws.code-workspace"
    )
    argv = ["probe_tag", "--preset", "bare", "--parent", str(tmp_path), "--devkit-ref", "v0.1.0"]

    assert new_project.main([*argv, "--no-remote", "--no-worktree", "--no-register"]) == 0
    assert "--no-register" in capsys.readouterr().out

    assert new_project.main([*argv, "--no-remote", "--no-worktree"]) == 0
    assert "--no-register" not in capsys.readouterr().out


@pytest.mark.parametrize("features", FEATURE_MATRIX)
def test_the_generated_diagnostic_scripts_actually_run_and_pass(tmp_path, features):
    """`lint-all.py` and `run-tests.py` must be green in a fresh project.

    These are what `tasks.json` and the PR gate invoke, so a failure here is a red
    gate on a project's first PR. Asserting `ruff`/`pytest` pass directly is not
    enough — it misses everything about how these wrappers *call* them. It did:
    `lint-all.py` ran `mypy .`, which type-checked the vendored harness (upstream
    code this repo may not edit) and reported 7 unfixable errors.
    """
    import subprocess

    root = generate(tmp_path, features)
    (root / "logs").mkdir(exist_ok=True)
    for script in ("scripts/run-tests.py", "scripts/lint-all.py"):
        result = subprocess.run([sys.executable, script], cwd=root, capture_output=True, text=True)
        artifact = {
            "scripts/run-tests.py": root / "logs" / "test-failures.log",
            "scripts/lint-all.py": root / "logs" / "lint-errors.log",
        }[script]
        detail = artifact.read_text(encoding="utf-8") if artifact.exists() else result.stdout
        assert result.returncode == 0, f"{script} failed in a fresh project:\n{detail}"


def test_a_linter_that_is_not_installed_is_skipped_not_reported(tmp_path):
    """An absent linter must never reach `logs/lint-errors.log`.

    `run_tool` promised this with `except FileNotFoundError`, which cannot fire for
    the form every linter actually uses: `[sys.executable, "-m", tool]` runs an
    interpreter that always exists, so a missing module is a plain exit 1 with
    "No module named mypy" on stderr. That text became an artifact finding with a
    `# fix:` hint no source edit could satisfy — and it turned CI red on a runner
    that installed ruff and pytest but not mypy.
    """
    import importlib.util

    root = generate(tmp_path, {})
    (root / "logs").mkdir(exist_ok=True)
    spec = importlib.util.spec_from_file_location(
        "generated_lint_all", root / "scripts" / "lint-all.py"
    )
    lint_all = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lint_all)

    absent = [sys.executable, "-m", "definitely_not_an_installed_linter", "."]
    assert lint_all.run_tool("mypy", absent, "hint") == ""
    # A tool that *is* importable still gets run, and a bare executable still takes
    # the FileNotFoundError path rather than being pre-emptively skipped.
    assert not lint_all._missing_module([sys.executable, "-m", "json", "."])
    assert not lint_all._missing_module(["ruff", "check", "."])


def test_generated_lint_runner_covers_the_workflows_and_env_file_it_ships(tmp_path):
    """Every generated project gets two workflows and a `.env.example` — and, until
    now, no linter that ever looked at them, while `session-start.sh` dutifully
    installed actionlint and dotenv-linter on every single session.

    Asserted through the rendered runner's own selectors so this stays true of the
    project's copy, not just devkit's.
    """
    import importlib.util

    root = generate(tmp_path, {"docker": True})
    spec = importlib.util.spec_from_file_location(
        "probe_lint_all", root / "scripts" / "lint-all.py"
    )
    generated = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generated)

    workflows = generated.workflow_files()
    assert ".github/workflows/pr-gate.yml" in workflows
    assert ".github/workflows/dependabot-automerge.yml" in workflows
    assert generated.env_files() == [".env.example"]
    # The gate must install what the runner calls, or the pass reports "not
    # installed — skipped" on every run and the check is inert.
    gate = (root / ".github" / "workflows" / "pr-gate.yml").read_text(encoding="utf-8")
    assert "download-actionlint" in gate
    assert "dotenv-linter" in gate


def test_lint_all_does_not_rewrite_the_vendored_harness(tmp_path):
    """`lint-all.py`'s auto-fix passes must leave `scripts/hooks/` byte-identical.

    `sync-devkit.py --check` fails the build when a vendored file differs from
    devkit's copy, so a formatter that reflows one turns the *next* CI step red with
    a diff no source edit can resolve. This is not hypothetical: the harness is
    lint-clean only because `scripts/**` ignores E501, and `ruff format` ignores
    per-file-ignores — it reflowed a 104-column line in `harness_config.py`.
    """
    import subprocess

    # `generate()` renders templates only; the harness is vendored by a separate
    # step, and it is precisely the vendored files this test is about.
    args = make_args(parent=str(tmp_path))
    the_plan = new_project.plan(args, registry())
    the_plan.root.mkdir(parents=True, exist_ok=True)
    new_project.render_tree(the_plan, dry_run=False)
    new_project.write_package(the_plan, dry_run=False)
    new_project.vendor_harness(the_plan, dry_run=False)
    root = the_plan.root
    (root / "logs").mkdir(exist_ok=True)

    # Drive this off the MANIFEST rather than a hardcoded directory, so a file added
    # to the vendored set is covered here the day it is added — `task_branch.py`
    # lives outside `scripts/hooks/` and was missed by exactly that assumption.
    manifest = new_project._read_manifest_paths(REPO_ROOT)
    vendored = [root / rel for rel in manifest if rel.endswith(".py")]
    present = [p for p in vendored if p.exists()]
    assert present, "no vendored harness files to check"
    before = {p: p.read_bytes() for p in present}

    subprocess.run([sys.executable, "scripts/lint-all.py"], cwd=root, capture_output=True)

    changed = [str(p.relative_to(root)) for p in present if p.read_bytes() != before[p]]
    assert not changed, f"lint-all rewrote vendored harness files: {changed}"


def test_a_generated_project_satisfies_the_vendored_ci_contract(tmp_path):
    """The rendered CI surface must pass the check that is vendored alongside it.

    `test_ci_workflow_contract.py` requires five files, and only two of them arrive
    vendored. The other three — `pr-gate.yml`, `dependabot.yml` and `nightly.yml` — are
    rendered from
    `templates/`, so the requirement and the thing that satisfies it live in different
    tiers and are edited by different changes. A template that stops emitting one, or
    emits it without a `schedule:` or without `cancel-in-progress: false`, hands the
    new owner a repo that fails its own first CI run.

    Run as a subprocess, in the generated tree: the module resolves everything off its
    own `conftest.REPO_ROOT`, so importing it here would test devkit against devkit and
    pass for the wrong reason.
    """
    import subprocess

    args = make_args(parent=str(tmp_path))
    the_plan = new_project.plan(args, registry())
    the_plan.root.mkdir(parents=True, exist_ok=True)
    new_project.render_tree(the_plan, dry_run=False)
    new_project.write_package(the_plan, dry_run=False)
    new_project.vendor_harness(the_plan, dry_run=False)

    contract = "scripts/hooks/tests/test_ci_workflow_contract.py"
    assert (the_plan.root / contract).exists(), f"{contract} was not vendored"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", contract, "-q"],
        cwd=the_plan.root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_generated_python_is_already_ruff_format_clean(tmp_path):
    """A new project's first `pre-commit run` must not rewrite its own files.

    devkit excludes `templates/` from its own format check (that Python is content, linted
    by the ruff.toml that ships beside it), which is right — and it meant
    `lint-all.py.tmpl` sat unformatted for the 100-column config it ships with. Nothing
    noticed until a generated project gained a `ruff-format` pre-commit hook, which
    reformatted the file on arrival: a brand-new repo, a failing hook, a dirty tree.
    """
    import subprocess

    root = generate(tmp_path, {})
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "format", "--check", "."],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if result.returncode == 1 and "No module named" in result.stderr:
        pytest.skip("ruff not importable")
    assert result.returncode == 0, f"generated files are not format-clean:\n{result.stdout}"


def test_generated_codex_skills_are_excluded_from_explicit_ruff_checks(tmp_path):
    """Pre-commit must lint skill sources once, not their generated Codex copy."""
    import subprocess

    root = generate(tmp_path, {})
    mirror = root / ".agents" / "skills" / "retro" / "extract.py"
    mirror.parent.mkdir(parents=True)
    mirror.write_text("print('generated mirror')\n", encoding="utf-8", newline="\n")
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", ".agents/skills/retro/extract.py"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if result.returncode == 1 and "No module named" in result.stderr:
        pytest.skip("ruff not importable")
    assert result.returncode == 0, (
        "generated Codex skill copy was explicitly linted instead of excluded:\n"
        f"{result.stdout}{result.stderr}"
    )


def test_generated_text_files_end_with_exactly_one_newline(tmp_path):
    """`end-of-file-fixer` must have nothing to do in a freshly generated repo.

    Stripping a trailing feature section leaves the blank line that preceded it, so
    `CLAUDE.md.tmpl` (which ends with `{{/archive}}`) rendered with a trailing blank in
    every non-archive project.
    """
    root = generate(tmp_path, {})
    offenders = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or ".git/" in str(path):
            continue
        body = path.read_bytes()
        if not body.strip():
            continue
        if not body.endswith(b"\n") or body.endswith(b"\n\n"):
            offenders.append(str(path.relative_to(root)))
    assert not offenders, f"files a pre-commit run would rewrite on arrival: {offenders}"


@pytest.mark.parametrize("features", [{}, {"postgres": True}, {"frontend": True, "redis": True}])
def test_generated_manifest_paths_all_exist(tmp_path, features):
    """Every directory the generated `.devkit.toml` declares must be there.

    `unit_tests` was `tests/unit`, which the generator never creates. That is the target
    `stop.py` runs for the whole-suite pass when application code changes, and
    `pytest tests/unit` on a missing path exits 4 — so the first app-code edit in every
    generated project blocked its stop with a bogus test failure. Found by the
    `devkit-manifest` pre-commit hook on a freshly generated repo, which is exactly
    the class of arrival bug that hook exists for.
    """
    root = generate(tmp_path, features)
    config = harness_config.load(root)
    for label, rel in (
        ("app", config.app_dir),
        ("tests", config.tests_dir),
        ("unit_tests", config.unit_tests),
    ):
        assert (root / rel).is_dir(), f"[paths] {label} = {rel!r} does not exist in the project"
    # `[frontend] dir` is deliberately not asserted: the `fullstack` preset declares the
    # tier without scaffolding the directory (devkit ships no frontend template), and every
    # consumer of that field guards on its existence first. See the same note in
    # scripts/precommit/check_harness_manifest.py.


def test_generated_pre_commit_config_pins_devkit_and_wires_its_hooks(tmp_path):
    """The pre-commit channel must arrive pinned, like the PR gate's devkit ref.

    A floating ref would let one devkit commit change the commit-time gate in every repo
    at once — the same blast radius the PR gate pins a tag to avoid.
    """
    yaml = pytest.importorskip("yaml")
    root = generate(tmp_path, {})
    config = root / ".pre-commit-config.yaml"
    assert config.exists(), "generated projects get no pre-commit gate"
    parsed = yaml.safe_load(config.read_text(encoding="utf-8"))

    devkit_repos = [r for r in parsed["repos"] if r["repo"].endswith("/devkit")]
    assert len(devkit_repos) == 1, "devkit's hooks are not wired in"
    entry = devkit_repos[0]
    assert entry["rev"] and entry["rev"] != "main", f"devkit rev is {entry['rev']!r}"

    # Every hook it references must actually be published, or the consumer's first
    # `pre-commit run` fails with "hook not found" against a tag they cannot fix.
    published = {
        h["id"]
        for h in yaml.safe_load((REPO_ROOT / ".pre-commit-hooks.yaml").read_text(encoding="utf-8"))
    }
    for hook in entry["hooks"]:
        assert hook["id"] in published, f"{hook['id']} is not in devkit's .pre-commit-hooks.yaml"

    # Third-party repos must be pinned too — an unpinned `rev` is rejected by pre-commit
    # itself, but a mutable branch name is not.
    for repo in parsed["repos"]:
        assert repo.get("rev"), f"{repo['repo']} has no rev"
        assert not repo["rev"].startswith(("main", "master")), f"{repo['repo']} tracks a branch"


def test_generated_pre_commit_ref_matches_the_pr_gate_ref(tmp_path):
    """One devkit version per project, not two that can drift apart."""
    yaml = pytest.importorskip("yaml")
    root = generate(tmp_path, {})
    config = yaml.safe_load((root / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    pre_commit_ref = next(r["rev"] for r in config["repos"] if r["repo"].endswith("/devkit"))
    gate = (root / ".github" / "workflows" / "pr-gate.yml").read_text(encoding="utf-8")
    assert f"ref: {pre_commit_ref}" in gate, (
        f"pre-commit pins devkit {pre_commit_ref} but the PR gate pins something else"
    )


def test_generated_project_installs_pre_commit(tmp_path):
    """`session-start.sh` wires the git hook from the venv; the venv needs the tool."""
    root = generate(tmp_path, {})
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    dev = pyproject["project"]["optional-dependencies"]["dev"]
    assert any(spec.startswith("pre-commit") for spec in dev), dev


def test_generated_lint_runner_accepts_every_flag_the_stop_hook_passes(tmp_path):
    """The vendored Stop hook's Tier 1 flags must parse in the lint runner it invokes.

    `stop.py` ships byte-identical and cannot introspect `lint-all.py`, so its argv is a
    contract. The generated runner did not accept `--no-secrets`, which argparse rejects
    with exit 2 — so Tier 1 reported a lint failure on *every* stop in *every* generated
    project, with a usage message where the finding should be and nothing in the source
    tree that could fix it. Invisible to CI, which calls the script without the flag.
    """
    import subprocess

    stop = load_script("scripts/hooks/stop.py")
    argv, _cwd, _artifact = stop._command_for(stop.CHECK_LINT)
    flags = [a for a in argv if a.startswith("--")]
    assert "--no-secrets" in flags, "the regression this test guards has been reverted"

    root = generate(tmp_path, {})
    result = subprocess.run(
        [sys.executable, "scripts/lint-all.py", "--help"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    for flag in flags:
        assert flag in result.stdout, f"stop.py passes {flag}; the generated runner rejects it"


def test_generated_lint_runner_accepts_the_paths_flag_ship_probes_for(tmp_path):
    """The other half of the vendored-caller contract, for `/ship` rather than the Stop
    hook: ship hands it the branch diff, and falls back to `--changed` without it --
    which on the clean tree ship insists on is the empty set. The gate then passes
    having linted nothing, in every generated project."""
    import subprocess

    root = generate(tmp_path, {})
    result = subprocess.run(
        [sys.executable, "scripts/lint-all.py", "--help"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "--paths" in result.stdout


def test_mypy_scope_excludes_the_vendored_harness(tmp_path):
    # Belt and braces, and both are load-bearing: the pyproject `exclude` covers
    # directory recursion, MYPY_SCOPE covers someone running `mypy .` by hand.
    root = generate(tmp_path, {})
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert "sync-harness" in pyproject and "exclude" in pyproject
    lint_all = (root / "scripts" / "lint-all.py").read_text(encoding="utf-8")
    scope = re.search(r"MYPY_SCOPE = \[(.*?)\]", lint_all, re.S).group(1)
    assert "scripts" not in scope


# --- dependency toolchain -----------------------------------------------------
# Generated projects were the only shape here using plain pip with no lockfile,
# and the template still carried leftovers from carameli's requirements-file
# model: the README told a new user to `pip install -r requirements-dev.txt`, a
# file the generator never produced.


def test_no_template_references_a_requirements_file_it_never_generates(tmp_path):
    root = generate(tmp_path, dict.fromkeys(new_project.FEATURES, True))
    produced = {p.name for p in root.rglob("*")}
    assert not any(n.startswith("requirements") for n in produced)
    # Nothing may *instruct* the user to use one either -- that was the actual bug.
    for rel in ("README.md", ".gitattributes", "Dockerfile"):
        f = root / rel
        if f.exists():
            assert "requirements" not in f.read_text(encoding="utf-8"), rel


def test_gitattributes_has_no_duplicate_patterns(tmp_path):
    """A second rule for the same path silently overrides the first.

    Swapping the old `requirements*.txt` line for `uv.lock` produced exactly that:
    `uv.lock` twice, with different flags, plus an existing `*.lock` covering both.
    """
    lines = [
        ln.split()[0]
        for ln in (generate(tmp_path, {}) / ".gitattributes")
        .read_text(encoding="utf-8")
        .splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    dupes = {p for p in lines if lines.count(p) > 1}
    assert not dupes, f"duplicate .gitattributes patterns: {dupes}"


def test_readme_quick_start_install_command_is_real(tmp_path):
    readme = (generate(tmp_path, {}) / "README.md").read_text(encoding="utf-8")
    assert "uv sync" in readme


def test_dockerfile_never_swallows_a_failed_dependency_install(tmp_path):
    """`|| true` on an install turns a build error into a runtime ImportError."""
    root = generate(tmp_path, {"docker": True})
    for line in (root / "Dockerfile").read_text(encoding="utf-8").splitlines():
        if "install" in line and line.startswith("RUN"):
            assert "|| true" not in line, line


@pytest.mark.parametrize("features", FEATURE_MATRIX)
def test_every_local_action_the_generated_gate_uses_is_actually_rendered(tmp_path, features):
    """`uses: ./path` resolves from the workspace and fails on the runner if absent.

    The composite action lives under `dot-github/actions/`, so it only reaches a
    project if the `dot-` rename walks *every* path segment — nested directories
    included. A miss there is invisible until a real PR runs the gate.
    """
    root = generate(tmp_path, features)
    gate = (root / ".github" / "workflows" / "pr-gate.yml").read_text(encoding="utf-8")
    referenced = set(re.findall(r"uses:\s+\./(\S+)", gate))
    assert referenced, "the gate references no local action — the layout changed"
    for rel in referenced:
        assert (root / rel / "action.yml").is_file(), f"{rel}/action.yml was not rendered"


def test_generated_gate_installs_through_uv_and_runs_inside_it(tmp_path):
    root = generate(tmp_path, {})
    gate = (root / ".github" / "workflows" / "pr-gate.yml").read_text(encoding="utf-8")
    # The sync moved into the composite action; what matters is that the gate still
    # reaches it. The action arrives from `templates/` as a byte-identical copy of
    # devkit's own — one-shot, so the project may rewrite these steps afterwards.
    action = (root / ".github" / "actions" / "setup-python-env" / "action.yml").read_text(
        encoding="utf-8"
    )
    assert "uv sync --all-extras" in action
    # A bare `python scripts/...` runs outside the synced environment and misses
    # every dependency uv just installed. The exception is `sync-devkit.py`: it is
    # stdlib-only by contract and runs in the drift job, which never syncs.
    stdlib_only = ("sync-devkit.py",)
    for line in gate.splitlines():
        stripped = line.strip().removeprefix("- ")
        if stripped.startswith("run: python ") and not any(s in stripped for s in stdlib_only):
            raise AssertionError(f"gate step runs outside the uv env: {stripped}")


def test_generated_gate_passes_its_own_python_version_to_the_composite_action(tmp_path):
    """The action is byte-identical everywhere, so its default cannot be any project's.

    It used to be *rendered*, and its default was then the project's version — which is
    why the gate could leave it implicit. Copying it verbatim removes that: a project
    generated on 3.13 would silently provision 3.12, lock-resolve against it, and pass.
    So every call site has to name the version, and nothing but this test says so.

    Verbatim-not-rendered is the property that matters here, and it outlives the tier:
    the file was vendored for v0.7.0 and is a `templates/` copy again, and neither tier
    ever substituted `python_version` into it.
    """
    yaml = pytest.importorskip("yaml")
    args = make_args(parent=str(tmp_path), python_version="3.13")
    the_plan = new_project.plan(args, registry())
    the_plan.root.mkdir(parents=True, exist_ok=True)
    new_project.render_tree(the_plan, dry_run=False)

    parsed = yaml.safe_load(
        (the_plan.root / ".github" / "workflows" / "pr-gate.yml").read_text(encoding="utf-8")
    )
    call_sites = [
        (job_name, step)
        for job_name, job in parsed["jobs"].items()
        for step in job.get("steps") or []
        if step.get("uses") == "./.github/actions/setup-python-env"
    ]
    assert call_sites, "the generated gate no longer calls the composite action"
    for job_name, step in call_sites:
        got = (step.get("with") or {}).get("python-version")
        assert got == "3.13", f"{job_name} would provision the action's default, not 3.13"


def _workflow_triggers(parsed: dict) -> dict:
    """A workflow's `on:` block.

    PyYAML follows YAML 1.1, where an unquoted `on` is the boolean True — so every
    parsed workflow has its trigger block under `True`, not `"on"`. Reading
    `parsed["on"]` gives a KeyError that looks like a malformed workflow and is not.
    """
    triggers = parsed.get("on", parsed.get(True))
    assert isinstance(triggers, dict), f"workflow has no `on:` block: {sorted(parsed)}"
    return triggers


def test_generated_gate_does_not_burn_minutes_on_superseded_commits(tmp_path):
    """Every push to a PR queues another full gate run against a stale commit.

    Not hypothetical now that Dependabot opens a batch of PRs weekly and rebases
    each survivor as the batch merges. The key must include the ref: without it a
    push to the default branch and a PR run share a group and cancel each other.
    """
    yaml = pytest.importorskip("yaml")
    parsed = yaml.safe_load(
        (generate(tmp_path, {}) / ".github" / "workflows" / "pr-gate.yml").read_text(
            encoding="utf-8"
        )
    )
    assert "github.ref" in parsed["concurrency"]["group"]
    # Conditional, not a bare `true`. A cancelled push run leaves that commit with no
    # CI signal at all, and `workflow_run.conclusion` is then 'cancelled' rather than
    # 'success' — which strands every Dependabot PR behind a run that never failed.
    assert parsed["concurrency"]["cancel-in-progress"] == (
        "${{ github.event_name == 'pull_request' }}"
    )


def test_generated_gate_is_least_privilege_and_re_runnable(tmp_path):
    """A gate with no `permissions:` inherits whatever the repo default is.

    That default is a repo setting, not a file under review, so the only way a
    generated project can guarantee its gate is read-only is to say so here.
    """
    yaml = pytest.importorskip("yaml")
    parsed = yaml.safe_load(
        (generate(tmp_path, {}) / ".github" / "workflows" / "pr-gate.yml").read_text(
            encoding="utf-8"
        )
    )
    assert parsed["permissions"] == {"contents": "read"}
    assert "workflow_dispatch" in _workflow_triggers(parsed)


DEPENDABOT_ECOSYSTEMS = [
    pytest.param({}, {"uv", "github-actions"}, id="bare"),
    pytest.param({"docker": True}, {"uv", "github-actions", "docker"}, id="docker"),
    pytest.param(
        {"frontend": True},
        {"uv", "github-actions", "docker", "npm"},
        id="frontend",
    ),
]


@pytest.mark.parametrize("features,expected", DEPENDABOT_ECOSYSTEMS)
def test_generated_dependabot_covers_exactly_the_manifests_the_project_has(
    tmp_path, features, expected
):
    """An ecosystem entry with no manifest behind it is a permanent Dependabot error.

    It never surfaces in CI — it sits in the repo's Dependabot insights, where nobody
    is looking — so the only place it can be caught is here. `npm` is the deliberate
    exception and carries its reason in the template: the frontend feature declares
    the tier before anything scaffolds `frontend/package.json`.
    """
    yaml = pytest.importorskip("yaml")
    root = generate(tmp_path, features)
    config = root / ".github" / "dependabot.yml"
    assert config.exists(), "generated projects get no dependency updates"
    parsed = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert parsed["version"] == 2
    assert {u["package-ecosystem"] for u in parsed["updates"]} == expected


def test_generated_dependabot_never_lets_the_python_runtime_move_by_bot(tmp_path):
    """A base-image bump 3.12 -> 3.14 is semver-MINOR, so it would auto-merge.

    The lock is resolved for one Python and the gate runs that same one, neither of
    which the image bump touches — and no gate job builds the image, so nothing goes
    red. The runtime moves with the lock and the workflows or not at all.
    """
    yaml = pytest.importorskip("yaml")
    root = generate(tmp_path, {"docker": True})
    parsed = yaml.safe_load((root / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    docker = next(u for u in parsed["updates"] if u["package-ecosystem"] == "docker")
    ignored = next(i for i in docker["ignore"] if i["dependency-name"] == "python")
    assert set(ignored["update-types"]) == {
        "version-update:semver-major",
        "version-update:semver-minor",
    }


def test_generated_automerge_waits_on_the_gate_the_project_actually_has(tmp_path):
    """The `workflow_run` trigger names a workflow by title, not by filename.

    Rename the gate and the merge job stops firing — silently, because a
    `workflow_run` that matches nothing produces no run and therefore no red X.
    Every Dependabot PR would simply sit there labelled `automerge` forever.
    """
    yaml = pytest.importorskip("yaml")
    root = generate(tmp_path, {})
    workflows = root / ".github" / "workflows"
    gate_name = yaml.safe_load((workflows / "pr-gate.yml").read_text(encoding="utf-8"))["name"]
    automerge = yaml.safe_load((workflows / "dependabot-automerge.yml").read_text(encoding="utf-8"))
    assert _workflow_triggers(automerge)["workflow_run"]["workflows"] == [gate_name]


def test_generated_automerge_carries_no_project_specific_value(tmp_path):
    """It is vendored byte-identical now, so it may not name this project's branch.

    It used to render `branches: [{{ default_branch }}]`, which is what kept it in
    `templates/` — and templates are a one-shot copy, so every later fix to this file
    stopped reaching projects already generated. Dropping the filter is what makes it
    vendorable: the classify job is already restricted to Dependabot's own PRs, so
    narrowing by branch bought nothing but a per-project token.

    A project on a non-default branch name is the case that would regress silently, so
    generate one and assert the file came out identical to devkit's.
    """
    yaml = pytest.importorskip("yaml")
    args = make_args(parent=str(tmp_path), default_branch="trunk")
    the_plan = new_project.plan(args, registry())
    the_plan.root.mkdir(parents=True, exist_ok=True)
    new_project.render_tree(the_plan, dry_run=False)
    vendor_manifest(the_plan.root)

    rel = ".github/workflows/dependabot-automerge.yml"
    theirs = (the_plan.root / rel).read_bytes()
    assert theirs == (REPO_ROOT / rel).read_bytes(), f"{rel} is vendored; it must not be rendered"
    assert b"trunk" not in theirs, "the project's default branch leaked into a vendored file"
    # `branches:` absent entirely, rather than present and set to something neutral —
    # any value here is a name that differs per repo.
    assert _workflow_triggers(yaml.safe_load(theirs.decode("utf-8")))["pull_request"] is None


def test_generated_automerge_can_create_the_labels_it_applies(tmp_path):
    """`gh pr edit --add-label` fails outright on a label the repo does not have.

    A brand-new repo has neither, so without the `gh label create` step the very
    first Dependabot PR fails the classify job — on a missing label, not on anything
    about the bump. Labels are the issues API, hence the third permission.
    """
    yaml = pytest.importorskip("yaml")
    root = generate(tmp_path, {})
    path = root / ".github" / "workflows" / "dependabot-automerge.yml"
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert parsed["permissions"] == {
        "contents": "write",
        "issues": "write",
        "pull-requests": "write",
    }
    body = path.read_text(encoding="utf-8")
    for label in ("automerge", "needs-manual-merge"):
        assert f"gh label create {label}" in body, f"{label} is applied but never created"


def test_generated_automerge_tells_gh_which_repo_it_is_acting_on(tmp_path):
    """Same defect as devkit's own copy, and quieter: it fires in someone else's repo.

    The classify job checks nothing out, so `gh` has no git remote to infer the
    repository from and exits on "fatal: not a git repository" before it labels
    anything — on the new owner's first Dependabot PR, in a job they did not write.
    """
    yaml = pytest.importorskip("yaml")
    root = generate(tmp_path, {})
    parsed = yaml.safe_load(
        (root / ".github" / "workflows" / "dependabot-automerge.yml").read_text(encoding="utf-8")
    )
    offenders = gh_steps_without_repo_context(parsed)
    assert not offenders, f"these steps run `gh` with no repo to resolve: {offenders}"


def test_generated_automerge_re_checks_every_guard_before_merging(tmp_path):
    """`workflow_run` hands over a branch name, not a PR — the guards must be re-run.

    A human can push to a `dependabot/...` branch, and a new commit can land after
    the gate passed. Each of these three checks is what keeps the merge tied to the
    exact commit that was gated, authored by the bot, and classified as safe.
    """
    root = generate(tmp_path, {})
    body = (root / ".github" / "workflows" / "dependabot-automerge.yml").read_text(encoding="utf-8")
    merge_job = body.split("  merge:", 1)[1]
    assert 'if [ "$author" != "app/dependabot" ]' in merge_job, "any author could be merged"
    assert 'if [ "$head_sha" != "$RUN_HEAD_SHA" ]' in merge_job, "an ungated commit could merge"
    assert 'index("automerge")' in merge_job, "a runtime major could merge unreviewed"


def test_generated_automerge_holds_runtime_majors_for_review(tmp_path):
    """The classifier's whole point: only dev-scoped majors may merge unattended.

    Written as one jq expression, so the risk of an edit widening it silently is
    real. `length > 0` matters as much as the rest — `all` over an empty list is
    true, so an empty metadata payload would otherwise classify as auto-mergeable.
    """
    root = generate(tmp_path, {})
    body = (root / ".github" / "workflows" / "dependabot-automerge.yml").read_text(encoding="utf-8")
    classify = body.split("  classify:", 1)[1].split("  merge:", 1)[0]
    assert "length > 0 and all(.[];" in classify
    assert '.dependencyType == "direct:development"' in classify
    assert '"needs-manual-merge"' in classify


def test_lock_step_is_skipped_gracefully_without_uv(tmp_path, monkeypatch):
    """Locking needs a network; a scaffold must still finish without it."""
    monkeypatch.setattr(new_project.shutil, "which", lambda _name: None)
    root = generate(tmp_path, {})
    assert (root / "pyproject.toml").exists()
    assert not (root / "uv.lock").exists()


def test_compose_publishes_every_port_through_a_variable(tmp_path):
    # The whole reason parallel worktrees work. A literal host port here is the bug
    # that makes two checkouts un-runnable at the same time.
    root = generate(tmp_path, {f: True for f in new_project.FEATURES})
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    published = re.findall(r'^\s+- "([^"]+)"', compose, flags=re.MULTILINE)
    assert published, "no published ports found — the assertion is not testing anything"
    for mapping in published:
        assert mapping.startswith("${"), f"hardcoded host port: {mapping}"


def test_compose_omits_the_volumes_key_when_nothing_declares_one(tmp_path):
    root = generate(tmp_path, {"app_service": True})
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    assert "\nvolumes:" not in compose


def test_dockerignore_excludes_the_env_file(tmp_path):
    # `.env` is gitignored so it never reaches the repo, but `COPY . .` would put it
    # in the image without this line.
    root = generate(tmp_path, {"postgres": True, "app_service": True})
    assert "\n.env\n" in (root / ".dockerignore").read_text(encoding="utf-8")


def test_env_example_ports_agree_with_the_registry(tmp_path):
    root = generate(tmp_path, {"postgres": True, "app_service": True})
    text = (root / ".env.example").read_text(encoding="utf-8")
    slot = new_project.plan(
        make_args(parent=str(tmp_path / "x"), postgres=True, app_service=True), registry()
    ).context["slot"]
    expected = registry().services["db"] + slot
    assert f"DB_HOST_PORT={expected}" in text
    # The DATABASE_URL must agree with DB_HOST_PORT — that pairing is the first
    # thing that breaks when a worktree's ports are edited by hand.
    assert f"127.0.0.1:{expected}/" in text


def test_pr_gate_pins_a_devkit_tag_and_sets_the_drift_variable(tmp_path):
    root = generate(tmp_path, {})
    gate = (root / ".github" / "workflows" / "pr-gate.yml").read_text(encoding="utf-8")
    pinned = re.search(r"ref: (\S+)", gate).group(1)
    # Never a branch: one bad devkit commit must not redden every consuming repo.
    assert pinned not in {"main", "master", "HEAD"}
    assert pinned.startswith("v"), f"expected a version tag, got {pinned!r}"
    # Without this the drift check exits 0 having compared nothing.
    assert "DEVKIT_DIR:" in gate


def test_default_devkit_ref_is_resolved_from_the_newest_tag():
    # Regression: the default was hardcoded `v0.1.0` and stayed there after v0.2.0
    # shipped, so a generated project pinned a tag whose vendored harness predated
    # the fix — its drift job failed on the first PR.
    tag = new_project.latest_devkit_tag()
    assert tag is not None, "devkit has no tags; the fallback would be used"
    assert tag != "v0.1.0" or new_project.FALLBACK_DEVKIT_REF != "v0.1.0"


def test_fallback_devkit_ref_tracks_the_newest_tag():
    """The fallback must not rot the way the old hardcoded default did.

    It only fires when git cannot resolve a tag, so a stale value here is invisible
    to everyone on the fast path and breaks exactly the users who are not — the
    hardest kind of staleness to notice. Failing here at release time is the cheap
    version of that discovery. If this is red, bump FALLBACK_DEVKIT_REF to the tag
    you just pushed.
    """
    tag = new_project.latest_devkit_tag()
    if tag is None:
        pytest.skip("no tags to compare against")
    assert tag == new_project.FALLBACK_DEVKIT_REF, (
        f"FALLBACK_DEVKIT_REF is {new_project.FALLBACK_DEVKIT_REF!r} but devkit's "
        f"newest tag is {tag!r} — bump the constant"
    )


def test_latest_devkit_tag_is_none_outside_a_git_repo(tmp_path):
    assert new_project.latest_devkit_tag(tmp_path) is None


def _repo_with_tags(tmp_path, tags):
    """A throwaway repo with one commit per entry of `tags`, tagged in that order.

    Returns the repo and a `git` callable bound to it. The global config is not
    inherited for the same reason `_seed_fake_devkit` refuses it: this seeds commits
    on `main`, which the installed branch policy blocks.
    """
    import subprocess

    repo = tmp_path / "tagged"
    repo.mkdir()
    env = dict(os.environ, GIT_CONFIG_GLOBAL=str(tmp_path / "gitconfig"), GIT_CONFIG_NOSYSTEM="1")

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=env
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "t")
    for index, tag in enumerate(tags):
        (repo / "f.txt").write_text(f"{index}\n", encoding="utf-8")
        git("add", "-A")
        git("commit", "-q", "-m", f"c{index}")
        git("tag", tag)
    return repo, git


def test_the_newest_tag_is_found_from_a_checkout_parked_behind_it(tmp_path):
    """Which release is current is a property of the repo, not of where HEAD sits.

    `git describe --tags --abbrev=0` answered the other question — the newest tag
    *reachable from HEAD* — and the gap is silent. Minutes after v0.9.1 was
    published, `install-git-policy.py --yes` in a checkout not yet fast-forwarded
    installed v0.9.0's runtime and reported that as success, which is exactly the
    stale-policy state that script's receipts exist to make visible. `--check`
    shared the blind spot, because it compares the receipt against this function.
    """
    repo, git = _repo_with_tags(tmp_path, ["v1.0.0", "v1.1.0"])
    git("checkout", "-q", "v1.0.0")  # detached, one release behind
    assert new_project.latest_devkit_tag(repo) == "v1.1.0"


def test_tags_are_ordered_by_version_and_not_lexically(tmp_path):
    # `v0.10.0` sorts *below* `v0.9.1` as a string, so the first release past a
    # two-digit minor is where a lexical sort would start pinning the older tag into
    # every generated project — and `FALLBACK_DEVKIT_REF`'s release-time test would
    # then demand a bump back down to it.
    repo, _ = _repo_with_tags(tmp_path, ["v0.9.1", "v0.10.0"])
    assert new_project.latest_devkit_tag(repo) == "v0.10.0"


def test_a_tag_that_is_not_a_release_is_not_a_candidate(tmp_path):
    # A marker or a vendor pin must never become the ref a generated project pins.
    repo, git = _repo_with_tags(tmp_path, ["v1.0.0"])
    git("tag", "nightly-2026-08-17")
    assert new_project.latest_devkit_tag(repo) == "v1.0.0"


def test_manifest_paths_are_read_from_the_sync_tool():
    # Read from sync-devkit.py rather than duplicated, so the two cannot disagree.
    manifest = new_project._read_manifest_paths(REPO_ROOT)
    assert "scripts/sync-devkit.py" in manifest
    assert "scripts/hooks/harness_config.py" in manifest


def test_harness_comparison_returns_none_for_an_unknown_ref():
    # Unknown ref must be reported as "could not compare", never as "no differences" —
    # a silent empty list would suppress the stale-pin warning entirely.
    assert new_project.harness_files_matching_ref("v99.99.99-nope") is None


def _seed_fake_devkit(tmp_path, manifest, files, tag="v1.0.0"):
    """A throwaway devkit checkout with `files` committed and tagged `tag`.

    `manifest` is the tuple `sync-devkit.py` will expose; it is written verbatim so a
    caller can list a path that is deliberately absent from the commit.
    """
    import subprocess

    repo = tmp_path / "fake_devkit"
    (repo / "scripts" / "hooks").mkdir(parents=True)
    (repo / "scripts" / "sync-devkit.py").write_text(f"MANIFEST = {manifest!r}\n", encoding="utf-8")
    for rel, text in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    # Do not inherit the developer machine's global/system git config. This fixture
    # seeds a commit on `main`, which the global branch policy installed by
    # `scripts/install-git-policy.py` blocks via `core.hooksPath` — so without this
    # the test passes in CI and fails on any machine that adopted the policy.
    # Mirrors the isolation in `scripts/hooks/tests/test_session_start.py`.
    env = dict(os.environ, GIT_CONFIG_GLOBAL=str(tmp_path / "gitconfig"), GIT_CONFIG_NOSYSTEM="1")
    for cmd in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "t@example.invalid"],
        ["git", "config", "user.name", "t"],
        ["git", "add", "-A"],
        ["git", "commit", "-q", "-m", "seed"],
        ["git", "tag", tag],
    ):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True, env=env)
    return repo


def test_harness_comparison_reports_no_differences_against_a_clean_tree(tmp_path):
    # Build a throwaway repo whose committed content matches its working tree, and
    # confirm the comparison finds nothing. Proves the check can return [] at all —
    # otherwise the warning would fire forever and be trained away as noise.
    repo = _seed_fake_devkit(
        tmp_path,
        ("scripts/hooks/harness_config.py",),
        {"scripts/hooks/harness_config.py": "x = 1\n"},
    )

    assert new_project.harness_files_matching_ref("v1.0.0", repo) == []

    (repo / "scripts" / "hooks" / "harness_config.py").write_text("x = 2\n", encoding="utf-8")
    assert new_project.harness_files_matching_ref("v1.0.0", repo) == [
        "scripts/hooks/harness_config.py"
    ]


def test_harness_comparison_flags_a_manifest_file_absent_at_the_ref(tmp_path):
    # A vendored file added *since* the tag is the strongest evidence the pin is stale,
    # and it used to be the one case that silenced the warning: `git show ref:path`
    # exits non-zero for a path that does not exist at `ref`, which the comparison read
    # as "cannot compare" and reported as a NOTE. Generating apt-finder against a devkit
    # nine commits past v0.5.3 therefore printed "could not compare" instead of naming
    # the four new files, and the drift hook rejected the first commit.
    repo = _seed_fake_devkit(
        tmp_path,
        ("scripts/hooks/harness_config.py", "scripts/hooks/session-sync.py"),
        {"scripts/hooks/harness_config.py": "x = 1\n"},
    )
    # Added after the tag, exactly like a new MANIFEST entry on devkit's main.
    (repo / "scripts" / "hooks" / "session-sync.py").write_text("y = 1\n", encoding="utf-8")

    assert new_project.harness_files_matching_ref("v1.0.0", repo) == [
        "scripts/hooks/session-sync.py"
    ]


def test_harness_comparison_still_returns_none_when_the_ref_itself_is_unknown(tmp_path):
    # The counterpart to the test above: "file missing at ref" must not be conflated
    # with "ref missing". An unknown ref is a real inability to compare, and reporting
    # it as wholesale drift would name every vendored file as differing.
    repo = _seed_fake_devkit(
        tmp_path,
        ("scripts/hooks/harness_config.py",),
        {"scripts/hooks/harness_config.py": "x = 1\n"},
    )
    assert new_project.harness_files_matching_ref("v99.99.99-nope", repo) is None


def test_generator_subprocesses_waive_the_branch_policy(monkeypatch, tmp_path):
    """The generator seeds a repo and pushes `main` — scripted setup, by definition.

    devkit's global branch policy (`core.hooksPath`) blocks commits and pushes to a
    protected branch, and it applies to the brand-new repo the generator has just
    created. Generating apt-finder with `--remote` therefore died at the last step:

        [devkit branch policy] push to protected branch 'main' blocked
        new-project: command failed (1): gh repo create ... --push

    after the private GitHub repo had already been created — leaving a repo on GitHub
    with nothing in it and a local checkout that had to be pushed by hand.

    `DEVKIT_SKIP_BRANCH_POLICY` is the escape hatch the policy documents for exactly
    this ("a generator that seeds an initial commit"). It waives the branch checks
    only: the project's own pre-commit gate still runs, which is what verifies the
    vendored harness on that first commit.
    """
    import subprocess as sp

    captured: dict[str, object] = {}

    def fake_run(cmd, cwd=None, env=None, **kwargs):
        captured["env"] = env
        return sp.CompletedProcess(cmd, 0)

    monkeypatch.setattr(new_project.subprocess, "run", fake_run)
    new_project.run(["git", "push", "-u", "origin", "main"], tmp_path, dry_run=False)

    env = captured["env"]
    assert env is not None, "generator ran git with the ambient env; the policy will block it"
    assert env["DEVKIT_SKIP_BRANCH_POLICY"] == "1"
    # Inherited, not replaced — a bare {"DEVKIT_SKIP_BRANCH_POLICY": "1"} would strip
    # PATH and break every `git`/`gh`/`uv` the generator shells out to on Windows.
    assert "PATH" in {k.upper() for k in env}


def test_claude_settings_only_wires_hooks_that_are_actually_vendored(tmp_path):
    # A hook command pointing at a script the MANIFEST does not ship fires on every
    # turn and fails silently. Carameli's settings reference several such
    # project-local hooks; a generated project must not inherit those.
    manifest_text = (REPO_ROOT / "scripts" / "sync-devkit.py").read_text(encoding="utf-8")
    root = generate(tmp_path, {})
    settings = (root / ".claude" / "settings.json").read_text(encoding="utf-8")
    for referenced in re.findall(r"\$\{CLAUDE_PROJECT_DIR:-\.\}/([^\"]+?\.(?:py|sh))", settings):
        assert referenced in manifest_text, (
            f"{referenced} is wired as a hook but is not in sync-devkit.py's MANIFEST"
        )


def test_generated_telemetry_endpoint_is_the_shared_collector_not_the_project_slot(tmp_path):
    """A generated project must export to the one collector, not to its own slot.

    `otel_http` was a slot-offset `[services]` base until 2026-08-17, so every project
    was scaffolded with a private endpoint -- 4318, 4322, 4324 -- while exactly one
    collector existed in the workspace. The two projects on the wrong end exported into
    a closed port for a month and neither could report it: an OTLP exporter that cannot
    connect retries in the background and Claude Code carries on regardless.

    Asserting against `[shared]` rather than a literal is deliberate. A literal here
    would still pass if someone moved the collector and left the template behind.
    """
    shared = devkit_ports.load(REPO_ROOT).shared_port("otel_http")
    root = generate(tmp_path, {})
    env = json.loads((root / ".claude/settings.json").read_text(encoding="utf-8"))["env"]

    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == f"http://localhost:{shared}"
    # The other half of the bargain: with one endpoint for everyone, the resource
    # attributes are the only thing left that says which project sent a metric.
    assert "service.name=" in env["OTEL_RESOURCE_ATTRIBUTES"]


def test_generated_claude_settings_keep_the_bash_cap_hook(tmp_path):
    """Codex drops this one handler; generation must not weaken Claude with it."""
    root = generate(tmp_path, {})
    settings = json.loads((root / ".claude/settings.json").read_text(encoding="utf-8"))
    groups = settings["hooks"]["PreToolUse"]

    bash_handlers = [
        handler["command"]
        for group in groups
        if group.get("matcher") == "^Bash$"
        for handler in group["hooks"]
    ]

    assert any("scripts/hooks/enforce-capped-bash.py" in command for command in bash_handlers)
