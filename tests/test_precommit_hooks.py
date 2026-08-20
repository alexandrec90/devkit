"""Tests for the hooks devkit publishes as a pre-commit channel.

Every test here is a *negative* case first. A pre-commit hook that passes is
indistinguishable from a pre-commit hook that does nothing, and the second kind is worse
than none at all: it occupies the slot where a real check would go and reports green while
doing it. So each hook is shown rejecting the thing it exists to reject, and only then
accepting a sound input.

The hooks are driven as subprocesses where they read `Path.cwd()` (which is how pre-commit
invokes them) and imported where they are pure.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from support import REPO_ROOT, load_script

PRECOMMIT = REPO_ROOT / "scripts" / "precommit"

check_manifest = load_script("scripts/precommit/check_harness_manifest.py")
check_stdlib = load_script("scripts/precommit/check_stdlib_only.py")

# A manifest that should pass everything, as a starting point for the negative cases.
SOUND_MANIFEST = """
[project]
env_prefix = "PROBE"

[paths]
app = "app/"
tests = "tests/"
unit_tests = "tests"

[db]
enabled = false

[frontend]
enabled = false
"""


def _project(tmp_path: Path, manifest: str) -> Path:
    """A throwaway repo with the directories a sound manifest declares."""
    (tmp_path / "app").mkdir()
    (tmp_path / "tests").mkdir()
    path = tmp_path / ".devkit.toml"
    path.write_text(manifest, encoding="utf-8")
    return path


# --- devkit-manifest ----------------------------------------------------


def test_manifest_hook_accepts_a_sound_manifest(tmp_path):
    assert check_manifest.check(_project(tmp_path, SOUND_MANIFEST)) == []


@pytest.mark.parametrize(
    "pin", ["", '[python]\nversion = "3.12"\n', '[python]\nversion = "pypy@3.10"\n']
)
def test_manifest_hook_accepts_an_absent_or_plain_interpreter_pin(tmp_path, pin):
    """Absent is the default and must stay silent; `worktree.py provision` then uses the
    interpreter running it, which is right for a project with no pin."""
    assert check_manifest.check(_project(tmp_path, SOUND_MANIFEST + pin)) == []


def test_manifest_hook_rejects_an_interpreter_pin_uv_could_not_resolve(tmp_path):
    """A box's provisioning failure is downgraded to a warning, so a typo here produces a
    box with no `.venv` that still announces itself provisioned."""
    problems = check_manifest.check(
        _project(tmp_path, SOUND_MANIFEST + '[python]\nversion = "python 3.12"\n')
    )
    assert len(problems) == 1
    assert "[python] version" in problems[0]


def test_manifest_hook_rejects_unparseable_toml(tmp_path):
    """The failure mode the whole hook exists for: `load()` would return defaults."""
    problems = check_manifest.check(_project(tmp_path, "[project\nenv_prefix = "))
    assert len(problems) == 1
    assert "not valid TOML" in problems[0]
    # The message must say *why* nothing else catches it, or the next person assumes
    # some other gate would have.
    assert "neutral defaults" in problems[0]


def test_manifest_hook_rejects_a_path_prefix_without_its_trailing_slash(tmp_path):
    manifest = SOUND_MANIFEST.replace('app = "app/"', 'app = "app"')
    problems = check_manifest.check(_project(tmp_path, manifest))
    assert any("must end with '/'" in p for p in problems)


def test_manifest_hook_rejects_paths_that_do_not_exist_here(tmp_path):
    """devkit itself shipped a manifest describing another project's directories."""
    manifest = SOUND_MANIFEST.replace('app = "app/"', 'app = "nonexistent/"')
    problems = check_manifest.check(_project(tmp_path, manifest))
    assert any("not a directory in this repo" in p for p in problems)


def test_manifest_hook_rejects_a_half_filled_db_block(tmp_path):
    manifest = SOUND_MANIFEST.replace(
        "[db]\nenabled = false",
        '[db]\nenabled = true\nuser = "probe"\nname = "probe"',
    )
    problems = check_manifest.check(_project(tmp_path, manifest))
    # password is missing, and url_env defaults are fine, so the password is the finding.
    assert any("password is empty" in p for p in problems)


def test_manifest_hook_rejects_a_db_service_missing_from_services(tmp_path):
    manifest = SOUND_MANIFEST.replace(
        "[db]\nenabled = false",
        '[db]\nenabled = true\nuser = "u"\npassword = "p"\nname = "n"\n'
        'services = ["redis"]\ndb_service = "db"',
    )
    problems = check_manifest.check(_project(tmp_path, manifest))
    assert any("is not in services" in p for p in problems)


