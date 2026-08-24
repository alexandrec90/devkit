#!/usr/bin/env python3
"""Create a new project wired to devkit, Docker, a worktree, and a GitHub repo.

Everything the existing projects were assembled by hand — the harness seam, the
port allocation, the `.env` worktree block, the VS Code tasks, the PR gate with a
drift check that actually gates — is rendered from `templates/` instead of copied
from whichever repo was nearest. What differs per project lives in flags; what does
not is identical by construction.

    # See exactly what would happen. This is the DEFAULT.
    python scripts/new-project.py sports_betting --with-postgres --with-archive

    # Actually do it.
    python scripts/new-project.py sports_betting --with-postgres --with-archive --yes

Ordering is deliberate: everything local and reversible happens first, and the two
outward-facing steps (creating the GitHub repo, pushing to it) come last and are
skippable with `--no-remote`. A failure before that point leaves nothing but a
directory you can delete.

Steps, in order:
  1. allocate a port slot from `ports.toml` (fails loudly rather than guessing)
  2. render `templates/` into the new directory
  3. `git init` + initial commit
  4. vendor the harness (`sync-devkit.py --pull`) and stamp `DEVKIT_VERSION`
  5. add the parallel worktree with its own offset `.env`
  6. register the project in the shared `alex-projects.code-workspace`
     (the one step that writes OUTSIDE the new directory; `--no-register` skips it)
  7. create the private GitHub repo and push          <- outward-facing
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import devkit_ports
import devkit_project
from devkit_render import TemplateError, render

DEVKIT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = DEVKIT_ROOT / "templates"

# Path segments starting `dot-` become dotted on output. Templates cannot ship a
# literal `.gitignore`/`.claude/` because those would take effect on devkit's OWN
# repo — a nested .gitignore inside templates/ would make git ignore the templates.
DOT_PREFIX = "dot-"
TEMPLATE_SUFFIX = ".tmpl"

# Services whose ports appear in a generated `.env`, keyed by the flag that needs them.
SERVICE_BY_FEATURE = {
    "app_service": "app",
    "postgres": "db",
    "redis": "redis",
    "frontend": "frontend",
}

FEATURES = ("docker", "app_service", "postgres", "redis", "alembic", "frontend", "archive")

# Named feature bundles. These exist for the VS Code task: a task picker passes one
# string, and VS Code cannot build a variable-length argument list from it. They are
# also the honest answer to "what shapes do we actually build here" — the two
# existing projects are `fullstack` (carameli) and `data` (ibkr_trader).
PRESETS: dict[str, tuple[str, ...]] = {
    "bare": (),
    "service": ("docker", "app_service", "postgres", "alembic"),
    "service-redis": ("docker", "app_service", "postgres", "alembic", "redis"),
    "data": ("docker", "postgres", "archive"),
    "fullstack": ("docker", "app_service", "postgres", "alembic", "redis", "frontend"),
}


# Fallback when git cannot answer (no tags, not a checkout, git absent). Only used
# if `latest_devkit_tag()` finds nothing, which is rare — but a stale value here
# reintroduces the very bug the runtime lookup fixed, silently and only for the
# users who cannot hit the fast path. So it must track the newest tag, and
# `test_fallback_devkit_ref_tracks_the_newest_tag` fails at release time if a tag
# lands without it being bumped. Bump it in the same commit as the tag.
FALLBACK_DEVKIT_REF = "v0.11.3"


class GeneratorError(RuntimeError):
    """A precondition failed. The message is written for a human, not a traceback."""


def latest_devkit_tag(root: Path = DEVKIT_ROOT) -> str | None:
    """devkit's highest release tag, or None when git cannot say.

    The generated PR gate must pin a tag, never `@main` — one bad devkit commit must
    not redden every consuming repo at once. Resolving the newest tag at generation
    time beats a hardcoded default, which is how the default came to say `v0.1.0`
    long after v0.2.0 shipped.

    This asks `git tag --sort=-v:refname`, and neither half of that is incidental.

    It was `git describe --tags --abbrev=0`, which answers a different question:
    the newest tag **reachable from HEAD**. Every caller here wants "which release
    is current", a property of the repository and not of whatever the checkout is
    parked on — and the gap between the two is silent in both directions. Minutes
    after v0.9.1 was published, `install-git-policy.py --yes` in a checkout not yet
    fast-forwarded installed v0.9.0's runtime and reported doing so as success,
    which is precisely the stale-policy failure that file's receipts exist to
    surface. `--check` could not see it either: it compares the receipt against this
    function, so both sides shared the blind spot.

    `-v:refname` rather than a plain sort because version order is not lexical
    order: `v0.10.0` sorts *below* `v0.9.1` as a string, so the first release past
    a two-digit minor would otherwise silently pin the older tag into every
    generated project.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "tag", "--list", "v*", "--sort=-v:refname"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        tag = line.strip()
        if tag:
            return tag
    return None


