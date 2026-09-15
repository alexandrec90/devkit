#!/usr/bin/env python3
"""Install Devkit's global pre-commit/pre-push branch policy.

The default is a read-only plan. Pass ``--yes`` to copy the runtime into a stable
user directory and configure Git globally. An unrelated existing ``core.hooksPath``
is preserved and causes a refusal rather than being overwritten.

``--check`` answers the question the install itself cannot: **is the runtime that
is actually enforcing the policy still the one in this checkout?** The install is a
*copy*, so the two drift the moment either moves, and nothing about a stale copy
looks wrong -- the hooks still fire, they just enforce an older policy. That is not
hypothetical: the runtime on the author's machine was installed from a
work-in-progress file roughly eighteen hours before that change was committed, so
the escape hatch (``DEVKIT_SKIP_BRANCH_POLICY``) silently did not exist there, and
the only symptom was an env var that appeared to do nothing.

The direction of the risk is what makes it worth a mode: a stale copy can be
missing a *loosening* (annoying) or a *tightening* (a policy everyone believes is
enforced and is not), and neither is visible anywhere.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TARGET = Path.home() / ".devkit" / "git-hooks"
RUNTIME_FILES = {
    "scripts/git_policy.py": "devkit_git_policy.py",
    "scripts/worktree_env.py": "devkit_worktree_env.py",
    "scripts/git-hooks/pre-commit": "pre-commit",
    "scripts/git-hooks/pre-push": "pre-push",
    "scripts/git-hooks/post-checkout": "post-checkout",
}
# Which of those git execs, and therefore which need the executable bit. Derived from the
# map rather than listed twice: a hook added to one and forgotten in the other installs
# as a plain file, and git skips a hook it cannot execute WITHOUT SAYING SO -- the same
# silence `worktree-guard-launch.py` was vendored into for a release.
HOOK_NAMES = frozenset(
    destination for source, destination in RUNTIME_FILES.items() if "/git-hooks/" in source
)
# Records what was installed and from where, beside the runtime it describes.
# Without it, "which policy is actually running?" can only be answered by diffing
# against a checkout -- which is a question about *this* machine that no artifact
# on this machine could answer.
RECEIPT_NAME = "installed.json"
# The sentinel `ref` for an install taken from the working tree rather than a
# commit. Recorded verbatim so a receipt never claims a provenance it does not have.
WORKTREE_REF = "worktree"

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class InstallRefusedError(RuntimeError):
    """The install would replace Git configuration not owned by Devkit."""


def run_command(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), capture_output=True, text=True, check=False)


def _generator():
    """`new-project.py`, loaded by path -- it owns which tag devkit pins to.

    Reused rather than reimplemented so the policy runtime and every generated
    project agree on what "the current devkit release" means, and so
    `FALLBACK_DEVKIT_REF` keeps the release-time test that is the only guard on
    that value. Hyphenated, hence the loader.
    """
    loader_dir = REPO_ROOT / "scripts" / "precommit"
    if str(loader_dir) not in sys.path:
        sys.path.insert(0, str(loader_dir))
    # Resolved by the sys.path insert above; `scripts/precommit/` is not an
    # importable package.
    from _loader import load_by_path

    return load_by_path("_new_project", REPO_ROOT / "scripts" / "new-project.py")


def resolve_ref(source_root: Path = REPO_ROOT) -> str:
    """The commit-ish to install from: devkit's newest tag, or the pinned fallback.

    A *tag*, not the working tree, and that is the whole point. The runtime this
    installs is what every repository on the machine enforces, and copying an
    uncommitted file into that position is how a policy came to be enforced that
    no commit contained -- for two days, with the source and the README both
    describing behaviour the running code did not have.
    """
    generator = _generator()
    return generator.latest_devkit_tag(source_root) or generator.FALLBACK_DEVKIT_REF


def read_blob(source_root: Path, ref: str, path: str, runner: Runner = run_command) -> bytes:
    """The bytes of `path` at `ref`. Raises `InstallRefusedError` if git will not say.

    Bytes rather than text: these files are copied verbatim into a position where
    a stray line-ending rewrite would change what the hook executes.
    """
    result = subprocess.run(
        ["git", "-C", str(source_root), "show", f"{ref}:{path}"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or b"").decode("utf-8", "replace").strip()
        raise InstallRefusedError(f"cannot read {path} at {ref}: {detail}")
    return result.stdout


def in_ref(source_root: Path, ref: str, path: str, runner: Runner = run_command) -> bool:
    """Whether `ref` carries `path` at all.

    Asked separately from `read_blob` because the two absences mean opposite things. A
    ref that cannot be read, or a path a ref was *expected* to have, is a refusal --
    that is `read_blob`'s job and it must stay loud. A path the ref simply predates is
    not an error: `RUNTIME_FILES` grows, releases do not move, and the newest tag is
    what `main()` installs from by default.
    """
    return (
        runner(["git", "-C", str(source_root), "cat-file", "-e", f"{ref}:{path}"]).returncode == 0
    )


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class Receipt:
    """What the installed runtime is, written beside it at install time.

    `files` maps installed name -> sha256 of the bytes written. Hashes rather than
    a bare version string so the check can run with no git and no network: the
    session-start status line has to answer "is this still what was installed?"
    without spawning anything.
    """

    ref: str
    installed_at: str
    files: dict[str, str]

    def to_json(self) -> str:
        return json.dumps(
            {"ref": self.ref, "installed_at": self.installed_at, "files": self.files},
            indent=2,
            sort_keys=True,
        )

    @classmethod
    def parse(cls, raw: str) -> Receipt | None:
        """A receipt from its JSON, or None for anything unreadable.

        Never raises: a corrupt receipt must degrade to "cannot tell", which the
        callers report, rather than taking down a session start.
        """
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        ref = payload.get("ref")
        installed_at = payload.get("installed_at", "")
        files = payload.get("files")
        if not isinstance(ref, str) or not ref or not isinstance(files, dict):
            return None
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in files.items()):
            return None
        return cls(ref=ref, installed_at=str(installed_at), files=files)


def read_receipt(target: Path) -> Receipt | None:
    """The receipt beside an installed runtime, or None when there is not one.

    None is a real answer, not an error: every runtime installed before receipts
    existed is in exactly this state, including the one that prompted them.
    """
    path = target / RECEIPT_NAME
    if not path.is_file():
        return None
    try:
        return Receipt.parse(path.read_text(encoding="utf-8"))
    except OSError:
        return None


def install_files(source_root: Path, target: Path, ref: str = WORKTREE_REF) -> dict[str, str]:
    """Write the runtime into `target` from `ref`; return installed name -> sha256.

    `ref` defaults to the working tree so that the explicit, auditable call is the
    one that reaches for uncommitted code -- and `main()` never makes it without
    `--from-worktree`.
    """
    target.mkdir(parents=True, exist_ok=True)
    hashes: dict[str, str] = {}
    skipped: list[str] = []
    for source_name, destination_name in RUNTIME_FILES.items():
        destination = target / destination_name
        if ref == WORKTREE_REF:
            shutil.copy2(source_root / source_name, destination)
        elif not in_ref(source_root, ref, source_name):
            # THE REF DECIDES THE RUNTIME. A file added to `RUNTIME_FILES` after `ref`
            # was cut is not part of that release, and demanding it would make every
            # install from the newest TAG refuse the moment this list grew -- which is
            # the default `main()` uses, and which `installers.py` re-runs nightly. So
            # the newer file is skipped and left out of the receipt, and `--check` is
            # judged against the receipt rather than against this list, or the skip
            # would report as drift forever and re-install forever.
            skipped.append(source_name)
            continue
        else:
            destination.write_bytes(read_blob(source_root, ref, source_name))
        hashes[destination_name] = digest(destination.read_bytes())
        if destination_name in HOOK_NAMES:
            destination.chmod(
                destination.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            )
    for source_name in skipped:
        print(f"install-git-policy: {source_name} is newer than {ref}; not installed")
    return hashes


def install(source_root: Path, target: Path, ref: str = WORKTREE_REF) -> Receipt:
    """Install the runtime and record what was installed, as one step.

    One function because a runtime without its receipt is the state this whole
    mechanism exists to remove: it is indistinguishable from a stale install, and
    the only way to identify it is the byte-diff that having a receipt avoids.
    """
    hashes = install_files(source_root, target, ref)
    receipt = Receipt(
        ref=ref,
        installed_at=_dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        files=hashes,
    )
    (target / RECEIPT_NAME).write_text(receipt.to_json() + "\n", encoding="utf-8")
    return receipt


def _configured_hooks_path(runner: Runner) -> str:
    result = runner(["git", "config", "--global", "--get", "core.hooksPath"])
    return result.stdout.strip() if result.returncode == 0 else ""


def ensure_compatible_hooks_path(target: Path, runner: Runner = run_command) -> None:
    configured = _configured_hooks_path(runner)
    if not configured:
        return
    target_value = target.resolve().as_posix()
    configured_path = Path(configured).expanduser()
    try:
        same = configured_path.resolve() == target.resolve()
    except OSError:
        same = configured.replace("\\", "/").rstrip("/") == target_value.rstrip("/")
    if not same:
        raise InstallRefusedError(
            f"global core.hooksPath is already '{configured}'; refusing to replace it"
        )


def _require_ok(result: subprocess.CompletedProcess[str], action: str) -> None:
    if result.returncode == 0:
        return
    detail = (result.stderr or result.stdout or "command failed").strip()
    raise InstallRefusedError(f"{action}: {detail}")


def configure_git(target: Path, runner: Runner = run_command) -> None:
    values = (
        ("core.hooksPath", target.resolve().as_posix()),
        ("fetch.prune", "true"),
        ("devkit.branchPolicy.failClosed", "true"),
    )
    for key, value in values:
        result = runner(["git", "config", "--global", key, value])
        _require_ok(result, f"could not configure {key}")


@dataclass(frozen=True)
class Drift:
    """One installed file that no longer matches the source it was copied from."""

    name: str
    reason: str


def compare_install(target: Path, receipt: Receipt | None) -> list[Drift]:
    """Installed files that are no longer what the receipt says was installed.

    Deliberately *not* a comparison against the working tree. The runtime is
    pinned to a released ref, so a checkout sitting ahead of that ref is the normal
    state and flagging it would make this warn constantly -- and a check that
    always warns is one nobody reads. Falling behind a release is a different
    question, answered by `behind_ref` against a tag rather than by bytes.

    Pure and filesystem-only -- no git, no network -- because the session-start
    status line calls it and may not spawn anything.
    """
    if receipt is None:
        # Every runtime installed before receipts existed lands here. It is not
        # provably stale, but it is unidentifiable, which needs the same fix.
        return [Drift(RECEIPT_NAME, "missing -- cannot tell what is installed")]
    drifted: list[Drift] = []
    # The RECEIPT, not `RUNTIME_FILES`: it records what the installed ref actually had,
    # so a file newer than that ref is not compared rather than reported missing every
    # night until the next release. See the skip in `install_runtime`.
    for destination_name in receipt.files:
        destination = target / destination_name
        expected = receipt.files.get(destination_name, "")
        if not destination.is_file():
            drifted.append(Drift(destination_name, "not installed"))
            continue
        if not expected:
            drifted.append(Drift(destination_name, "not recorded in the receipt"))
            continue
        try:
            actual = digest(destination.read_bytes())
        except OSError as error:
            # Unreadable is not "unchanged". Reporting it errs toward the answer
            # that makes someone look, which is the safe direction here.
            drifted.append(Drift(destination_name, f"unreadable ({error.strerror or error})"))
            continue
        if actual != expected:
            drifted.append(Drift(destination_name, "modified since it was installed"))
    return drifted


def worktree_drift(source_root: Path, target: Path, receipt: Receipt | None) -> list[Drift]:
    """For a working-tree install only: installed bytes that the checkout has moved past.

    The hole this fills. `compare_install` asks whether the install still matches its
    own receipt, and `behind_ref` asks whether a newer *tag* exists -- and `behind_ref`
    is silent for `WORKTREE_REF` on the reasoning that a working-tree install is
    already as current as it can be described. It is not. A worktree install has no ref
    to fall behind, so neither question has anything to say about it, and `--check`
    printed "up to date" over a dispatcher installed from a checkout that had since
    gained the whole pre-push wiring: `devkit-push-gate` had not run on any push for
    five days, and nothing on the machine reported it.

    So the working tree is exactly the right comparison *here*, for the reason
    `compare_install` gives for refusing it everywhere else -- there, the checkout
    sitting ahead of a pinned release is the normal state; here, the checkout is what
    the install claims to be a copy of.

    A source file that has since disappeared is not drift. `install_files` skips what a
    ref does not hold, and reporting a file the next install would not write either
    would be a failure nothing could clear.
    """
    if receipt is None or receipt.ref != WORKTREE_REF:
        return []
    drifted: list[Drift] = []
    for source_name, destination_name in RUNTIME_FILES.items():
        if destination_name not in receipt.files:
            continue
        source = source_root / source_name
        try:
            expected = digest(source.read_bytes())
        except OSError:
            continue
        if receipt.files[destination_name] != expected:
            drifted.append(Drift(destination_name, "older than the working tree it came from"))
    return drifted


def behind_ref(receipt: Receipt | None, latest: str) -> str:
    """The installed ref when a newer release exists; "" when there is nothing to say.

    Silent when either side is unknown, and silent for a working-tree install: that
    one has no ref to fall behind, so a tag comparison says nothing true about it.
    That silence used to be the whole answer for a worktree install and was read as
    "current"; `worktree_drift` is what actually answers the question, by bytes.
    """
    if receipt is None or not latest or receipt.ref in {WORKTREE_REF, latest}:
        return ""
    return receipt.ref


def render_drift(target: Path, drifted: Sequence[Drift], behind: str = "", latest: str = "") -> str:
    """Why a `--check` failed, and the one command that fixes it."""
    lines = [f"install-git-policy: the runtime installed at {target} needs attention:"]
    lines += [f"  {drift.name} -- {drift.reason}" for drift in drifted]
    if behind:
        lines.append(f"  installed from {behind}; {latest} is available")
    lines.append(
        "The hooks run the *installed* copy, so this is the policy being enforced. "
        "Re-run: python scripts/install-git-policy.py --yes"
    )
    return "\n".join(lines)


def run_check(source_root: Path, target: Path, runner: Runner = run_command) -> int:
    """`--check`: 0 current, 1 modified or behind, 2 not installed here.

    "Not installed" is deliberately not a drift: a fresh clone, a CI runner and
    anyone else's machine all have nothing installed, and reporting that as a
    failure would make the check meaningless everywhere it is not the point.
    """
    if not _configured_hooks_path(runner):
        print(
            "install-git-policy: global core.hooksPath is unset -- the branch policy "
            "is not installed on this machine",
            file=sys.stderr,
        )
        return 2
    try:
        ensure_compatible_hooks_path(target, runner)
    except InstallRefusedError as error:
        print(f"install-git-policy: {error}", file=sys.stderr)
        return 2

    receipt = read_receipt(target)
    drifted = compare_install(target, receipt) + worktree_drift(source_root, target, receipt)
    latest = resolve_ref(source_root)
    behind = behind_ref(receipt, latest)
    if not drifted and not behind:
        ref = receipt.ref if receipt else "?"
        print(f"install-git-policy: up to date ({target}, from {ref})")
        return 0
    print(render_drift(target, drifted, behind, latest), file=sys.stderr)
    return 1


def render_plan(target: Path, ref: str = WORKTREE_REF) -> str:
    source_label = "the working tree" if ref == WORKTREE_REF else ref
    files = "\n".join(
        f"  install {source} @ {source_label} -> {target / destination}"
        for source, destination in RUNTIME_FILES.items()
    )
    warning = (
        "\n  WARNING: installing uncommitted code as the policy every repository "
        "on this machine enforces\n"
        if ref == WORKTREE_REF
        else ""
    )
    return (
        "Devkit global Git policy install:\n"
        f"{files}\n"
        f"  write {target / RECEIPT_NAME}\n"
        f"{warning}"
        f"  git config --global core.hooksPath {target.resolve().as_posix()}\n"
        "  git config --global fetch.prune true\n"
        "  git config --global devkit.branchPolicy.failClosed true"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        type=Path,
        default=DEFAULT_TARGET,
        help=f"stable runtime directory (default: {DEFAULT_TARGET})",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="print the install plan without changing anything (default)",
    )
    mode.add_argument(
        "--yes",
        dest="dry_run",
        action="store_false",
        help="copy the hooks and update global Git configuration",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help=(
            "report whether the installed runtime still matches this checkout; "
            "exit 1 when it has drifted, 2 when nothing is installed here"
        ),
    )
    parser.add_argument(
        "--ref",
        default="",
        help=(
            "commit-ish to install the runtime from (default: devkit's newest tag). "
            "A released ref, never the working tree, so the policy every repository "
            "on this machine enforces is one that exists in a commit"
        ),
    )
    parser.add_argument(
        "--from-worktree",
        action="store_true",
        help=(
            "install from the working tree instead of a tag. Installs uncommitted "
            "code as the policy every repository on this machine enforces -- which "
            "is how a runtime once ended up missing an escape hatch its source had"
        ),
    )
    args = parser.parse_args(argv)
    target = args.target.expanduser().resolve()
    if args.check:
        return run_check(REPO_ROOT, target)
    if args.from_worktree and args.ref:
        parser.error("--ref and --from-worktree choose different sources; pass one")

    try:
        ref = WORKTREE_REF if args.from_worktree else (args.ref or resolve_ref(REPO_ROOT))
        print(render_plan(target, ref))
        ensure_compatible_hooks_path(target)
        if args.dry_run:
            print("\nDry run -- nothing changed. Re-run with --yes to install.")
            return 0
        receipt = install(REPO_ROOT, target, ref)
        configure_git(target)
    except (InstallRefusedError, OSError) as error:
        print(f"\ninstall-git-policy: REFUSED -- {error}", file=sys.stderr)
        return 2
    print(f"\ninstall-git-policy: installed from {receipt.ref}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