def test_manifest_hook_rejects_a_blank_or_lowercase_env_prefix(tmp_path):
    lower = SOUND_MANIFEST.replace('env_prefix = "PROBE"', 'env_prefix = "probe"')
    assert any("UPPER_CASE" in p for p in check_manifest.check(_project(tmp_path, lower)))


def test_manifest_hook_exits_nonzero_and_names_the_file(tmp_path, capsys):
    path = _project(tmp_path, "[project\n")
    assert check_manifest.main([str(path)]) == 1
    assert str(path) in capsys.readouterr().out


def test_manifest_hook_is_a_no_op_with_no_filenames():
    assert check_manifest.main([]) == 0


# --- devkit-hooks-stdlib-only -------------------------------------------------


def test_stdlib_hook_rejects_a_third_party_import(tmp_path):
    script = tmp_path / "hook.py"
    script.write_text("import requests\n", encoding="utf-8")
    problems = check_stdlib.check(script, allowed_first_party=set())
    assert len(problems) == 1 and "requests" in problems[0]


def test_stdlib_hook_accepts_stdlib_and_future_imports(tmp_path):
    script = tmp_path / "hook.py"
    script.write_text(
        "from __future__ import annotations\nimport json, subprocess\nfrom pathlib import Path\n",
        encoding="utf-8",
    )
    assert check_stdlib.check(script, allowed_first_party=set()) == []


def test_stdlib_hook_accepts_a_first_party_sibling(tmp_path):
    """`stop.py` does `import harness_config`; that must not read as third-party."""
    (tmp_path / "harness_config.py").write_text("", encoding="utf-8")
    script = tmp_path / "stop.py"
    script.write_text("import harness_config\n", encoding="utf-8")
    allowed = check_stdlib.first_party_modules([script])
    assert "harness_config" in allowed
    assert check_stdlib.check(script, allowed) == []


def test_stdlib_hook_ignores_relative_imports(tmp_path):
    script = tmp_path / "hook.py"
    script.write_text("from . import sibling\nfrom .pkg import thing\n", encoding="utf-8")
    assert check_stdlib.check(script, allowed_first_party=set()) == []


def test_stdlib_hook_reports_unparseable_python(tmp_path):
    script = tmp_path / "hook.py"
    script.write_text("def broken(\n", encoding="utf-8")
    assert any("not parseable" in p for p in check_stdlib.check(script, set()))


def test_stdlib_hook_passes_on_the_real_harness():
    """The contract this enforces must actually hold in devkit right now."""
    scripts = [p for p in (REPO_ROOT / "scripts" / "hooks").glob("*.py") if p.is_file()]
    assert scripts, "no hook scripts found"
    allowed = check_stdlib.first_party_modules(scripts)
    for script in scripts:
        assert check_stdlib.check(script, allowed) == [], f"{script.name} broke stdlib-only"


# --- devkit-drift -------------------------------------------------------------


def _manifest_paths() -> tuple[str, ...]:
    sync = load_script("scripts/sync-devkit.py")
    return sync.MANIFEST


def _fake_consumer(tmp_path: Path) -> Path:
    """A repo with devkit's vendored files copied in, i.e. perfectly in sync."""
    root = tmp_path / "consumer"
    for rel in _manifest_paths():
        src = REPO_ROOT / rel
        if not src.exists():
            continue
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    return root


def _run_drift(cwd: Path, encoding: str | None = None) -> subprocess.CompletedProcess[str]:
    env = None
    if encoding is not None:
        env = {**os.environ, "PYTHONIOENCODING": encoding}
    return subprocess.run(
        [sys.executable, str(PRECOMMIT / "check_harness_drift.py")],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
    )


