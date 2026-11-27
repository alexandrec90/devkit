"""Tests for `scripts/node_toolchain.py`: npm ranges, what a checkout asks for, the
per-user cache, and a fetch checked against its release's digest."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest
from support import load_script

node = load_script("scripts/node_toolchain.py")


# --- ranges ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "spec", "expected"),
    [
        ("18.15.0", ">=24", False),
        ("24.0.0", ">=24", True),
        ("25.2.1", ">=24", True),
        ("24.3.0", "^24.1", True),
        ("25.0.0", "^24.1", False),
        ("0.3.4", "^0.3.1", True),
        ("0.4.0", "^0.3.1", False),
        ("24.1.9", "~24.1.2", True),
        ("24.2.0", "~24.1.2", False),
        ("22.11.0", "22", True),
        ("23.0.0", "22.x", False),
        ("20.5.0", ">=18 <21", True),
        ("21.0.0", ">= 18 < 21", False),
        ("16.0.0", "^18 || ^20", False),
        ("20.9.0", "^18 || ^20", True),
        ("20.9.0", "18 - 20", True),
        ("21.0.0", "18 - 20", False),
        ("21.0.0", ">20", True),
        ("20.9.0", ">20", False),
        ("20.9.0", "<=20", True),
        ("24.1.0", "v24.1.0", True),
        ("24.1.1", "24.1.0", False),
        ("3.0.0", "*", True),
    ],
)
def test_npm_ranges(version, spec, expected):
    assert node.satisfies(node.parse_version(version), spec) is expected


@pytest.mark.parametrize("spec", ["banana", ">=", "lts/*"])
def test_a_range_that_is_none_is_refused(spec):
    with pytest.raises(ValueError):
        node.parse_range(spec)


def test_versions_parse_with_and_without_the_v():
    assert node.parse_version("v18.15.0") == (18, 15, 0)
    assert node.parse_version("24") == (24, 0, 0)
    assert node.parse_version("node") is None
    assert node.shortfall("18.15.0", ">=24") == "node 18.15.0 < >=24"
    assert node.shortfall("", ">=24") == "node none < >=24"


# --- what a checkout asks for ----------------------------------------------------------


def _package(directory: Path, engines: object) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "package.json").write_text(json.dumps({"engines": engines}), encoding="utf-8")


def test_engines_node_in_the_frontend_is_the_requirement(tmp_path):
    """ibkr_trader's shape: `frontend/package.json` declares `>=24`."""
    _package(tmp_path / "frontend", {"node": ">=24"})
    assert node.requirement(tmp_path) == (">=24", "frontend/package.json engines.node")


def test_a_pin_file_wins_over_the_engines_range(tmp_path):
    _package(tmp_path / "frontend", {"node": ">=20"})
    (tmp_path / ".nvmrc").write_text("v24.1.0\n", encoding="utf-8")
    assert node.requirement(tmp_path) == ("24.1.0", ".nvmrc")
    (tmp_path / "frontend" / ".node-version").write_text("22\n", encoding="utf-8")
    assert node.requirement(tmp_path) == ("22", "frontend/.node-version")


def test_an_alias_pin_leaves_the_range_to_decide(tmp_path):
    _package(tmp_path / "frontend", {"node": ">=24"})
    (tmp_path / ".nvmrc").write_text("lts/*\n", encoding="utf-8")
    assert node.requirement(tmp_path)[0] == ">=24"


def test_the_frontend_dir_comes_from_the_manifest(tmp_path):
    (tmp_path / ".devkit.toml").write_text('[frontend]\ndir = "web"\n', encoding="utf-8")
    _package(tmp_path / "web", {"node": "^22"})
    assert node.frontend_dir_of(tmp_path) == "web"
    assert node.requirement(tmp_path) == ("^22", "web/package.json engines.node")


def test_a_checkout_asking_for_nothing_has_no_requirement(tmp_path):
    _package(tmp_path / "frontend", {})
    assert node.requirement(tmp_path) == ("", "")
    assert node.ensure(tmp_path) is None


# --- providing it ----------------------------------------------------------------------


def _install(root: Path, version: str, windows: bool) -> Path:
    home = root / f"v{version}"
    bin_dir = node.bin_dir(home, windows)
    bin_dir.mkdir(parents=True)
    (bin_dir / ("node.exe" if windows else "node")).write_bytes(b"")
    return bin_dir


