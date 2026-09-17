#!/usr/bin/env python3
"""What the installed git-policy runtime is, and whether it still matches its source.

Cut out of `install-git-policy.py`, whose structural ceiling had been raised three
times running -- which `.claude/rules/engineering.md` makes a defect report rather than
a raise. The measurement recorded with the third raise found no god function (the
largest was 52 lines) but six responsibilities never separated: runtime layout, ref
reading, the receipt, installing, drift comparison, and git config plus the CLI. It
named this tier as the first cut, for two reasons that still hold:

- it imports nothing the install path needs -- only the layout module and the receipt
  it is written against -- so the seam is real rather than drawn;
- `installers.py` runs `--check` nightly on every machine, so this is the half with a
  caller of its own and a natural test boundary.

The receipt comes with it rather than staying behind. It is the drift check's data
model: `compare_install` is a comparison *against the receipt*, `worktree_drift` reads
`receipt.ref`, and `behind_ref` is nothing but a question about it. Leaving it in the
installer would have meant the new module importing back into the one it was cut from.

**Filesystem only, and deliberately.** Nothing here spawns git or touches the network:
the session-start status line calls `compare_install`, and a status line that can hang
is worse than no status line. `resolve_ref` -- the one question that does need git --
stays in the installer, which passes its answer in as `latest`.

Stdlib only. Tested in `tests/test_policy_drift.py`.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from install_policy_layout import (
    RUNTIME_FILES,
    entrypoint_path,
    shadowing_entrypoint,
)

# The sentinel `ref` for an install taken from the working tree rather than a
# commit. Recorded verbatim so a receipt never claims a provenance it does not have.
WORKTREE_REF = "worktree"
RECEIPT_NAME = "installed.json"


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
    # The receipt can only describe what it wrote. A leftover of the OTHER layout is
    # invisible to the loop above and is the one difference that changes which code
    # runs, so it is asked about separately.
    stale = shadowing_entrypoint(receipt.files)
    if stale and entrypoint_path(target, stale).exists():
        drifted.append(
            Drift(
                stale,
                "left by an older install and shadows the runtime this receipt describes"
                if "/" in stale
                else "left by an older install of the flat module",
            )
        )
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