def test_drift_hook_passes_on_an_in_sync_consumer(tmp_path):
    result = _run_drift(_fake_consumer(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "match devkit" in result.stdout


def test_drift_hook_fails_on_a_modified_vendored_file(tmp_path):
    root = _fake_consumer(tmp_path)
    target = root / "scripts" / "hooks" / "harness_config.py"
    target.write_text(target.read_text(encoding="utf-8") + "\n# local edit\n", encoding="utf-8")
    result = _run_drift(root)
    assert result.returncode == 1
    assert "harness_config.py (modified)" in result.stdout
    # The remedy must be in the output; "drifted" alone leaves the reader stuck.
    assert "--pull" in result.stdout and "--push" in result.stdout


def test_drift_hook_fails_on_a_vendored_file_that_was_never_adopted(tmp_path):
    root = _fake_consumer(tmp_path)
    (root / "scripts" / "hooks" / "harness_config.py").unlink()
    result = _run_drift(root)
    assert result.returncode == 1
    assert "absent here" in result.stdout


def test_drift_hook_reports_drift_on_a_legacy_codepage(tmp_path):
    """Regression: the report died on its own output before naming the drifted file.

    pre-commit pipes hook stdout, so on a Windows consumer it encodes as cp1252.
    The per-file line used U+2717, which cp1252 cannot map -- the print raised and
    the report truncated to the header, so the hook failed the commit without ever
    saying which file drifted. Pinned with an explicit codepage so the regression is
    caught on any CI runner, not only a Windows one.
    """
    root = _fake_consumer(tmp_path)
    target = root / "scripts" / "hooks" / "harness_config.py"
    target.write_text(target.read_text(encoding="utf-8") + "\n# local edit\n", encoding="utf-8")

    result = _run_drift(root, encoding="cp1252")

    assert "UnicodeEncodeError" not in result.stderr, result.stderr
    assert result.returncode == 1, result.stdout + result.stderr
    assert "harness_config.py (modified)" in result.stdout
    assert "--pull" in result.stdout


def test_drift_hook_skips_inside_devkit_itself():
    """The comparison is vacuous here; saying so beats reporting a vacuous pass."""
    result = _run_drift(REPO_ROOT)
    assert result.returncode == 0
    assert "this IS devkit" in result.stdout


# --- the pin the generator hands to new projects --------------------------------


def test_ref_check_distinguishes_absent_from_unanswerable():
    """A bogus ref must read as "cannot answer", not as "the channel is unpublished".

    Both exit non-zero from `git cat-file`, and conflating them makes the generator print a
    warning about a missing file when the real problem is a tag that does not exist.
    """
    new_project = load_script("scripts/new-project.py")
    assert new_project.ref_publishes_pre_commit_hooks("definitely-not-a-ref") is None
    # A tag cut before this channel existed resolves fine but has no hooks file.
    assert new_project.ref_publishes_pre_commit_hooks("v0.4.1") is False


# --- the published channel -----------------------------------------------------


def test_published_hooks_point_at_scripts_that_exist_and_are_executable():
    """`language: script` means pre-commit execs the file directly — it needs the bit set.

    A missing executable bit fails only on a consumer's machine, at commit time, after the
    rev is already tagged and pinned.
    """
    yaml = pytest.importorskip("yaml")
    hooks = yaml.safe_load((REPO_ROOT / ".pre-commit-hooks.yaml").read_text(encoding="utf-8"))
    assert hooks, "no hooks published"
    for hook in hooks:
        entry = Path(hook["entry"])
        target = REPO_ROOT / entry
        assert target.exists(), f"{hook['id']} points at missing {entry}"
        assert hook["language"] == "script", f"{hook['id']} is not language: script"
        first_line = target.read_text(encoding="utf-8").splitlines()[0]
        assert first_line.startswith("#!"), f"{entry} has no shebang but runs as a script"
        # The mode *git records* is what a consumer's clone gets — not the mode in this
        # working tree, which a local chmod could have fixed after the fact.
        recorded = subprocess.run(
            ["git", "ls-files", "-s", "--", str(entry)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        ).stdout.split()
        assert recorded, f"{entry} is not tracked by git, so no consumer can run it"
        assert recorded[0] == "100755", (
            f"{entry} is committed as {recorded[0]}, not 100755 — pre-commit execs it "
            f"directly, so a consumer's clone would fail with 'permission denied'"
        )


def test_published_hook_ids_match_devkits_own_local_config():
    """devkit must run the same hooks it publishes, or the channel is untested.

    Its config wires them as `repo: local` (pinning a rev here would validate a released
    tag's hooks instead of the working tree), so the ids are the only link between the two
    files — and a typo in either silently drops a check.
    """
    yaml = pytest.importorskip("yaml")
    published = {
        h["id"]
        for h in yaml.safe_load((REPO_ROOT / ".pre-commit-hooks.yaml").read_text(encoding="utf-8"))
    }
    config = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    local = {h["id"] for repo in config["repos"] if repo["repo"] == "local" for h in repo["hooks"]}
    # devkit-drift is the documented exception: in devkit it compares against itself.
    assert published - local == {"devkit-drift"}, (
        f"published but not run here: {sorted(published - local - {'devkit-drift'})}"
    )
