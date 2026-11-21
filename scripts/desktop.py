#!/usr/bin/env python3
"""Lint, test, pack and publish a devkit desktop app -- the one recipe they all share.

A desktop project is a .NET solution shipped to users as an installer that updates
itself: Velopack's `vpk` packs the published app into a `Setup.exe` plus the release
files an installed copy polls, and GitHub Releases is where both live. Every step of
that is the same in every such project, so it lives here, vendored through the gated
tier to each project whose `.devkit.toml` says `[desktop] enabled = true`, and a fix to
it reaches all of them on their next `sync-devkit.py --pull`. What differs per project
is read from that `[desktop]` table; nothing here names one.

    python scripts/desktop.py lint             # dotnet format (fixing), then a build
    python scripts/desktop.py lint --check     # what CI runs: format may change nothing
    python scripts/desktop.py test             # dotnet test over the solution
    python scripts/desktop.py release --version v1.2.3            # pack into Releases/
    python scripts/desktop.py release --version v1.2.3 --publish  # ...and upload

**The build is the type check.** Warnings are errors in a generated project's
`Directory.Build.props`, so `lint` fails on a nullable dereference the way `mypy` fails
on a Python one; that is why it builds rather than only formatting.

**A release is idempotent per tag.** `--publish` downloads the previous release first
so `vpk` can build a delta, then uploads the installer, the full package and the delta
as the GitHub release for `v<version>`. The installed app's updater reads that feed:
an install only accepts an update whose package id matches its own, which is why
`[desktop] pack_id` must never change after the first release.

**Signing is opt-in from the environment.** `VPK_SIGN_PARAMS` is handed to
`vpk pack --signParams` verbatim (signtool's arguments); unset, the installer is
unsigned and Windows SmartScreen warns whoever downloads it. Nothing here can buy the
certificate.

Each verb writes `logs/desktop-<verb>.log` on every run, empty on success, per the
failure-artifact rule. Stdlib only: it runs in a release job before any project
environment exists.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
LOGS = REPO_ROOT / "logs"
HARNESS_CONFIG = Path("scripts") / "hooks" / "harness_config.py"
# Where `release` leaves what it built; the project's `.gitignore` keeps both out.
PUBLISH_DIR = "publish"
RELEASES_DIR = "Releases"
# The tail of a failed command the artifact keeps: dotnet's summary is at the end.
TAIL_LINES = 120
SIGN_ENV = "VPK_SIGN_PARAMS"
TOKEN_ENV = "GITHUB_TOKEN"  # noqa: S105 - the variable's name, not a credential
# A release version is SemVer: Velopack orders releases by it, and an installed copy
# compares against it to decide whether an update is newer.
SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
VELOPACK_REF = re.compile(
    r"""<PackageReference\s+Include\s*=\s*["']Velopack["']\s+Version\s*=\s*["']([^"']+)["']""",
    re.IGNORECASE,
)
ASSEMBLY_NAME = re.compile(r"<AssemblyName>\s*([^<\s]+)\s*</AssemblyName>", re.IGNORECASE)
GITHUB_REMOTE = re.compile(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")


class DesktopError(RuntimeError):
    """A precondition failed. The message is written for a human, not a traceback."""


@dataclass(frozen=True)
class Desktop:
    """The `[desktop]` table, with every derived name resolved."""

    solution: str
    project: str
    pack_id: str
    pack_title: str
    runtime: str


def load_settings(root: Path) -> Desktop:
    """The project's `[desktop]` table, read through its vendored `harness_config.py`.

    Refuses rather than defaulting: every verb here acts on a solution, and guessing one
    would build -- or worse, publish -- the wrong thing.
    """
    raw = _desktop_table(root)
    if raw is None or not raw.enabled:
        raise DesktopError("`.devkit.toml` does not turn the [desktop] tier on")
    if not raw.solution or not raw.project:
        raise DesktopError("[desktop] needs both `solution` and `project` in `.devkit.toml`")
    pack_id = raw.pack_id or Path(raw.project).stem
    return Desktop(
        solution=raw.solution,
        project=raw.project,
        pack_id=pack_id,
        pack_title=raw.pack_title or pack_id,
        runtime=raw.runtime or "win-x64",
    )


def _desktop_table(root: Path) -> Any | None:
    path = root / HARNESS_CONFIG
    spec = importlib.util.spec_from_file_location("_desktop_harness_config", path)
    if spec is None or spec.loader is None or not path.is_file():
        return None
    module = importlib.util.module_from_spec(spec)
    # Registered before it runs: `@dataclass` looks its defining module up by name.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        return module.load(root).desktop
    except (ImportError, OSError, AttributeError):
        return None


def release_version(tag: str) -> str:
    """`v1.2.3` -> `1.2.3`. Anything that is not SemVer is refused, not passed on."""
    version = tag.strip().removeprefix("refs/tags/").removeprefix("v")
    if not SEMVER.match(version):
        raise DesktopError(
            f"{tag!r} is not a release version: tag releases as v<major>.<minor>.<patch>"
        )
    return version


def velopack_version(project_text: str) -> str:
    """The Velopack package the app references, "" when it references none.

    `vpk` is installed at this version so the packer and the in-app updater agree on
    the release format. An app without the updater still packs an installer, with
    whatever `vpk` is current.
    """
    match = VELOPACK_REF.search(project_text)
    return match.group(1) if match else ""


def main_exe(project: str, project_text: str) -> str:
    """The executable `dotnet publish` produces: `<AssemblyName>.exe`, else the project's."""
    match = ASSEMBLY_NAME.search(project_text)
    return f"{match.group(1) if match else Path(project).stem}.exe"


def repo_url(env: dict[str, str], remote: str) -> str:
    """`https://github.com/<owner>/<repo>` for the release feed.

    `GITHUB_REPOSITORY` in a workflow; the `origin` remote elsewhere, in either the
    HTTPS or the SSH spelling.
    """
    if slug := env.get("GITHUB_REPOSITORY", "").strip():
        return f"https://github.com/{slug}"
    match = GITHUB_REMOTE.search(remote.strip())
    if not match:
        raise DesktopError(f"cannot tell the GitHub repository from origin {remote!r}")
    return f"https://github.com/{match.group(1)}/{match.group(2)}"


def lint_commands(cfg: Desktop, check: bool) -> list[list[str]]:
    """Format (fixing, or verifying under `check`), then build with warnings as errors."""
    fmt = ["dotnet", "format", cfg.solution, *(["--verify-no-changes"] if check else [])]
    return [fmt, ["dotnet", "build", cfg.solution, "-c", "Release", "-warnaserror", "--nologo"]]


def test_commands(cfg: Desktop) -> list[list[str]]:
    return [["dotnet", "test", cfg.solution, "-c", "Release", "--nologo"]]


def publish_command(cfg: Desktop, version: str, out: str) -> list[str]:
    """Self-contained, so the installer needs no .NET on the user's machine."""
    return [
        *("dotnet", "publish", cfg.project, "-c", "Release"),
        *("-r", cfg.runtime, "--self-contained", "true"),
        *("-o", out, f"-p:Version={version}", "--nologo"),
    ]


def pack_command(vpk: str, cfg: Desktop, version: str, exe: str, sign: str = "") -> list[str]:
    command = [
        *(vpk, "pack", "--packId", cfg.pack_id, "--packVersion", version),
        *("--packDir", PUBLISH_DIR, "--mainExe", exe, "--packTitle", cfg.pack_title),
        *("--outputDir", RELEASES_DIR),
    ]
    return [*command, "--signParams", sign] if sign else command


def download_command(vpk: str, url: str, token: str) -> list[str]:
    """The previous release, so `pack` can build a delta. Nothing to fetch on the first."""
    return [vpk, "download", "github", "--repoUrl", url, "--token", token, "-o", RELEASES_DIR]


def upload_command(vpk: str, url: str, token: str, cfg: Desktop, version: str) -> list[str]:
    return [
        *(vpk, "upload", "github", "--repoUrl", url, "--token", token),
        *("-o", RELEASES_DIR, "--publish", "--tag", f"v{version}"),
        *("--releaseName", f"{cfg.pack_title} {version}"),
    ]


def redact(text: str, mask: str) -> str:
    """`text` with every occurrence of `mask` hidden: the token never reaches a log."""
    return text.replace(mask, "***") if mask else text


def tail(text: str, lines: int = TAIL_LINES) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def run_all(
    commands: list[list[str]],
    cwd: Path,
    mask: str = "",
    run: Any = subprocess.run,
    env: dict[str, str] | None = None,
) -> str:
    """Run `commands` in order, stopping at the first failure; that failure's text, or "".

    `mask` is hidden wherever a command line or its output is printed or returned.
    """
    for command in commands:
        shown = redact(" ".join(command), mask)
        print(f"desktop: {shown}", flush=True)
        try:
            result = run(
                command,
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            return f"# {shown}\n# could not start: {exc}\n"
        if result.returncode != 0:
            body = tail((result.stdout or "") + (result.stderr or ""))
            return f"# {shown}\n# exit {result.returncode}\n{redact(body, mask)}\n"
    return ""


def vpk_env(base: dict[str, str]) -> dict[str, str]:
    """`base` with `vpk` allowed onto a newer .NET than the one it was built for.

    A `vpk` release targets the runtime current when it shipped, and the SDK `global.json`
    pins carries only its own: without the roll-forward, `vpk` refuses to start on a
    machine that has exactly the SDK the project asks for.
    """
    return {**base, "DOTNET_ROLL_FORWARD": "Major"}


def install_vpk(tool_dir: Path, version: str, run: Any = subprocess.run) -> str:
    """`vpk` installed into `tool_dir`, never globally, at `version` when one is known."""
    command = ["dotnet", "tool", "install", "vpk", "--tool-path", str(tool_dir)]
    failure = run_all([command + (["--version", version] if version else [])], REPO_ROOT, run=run)
    if failure:
        raise DesktopError(f"could not install vpk:\n{failure}")
    found = shutil.which("vpk", path=str(tool_dir))
    if not found:
        raise DesktopError(f"vpk installed but not found in {tool_dir}")
    return found


def git_remote(root: Path) -> str:
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"], cwd=root, capture_output=True, text=True
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def release(cfg: Desktop, tag: str, publish: bool, root: Path = REPO_ROOT) -> str:
    """Publish, pack and (with `publish`) upload `tag`; the failure's text, or ""."""
    version = release_version(tag)
    text = (root / cfg.project).read_text(encoding="utf-8")
    token = os.environ.get(TOKEN_ENV, "")
    if publish and not token:
        raise DesktopError(f"--publish needs ${TOKEN_ENV}: the upload is a GitHub release")
    url = repo_url(dict(os.environ), git_remote(root)) if publish else ""
    for stale in (PUBLISH_DIR, RELEASES_DIR):
        shutil.rmtree(root / stale, ignore_errors=True)
    with tempfile.TemporaryDirectory(prefix="vpk-") as tools:
        vpk = install_vpk(Path(tools), velopack_version(text))
        if failure := run_all([publish_command(cfg, version, PUBLISH_DIR)], root):
            return failure
        env = vpk_env(dict(os.environ))
        if publish:
            # Best effort: the first release has no predecessor, and a missing delta
            # costs users a full download, not a broken update.
            run_all([download_command(vpk, url, token)], root, mask=token, env=env)
        sign = os.environ.get(SIGN_ENV, "")
        if not sign:
            print(f"desktop: ${SIGN_ENV} is unset -- the installer will be unsigned")
        steps = [pack_command(vpk, cfg, version, main_exe(cfg.project, text), sign)]
        if publish:
            steps.append(upload_command(vpk, url, token, cfg, version))
        return run_all(steps, root, mask=token, env=env)


def write_artifact(verb: str, failure: str) -> Path:
    """`logs/desktop-<verb>.log`, overwritten on every run and empty on a pass."""
    LOGS.mkdir(parents=True, exist_ok=True)
    path = LOGS / f"desktop-{verb}.log"
    header = f"# source: scripts/desktop.py {verb}\n" if failure else ""
    path.write_text(header + failure, encoding="utf-8")
    return path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    verbs = parser.add_subparsers(dest="verb", required=True)
    lint = verbs.add_parser("lint", help="format, then build with warnings as errors")
    lint.add_argument("--check", action="store_true", help="fail on a format change; fix none")
    verbs.add_parser("test", help="dotnet test over the solution")
    rel = verbs.add_parser("release", help="publish and pack a release, optionally upload it")
    rel.add_argument("--version", required=True, help="the release tag, e.g. v1.2.3")
    rel.add_argument("--publish", action="store_true", help="upload it as a GitHub release")
    return parser


def dispatch(args: argparse.Namespace, root: Path = REPO_ROOT) -> str:
    """The failure text for one verb, "" on success. Raises `DesktopError` on a refusal."""
    cfg = load_settings(root)
    if not shutil.which("dotnet"):
        raise DesktopError("`dotnet` is not on PATH: install the SDK `global.json` names")
    if args.verb == "lint":
        return run_all(lint_commands(cfg, args.check), root)
    if args.verb == "test":
        return run_all(test_commands(cfg), root)
    return release(cfg, args.version, args.publish, root)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        failure = dispatch(args)
    except DesktopError as exc:
        failure = f"# refused\n{exc}\n"
    path = write_artifact(args.verb, failure)
    if failure:
        print(f"desktop: {args.verb} FAILED -- details in {path.relative_to(REPO_ROOT)}")
        return 1
    print(f"desktop: {args.verb} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
