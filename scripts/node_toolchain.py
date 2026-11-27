#!/usr/bin/env python3
"""The Node a checkout's frontend asks for, and a per-user copy of it when PATH's is older.

The Python half of provisioning has a seam: `[python] version`, or the pin a project's
build files carry, reaches `uv venv --python`, which fetches an interpreter the machine
lacks into uv's own cache. The Node half had none. A frontend was installed and built by
whatever `node` PATH held, so ibkr_trader -- `engines.node` `>=24` in
`frontend/package.json` -- met the only Node on desktop-ml5f3pj, 18.15 from winget, and
its dashboard's `npm run build` died on `node:util` having no `styleText`. The gateway
then answered 503 on every request with nothing saying why (2026-11-27).

So this module does for Node what uv does for Python, and no more:

- `requirement` reads what the checkout asks for: an exact pin in `.nvmrc` or
  `.node-version` (the frontend's directory first, then the root), else the `engines.node`
  range in the frontend's `package.json`. A pin wins over a range for the reason
  `worktree.PIN_FILES` gives for Python.
- `provide` answers with PATH's Node when it satisfies that, else a copy already in the
  per-user cache, else one fetched from nodejs.org into that cache, checked against the
  release's `SHASUMS256.txt`. Never a machine-wide install: that would change every other
  project's Node to fix one.
- `environ` and `rebind` are how a caller runs under the answer: the copy's directory first
  on PATH, and `npm` resolved there rather than to PATH's (on Windows `npm.cmd` runs the
  `node.exe` beside it, so PATH's npm would still be Node 18).

When nothing can satisfy the requirement, the `Provision` says so in one line naming
`node <have> < <need>`, which is what the gateway's 503 and the provisioner's FAILED line
carry. Stdlib only: `worktree.py` and `dashboards.py` both import it, and the gateway runs
under the machine's interpreter. Tested in `tests/test_node_toolchain.py`.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
import zipfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

DIST = "https://nodejs.org/dist"
DEFAULT_FRONTEND_DIR = "frontend"
# Exact pins, in the order they win; `engines.node` is read only when neither pins.
PIN_FILES = (".nvmrc", ".node-version")
FETCH_TIMEOUT = 120.0
NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)

Version = tuple[int, int, int]
# One comparator: an operator from `OPERATORS` and the version it compares against.
Comparator = tuple[str, Version]
OPERATORS = (">=", "<=", ">", "<", "=")

_PARTIAL = re.compile(r"^v?(\d+|[xX*])(?:\.(\d+|[xX*]))?(?:\.(\d+|[xX*]))?(?:[-+].*)?$")
_OPERATOR = re.compile(r"^(>=|<=|>|<|=|\^|~>?)?\s*(.*)$")


# --- versions and ranges ---------------------------------------------------------------


def parse_version(text: str) -> Version | None:
    """`v18.15.0` / `24.1.0` / `24` -> a full version; None when it is not one."""
    parts = _partial(text.strip())
    if parts is None:
        return None
    major, minor, patch = parts
    if major is None:
        return None
    return (major, minor or 0, patch or 0)


def render(version: Version) -> str:
    return ".".join(str(part) for part in version)


def _partial(text: str) -> tuple[int | None, int | None, int | None] | None:
    """`24.x` -> (24, None, None); None when `text` is no version at all."""
    match = _PARTIAL.match(text)
    if not match:
        return None
    out: list[int | None] = []
    wild = False
    for group in match.groups():
        if group is None or group in "xX*" or wild:
            wild = True
            out.append(None)
        else:
            out.append(int(group))
    return out[0], out[1], out[2]


def _bump(parts: tuple[int | None, int | None, int | None]) -> Version:
    """The first version past the last part `parts` names: `24` -> 25.0.0, `24.1` -> 24.2.0."""
    major, minor, patch = parts
    if major is None:
        raise ValueError("a wildcard has no upper bound")
    if minor is None:
        return (major + 1, 0, 0)
    if patch is None:
        return (major, minor + 1, 0)
    return (major, minor, patch + 1)


def _floor(parts: tuple[int | None, int | None, int | None]) -> Version:
    return (parts[0] or 0, parts[1] or 0, parts[2] or 0)


def _caret(parts: tuple[int | None, int | None, int | None]) -> list[Comparator]:
    major, minor, patch = parts
    low = _floor(parts)
    if major is None:
        return []
    if major > 0 or minor is None:
        return [(">=", low), ("<", (major + 1, 0, 0))]
    if minor > 0 or patch is None:
        return [(">=", low), ("<", (0, minor + 1, 0))]
    return [(">=", low), ("<", (0, 0, patch + 1))]


def _comparators(operator: str, operand: str) -> list[Comparator]:
    parts = _partial(operand)
    if parts is None:
        raise ValueError(f"not a version: {operand!r}")
    if parts[0] is None:
        return []  # `*`, `x`: anything
    exact = parts[2] is not None
    if operator == "^":
        return _caret(parts)
    if operator in ("~", "~>"):
        tilde = (parts[0], parts[1], None) if parts[1] is not None else parts
        return [(">=", _floor(parts)), ("<", _bump(tilde))]
    if operator == ">=":
        return [(">=", _floor(parts))]
    if operator == "<":
        return [("<", _floor(parts))]
    if operator == ">":
        return [(">", _floor(parts))] if exact else [(">=", _bump(parts))]
    if operator == "<=":
        return [("<=", _floor(parts))] if exact else [("<", _bump(parts))]
    # `=` or nothing: an exact version, or every version a partial one names.
    return [("=", _floor(parts))] if exact else [(">=", _floor(parts)), ("<", _bump(parts))]


def _tokens(text: str) -> list[str]:
    """`>= 24 < 26` -> [">=24", "<26"]: npm allows a space after an operator."""
    out: list[str] = []
    pending = ""
    for word in text.split():
        if word in ("^", "~", "~>", *OPERATORS):
            pending += word
            continue
        out.append(pending + word)
        pending = ""
    if pending:
        raise ValueError(f"an operator with no version: {text!r}")
    return out


def parse_range(spec: str) -> list[list[Comparator]]:
    """An npm range as alternatives of comparator sets. ValueError when it is not one.

    The subset `engines.node` is written in: `||`, comparators, `^`, `~`, x-ranges, bare
    and partial versions, and hyphen ranges. Prerelease tags are ignored, since no
    release Node carries one.
    """
    alternatives: list[list[Comparator]] = []
    for alternative in spec.split("||"):
        text = alternative.strip()
        hyphen = re.fullmatch(r"(\S+)\s+-\s+(\S+)", text)
        if hyphen:
            low = _comparators(">=", hyphen.group(1))
            high = _comparators("<=", hyphen.group(2))
            alternatives.append(low + high)
            continue
        comparators: list[Comparator] = []
        for token in _tokens(text):
            match = _OPERATOR.match(token)
            operator, operand = (match.group(1) or "", match.group(2)) if match else ("", token)
            comparators.extend(_comparators(operator, operand))
        alternatives.append(comparators)
    return alternatives


def _holds(version: Version, comparator: Comparator) -> bool:
    operator, bound = comparator
    return {
        ">=": version >= bound,
        "<=": version <= bound,
        ">": version > bound,
        "<": version < bound,
        "=": version == bound,
    }[operator]


def satisfies(version: Version, spec: str) -> bool:
    """Whether `version` is in the npm range `spec`. ValueError when `spec` is no range."""
    return any(
        all(_holds(version, comparator) for comparator in alternative)
        for alternative in parse_range(spec)
    )


def shortfall(have: str, need: str) -> str:
    """The one line a refusal names: `node 18.15.0 < >=24`."""
    return f"node {have or 'none'} < {need}"


# --- what a checkout asks for ----------------------------------------------------------


def frontend_dir_of(checkout: Path) -> str:
    """`[frontend] dir` in the checkout's `.devkit.toml`, else `frontend`.

    Read here with `tomllib` rather than through `harness_config`, because the gateway
    imports this from devkit's scripts while the file belongs to another project.
    """
    try:
        data = tomllib.loads((checkout / ".devkit.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return DEFAULT_FRONTEND_DIR
    frontend = data.get("frontend")
    found = frontend.get("dir") if isinstance(frontend, Mapping) else None
    return found if isinstance(found, str) and found else DEFAULT_FRONTEND_DIR


def _pin(path: Path) -> str:
    """The version line of an `.nvmrc` / `.node-version`, when it names one.

    `lts/*`, `node` and alias names say no number devkit can check, so they read as no
    pin and leave `engines.node` to decide.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            parts = _partial(line)
            return line.lstrip("v") if parts is not None and parts[0] is not None else ""
    return ""


