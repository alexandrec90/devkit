"""Tests for `scripts/desktop.py`, the desktop tier's lint/test/release driver.

Vendored with it through the gated tier, so it runs in every desktop project's
harness-test step -- on a Linux runner with no .NET SDK. Every test here therefore
drives the pure half: the commands it would run, the names it derives, the refusals.
Nothing spawns `dotnet`.
"""

import argparse
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import REPO_ROOT, load_module

desktop = load_module("scripts/desktop.py")

CFG = desktop.Desktop(
    solution="App.slnx",
    project="src/App/App.csproj",
    pack_id="App",
    pack_title="My App",
    runtime="win-x64",
)
PROJECT = """\
<Project Sdk="Microsoft.NET.Sdk">
  <ItemGroup>
    <PackageReference Include="Velopack" Version="0.0.1298" />
  </ItemGroup>
</Project>
"""


def _consumer(root: Path, table: str) -> Path:
    """A project holding the vendored config reader and a `.devkit.toml`."""
    reader = root / "scripts" / "hooks" / "harness_config.py"
    reader.parent.mkdir(parents=True, exist_ok=True)
    reader.write_bytes((REPO_ROOT / "scripts" / "hooks" / "harness_config.py").read_bytes())
    (root / ".devkit.toml").write_text(table, encoding="utf-8")
    return root


# --- settings ------------------------------------------------------------------


def test_settings_derive_the_pack_id_and_title_from_the_project(tmp_path):
    root = _consumer(
        tmp_path,
        '[desktop]\nenabled = true\nsolution = "Skin.slnx"\nproject = "src/Skin/Skin.csproj"\n',
    )
    cfg = desktop.load_settings(root)
    assert (cfg.pack_id, cfg.pack_title, cfg.runtime) == ("Skin", "Skin", "win-x64")


def test_settings_keep_an_explicit_pack_id_and_title(tmp_path):
    root = _consumer(
        tmp_path,
        "[desktop]\nenabled = true\n"
        'solution = "S.slnx"\nproject = "src/S/S.csproj"\npack_id = "Acme.S"\n'
        'pack_title = "Acme S"\n',
    )
    cfg = desktop.load_settings(root)
    assert (cfg.pack_id, cfg.pack_title) == ("Acme.S", "Acme S")


@pytest.mark.parametrize(
    "table",
    [
        "",  # no table at all
        "[desktop]\nenabled = false\n",
        '[desktop]\nenabled = true\nsolution = "S.slnx"\n',  # no project
        '[desktop]\nenabled = true\nproject = "p.csproj"\n',  # no solution
    ],
)
def test_settings_refuse_rather_than_guess_a_solution(tmp_path, table):
    with pytest.raises(desktop.DesktopError):
        desktop.load_settings(_consumer(tmp_path, table))


def test_settings_refuse_a_tree_without_the_config_reader(tmp_path):
    """The bootstrap state: no vendored reader, so no answer -- and no default build."""
    (tmp_path / ".devkit.toml").write_text("[desktop]\nenabled = true\n", encoding="utf-8")
    with pytest.raises(desktop.DesktopError):
        desktop.load_settings(tmp_path)


# --- names derived from the project ----------------------------------------------


@pytest.mark.parametrize(
    ("tag", "version"),
    [
        ("v1.2.3", "1.2.3"),
        ("1.2.3", "1.2.3"),
        ("refs/tags/v0.4.0", "0.4.0"),
        ("v2.0.0-beta.1", "2.0.0-beta.1"),
    ],
)
def test_a_release_tag_becomes_its_semver(tag, version):
    assert desktop.release_version(tag) == version


@pytest.mark.parametrize("tag", ["", "v1.2", "latest", "v1.2.3.4", "main"])
def test_a_tag_that_is_not_semver_is_refused(tag):
    """Velopack orders releases by version; a tag it cannot order is no release."""
    with pytest.raises(desktop.DesktopError):
        desktop.release_version(tag)


def test_vpk_is_pinned_to_the_velopack_package_the_app_references():
    assert desktop.velopack_version(PROJECT) == "0.0.1298"
    single = PROJECT.replace('"', "'")
    assert desktop.velopack_version(single) == "0.0.1298"


def test_an_app_without_the_updater_names_no_vpk_version():
    assert desktop.velopack_version('<Project Sdk="Microsoft.NET.Sdk" />') == ""


