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
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

# The layout tier, beside this file. `scripts/` is on `sys.path` both when this runs as a
# script and when the suite loads it by path, so a plain import resolves either way.
# Re-exported rather than reached through, so nothing that already said
# `installer.RUNTIME_FILES` has to change.
from install_policy_layout import (
    HOOK_NAMES,
    RUNTIME_FILES,
    clear_shadowing_entrypoint,
    install_refusal,
)

# The receipt-and-drift tier, cut out of this file when its structural ceiling was
# raised a third consecutive time. Imported rather than re-exported by hand: `run_check`
# below is its caller, so every name here is genuinely used, and a test that said
# `installer.compare_install` keeps resolving.
from policy_drift import (
    RECEIPT_NAME,
    WORKTREE_REF,
    Receipt,
    behind_ref,
    compare_install,
    digest,
    read_receipt,
    render_drift,
    worktree_drift,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TARGET = Path.home() / ".devkit" / "git-hooks"

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


def _make_room(destination: Path) -> None:
    """Create a destination's parent directory, and only when something will be written.

    Called at the write rather than at the top of the loop, which is where it was first
    put and was wrong: a skipped entry would still have created the directory, so
    installing from a tag that predates the package left an empty `devkit_git_policy/`
    beside the flat module it did install. An empty directory is a *namespace* package,
    and while a real module still wins over one, shipping an empty package directory
    next to the module it looks like is a trap for whoever reads the install next.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)


def install_files(source_root: Path, target: Path, ref: str = WORKTREE_REF) -> dict[str, str]:
    """Write the runtime into `target` from `ref`; return installed name -> sha256.

    `ref` defaults to the working tree so that the explicit, auditable call is the
    one that reaches for uncommitted code -- and `main()` never makes it without
    `--from-worktree`.
    """
    target.mkdir(parents=True, exist_ok=True)
    hashes: dict[str, str] = {}
    # (source, why) rather than bare names: the two skips have different causes now, and
    # "newer than the ref" printed over a file the ref was never going to hold sends the
    # reader looking for a release that would fix it.
    skipped: list[tuple[str, str]] = []
    for source_name, destination_name in RUNTIME_FILES.items():
        destination = target / destination_name
        if ref == WORKTREE_REF:
            # The same skip the ref branch makes, for the same reason: the working tree
            # holds ONE of the policy's two layouts, so the other is legitimately absent
            # rather than a broken checkout. Before the package this could not happen --
            # every `RUNTIME_FILES` source was always present -- and `copy2` raising here
            # would refuse the whole install over a file no ref was ever going to have.
            if not (source_root / source_name).is_file():
                skipped.append((source_name, "not in the working tree"))
                continue
            _make_room(destination)
            shutil.copy2(source_root / source_name, destination)
        elif not in_ref(source_root, ref, source_name):
            # THE REF DECIDES THE RUNTIME. A file added to `RUNTIME_FILES` after `ref`
            # was cut is not part of that release, and demanding it would make every
            # install from the newest TAG refuse the moment this list grew -- which is
            # the default `main()` uses, and which `installers.py` re-runs nightly. So
            # the newer file is skipped and left out of the receipt, and `--check` is
            # judged against the receipt rather than against this list, or the skip
            # would report as drift forever and re-install forever.
            skipped.append((source_name, f"not in {ref}"))
            continue
        else:
            _make_room(destination)
            destination.write_bytes(read_blob(source_root, ref, source_name))
        hashes[destination_name] = digest(destination.read_bytes())
        if destination_name in HOOK_NAMES:
            destination.chmod(
                destination.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            )
    for source_name, why in skipped:
        print(f"install-git-policy: {source_name} {why}; not installed")
    cleared = clear_shadowing_entrypoint(target, hashes)
    if cleared:
        print(f"install-git-policy: removed {cleared}, left by an install of the other layout")
    refusal = install_refusal(hashes, ref)
    if refusal:
        raise InstallRefusedError(refusal)
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


def unconfigure_git(target: Path, runner: Runner = run_command) -> list[str]:
    """Undo what `configure_git` set, and report what was undone.

    **`core.hooksPath` is cleared, and only when it still points here.** A global
    `core.hooksPath` naming a directory that does not exist makes *every* git command on
    the machine fail, in every repository, with an error naming a path rather than this
    installer -- so leaving it set is the one outcome an uninstall must not produce, and
    clearing somebody else's is the other. That is the same test
    `ensure_compatible_hooks_path` applies on the way in.

    `devkit.branchPolicy.failClosed` goes too: it is devkit's own namespace and means
    nothing once the hooks it gates are gone. **`fetch.prune` deliberately stays.** It is
    not devkit's setting in any meaningful sense -- it is a widely-wanted git default this
    installer happened to turn on -- and taking away a behaviour the operator may now rely
    on is worse than leaving a harmless one behind.
    """
    undone = []
    if _configured_hooks_path(runner) == target.resolve().as_posix():
        result = runner(["git", "config", "--global", "--unset", "core.hooksPath"])
        _require_ok(result, "could not clear core.hooksPath")
        undone.append("core.hooksPath")
    # `--unset` on a key that is not set exits 5, which is "nothing to do" and not a
    # failure; anything else is.
    result = runner(["git", "config", "--global", "--unset", "devkit.branchPolicy.failClosed"])
    if result.returncode == 0:
        undone.append("devkit.branchPolicy.failClosed")
    elif result.returncode != 5:
        _require_ok(result, "could not clear devkit.branchPolicy.failClosed")
    return undone


def uninstall(target: Path, runner: Runner = run_command) -> list[str]:
    """Remove the installed runtime and the configuration pointing at it.

    **Configuration first, files second**, which is the whole safety property: between the
    two steps the hooks path either names a directory that still exists or is unset, and
    never a deleted one. Reversing the order leaves every git command on the machine
    broken for the width of the window, and permanently if the second step fails.
    """
    done = [f"git config --global --unset {key}" for key in unconfigure_git(target, runner)]
    if target.is_dir():
        shutil.rmtree(target)
        done.append(f"removed {target}")
    return done


def render_uninstall_plan(target: Path) -> str:
    return (
        "Devkit global Git policy uninstall:\n"
        "  git config --global --unset core.hooksPath (only while it points here)\n"
        "  git config --global --unset devkit.branchPolicy.failClosed\n"
        f"  remove {target}\n"
        "\n`fetch.prune` is left set: it is a general git preference rather than devkit's."
    )


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


def build_parser() -> argparse.ArgumentParser:
    """The CLI. Its own function so `main` holds decisions rather than declarations --
    the shape `structure_check`'s `function_lines` limit asks for, and the one
    `install-reconcile-task.py` already had."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        type=Path,
        default=DEFAULT_TARGET,
        help=f"stable runtime directory (default: {DEFAULT_TARGET})",
    )
    # The verbs. `--yes` / `--dry-run` are deliberately *not* among them: they say whether
    # to apply, and `--uninstall --yes` has to be expressible.
    mode = parser.add_mutually_exclusive_group()
    apply_mode = parser.add_mutually_exclusive_group()
    apply_mode.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="print the plan without changing anything (default)",
    )
    apply_mode.add_argument(
        "--yes",
        dest="dry_run",
        action="store_false",
        help="apply: copy the hooks and update global Git configuration, or confirm an --uninstall",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help=(
            "report whether the installed runtime still matches this checkout; "
            "exit 1 when it has drifted, 2 when nothing is installed here"
        ),
    )
    mode.add_argument(
        "--uninstall",
        action="store_true",
        help=(
            "remove the installed runtime and the global config pointing at it "
            "(dry run unless --yes)"
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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    target = args.target.expanduser().resolve()
    if args.check:
        return run_check(REPO_ROOT, target)
    if args.uninstall:
        print(render_uninstall_plan(target))
        if args.dry_run:
            print("\nDry run -- nothing changed. Re-run with --yes to uninstall.")
            return 0
        try:
            done = uninstall(target)
        except (InstallRefusedError, OSError) as error:
            print(f"\ninstall-git-policy: REFUSED -- {error}", file=sys.stderr)
            return 2
        print("\n" + ("\n".join(f"  {line}" for line in done) or "  nothing was installed here"))
        print("install-git-policy: uninstalled")
        return 0
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