def requirement(checkout: Path, frontend_dir: str = "") -> tuple[str, str]:
    """`(range, the file that said so)` for the checkout's frontend; `("", "")` when none."""
    frontend = frontend_dir or frontend_dir_of(checkout)
    for directory in (checkout / frontend, checkout):
        for name in PIN_FILES:
            pin = _pin(directory / name)
            if pin:
                return pin, str((directory / name).relative_to(checkout).as_posix())
    for directory in (checkout / frontend, checkout):
        try:
            manifest = json.loads((directory / "package.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        engines = manifest.get("engines") if isinstance(manifest, dict) else None
        need = engines.get("node") if isinstance(engines, dict) else None
        if isinstance(need, str) and need.strip():
            origin = (directory / "package.json").relative_to(checkout).as_posix()
            return need.strip(), f"{origin} engines.node"
    return "", ""


# --- the machine's Node, and the per-user cache ----------------------------------------


def machine_version(which: Callable[[str], str | None] = shutil.which) -> str:
    """`node --version` of PATH's Node, without the `v`; "" when there is none."""
    node = which("node")
    if not node:
        return ""
    try:
        done = subprocess.run(
            [node, "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    version = parse_version(done.stdout.strip()) if done.returncode == 0 else None
    return render(version) if version else ""


def cache_root(environ: Mapping[str, str] | None = None, windows: bool = os.name == "nt") -> Path:
    """Where fetched Nodes live: per user, beside uv's own cache, never machine-wide."""
    env = os.environ if environ is None else environ
    if windows:
        base = env.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "devkit" / "node"
    base = env.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "devkit" / "node"


def bin_dir(home: Path, windows: bool = os.name == "nt") -> Path:
    """The directory holding `node` in an unpacked release: its root on Windows."""
    return home if windows else home / "bin"


def _node_binary(windows: bool) -> str:
    return "node.exe" if windows else "node"


def cached(root: Path, need: str, windows: bool = os.name == "nt") -> tuple[str, Path] | None:
    """The newest copy under `root` that satisfies `need`: `(version, bin dir)`."""
    best: tuple[Version, Path] | None = None
    with contextlib.suppress(OSError):
        for home in root.iterdir():
            version = parse_version(home.name)
            if version is None or not (bin_dir(home, windows) / _node_binary(windows)).is_file():
                continue
            if satisfies(version, need) and (best is None or version > best[0]):
                best = (version, bin_dir(home, windows))
    return (render(best[0]), best[1]) if best else None


@dataclass(frozen=True)
class Platform:
    """The release a machine runs: the key nodejs.org's index lists it under, and the
    archive's name."""

    key: str
    stem: str
    suffix: str


def platform_of(system: str | None = None, machine: str | None = None) -> Platform:
    """This machine's release. ValueError for one nodejs.org ships no build for."""
    system = (system or platform.system()).lower()
    raw = (machine or platform.machine()).lower()
    arch = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(raw)
    if arch is None:
        raise ValueError(f"no Node release for the {raw} architecture")
    if system == "windows":
        return Platform(f"win-{arch}-zip", f"win-{arch}", ".zip")
    if system == "linux":
        return Platform(f"linux-{arch}", f"linux-{arch}", ".tar.xz")
    if system == "darwin":
        return Platform(f"osx-{arch}-tar", f"darwin-{arch}", ".tar.gz")
    raise ValueError(f"no Node release for {system}")


def choose(index: Iterable[Mapping[str, object]], need: str, key: str) -> str:
    """The release to fetch: the newest LTS satisfying `need` built for `key`, else the
    newest satisfying release. "" when none does."""
    found: list[tuple[bool, Version, str]] = []
    for entry in index:
        raw = entry.get("version")
        files = entry.get("files")
        version = parse_version(raw) if isinstance(raw, str) else None
        if version is None or not isinstance(files, list) or key not in files:
            continue
        if satisfies(version, need):
            found.append((bool(entry.get("lts")), version, render(version)))
    return max(found)[2] if found else ""


Opener = Callable[[str], bytes]


def download(url: str) -> bytes:
    """GET `url`. Only nodejs.org URLs this module builds reach here."""
    request = urllib.request.Request(url, headers={"User-Agent": "devkit-node-toolchain"})
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
        return response.read()


def _inside(base: Path, name: str) -> bool:
    target = (base / name).resolve()
    return target == base.resolve() or base.resolve() in target.parents


def unpack(archive: Path, into: Path) -> None:
    """Extract a release archive, refusing any member that would land outside `into`."""
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as bundle:
            for name in bundle.namelist():
                if not _inside(into, name):
                    raise ValueError(f"{archive.name} holds a path outside itself: {name}")
            bundle.extractall(into)  # noqa: S202 - every member checked to stay inside above
        return
    with tarfile.open(archive) as bundle:
        bundle.extractall(into, filter="data")


def fetch(
    need: str,
    root: Path,
    *,
    opener: Opener = download,
    target: Platform | None = None,
    windows: bool = os.name == "nt",
) -> tuple[str, Path]:
    """Fetch the release `choose` picks into `root`: `(version, bin dir)`.

    The archive is checked against the release's `SHASUMS256.txt` before it is unpacked,
    and moved into place only once whole, so an interrupted fetch leaves no copy for
    `cached` to trust.
    """
    target = target or platform_of()
    index = json.loads(opener(f"{DIST}/index.json"))
    version = choose(index if isinstance(index, list) else [], need, target.key)
    if not version:
        raise ValueError(f"nodejs.org lists no {target.stem} release satisfying {need}")
    tag = f"v{version}"
    name = f"node-{tag}-{target.stem}{target.suffix}"
    sums = opener(f"{DIST}/{tag}/SHASUMS256.txt").decode("utf-8", errors="replace")
    expected = next(
        (line.split()[0] for line in sums.splitlines() if line.strip().endswith(f"  {name}")),
        "",
    )
    if not expected:
        raise ValueError(f"SHASUMS256.txt for {tag} does not list {name}")
    payload = opener(f"{DIST}/{tag}/{name}")
    if hashlib.sha256(payload).hexdigest() != expected:
        raise ValueError(f"{name} does not match its SHASUMS256.txt digest")
    root.mkdir(parents=True, exist_ok=True)
    home = root / tag
    with tempfile.TemporaryDirectory(dir=root, prefix=".fetch-") as scratch:
        archive = Path(scratch) / name
        archive.write_bytes(payload)
        unpacked = Path(scratch) / "unpacked"
        unpack(archive, unpacked)
        top = unpacked / f"node-{tag}-{target.stem}"
        if not (bin_dir(top, windows) / _node_binary(windows)).is_file():
            raise ValueError(f"{name} holds no {_node_binary(windows)}")
        if home.exists():
            shutil.rmtree(home)
        top.rename(home)
    return version, bin_dir(home, windows)


# --- the answer ------------------------------------------------------------------------


@dataclass(frozen=True)
class Provision:
    """The Node a run gets for `need`.

    `bin_dir` is None when PATH's Node already satisfies it. `problem` is set, and names
    `node <have> < <need>`, when nothing could.
    """

    need: str
    origin: str = ""
    have: str = ""
    version: str = ""
    bin_dir: Path | None = None
    problem: str = ""

    @property
    def ok(self) -> bool:
        return not self.problem


def provide(
    need: str,
    origin: str = "",
    *,
    root: Path | None = None,
    machine: Callable[[], str] = machine_version,
    fetcher: Callable[[str, Path], tuple[str, Path]] = fetch,
    windows: bool = os.name == "nt",
) -> Provision:
    """PATH's Node when it satisfies `need`, else a cached copy, else a fetched one."""
    where = f" ({origin})" if origin else ""
    try:
        parse_range(need)
    except ValueError as exc:
        return Provision(
            need, origin, problem=f"unreadable Node requirement {need!r}{where}: {exc}"
        )
    have = machine()
    version = parse_version(have) if have else None
    if version is not None and satisfies(version, need):
        return Provision(need, origin, have, have)
    root = root or cache_root(windows=windows)
    hit = cached(root, need, windows)
    if hit is None:
        try:
            hit = fetcher(need, root)
        except (OSError, ValueError) as exc:
            return Provision(
                need,
                origin,
                have,
                problem=f"{shortfall(have, need)}{where}, and no Node satisfying it could "
                f"be fetched: {exc}",
            )
    return Provision(need, origin, have, hit[0], hit[1])


def ensure(
    checkout: Path,
    frontend_dir: str = "",
    *,
    provider: Callable[[str, str], Provision] = provide,
) -> Provision | None:
    """`provide` for what `checkout` asks for; None when it asks for nothing."""
    need, origin = requirement(checkout, frontend_dir)
    return provider(need, origin) if need else None


def environ(
    provision: Provision | None, base: Mapping[str, str] | None = None
) -> dict[str, str] | None:
    """`base` with the provided Node first on PATH; None when PATH's own serves."""
    if provision is None or provision.bin_dir is None:
        return None
    env = dict(os.environ if base is None else base)
    key = next((name for name in env if name.upper() == "PATH"), "PATH")
    env[key] = os.pathsep.join(filter(None, (str(provision.bin_dir), env.get(key, ""))))
    return env


NPM_NAMES = frozenset({"npm", "npm.cmd", "npm.exe", "npx", "npx.cmd", "npx.exe"})


def rebind(
    argv: Sequence[str], provision: Provision | None, windows: bool = os.name == "nt"
) -> tuple[str, ...]:
    """`argv` with an npm/npx program resolved in the provided Node's directory."""
    if not argv or provision is None or provision.bin_dir is None:
        return tuple(argv)
    program = Path(argv[0]).name.lower()
    if program not in NPM_NAMES:
        return tuple(argv)
    stem = program.split(".")[0]
    return (str(provision.bin_dir / (f"{stem}.cmd" if windows else stem)), *argv[1:])


def main(argv: list[str] | None = None) -> int:
    """`node_toolchain.py [CHECKOUT]`: what the checkout asks for and what it gets."""
    args = sys.argv[1:] if argv is None else argv
    checkout = Path(args[0] if args else ".").resolve()
    provision = ensure(checkout)
    if provision is None:
        print(f"{checkout.name}: asks for no Node version")
        return 0
    if not provision.ok:
        print(f"{checkout.name}: {provision.problem}", file=sys.stderr)
        return 1
    where = provision.bin_dir or "PATH"
    print(f"{checkout.name}: node {provision.version} from {where} for {provision.need}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
