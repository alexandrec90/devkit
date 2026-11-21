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

    line, *_, ratchet = refresh.refresh(root, source, reconcile=reconcile)
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
    line, *_ = refresh.refresh(root, source)
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


def test_only_templates_whose_every_substitution_is_a_named_line_are_refreshed():
    """A rendered copy differs per project and nothing records what it was rendered with,
    so a `{{ }}` can match a past version only on a line `RENDERED_LINES` takes from the
    project's own copy. A substitution anywhere else would never match, silently."""
    for target, template in refresh.REFRESHED.items():
        text = (REPO_ROOT / template).read_text(encoding="utf-8")
        assert "{{" not in refresh.without_rendered(target, text), template
        assert not target.startswith("templates/")


LINT_TARGET = "scripts/lint-all.py"
LINT_TEMPLATE = refresh.REFRESHED[LINT_TARGET]
LINT_OLD = 'MYPY_SCOPE = ["{{ app_dir }}", "{{ tests_dir }}"]\nprint("no secrets pass")\n'
LINT_NEW = 'MYPY_SCOPE = ["{{ app_dir }}", "{{ tests_dir }}"]\nprint("scans for secrets")\n'


def lint_devkit(tmp_path: Path, *versions: str) -> Path:
    """A devkit whose lint template went through `versions`, the last one current."""
    root = tmp_path / "devkit"
    (root / LINT_TEMPLATE).parent.mkdir(parents=True)
    git(root, "init", "-q")
    for text in versions:
        (root / LINT_TEMPLATE).write_text(text, encoding="utf-8", newline="\n")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "template")
    return root


def rendered(text: str) -> str:
    return text.replace("{{ app_dir }}", "homelab/").replace("{{ tests_dir }}", "tests/")


def test_a_rendered_copy_is_refreshed_and_keeps_the_values_it_was_rendered_with(tmp_path):
    """05000136: homelab, photolab and three others hold `lint-all.py` as the template stood
    at 62b4aa95, whose only difference from it is `MYPY_SCOPE`. Their commit's secrets
    hook refused what their lint had just passed, and a template fix alone reaches none."""
    source = lint_devkit(tmp_path, LINT_OLD, LINT_NEW)
    root = project(tmp_path, rendered(LINT_OLD).encode())
    (root / LINT_TARGET).write_bytes(rendered(LINT_OLD).encode())
    assert refresh.refresh_one(root, LINT_TARGET, source).endswith(refresh.REFRESHED_LINE)
    assert (root / LINT_TARGET).read_text(encoding="utf-8") == rendered(LINT_NEW)
    assert "already" in refresh.refresh_one(root, LINT_TARGET, source)


def test_rendered_as_carries_the_projects_line_and_nothing_else():
    held = 'MYPY_SCOPE = ["homelab/"]\n'
    assert refresh.rendered_as(LINT_TARGET, LINT_NEW, held).startswith(held)
    assert refresh.rendered_as(LINT_TARGET, LINT_NEW, "no scope line\n") == LINT_NEW
    assert refresh.rendered_as(TARGET, NEW, held) == NEW, "no rendered line for this target"


def test_a_rendered_copy_edited_off_its_named_line_is_the_projects_own(tmp_path):
    """The named line is the only one a project's copy may differ on and still be devkit's."""
    source = lint_devkit(tmp_path, LINT_OLD, LINT_NEW)
    root = project(tmp_path, None)
    edited = rendered(LINT_OLD) + "print('a pass this project added')\n"
    (root / LINT_TARGET).write_bytes(edited.encode())
    assert "the project's own" in refresh.refresh_one(root, LINT_TARGET, source)
    assert (root / LINT_TARGET).read_bytes() == edited.encode()


def test_a_copy_with_no_line_to_keep_is_never_written_with_a_bare_substitution(tmp_path):
    """The template before its rendered line existed: a match, and nothing to render with."""
    bare = 'print("no scope line yet")\n'
    source = lint_devkit(tmp_path, bare, LINT_NEW)
    root = project(tmp_path, None)
    (root / LINT_TARGET).write_bytes(bare.encode())
    assert "the project's own" in refresh.refresh_one(root, LINT_TARGET, source)
    assert (root / LINT_TARGET).read_bytes() == bare.encode()


def test_the_task_file_every_project_holds_untouched_is_refreshed(tmp_path):
    """d02a3ddd: `.vscode/tasks.json` carries nothing per-project, and all eight consumers
    hold an earlier version, so a stale comment in it stays until this reaches them."""
    target = ".vscode/tasks.json"
    assert refresh.REFRESHED[target] == "templates/core/dot-vscode/tasks.json.tmpl"
    assert refresh.without_rendered(target, "any\ntext\n") == "any\ntext\n"


def test_the_cli_refreshes_the_named_project(tmp_path, capsys):
    source, root = devkit(tmp_path), project(tmp_path, OLD.encode())
    assert refresh.main([str(root), "--devkit", str(source)]) == 0
    assert "refreshed" in capsys.readouterr().out
    assert (root / TARGET).read_text(encoding="utf-8") == NEW
