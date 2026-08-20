#!/usr/bin/env python3
"""pre-commit hook: validate a project's `.devkit.toml`.

**Why a hook and not just a test.** `harness_config.load()` never raises, by design — a
hook script must not die over config, so a missing, unparseable or half-filled manifest
degrades silently to `Config()` neutral defaults. That is the right runtime behaviour and
a terrible authoring experience: a stray bracket does not fail anything, it just quietly
turns the DB tier off and re-points `app_dir` at a directory this project may not have.
Nothing downstream complains; the Stop hook simply stops running half its checks. This
hook is where that gets caught, at the moment the manifest is edited.

The checks are the ones whose absence has actually cost something:

  - It parses as TOML *before* the loader's fallback can hide the fact.
  - `app`/`tests` end in `/`. `stop.py` selects checks with `path.startswith(app_dir)`,
    so a missing slash makes `apps/` match `app/` and silently widens test selection.
  - The declared directories exist **in this repo**. devkit itself shipped a manifest
    describing a different project (an `app/` tree, a Postgres tier, a frontend, none of
    which existed here) for as long as nothing read it.
  - A `[db]`/`[frontend]` block that is switched on is filled in. Half a DB block yields
    `postgresql://:@host:5432/`, which fails at connect time with a message that points
    nowhere.

Usage (pre-commit passes the filenames):
    python scripts/precommit/check_harness_manifest.py .devkit.toml

stdlib only, like everything else that runs before a virtualenv exists.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loader import load_by_path

DEVKIT_ROOT = Path(__file__).resolve().parents[2]

# An interpreter request uv could resolve: a version, optionally an implementation
# ("pypy@3.10") or an explicit path. Deliberately loose — the point is to reject a value
# no spelling could be (a stray space, an empty quote, prose), not to re-implement uv's
# parser and start failing commits over a form it added later.
_VERSION_REQUEST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.@:_/\\+-]*")


def _load_harness_config():
    """Import the sibling `scripts/hooks/harness_config.py` by path.

    By path rather than by name: this script runs with the *consuming* repo as the cwd
    (pre-commit's working directory), so `scripts/hooks/` is not importable, and when the
    hook is consumed from a pinned devkit rev the loader lives in pre-commit's clone
    rather than anywhere near the files being checked.
    """
    return load_by_path("_harness_config", DEVKIT_ROOT / "scripts" / "hooks" / "harness_config.py")


def check(manifest_path: Path) -> list[str]:
    """Return a list of human-readable problems; empty means the manifest is sound."""
    problems: list[str] = []
    root = manifest_path.parent

    try:
        raw = manifest_path.read_bytes()
    except OSError as exc:
        return [f"{manifest_path}: cannot be read ({exc})"]

    # Parse explicitly. `harness_config.load()` would swallow a syntax error and hand
    # back defaults, which is exactly the silence this hook exists to break.
    try:
        tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return [
            f"{manifest_path}: not valid TOML ({exc}). The hooks would silently fall "
            f"back to neutral defaults rather than fail, so nothing else would tell you."
        ]

    cfg = _load_harness_config().load(root)

    # A blank prefix makes every control var `_SKIP_STOP_VERIFY`, which collides across
    # projects sharing one shell.
    if not cfg.env_prefix:
        problems.append("[project] env_prefix is empty")
    elif not cfg.env_prefix.isupper():
        problems.append(f"[project] env_prefix {cfg.env_prefix!r} should be UPPER_CASE")

    for label, value in (("app", cfg.app_dir), ("tests", cfg.tests_dir)):
        if not value.endswith("/"):
            problems.append(
                f"[paths] {label} = {value!r} must end with '/' — the hooks match it as a "
                f"path prefix, so without it a sibling directory can match too"
            )
        if not (root / value).is_dir():
            problems.append(f"[paths] {label} = {value!r} is not a directory in this repo")
    if not cfg.unit_tests:
        problems.append("[paths] unit_tests is empty")
    elif not (root / cfg.unit_tests).is_dir():
        problems.append(f"[paths] unit_tests = {cfg.unit_tests!r} is not a directory in this repo")

    # `[python] version` is handed to `uv venv --python` / `uv sync --python` as argv.
    # uv accepts several request spellings ("3.12", "3.12.7", "pypy@3.10"), so this only
    # rejects what none of them can be. It matters because a box's provisioning failure
    # is a warning, not an error: a typo here yields a box with no .venv that still
    # reports itself provisioned, and the missing toolchain surfaces much later.
    if cfg.python.version and not _VERSION_REQUEST.fullmatch(cfg.python.version):
        problems.append(
            f"[python] version = {cfg.python.version!r} is not an interpreter uv can be "
            f"asked for — expected something like '3.12' or '3.12.7'"
        )

    if cfg.db.enabled:
        for field in ("user", "password", "name", "url_scheme"):
            if not getattr(cfg.db, field):
                problems.append(f"[db] enabled but {field} is empty")
        if not cfg.db.url_env:
            problems.append("[db] enabled but url_env lists no environment variable")
        if cfg.db.db_service not in cfg.db.services:
            problems.append(
                f"[db] db_service = {cfg.db.db_service!r} is not in services "
                f"{list(cfg.db.services)!r}, so readiness is never checked for it"
            )

    if cfg.frontend.enabled:
        # Deliberately NOT checking that `dir` exists, unlike `[paths]` above. The two are
        # consumed differently: `[paths]` values are handed straight to pytest and ruff as
        # targets, where a missing directory is a hard error, while every consumer of
        # `[frontend] dir` guards on it first — `session-start.sh` tests `[ -d ]` before
        # npm install, and `stop.py` gates the vitest tier on a changed path under `src`.
        # A project that declares its frontend tier before scaffolding the directory (the
        # `fullstack` preset does exactly that; devkit ships no frontend scaffold) is in a
        # legitimate intermediate state, and failing its commits for it would be wrong.
        if not cfg.frontend.dir:
            problems.append("[frontend] enabled but dir is empty")
        if not cfg.frontend.src.endswith("/"):
            problems.append(
                f"[frontend] src = {cfg.frontend.src!r} must end with '/' (prefix match)"
            )
        if not cfg.frontend.test_cmd:
            problems.append("[frontend] enabled but test_cmd is empty")

    return problems


def main(argv: list[str] | None = None) -> int:
    paths = [Path(a) for a in (argv if argv is not None else sys.argv[1:])]
    if not paths:
        return 0
    failed = False
    for path in paths:
        problems = check(path)
        if problems:
            failed = True
            print(f"{path}: {len(problems)} problem(s)")
            for problem in problems:
                print(f"  - {problem}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