def test_the_main_exe_follows_the_assembly_name():
    assert desktop.main_exe("src/App/App.csproj", PROJECT) == "App.exe"
    named = PROJECT.replace(
        "<ItemGroup>", "<PropertyGroup><AssemblyName>Skin</AssemblyName></PropertyGroup><ItemGroup>"
    )
    assert desktop.main_exe("src/App/App.csproj", named) == "Skin.exe"


@pytest.mark.parametrize(
    "remote",
    [
        "https://github.com/acme/skin.git",
        "https://github.com/acme/skin",
        "git@github.com:acme/skin.git",
    ],
)
def test_the_release_feed_is_read_from_the_origin_remote(remote):
    assert desktop.repo_url({}, remote) == "https://github.com/acme/skin"


def test_a_workflow_names_its_repository_directly():
    url = desktop.repo_url({"GITHUB_REPOSITORY": "acme/skin"}, "git@gitlab.com:x/y.git")
    assert url == "https://github.com/acme/skin"


def test_a_remote_that_is_not_github_is_refused():
    with pytest.raises(desktop.DesktopError):
        desktop.repo_url({}, "git@gitlab.com:acme/skin.git")


# --- the commands ----------------------------------------------------------------


def test_lint_fixes_the_format_locally_and_only_verifies_it_in_ci():
    local, build = desktop.lint_commands(CFG, check=False)
    assert local == ["dotnet", "format", "App.slnx"]
    checked, _ = desktop.lint_commands(CFG, check=True)
    assert checked[-1] == "--verify-no-changes"
    assert "-warnaserror" in build, "the build is the type check"


def test_publish_is_self_contained_at_the_release_version():
    command = desktop.publish_command(CFG, "1.2.3", "publish")
    assert command[:3] == ["dotnet", "publish", "src/App/App.csproj"]
    assert "--self-contained" in command and "-p:Version=1.2.3" in command
    assert command[command.index("-r") + 1] == "win-x64"


def test_pack_names_the_install_identity_and_signs_only_when_asked():
    command = desktop.pack_command("vpk", CFG, "1.2.3", "App.exe")
    assert command[command.index("--packId") + 1] == "App"
    assert command[command.index("--mainExe") + 1] == "App.exe"
    assert "--signParams" not in command
    signed = desktop.pack_command("vpk", CFG, "1.2.3", "App.exe", sign="/a /fd sha256")
    assert signed[-2:] == ["--signParams", "/a /fd sha256"]


def test_test_commands_run_the_whole_solution():
    assert desktop.test_commands(CFG) == [
        ["dotnet", "test", "App.slnx", "-c", "Release", "--nologo"]
    ]


def test_download_command_fetches_the_previous_release_into_the_pack_output():
    command = desktop.download_command("vpk", "https://github.com/a/b", "tok")
    assert command[:3] == ["vpk", "download", "github"]
    assert command[command.index("-o") + 1] == desktop.RELEASES_DIR, "where pack finds it"


def test_redact_hides_every_occurrence_and_nothing_without_a_mask():
    assert desktop.redact("a tok b tok", "tok") == "a *** b ***"
    assert desktop.redact("a tok", "") == "a tok"