def harness_files_matching_ref(ref: str, root: Path = DEVKIT_ROOT) -> list[str] | None:
    """MANIFEST paths whose working-tree bytes differ from those at `ref`.

    This is the exact condition the generated project's `devkit-drift` job checks: it
    vendors from the working tree but drift-checks against the pinned tag. If they
    disagree, that job fails on the very first PR — so warn at generation time, when
    the fix (tag devkit, or pass --devkit-ref) is still cheap.

    None means the comparison could not be made (unknown ref, no git).
    """
    manifest = _read_manifest_paths(root)
    if not manifest:
        return None
    # Resolve the ref once, up front. Without this, "the ref does not exist" and "the
    # file does not exist at that ref" are the same non-zero exit from `git show`, and
    # the loop below cannot tell a real inability to compare from the single most
    # important thing it exists to report — see the absent-at-ref branch.
    try:
        resolved = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if resolved.returncode != 0:
        return None
    differing: list[str] = []
    for rel in manifest:
        try:
            result = subprocess.run(
                ["git", "-C", str(root), "show", f"{ref}:{rel}"],
                capture_output=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            # The ref resolved, so this is a MANIFEST entry that did not exist at it —
            # a vendored file added since the tag. That is drift, and the loudest
            # possible evidence the pin is stale; reporting it as "could not compare"
            # suppressed the warning in exactly the case it was written for.
            differing.append(rel)
            continue
        try:
            current = (root / rel).read_bytes()
        except OSError:
            differing.append(rel)
            continue
        # git normalizes to LF in the object store; .gitattributes sets eol=lf, so a
        # CRLF working copy on Windows would otherwise report every file as differing.
        if current.replace(b"\r\n", b"\n") != result.stdout.replace(b"\r\n", b"\n"):
            differing.append(rel)
    return differing


def _read_manifest_paths(root: Path) -> tuple[str, ...]:
    """`sync-devkit.py`'s MANIFEST, read from the tool itself so it cannot go stale.

    Loaded by path rather than duplicated here: a second copy of the file list is a
    second thing to forget to update when the manifest grows.
    """
    path = root / "scripts" / "sync-devkit.py"
    try:
        spec = importlib.util.spec_from_file_location("_sync_harness_manifest", path)
        if spec is None or spec.loader is None:
            return ()
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return tuple(module.MANIFEST)
    except (OSError, AttributeError, ImportError, SyntaxError):
        return ()


@dataclass
class Plan:
    """Everything decided before a single byte is written."""

    name: str
    root: Path
    context: dict[str, object]
    files: list[tuple[Path, Path, bool]] = field(default_factory=list)
    worktree: str | None = None
    worktree_env: dict[str, str] = field(default_factory=dict)
    remote: bool = True
    register: bool = True


def slugify_package(name: str) -> str:
    """`sports-betting` -> `sports_betting`. The importable form of a project name."""
    return re.sub(r"[^a-z0-9_]", "_", name.lower()).strip("_")


def validate_name(name: str) -> str:
    """Reject names that break a directory, a Python package, or COMPOSE_PROJECT_NAME."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
        raise GeneratorError(
            f"invalid project name {name!r}: use letters, digits, '-' and '_', "
            f"starting with a letter or digit. The name becomes a directory, a "
            f"Python package, and COMPOSE_PROJECT_NAME — all three constrain it."
        )
    return name


def iter_template_files(features: dict[str, bool]) -> list[tuple[Path, Path]]:
    """`(source, relative destination)` for every template the feature set selects.

    Selection is by directory: `templates/core/**` always, and
    `templates/features/<f>/**` only when `<f>` is on. Inline `{{#flag}}` blocks
    then handle the cross-cutting bits inside the files that survive.
    """
    roots = [TEMPLATES / "core"]
    roots += [
        TEMPLATES / "features" / feature
        for feature, on in features.items()
        if on and (TEMPLATES / "features" / feature).is_dir()
    ]

    pairs: list[tuple[Path, Path]] = []
    for root in roots:
        for source in sorted(root.rglob("*")):
            if source.is_dir():
                continue
            relative = source.relative_to(root)
            pairs.append((source, Path(_destination_name(relative))))
    return pairs


def _destination_name(relative: Path) -> str:
    """Apply the `dot-` and `.tmpl` conventions to every segment of a path.

    `dot-claude/settings.json.tmpl` -> `.claude/settings.json`. Every segment is
    considered, because directories need the rename too.
    """
    parts = [
        f".{part[len(DOT_PREFIX) :]}" if part.startswith(DOT_PREFIX) else part
        for part in relative.parts
    ]
    if parts[-1].endswith(TEMPLATE_SUFFIX):
        parts[-1] = parts[-1][: -len(TEMPLATE_SUFFIX)]
    return str(Path(*parts))


def build_context(
    args: argparse.Namespace,
    slot: int,
    ports: dict[str, int],
    shared: dict[str, int] | None = None,
) -> dict[str, object]:
    """The full template namespace. Flags and values share it deliberately."""
    package = slugify_package(args.name)
    features = {f: bool(getattr(args, f)) for f in FEATURES}

    context: dict[str, object] = {
        "name": args.name,
        "checkout": args.name,
        "package": package,
        "display_name": args.display_name or args.name.replace("_", " ").replace("-", " ").title(),
        "description": args.description,
        "env_prefix": package.upper(),
        "github_owner": args.github_owner,
        "python_version": args.python_version,
        "python_version_nodot": args.python_version.replace(".", ""),
        "default_branch": args.default_branch,
        "devkit_ref": args.devkit_ref,
        "slot": slot,
        "src_root": "src" if args.src_layout else ".",
        "app_dir": f"src/{package}/" if args.src_layout else f"{package}/",
        "tests_dir": "tests/",
        # The layout this generator actually produces is flat -- `tests/test_smoke.py`,
        # no unit/integration split -- so this must be `tests`, not `tests/unit`.
        # Pointing it at a directory that is never created was not cosmetic: it is the
        # target `stop.py` selects for the whole-suite run when application code changes,
        # and `pytest tests/unit` on a missing path exits 4, which the Stop hook reports
        # as a test failure on the first app-code edit in every generated project. The
        # `devkit-manifest` pre-commit hook is what surfaced it.
        "unit_tests": "tests",
        "db_user": package[:16],
        "db_password": f"{package[:16]}_local_dev",
        "db_name": package,
        "db_url_scheme": args.db_url_scheme,
        "db_services_toml": ", ".join(
            f'"{s}"' for s in (["db"] + (["redis"] if features["redis"] else []))
        ),
        "has_volumes": features["postgres"] or features["frontend"],
        "worktree": f"{args.name}-b" if args.worktree else "",
        "worktree_slot": "",
        **features,
    }
    # Ports the feature set actually uses, plus otel which is always exported.
    #
    # `otel_http` comes from `[shared]`, not from this project's slot: one collector
    # serves the whole workspace, so a generated project must point at *that* port and
    # not at a per-slot one nothing is listening on. See `[shared]` in `ports.toml`.
    for service in SERVICE_BY_FEATURE.values():
        context[f"{service}_port"] = ports.get(service, 0)
    #
    # Missing is a hard error rather than a default: the failure mode of a wrong
    # telemetry port is an exporter retrying into a closed socket forever, which no
    # generated project would ever report.
    try:
        context["otel_http_port"] = (shared or {})["otel_http"]
    except KeyError:
        raise GeneratorError(
            "no shared `otel_http` port in ports.toml; add it under [shared]"
        ) from None
    return context


def plan(args: argparse.Namespace, registry: devkit_ports.Registry) -> Plan:
    """Decide everything. Touches nothing."""
    validate_name(args.name)
    root = Path(args.parent).expanduser().resolve() / args.name
    if root.exists() and any(root.iterdir()):
        raise GeneratorError(
            f"{root} already exists and is not empty. Generating into it would "
            f"overwrite files; remove it or pick another name."
        )

    slot = _slot_for(registry, args.name)
    ports = registry.ports_for_slot(slot)
    context = build_context(args, slot, ports, registry.shared)

    worktree_env: dict[str, str] = {}
    if args.worktree:
        worktree = f"{args.name}-b"
        # `taken` keeps a brand-new project and its brand-new worktree from both
        # being handed the same "next free" slot in a single run.
        worktree_slot = _slot_for(registry, worktree, taken={slot})
        context["worktree_slot"] = worktree_slot
        worktree_env = _env_block(registry, worktree, worktree_slot, context)

    features = {f: bool(context[f]) for f in FEATURES}
    files = [
        (source, root / relative, source.name.endswith(TEMPLATE_SUFFIX))
        for source, relative in iter_template_files(features)
    ]

    return Plan(
        name=args.name,
        root=root,
        context=context,
        files=files,
        worktree=f"{args.name}-b" if args.worktree else None,
        worktree_env=worktree_env,
        remote=args.remote,
        register=args.register,
    )


def _slot_for(registry: devkit_ports.Registry, checkout: str, taken: set[int] | None = None) -> int:
    """A registered slot, or the next free one when the checkout is new.

    `taken` covers slots already handed out within this run but not yet written to
    `ports.toml` — without it a new project and its new worktree would both be
    offered the same free slot and collide on first `docker compose up`.
    """
    try:
        return registry.slot_of(checkout)
    except devkit_ports.RegistryError:
        pass
    reserved = taken or set()
    candidate = registry.next_free_slot()
    while candidate in reserved:
        candidate += 1
        if candidate >= registry.max_slots:
            raise devkit_ports.RegistryError(
                f"no free slot below max_slots={registry.max_slots} after reserving "
                f"{sorted(reserved)}; raise it in {devkit_ports.REGISTRY_NAME}."
            ) from None
    return candidate


def _env_block(
    registry: devkit_ports.Registry, checkout: str, slot: int, context: dict[str, object]
) -> dict[str, str]:
    """The `*_HOST_PORT` overrides one worktree needs, restricted to its services."""
    services = [
        service
        for feature, service in SERVICE_BY_FEATURE.items()
        if context.get(feature) and service in registry.services
    ]
    block = registry.env_for_slot(slot, services)
    block["COMPOSE_PROJECT_NAME"] = checkout
    return block


def render_tree(plan: Plan, dry_run: bool) -> None:
    """Write every planned file. Templates are rendered; everything else is copied."""
    for source, destination, is_template in plan.files:
        rel = destination.relative_to(plan.root)
        if dry_run:
            print(f"  write   {rel}")
            # Render anyway, so a broken template fails the DRY RUN rather than
            # surfacing halfway through a real generation with files on disk.
            if is_template:
                _render_or_die(source, plan.context)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if is_template:
            destination.write_text(
                _one_trailing_newline(_render_or_die(source, plan.context)),
                encoding="utf-8",
                newline="\n",
            )
        else:
            shutil.copy2(source, destination)


def _one_trailing_newline(body: str) -> str:
    """Exactly one `\\n` at the end, whatever the template and its sections left behind.

    Stripping a feature section takes its content but not the blank line that separated it
    from the previous one, so a template whose last block is conditional renders with a
    trailing blank whenever that feature is off — `CLAUDE.md.tmpl` ends with
    `{{/archive}}`, so every non-archive project got one. Harmless until the project has a
    pre-commit gate, at which point `end-of-file-fixer` rewrites the file on the first run
    and the brand-new repo greets its owner with a failing hook and a dirty tree.

    Normalising here rather than in `render()` keeps the renderer a faithful substitution
    function (its tests assert exact output) and puts the file-shape concern at the point
    where a file is actually written.
    """
    return body.rstrip("\n") + "\n" if body.strip() else body


def _render_or_die(source: Path, context: dict[str, object]) -> str:
    try:
        return render(source.read_text(encoding="utf-8"), context)
    except TemplateError as exc:
        raise GeneratorError(f"{source.relative_to(TEMPLATES)}: {exc}") from exc


def write_package(plan: Plan, dry_run: bool) -> None:
    """The importable package stub the smoke test and the Dockerfile CMD expect."""
    app_dir = plan.root / str(plan.context["app_dir"])
    init = app_dir / "__init__.py"
    body = f'"""{plan.context["display_name"]}."""\n\n__version__ = "0.1.0"\n'
    unit_tests = plan.root / str(plan.context["unit_tests"])
    if dry_run:
        print(f"  write   {init.relative_to(plan.root)}")
        print(f"  write   {plan.context['unit_tests']}/.gitkeep")
        print("  write   logs/.gitkeep")
        return
    app_dir.mkdir(parents=True, exist_ok=True)
    init.write_text(body, encoding="utf-8", newline="\n")
    # The rendered manifest names this as `[paths] unit_tests`, and the Stop hook's
    # DB tier resolves an app/ change straight to it (`host_test_targets` returns
    # [CFG.unit_tests]). Generated without it, the tier ran `pytest tests/unit` against
    # a path that did not exist -- pytest exits 4 on an unrecognised target, so the
    # gate failed on a usage error rather than a test. Empty but present is the state
    # the manifest already claims; git needs the .gitkeep to carry it.
    unit_tests.mkdir(parents=True, exist_ok=True)
    (unit_tests / ".gitkeep").write_text("", encoding="utf-8")
    # `logs/` is gitignored but must exist: the diagnostic runners write their
    # artifacts there, and a missing directory turns a test failure into a
    # confusing FileNotFoundError from the runner itself.
    (plan.root / "logs").mkdir(exist_ok=True)
    (plan.root / "logs" / ".gitkeep").write_text("", encoding="utf-8")


def scripted_setup_env() -> dict[str, str]:
    """The ambient environment, with devkit's branch policy waived.

    The generator seeds a repo and pushes its default branch, which is precisely the
    "scripted repo setup" case that policy's `DEVKIT_SKIP_BRANCH_POLICY` exists for.
    Without it, the policy's pre-push hook blocks `gh repo create --push` and the
    generator dies *after* creating the private GitHub repo — leaving an empty repo
    upstream and a local checkout that has to be pushed by hand.

    The hatch waives the branch checks only. The project's own pre-commit gate still
    runs on the initial commit, which is what verifies the vendored harness there.
    """
    return {**os.environ, "DEVKIT_SKIP_BRANCH_POLICY": "1"}


def run(cmd: list[str], cwd: Path, dry_run: bool, check: bool = True) -> int:
    """Run a command, or print it under `--dry-run`."""
    if dry_run:
        print(f"  run     {' '.join(cmd)}    (in {cwd})")
        return 0
    result = subprocess.run(cmd, cwd=cwd, env=scripted_setup_env())
    if check and result.returncode != 0:
        raise GeneratorError(f"command failed ({result.returncode}): {' '.join(cmd)}")
    return result.returncode


def vendor_harness(plan: Plan, dry_run: bool) -> None:
    """Copy devkit's MANIFEST into the new repo and stamp DEVKIT_VERSION.

    Done by invoking the project's own freshly-vendored `sync-devkit.py`, not by
    reimplementing the copy — so the generator can never disagree with the tool that
    drift-checks the result.
    """
    bootstrap = plan.root / "scripts" / "sync-devkit.py"
    if not dry_run:
        bootstrap.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(DEVKIT_ROOT / "scripts" / "sync-devkit.py", bootstrap)
    else:
        print("  write   scripts/sync-devkit.py    (bootstrap copy)")
    run(
        [
            sys.executable,
            "scripts/sync-devkit.py",
            "--pull",
            "--src",
            str(DEVKIT_ROOT),
            # Generating is not adopting a release, so `--pull`'s release guards do
            # not apply here. They exist to stop an *existing* consumer vendoring
            # from an unreleased revision it can never pin. This project has no pins
            # yet: the generator renders both from `latest_devkit_tag() or
            # FALLBACK_DEVKIT_REF`, which is the tagged ref, not this checkout's HEAD.
            # Refusing here would mean devkit could not generate a project at all
            # while it had uncommitted work — which is most of the time.
            "--allow-dirty",
            "--allow-untagged",
        ],
        plan.root,
        dry_run,
    )
    # Codex reads project instructions from CLAUDE.md through its configured fallback,
    # but repository skills and hooks still use Codex-specific paths. Runs after the
    # pull because the compatibility script arrives with the vendored harness.
    run([sys.executable, "scripts/sync-codex-context.py"], plan.root, dry_run, check=False)


def lock_dependencies(plan: Plan, dry_run: bool) -> None:
    """Generate `uv.lock` so the project starts reproducible.

    Best-effort by design: locking resolves against PyPI, so it needs uv installed
    and a network. When it cannot run, the project is still complete and valid --
    every consumer of the lock (`uv sync`, the Dockerfile's `uv.lock*` glob,
    session-start.sh's detection) degrades to resolving fresh. So a failure here
    prints what to run and continues rather than aborting a scaffold that is
    otherwise finished.

    Runs before `git_init` so the lock lands in the initial commit.
    """
    if dry_run:
        print("  run     uv lock    (in the new project)")
        return
    if shutil.which("uv") is None:
        print("  skip    uv lock -- uv is not installed")
        print("          The project has no uv.lock; run `uv lock` in it to add one.")
        return
    result = subprocess.run(["uv", "lock"], cwd=plan.root, capture_output=True, text=True)
    if result.returncode != 0:
        tail = (result.stderr or result.stdout).strip().splitlines()[-3:]
        print("  warn    uv lock failed -- continuing without a lockfile")
        for line in tail:
            print(f"          {line}")
        return
    print("  write   uv.lock")


def git_init(plan: Plan, dry_run: bool) -> None:
    run(["git", "init", "-b", str(plan.context["default_branch"])], plan.root, dry_run)
    run(["git", "add", "-A"], plan.root, dry_run)
    run(
        ["git", "commit", "-m", f"Generate {plan.name} from devkit's project template"],
        plan.root,
        dry_run,
    )


def add_worktree(plan: Plan, dry_run: bool) -> None:
    """The parallel checkout, with its own offset ports written into its `.env`."""
    if not plan.worktree:
        return
    target = plan.root.parent / plan.worktree
    run(["git", "worktree", "add", "-b", plan.worktree, str(target)], plan.root, dry_run)

    lines = [
        "# Worktree overrides. Copied from the primary checkout's .env, with the",
        "# ports replaced by this checkout's slot allocation (devkit ports.toml).",
        "# COMPOSE_PROJECT_NAME must stay equal to this directory's name.",
        *(f"{k}={v}" for k, v in sorted(plan.worktree_env.items())),
    ]
    if dry_run:
        print(f"  write   ../{plan.worktree}/.env")
        for line in lines[3:]:
            print(f"            {line}")
        return
    (target / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def register_in_workspace(plan: Plan, dry_run: bool) -> None:
    """Add the project (and its worktree) to `alex-projects.code-workspace`.

    This used to write a *new* `<name>.code-workspace` per project, to keep the task
    quick-pick free of every other repo's tasks. Two things made that wrong. The task
    noise it avoided is gone — the generic tasks are defined once at workspace level
    now and the per-project files hold only their own — and, more seriously, the new
    project was never added to the shared workspace at all. `sweep.py` reads that one
    file's `folders` as its registry, so a freshly scaffolded project was invisible to
    every sweep and could strand work with nothing reporting it. That matters more
    now than it did then: the sweep has no workspace task left, so the only readers
    are automatic ones -- `workspace-status.py`'s session-start line and reconcile's
    checkout pass -- and an unregistered project is invisible to both.

    Registration is best-effort by design: the project itself is already written and
    usable at this point, so a workspace file that has been restructured out of
    recognition is a warning to fix by hand, not a reason to fail the run.

    `--no-register` is for a project that is generated to be *thrown away* — the
    RELEASING.md acceptance test renders one per release purely to prove the tag can
    serve its pre-commit hook ids. Registering that probe edits the real workspace
    file: it lands in `folders` and in every scope picker, which puts a directory
    under a temp path into `sweep.py`'s registry, and it survives the `rm -rf` that
    ends the acceptance test.
    """
    if not plan.register:
        print("  skip    workspace registration (--no-register)")
        return
    path = devkit_project.DEFAULT_WORKSPACE
    names = [plan.name] + ([plan.worktree] if plan.worktree else [])
    if dry_run:
        print(f"  update  ../{path.name} (folders + project picker: {', '.join(names)})")
        return
    try:
        text = path.read_text(encoding="utf-8")
        path.write_text(devkit_project.register(text, names), encoding="utf-8", newline="\n")
    except (OSError, devkit_project.RegistryEditError) as exc:
        print(f"  WARNING could not register in {path.name}: {exc}")
        print(f"          add {', '.join(names)} to its folders list and project picker by hand")
        return
    print(f"  update  ../{path.name} (folders + project picker)")


def create_remote(plan: Plan, dry_run: bool) -> None:
    """Create the PRIVATE GitHub repo and push. The only outward-facing step."""
    if not plan.remote:
        print("  skip    GitHub repo (--no-remote)")
        return
    owner = plan.context["github_owner"]
    run(
        [
            "gh",
            "repo",
            "create",
            f"{owner}/{plan.name}",
            "--private",
            "--source=.",
            "--remote=origin",
            "--push",
        ],
        plan.root,
        dry_run,
    )


def ref_publishes_pre_commit_hooks(ref: str, root: Path | None = None) -> bool | None:
    """Does `ref` contain `.pre-commit-hooks.yaml`? None when git cannot answer.

    The generated `.pre-commit-config.yaml` pins this ref for devkit's published hooks, and
    pre-commit resolves hook ids strictly: if the tag predates the channel, the consumer's
    first `pre-commit run` aborts with "hook not found" rather than skipping. Unlike the
    devkit-drift pin, there is no partial-credit failure mode, so it is worth its own
    check.
    """
    devkit = root or DEVKIT_ROOT

    def _git(*args: str) -> int | None:
        try:
            return subprocess.run(
                ["git", "-C", str(devkit), *args], capture_output=True, timeout=10
            ).returncode
        except (OSError, subprocess.TimeoutExpired):
            return None

    # Resolve the ref first. Without this, an unknown tag and a tag that predates the
    # channel both come back "absent", and the caller would print a warning about a
    # missing file when the real problem is a ref that does not exist.
    resolved = _git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if resolved is None or resolved != 0:
        return None
    present = _git("cat-file", "-e", f"{ref}:.pre-commit-hooks.yaml")
    return None if present is None else present == 0


def _warn_if_pre_commit_channel_is_unpublished(ref: str) -> None:
    """Warn when the pinned ref cannot serve the pre-commit hooks the config asks for."""
    published = ref_publishes_pre_commit_hooks(ref)
    if published is None:
        print(f"  NOTE: could not check whether {ref} publishes devkit's pre-commit hooks.\n")
        return
    if published:
        return
    print(f"  WARNING: {ref} does not contain .pre-commit-hooks.yaml.")
    print(
        "        The generated .pre-commit-config.yaml pins that ref for devkit's hooks,\n"
        "        and pre-commit fails hard on an unknown hook id — so the first commit in\n"
        "        the new repo will abort, not degrade. Fix by tagging devkit\n"
        "        (git tag -a vX.Y.Z -m ... && git push --tags) and re-running, or by\n"
        "        removing the devkit block from the generated config until you do.\n"
    )


def _warn_if_pin_is_stale(ref: str) -> None:
    """Say so loudly when the pinned tag predates the harness being vendored.

    The generated project vendors from devkit's working tree but its `devkit-drift`
    job compares against `ref`. If those differ the job fails on the first PR, with a
    message about drift that points at the consuming repo rather than at the missing
    tag. Warn, don't block: generating against an untagged devkit is a legitimate
    thing to do while iterating.
    """
    differing = harness_files_matching_ref(ref)
    if differing is None:
        print(f"  NOTE: could not compare the vendored harness against {ref} (no git/tag?).")
        print("        Verify the generated PR gate's --devkit-ref before opening a PR.\n")
        return
    if not differing:
        return
    print(f"  WARNING: {len(differing)} vendored harness file(s) differ from {ref}:")
    for rel in differing:
        print(f"             {rel}")
    print(
        "        The generated PR gate pins that tag and drift-checks against it, so its\n"
        "        devkit-drift job WILL FAIL on the first PR. Fix by tagging devkit\n"
        "        (git tag -a vX.Y.Z -m ... && git push --tags) and re-running, or pass\n"
        "        --devkit-ref <tag> explicitly.\n"
    )


def register_slot_hint(plan: Plan) -> None:
    """Tell the user what to add to ports.toml. Deliberately not automatic.

    The generator does not edit devkit's registry itself: devkit is a git repo with
    its own PR gate, and a tool that silently commits to its own source of truth is
    how two projects end up claiming one slot from two different sessions.
    """
    print("\nNext: register the slot(s) in devkit's ports.toml [slots] table:")
    print(f"  {plan.name} = {plan.context['slot']}")
    if plan.worktree:
        print(f"  {plan.worktree} = {plan.context['worktree_slot']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("name", help="project name (directory, package, COMPOSE_PROJECT_NAME)")
    parser.add_argument("--description", default="", help="one-line description")
    parser.add_argument("--display-name", default="", help="human title (default: from name)")
    parser.add_argument(
        "--parent",
        default=str(Path.home() / "Desktop" / "vs_code"),
        help="directory to create the project in",
    )
    parser.add_argument("--github-owner", default="alexandrec90")
    parser.add_argument("--python-version", default="3.12")
    parser.add_argument("--default-branch", default="main")
    parser.add_argument(
        "--devkit-ref",
        default=None,
        help="devkit tag the generated PR gate pins (default: devkit's newest tag)",
    )
    parser.add_argument("--db-url-scheme", default="postgresql+psycopg")
    parser.add_argument(
        "--src-layout", action="store_true", help="use src/<pkg>/ instead of <pkg>/"
    )
    parser.add_argument(
        "--preset",
        choices=sorted(PRESETS),
        help="a named feature bundle; individual --with-* flags are added on top",
    )

    for feature, help_text in [
        ("docker", "docker-compose stack + Dockerfile + .dockerignore"),
        ("app-service", "an `app` service in the compose stack (implies --with-docker)"),
        ("postgres", "a Postgres service, DATABASE_URL, and the harness DB test tier"),
        ("redis", "a Redis service and REDIS_URL"),
        ("alembic", "alembic.ini and the migration tasks (implies --with-postgres)"),
        ("frontend", "a Vite frontend service and its npm tasks"),
        ("archive", "the data-lake archive seam and its rule file"),
    ]:
        parser.add_argument(
            f"--with-{feature}",
            dest=feature.replace("-", "_"),
            action="store_true",
            help=help_text,
        )

    parser.add_argument("--no-worktree", dest="worktree", action="store_false", default=True)
    # Same reason as --dry-run below: a VS Code picker must supply one real token.
    remote = parser.add_mutually_exclusive_group()
    remote.add_argument(
        "--remote",
        dest="remote",
        action="store_true",
        default=True,
        help="create and push to the GitHub repo (the default)",
    )
    remote.add_argument(
        "--no-remote",
        dest="remote",
        action="store_false",
        help="skip creating and pushing to the GitHub repo",
    )
    # Same picker rule again, and the same reason `--remote` exists alongside its
    # negation: one real token in every branch.
    register = parser.add_mutually_exclusive_group()
    register.add_argument(
        "--register",
        dest="register",
        action="store_true",
        default=True,
        help="add the project to the shared workspace file (the default)",
    )
    register.add_argument(
        "--no-register",
        dest="register",
        action="store_false",
        help="leave the shared workspace file alone (for a throwaway probe)",
    )
    # `--dry-run` is redundant with the default, and exists anyway: the VS Code task
    # picks one of these two strings, and passing "" as an argument instead would
    # make argparse reject it as a stray positional.
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="print the plan without writing anything (the default)",
    )
    mode.add_argument("--yes", dest="dry_run", action="store_false", help="actually apply the plan")
    args = parser.parse_args(argv)

    # Resolve the tag the generated PR gate will pin before anything is rendered.
    if args.devkit_ref is None:
        args.devkit_ref = latest_devkit_tag() or FALLBACK_DEVKIT_REF

    # A preset turns its features on; explicit --with-* flags add to it, never
    # subtract, so `--preset data --with-redis` does what it reads like.
    if args.preset:
        for feature in PRESETS[args.preset]:
            setattr(args, feature, True)

    # Implied features, resolved once so templates never re-derive them.
    if args.alembic:
        args.postgres = True
    if args.app_service or args.postgres or args.redis or args.frontend:
        args.docker = True

    try:
        registry = devkit_ports.load(DEVKIT_ROOT)
        the_plan = plan(args, registry)

        # Not `mode`: that name is already bound to the argparse mutually-exclusive
        # group above, and rebinding it here shadows the group with a str.
        run_mode = "DRY RUN — nothing will be written" if args.dry_run else "generating"
        print(f"devkit new-project: {run_mode}")
        print(f"  project   {the_plan.name}  ->  {the_plan.root}")
        print(f"  slot      {the_plan.context['slot']}")
        print(f"  features  {', '.join(f for f in FEATURES if the_plan.context[f]) or '(none)'}")
        if the_plan.worktree:
            print(f"  worktree  {the_plan.worktree} (slot {the_plan.context['worktree_slot']})")
        print(f"  remote    {'yes' if the_plan.remote else 'no'}")
        print(f"  devkit    {the_plan.context['devkit_ref']} (pinned by the generated PR gate)")
        print()
        _warn_if_pin_is_stale(str(the_plan.context["devkit_ref"]))
        _warn_if_pre_commit_channel_is_unpublished(str(the_plan.context["devkit_ref"]))

        if not args.dry_run:
            the_plan.root.mkdir(parents=True, exist_ok=True)
        render_tree(the_plan, args.dry_run)
        write_package(the_plan, args.dry_run)
        vendor_harness(the_plan, args.dry_run)
        lock_dependencies(the_plan, args.dry_run)
        git_init(the_plan, args.dry_run)
        add_worktree(the_plan, args.dry_run)
        register_in_workspace(the_plan, args.dry_run)
        create_remote(the_plan, args.dry_run)
    except (GeneratorError, devkit_ports.RegistryError) as exc:
        print(f"\nnew-project: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        print("\nDry run complete. Re-run with --yes to apply.")
    else:
        print(f"\nDone: {the_plan.root}")
    register_slot_hint(the_plan)
    return 0


if __name__ == "__main__":
    sys.exit(main())
