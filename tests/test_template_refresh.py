"""An untouched copy of a devkit template is brought up to the current one; an edited
copy is left alone (114da279)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from support import REPO_ROOT, load_script

refresh = load_script("scripts/template_refresh.py")

TARGET = "scripts/run-tests.py"
TEMPLATE = refresh.REFRESHED[TARGET]
OLD = "print('the whole suite')\n"
NEW = "print('the tests for what changed')\n"


def git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


def devkit(tmp_path: Path) -> Path:
    """A devkit whose template was `OLD` and is now `NEW`."""
    root = tmp_path / "devkit"
    (root / TEMPLATE).parent.mkdir(parents=True)
    git(root, "init", "-q")
    for text in (OLD, NEW):
        (root / TEMPLATE).write_text(text, encoding="utf-8", newline="\n")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", text.strip())
    return root


def project(tmp_path: Path, data: bytes | None) -> Path:
    root = tmp_path / "project"
    (root / "scripts").mkdir(parents=True)
    if data is not None:
        (root / TARGET).write_bytes(data)
    return root


def no_reconcile(project: Path, rewritten: tuple[str, ...]) -> None:
    raise AssertionError(f"reconciled {project} though nothing was rewritten: {rewritten}")


def test_an_untouched_earlier_template_is_refreshed(tmp_path):
    """roguelike, social-scraper and sports_betting held `run-tests.py.tmpl` as it stood
    at e9eb759, which runs the whole suite bare, weeks after #391 changed the default."""
    source, root = devkit(tmp_path), project(tmp_path, OLD.encode())
    seen: list[tuple[Path, tuple[str, ...]]] = []

    def reconcile(project: Path, rewritten: tuple[str, ...]) -> tuple[int, int]:
        seen.append((project, rewritten))
        return 0, 2

    line, ratchet = refresh.refresh(root, source, reconcile=reconcile)
    assert "refreshed" in line
    assert (root / TARGET).read_text(encoding="utf-8") == NEW
    assert seen == [(root, (TARGET,))]
    assert ratchet == refresh.ratchet_line((0, 2))
    assert "recorded 2 gap(s)" in ratchet


def test_the_ratchet_line_says_when_nothing_was_reconciled(tmp_path):
    assert "not reconciled" in refresh.ratchet_line(None)
    assert "dropped 1 line(s)" in refresh.ratchet_line((1, 0))
    # A project with no baseline has adopted no ratchet, so there is nothing to carry.
    assert refresh.reconcile_untested(project(tmp_path, OLD.encode()), (TARGET,)) is None


def committed_project(tmp_path: Path, runner: str, baseline: str) -> Path:
    """A consumer with the vendored scanner, its baseline and `runner` committed."""
    root = project(tmp_path, runner.encode())
    hooks = root / "scripts" / "hooks"
    hooks.mkdir()
    for name in ("untested_symbols.py", "harness_config.py", "code_text.py"):
        (hooks / name).write_bytes((REPO_ROOT / "scripts" / "hooks" / name).read_bytes())
    (root / ".devkit-untested.txt").write_text(baseline, encoding="utf-8", newline="\n")
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "generated")
    return root


def test_a_refresh_records_the_gaps_the_newer_template_added(tmp_path):
    """roguelike #48: the pull reconciled the baseline, then the refresh brought in
    `run-tests.py`'s `changed_paths`, `default_branch`, `tests_for` and `with_basetemp`,
    and the vendored gate read devkit's four functions as debt the project wrote."""
    old, new = "def alpha():\n    pass\n", "def alpha():\n    pass\n\n\ndef beta():\n    pass\n"
    source = tmp_path / "devkit"
    (source / TEMPLATE).parent.mkdir(parents=True)
    git(source, "init", "-q")
    for text in (old, new):
        (source / TEMPLATE).write_text(text, encoding="utf-8", newline="\n")
        git(source, "add", "-A")
        git(source, "commit", "-q", "-m", "template")
    us = load_script("scripts/hooks/untested_symbols.py")
    root = committed_project(tmp_path, old, "")
    # Seeded as a generated project's is: every gap the tree holds, the runner's included.
    seeded = us.render_baseline(us.gaps(root, us.harness_config.load(root)))
    (root / ".devkit-untested.txt").write_text(seeded, encoding="utf-8", newline="\n")
    git(root, "commit", "-q", "-am", "seed")

    lines = refresh.refresh(root, source)

    assert (root / TARGET).read_text(encoding="utf-8") == new
    assert lines[-1] == refresh.ratchet_line((0, 1))
    recorded = (root / ".devkit-untested.txt").read_text(encoding="utf-8")
    assert f"{TARGET}::beta" in recorded.splitlines()
    assert us.verdict(root, us.harness_config.load(root)) == ([], [])


def test_a_copy_checked_out_with_crlf_is_still_the_template_and_keeps_its_endings(tmp_path):
    source = devkit(tmp_path)
    root = project(tmp_path, OLD.replace("\n", "\r\n").encode())
    assert "refreshed" in refresh.refresh_one(root, TARGET, source)
    assert (root / TARGET).read_bytes() == NEW.replace("\n", "\r\n").encode()


def test_the_projects_own_runner_is_left_alone(tmp_path):
    """carameli's runs in a container and ibkr_trader's delegates to its own artifact
    runner: what they chose instead of the template is theirs."""
    source, root = devkit(tmp_path), project(tmp_path, b"print('in the container')\n")
    [line] = refresh.refresh(root, source)
    assert "the project's own" in line
    assert (root / TARGET).read_bytes() == b"print('in the container')\n"


def test_a_current_copy_and_a_missing_one_are_said_and_not_written(tmp_path):
    source = devkit(tmp_path)
    current = project(tmp_path, NEW.encode())
    assert "already" in refresh.refresh(current, source)[0]
    (current / TARGET).unlink()
    assert "not there" in refresh.refresh(current, source)[0]
    assert not (current / TARGET).exists()


def test_a_devkit_git_cannot_read_refreshes_nothing(tmp_path):
    """No history is no evidence the copy is untouched, so nothing is replaced."""
    source = tmp_path / "devkit"
    (source / TEMPLATE).parent.mkdir(parents=True)
    (source / TEMPLATE).write_text(NEW, encoding="utf-8")
    root = project(tmp_path, OLD.encode())
    assert refresh.past_versions(source, TEMPLATE) == set()
    assert "the project's own" in refresh.refresh(root, source)[0]


def test_only_templates_with_nothing_to_substitute_are_refreshed():
    """A rendered copy of a template with `{{ }}` differs per project, and nothing
    records what it was rendered with, so it can never match a past version."""
    for target, template in refresh.REFRESHED.items():
        text = (REPO_ROOT / template).read_text(encoding="utf-8")
        assert "{{" not in text, template
        assert not target.startswith("templates/")


def test_the_cli_refreshes_the_named_project(tmp_path, capsys):
    source, root = devkit(tmp_path), project(tmp_path, OLD.encode())
    assert refresh.main([str(root), "--devkit", str(source)]) == 0
    assert "refreshed" in capsys.readouterr().out
    assert (root / TARGET).read_text(encoding="utf-8") == NEW