def test_install_vpk_installs_into_its_own_directory_at_the_pinned_version(tmp_path, monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(shutil, "which", lambda name, path=None: str(Path(path) / name))
    found = desktop.install_vpk(tmp_path, "0.0.1298", run=_fake_run([0], seen))
    assert seen == [
        ["dotnet", "tool", "install", "vpk", "--tool-path", str(tmp_path), "--version", "0.0.1298"]
    ]
    assert found == str(tmp_path / "vpk")


def test_install_vpk_takes_the_current_vpk_for_an_app_without_the_updater(tmp_path, monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(shutil, "which", lambda name, path=None: str(Path(path) / name))
    desktop.install_vpk(tmp_path, "", run=_fake_run([0], seen))
    assert "--version" not in seen[0]


def test_a_failed_vpk_install_is_a_refusal(tmp_path):
    with pytest.raises(desktop.DesktopError, match="could not install vpk"):
        desktop.install_vpk(tmp_path, "1.0.0", run=_fake_run([1], []))


def test_git_remote_is_empty_outside_a_repository(tmp_path):
    """Read only for `--publish`, where `repo_url` then refuses with the remote it saw."""
    assert desktop.git_remote(tmp_path) == ""


def test_upload_publishes_the_release_under_its_tag():
    command = desktop.upload_command("vpk", "https://github.com/a/b", "tok", CFG, "1.2.3")
    assert command[command.index("--tag") + 1] == "v1.2.3"
    assert "--publish" in command
    assert command[command.index("--releaseName") + 1] == "My App 1.2.3"


# --- running and reporting ---------------------------------------------------------


def _fake_run(returncodes, seen):
    def run(command, **_kwargs):
        seen.append(command)
        code = returncodes.pop(0)
        return subprocess.CompletedProcess(command, code, stdout="out tok\n", stderr="err\n")

    return run


def test_run_all_stops_at_the_first_failure_and_names_it(tmp_path):
    seen: list[list[str]] = []
    failure = desktop.run_all([["a"], ["b"], ["c"]], tmp_path, run=_fake_run([0, 3, 0], seen))
    assert seen == [["a"], ["b"]], "nothing runs after a failed step"
    assert failure.startswith("# b\n# exit 3")


def test_run_all_is_empty_when_every_step_passes(tmp_path):
    assert desktop.run_all([["a"]], tmp_path, run=_fake_run([0], [])) == ""


def test_the_token_never_reaches_the_artifact(tmp_path):
    failure = desktop.run_all(
        [["vpk", "upload", "--token", "tok"]], tmp_path, mask="tok", run=_fake_run([1], [])
    )
    assert "tok" not in failure and "***" in failure


def test_vpk_may_run_on_a_newer_dotnet_than_it_was_built_for():
    """Found by packing a real release: `vpk` targeted an older runtime than the SDK
    `global.json` pins, and refused to start on a machine holding only that SDK."""
    env = desktop.vpk_env({"PATH": "x"})
    assert env == {"PATH": "x", "DOTNET_ROLL_FORWARD": "Major"}


def test_run_all_hands_its_environment_to_every_step(tmp_path):
    seen_env = []

    def run(command, **kwargs):
        seen_env.append(kwargs.get("env"))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    desktop.run_all([["a"], ["b"]], tmp_path, run=run, env={"K": "v"})
    assert seen_env == [{"K": "v"}, {"K": "v"}]


def test_a_command_that_cannot_start_is_a_failure_not_a_crash(tmp_path):
    def run(command, **_kwargs):
        raise FileNotFoundError(command[0])

    assert "could not start" in desktop.run_all([["nope"]], tmp_path, run=run)


def test_the_artifact_is_written_on_a_pass_too(tmp_path, monkeypatch):
    """Empty on success, so a stale failure never sends the next agent after a fixed one."""
    monkeypatch.setattr(desktop, "LOGS", tmp_path)
    path = desktop.write_artifact("lint", "# boom\n")
    assert path.read_text(encoding="utf-8").startswith("# source: scripts/desktop.py lint")
    desktop.write_artifact("lint", "")
    assert path.read_text(encoding="utf-8") == ""


def test_a_publish_without_a_token_is_refused_before_anything_runs(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    (tmp_path / "src" / "App").mkdir(parents=True)
    (tmp_path / "src" / "App" / "App.csproj").write_text(PROJECT, encoding="utf-8")
    with pytest.raises(desktop.DesktopError, match="GITHUB_TOKEN"):
        desktop.release(CFG, "v1.0.0", publish=True, root=tmp_path)


def test_a_machine_without_dotnet_is_told_which_sdk_to_install(tmp_path, monkeypatch):
    root = _consumer(
        tmp_path, '[desktop]\nenabled = true\nsolution = "S.slnx"\nproject = "p/P.csproj"\n'
    )
    monkeypatch.setattr(shutil, "which", lambda *_a, **_k: None)
    with pytest.raises(desktop.DesktopError, match=r"global\.json"):
        desktop.dispatch(argparse.Namespace(verb="test"), root)


def test_every_verb_parses_and_release_requires_a_version():
    parser = desktop._parser()
    assert parser.parse_args(["lint", "--check"]).check is True
    assert parser.parse_args(["test"]).verb == "test"
    assert parser.parse_args(["release", "--version", "v1.0.0"]).publish is False
    with pytest.raises(SystemExit):
        parser.parse_args(["release"])