def test_paths_node_serves_when_it_satisfies(tmp_path):
    def refuse(*_a):
        raise AssertionError("nothing should be fetched")

    got = node.provide(">=24", root=tmp_path, machine=lambda: "24.2.0", fetcher=refuse)
    assert got.ok and got.bin_dir is None and got.version == "24.2.0"
    assert node.environ(got) is None


def test_node_18_on_path_gets_a_cached_copy_that_satisfies(tmp_path):
    """desktop-ml5f3pj: winget's Node 18.15 and a `>=24` frontend."""
    _install(tmp_path, "22.0.0", windows=True)
    newest = _install(tmp_path, "24.5.0", windows=True)
    got = node.provide(">=24", root=tmp_path, machine=lambda: "18.15.0", fetcher=None, windows=True)
    assert got.ok and got.version == "24.5.0" and got.bin_dir == newest
    env = node.environ(got, {"Path": r"C:\nodejs"})
    assert env == {"Path": f"{newest}{node.os.pathsep}C:\\nodejs"}


def test_with_no_copy_cached_one_is_fetched(tmp_path):
    fetched = []

    def fetcher(need, root):
        fetched.append((need, root))
        return "24.5.0", root / "v24.5.0"

    got = node.provide(">=24", root=tmp_path, machine=lambda: "18.15.0", fetcher=fetcher)
    assert got.ok and got.version == "24.5.0" and fetched == [(">=24", tmp_path)]


def test_a_fetch_that_fails_names_the_shortfall(tmp_path):
    def fetcher(_need, _root):
        raise OSError("no network")

    got = node.provide(
        ">=24",
        "frontend/package.json engines.node",
        root=tmp_path,
        machine=lambda: "18.15.0",
        fetcher=fetcher,
    )
    assert not got.ok
    assert got.problem.startswith("node 18.15.0 < >=24 (frontend/package.json engines.node)")
    assert "no network" in got.problem


def test_an_unreadable_requirement_is_a_problem_not_a_crash(tmp_path):
    got = node.provide("banana", root=tmp_path, machine=lambda: "24.0.0")
    assert not got.ok and "unreadable" in got.problem


def test_npm_is_bound_to_the_provided_copy(tmp_path):
    provided = node.Provision(">=24", version="24.5.0", bin_dir=tmp_path)
    assert node.rebind((r"C:\nodejs\npm.cmd", "ci"), provided, windows=True) == (
        str(tmp_path / "npm.cmd"),
        "ci",
    )
    assert node.rebind(("npm", "ci"), provided, windows=False) == (str(tmp_path / "npm"), "ci")
    assert node.rebind(("uv", "sync"), provided) == ("uv", "sync")
    assert node.rebind(("npm", "ci"), node.Provision(">=24")) == ("npm", "ci")


def test_the_cache_is_per_user():
    assert (
        node.cache_root({"LOCALAPPDATA": r"C:\Users\u\AppData\Local"}, windows=True)
        == Path(r"C:\Users\u\AppData\Local") / "devkit" / "node"
    )
    assert node.cache_root({"XDG_CACHE_HOME": "/home/u/.cache"}, windows=False) == Path(
        "/home/u/.cache/devkit/node"
    )


# --- fetching --------------------------------------------------------------------------


INDEX = [
    {"version": "v25.1.0", "lts": False, "files": ["win-x64-zip", "linux-x64"]},
    {"version": "v24.5.0", "lts": "Krypton", "files": ["win-x64-zip", "linux-x64"]},
    {"version": "v24.4.0", "lts": "Krypton", "files": ["win-x64-zip"]},
    {"version": "v18.20.0", "lts": "Hydrogen", "files": ["win-x64-zip"]},
]


def test_choose_prefers_the_newest_lts_that_satisfies():
    assert node.choose(INDEX, ">=24", "win-x64-zip") == "24.5.0"
    assert node.choose(INDEX, ">=25", "win-x64-zip") == "25.1.0"
    assert node.choose(INDEX, ">=26", "win-x64-zip") == ""
    assert node.choose(INDEX, "^24", "osx-arm64-tar") == ""


