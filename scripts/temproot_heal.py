"""Move a pytest run off a scratch root this user can no longer sweep.

pytest keeps every run's scratch tree under one per-user root (`pytest-of-<user>` in
the temp dir) with a `pytest-current` link to the newest, and at session end it
resolves every link there to sweep the dead ones. Python creates that root with an
`OWNER RIGHTS`-only ACL on Windows, so a link an *elevated* process made there is owned
by Administrators and a normal process can neither resolve nor remove it -- pytest's own
`_force_symlink` tries the removal on every run and swallows the refusal. From then on
every default pytest run on the machine passed its tests and died in
`cleanup_dead_symlinks` with `PermissionError: [WinError 5]`, whichever session ran it
(2026-09-27: one elevated pytest at 23:00, every run after it).

Only an elevated process can delete that link, so the run moves instead: it points
`PYTEST_DEBUG_TEMPROOT`, which pytest reads when it first needs a scratch dir, at a
sibling root with nothing poisoned in it, where pytest's own numbering and retention
carry on as normal. Child processes inherit the variable, so a nested pytest goes
straight there. An elevated run can still read the old root, so it keeps using that one
and never poisons the new one.

Called from the repo's root `conftest.py`. Tested in `tests/test_temproot_heal.py`.
"""

from __future__ import annotations

import getpass
import os
import tempfile
from collections.abc import Callable, MutableMapping
from pathlib import Path

TEMPROOT = "PYTEST_DEBUG_TEMPROOT"
REHOMED = "pytest-rehomed"
# Each rehome is a root another elevated run could poison in turn; past this many, give up.
MAX_REHOMES = 5


def current_user() -> str:
    """The name pytest puts in `pytest-of-<user>`, or "" where it has none."""
    try:
        return getpass.getuser()
    except (ImportError, OSError, KeyError):
        return ""


def readable(link: Path) -> bool:
    """pytest's own sweep would get past `link`: resolving it is not refused."""
    try:
        link.resolve().exists()
    except PermissionError:
        return False
    return True


def poisoned_links(
    root: Path,
    is_link: Callable[[Path], bool] = Path.is_symlink,
    can_read: Callable[[Path], bool] = readable,
) -> list[Path]:
    """Every link directly under `root` that pytest's end-of-session sweep would die on."""
    try:
        children = sorted(root.iterdir())
    except OSError:
        return []
    found = []
    for child in children:
        try:
            link = is_link(child)
        except OSError:
            link = True  # lstat refused too: the same failure, one call earlier
        if link and not can_read(child):
            found.append(child)
    return found


def healthy_temproot(
    base: Path,
    user: str,
    is_link: Callable[[Path], bool] = Path.is_symlink,
    can_read: Callable[[Path], bool] = readable,
) -> tuple[Path | None, list[Path]]:
    """`(temproot, poisoned)`: the first temproot at or beside `base` whose scratch root
    is clean -- `base` itself when nothing is wrong -- and what made the default unusable.

    The temproot is None when every candidate is poisoned.
    """
    name = f"pytest-of-{user or 'unknown'}"
    poisoned = poisoned_links(base / name, is_link, can_read)
    if not poisoned:
        return base, []
    for n in range(1, MAX_REHOMES + 1):
        candidate = base / (REHOMED if n == 1 else f"{REHOMED}-{n}")
        if not poisoned_links(candidate / name, is_link, can_read):
            return candidate, poisoned
    return None, poisoned


def guard(
    option,
    environ: MutableMapping[str, str] = os.environ,
    user: str | None = None,
    say: Callable[[str], object] = print,
    is_link: Callable[[Path], bool] = Path.is_symlink,
    can_read: Callable[[Path], bool] = readable,
) -> None:
    """Point this run at a scratch root it can sweep; `option` is pytest's `config.option`.

    An explicit basetemp -- the user's, or the one xdist hands each worker -- is never
    swept, so it is left alone.
    """
    if getattr(option, "basetemp", None):
        return
    base = Path(environ.get(TEMPROOT) or tempfile.gettempdir()).resolve()
    user = current_user() if user is None else user
    temproot, poisoned = healthy_temproot(base, user, is_link, can_read)
    if not poisoned:
        return
    links = ", ".join(map(str, poisoned))
    if temproot is None:
        say(f"temproot_heal: {links} cannot be read, and neither can {MAX_REHOMES} rehomes")
        return
    temproot.mkdir(parents=True, exist_ok=True)
    environ[TEMPROOT] = str(temproot)
    say(
        f"temproot_heal: {links} cannot be read by this user (an elevated run made it; "
        f"only an elevated shell can delete it) -- scratch dirs go under {temproot}"
    )