def test_the_platform_names_the_release():
    assert node.platform_of("Windows", "AMD64") == node.Platform("win-x64-zip", "win-x64", ".zip")
    assert node.platform_of("Linux", "aarch64").suffix == ".tar.xz"
    with pytest.raises(ValueError):
        node.platform_of("Windows", "i386")


def _zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for name, data in members.items():
            bundle.writestr(name, data)
    return buffer.getvalue()


WIN = node.Platform("win-x64-zip", "win-x64", ".zip")


def _site(archive: bytes, digest: str | None = None):
    name = "node-v24.5.0-win-x64.zip"
    sums = f"{digest or hashlib.sha256(archive).hexdigest()}  {name}\n"
    pages = {
        f"{node.DIST}/index.json": json.dumps(INDEX).encode(),
        f"{node.DIST}/v24.5.0/SHASUMS256.txt": sums.encode(),
        f"{node.DIST}/v24.5.0/{name}": archive,
    }
    return pages.__getitem__


def test_a_fetch_unpacks_a_verified_release_into_the_cache(tmp_path):
    archive = _zip({"node-v24.5.0-win-x64/node.exe": b"MZ", "node-v24.5.0-win-x64/npm.cmd": b""})
    version, where = node.fetch(">=24", tmp_path, opener=_site(archive), target=WIN, windows=True)
    assert version == "24.5.0" and where == tmp_path / "v24.5.0"
    assert (where / "node.exe").read_bytes() == b"MZ"
    assert node.cached(tmp_path, ">=24", windows=True) == ("24.5.0", where)


def test_a_release_that_fails_its_digest_is_not_kept(tmp_path):
    archive = _zip({"node-v24.5.0-win-x64/node.exe": b"MZ"})
    with pytest.raises(ValueError, match="digest"):
        node.fetch(">=24", tmp_path, opener=_site(archive, "0" * 64), target=WIN, windows=True)
    assert node.cached(tmp_path, ">=24", windows=True) is None


def test_an_archive_escaping_its_directory_is_refused(tmp_path):
    archive = _zip({"../evil.exe": b"MZ"})
    with pytest.raises(ValueError, match="outside"):
        node.fetch(">=24", tmp_path, opener=_site(archive), target=WIN, windows=True)
    assert not (tmp_path.parent / "evil.exe").exists()


def test_render_spells_a_version_back():
    assert node.render((24, 5, 0)) == "24.5.0"


def test_unpack_extracts_a_tarball_and_refuses_one_escaping(tmp_path):
    import tarfile

    good = tmp_path / "good.tar.gz"
    payload = tmp_path / "node"
    payload.write_bytes(b"ELF")
    with tarfile.open(good, "w:gz") as bundle:
        bundle.add(payload, arcname="node-v24.5.0-linux-x64/bin/node")
    node.unpack(good, tmp_path / "out")
    assert (tmp_path / "out" / "node-v24.5.0-linux-x64" / "bin" / "node").read_bytes() == b"ELF"
    bad = tmp_path / "bad.tar.gz"
    with tarfile.open(bad, "w:gz") as bundle:
        bundle.add(payload, arcname="../escaped")
    with pytest.raises(tarfile.TarError):
        node.unpack(bad, tmp_path / "out2")
    assert not (tmp_path / "escaped").exists()


def test_download_reads_the_url_it_is_given(monkeypatch):
    seen = {}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def urlopen(request, timeout):
        seen["url"], seen["timeout"] = request.full_url, timeout
        return Response(b"[]")

    monkeypatch.setattr(node.urllib.request, "urlopen", urlopen)
    assert node.download(f"{node.DIST}/index.json") == b"[]"
    assert seen == {"url": f"{node.DIST}/index.json", "timeout": node.FETCH_TIMEOUT}


def test_the_machines_node_is_read_off_node_version(monkeypatch):
    monkeypatch.setattr(
        node.subprocess,
        "run",
        lambda argv, **_k: node.subprocess.CompletedProcess(argv, 0, "v18.15.0\n", ""),
    )
    assert node.machine_version(lambda _name: "/usr/bin/node") == "18.15.0"
    assert node.machine_version(lambda _name: None) == ""


def test_main_reports_what_the_checkout_gets(tmp_path, capsys):
    assert node.main([str(tmp_path)]) == 0
    assert "asks for no Node version" in capsys.readouterr().out
