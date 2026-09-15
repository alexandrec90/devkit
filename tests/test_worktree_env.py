"""`scripts/worktree_env.py`: the global post-checkout hook that keeps a worktree's
compose project off its checkout's.

The decisions are pure and driven as such. `main` is driven against real repos on disk,
because what it asks git -- `--git-common-dir` against `--git-dir`, and `check-ignore` --
is the thing under test, and a stub would only assert that the stub works.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from support import load_script

wt_env = load_script("scripts/worktree_env.py")

NULL40 = "0" * 40
NULL64 = "0" * 64
# A git object id, standing in for a commit that exists: `post-checkout` is handed one and
# only its all-zeros form means anything here. Not a credential -- detect-secrets reads
# any 40 hex characters as one.
REAL = "26cca5d8c62f3fca3e26bac581711fd66e196d05"  # pragma: allowlist secret


def _git(root: Path, *args: str):
    """git in `root`, with this machine's global config out of the way.

    `core.hooksPath` is a global branch policy here (`install-git-policy.py`), so a
    fixture repo left to inherit it would be judged by it -- and, once this hook is
    installed, would run the very thing under test."""
    return subprocess.run(
        ["git", "-C", str(root), "-c", f"core.hooksPath={root / '.nohooks'}", *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _repo(path: Path, compose: bool = True, ignore_env: bool = True) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.invalid")
    _git(path, "config", "user.name", "t")
    (path / ".gitignore").write_text(".env\n" if ignore_env else "logs/\n", encoding="utf-8")
    if compose:
        (path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "seed")
    return path


def _worktree(checkout: Path, at: Path, branch: str = "topic") -> Path:
    _git(checkout, "worktree", "add", "--quiet", str(at), "-b", branch)
    return at


# --- when this hook is even talking about a new tree ------------------------


@pytest.mark.parametrize(
    "old_oid, flag, fresh",
    [
        (NULL40, "1", True),  # `git worktree add`
        (NULL64, "1", True),  # ...in a sha256 repository
        (REAL, "1", False),  # an ordinary `git checkout <branch>`
        (NULL40, "0", False),  # `git checkout -- path`: a FILE checkout, no tree moved
        ("", "1", False),
        (NULL40, "", False),
    ],
)
def test_only_a_freshly_created_tree_is_this_hooks_business(old_oid, flag, fresh):
    """Both conditions carry weight. The flag separates a branch checkout from a file
    checkout, which moves no tree; the all-zero old OID separates a tree that has just
    come into existence from every `git checkout` in one that already had a `.env`."""
    assert wt_env.is_fresh_checkout(old_oid, flag) is fresh


# --- the name -----------------------------------------------------------------


def test_the_project_name_carries_the_repo_as_well_as_the_worktree():
    """Compose project names are global to the docker daemon rather than scoped per
    repository, so two repos each with a worktree called `main` would be one project."""
    assert wt_env.compose_project("carameli", "carameli") == "carameli-carameli"
    assert wt_env.compose_project("carameli", "tidy-munching-alpaca") == (
        "carameli-tidy-munching-alpaca"
    )


def test_the_name_is_never_the_checkouts_own():
    """The whole point: `<repo>-<worktree>` cannot equal `<repo>`, whatever the runtime
    called the directory -- which is what stops a Codex worktree named after its repo
    adopting the checkout's containers and volumes."""
    assert wt_env.compose_project("carameli", "carameli") != wt_env.compose_project(
        "carameli", ""
    ).rstrip("-")


def test_the_name_is_normalised_the_way_compose_normalises_one():
    assert wt_env.compose_project("Carameli", "Fix/320") == "carameli-fix320"
    assert wt_env.compose_project("_repo", "x").startswith("repo")


# --- what gets written --------------------------------------------------------


def test_an_absent_env_is_created_with_the_note_and_the_assignment():
    out = wt_env.rendered("", "carameli-topic")
    assert "COMPOSE_PROJECT_NAME=carameli-topic" in out
    assert "Delete the line" in out


def test_an_existing_env_is_appended_to_rather_than_rewritten():
    """The file is the project's, not devkit's. The box tier owns a whole managed block
    in a `.env` it seeded itself; this hook seeds nothing and adds one line."""
    out = wt_env.rendered("DATABASE_URL=postgres://x\n", "carameli-topic")
    assert out.startswith("DATABASE_URL=postgres://x\n")
    assert "COMPOSE_PROJECT_NAME=carameli-topic" in out


def test_a_file_with_no_trailing_newline_does_not_lose_its_last_line():
    out = wt_env.rendered("A=1", "n")
    assert "A=1\n" in out
    assert "COMPOSE_PROJECT_NAME=n" in out


@pytest.mark.parametrize(
    "existing",
    [
        "COMPOSE_PROJECT_NAME=mine\n",
        "  COMPOSE_PROJECT_NAME = mine\n",
        "export COMPOSE_PROJECT_NAME=mine\n",
    ],
)
def test_a_name_the_project_already_set_is_left_exactly_alone(existing):
    """A second assignment would silently win, so recognising the spellings git might
    have left there is the difference between idempotent and destructive."""
    assert wt_env.already_named(existing) is True
    assert wt_env.rendered(existing, "other") == existing


def test_a_similar_looking_key_is_not_that_key():
    assert wt_env.already_named("MY_COMPOSE_PROJECT_NAME=x\n") is False


def test_has_compose_file_accepts_every_name_compose_itself_looks_for(tmp_path):
    """A project that spells it `compose.yaml` has a stack just as much as one that
    spells it `docker-compose.yml`, and missing it would leave that project exposed."""
    for name in wt_env.COMPOSE_FILES:
        tree = tmp_path / name.replace(".", "_")
        tree.mkdir()
        assert wt_env.has_compose_file(tree) is False
        (tree / name).write_text("services: {}\n", encoding="utf-8")
        assert wt_env.has_compose_file(tree) is True


def test_ignores_env_reads_the_projects_own_gitignore(tmp_path):
    """`check-ignore --quiet` answers in its exit code and prints nothing, so this reads
    the code directly -- stdout cannot tell the two answers apart. Any failure answers
    False: the hook declines rather than creating a file in a repo it could not ask."""
    ignoring = _repo(tmp_path / "ignoring", ignore_env=True)
    tracking = _repo(tmp_path / "tracking", ignore_env=False)
    assert wt_env.ignores_env(ignoring) is True
    assert wt_env.ignores_env(tracking) is False
    assert wt_env.ignores_env(tmp_path / "not-a-repo") is False


# --- end to end, against real worktrees ---------------------------------------


def test_a_worktree_gets_a_project_name_of_its_own(tmp_path):
    """The hazard, closed: the worktree is deliberately named after the repo, which is
    what `codex --worktree` does and what makes compose adopt the checkout's stack."""
    checkout = _repo(tmp_path / "carameli")
    tree = _worktree(checkout, tmp_path / "elsewhere" / "carameli")

    assert wt_env.main([NULL40, REAL, "1"], root=tree) == 0
    written = (tree / ".env").read_text(encoding="utf-8")
    assert "COMPOSE_PROJECT_NAME=carameli-carameli" in written
    assert not (checkout / ".env").exists()


def test_running_twice_writes_the_same_file(tmp_path):
    checkout = _repo(tmp_path / "carameli")
    tree = _worktree(checkout, tmp_path / "wt")
    wt_env.main([NULL40, REAL, "1"], root=tree)
    once = (tree / ".env").read_text(encoding="utf-8")
    wt_env.main([NULL40, REAL, "1"], root=tree)
    assert (tree / ".env").read_text(encoding="utf-8") == once


def test_the_checkout_itself_is_never_written_to(tmp_path):
    """`--git-common-dir` equals `--git-dir` there, which is how a checkout is told from
    a worktree without any path convention -- the reason this answers for a nested Claude
    worktree, a detached Codex one and a `git worktree add` somebody typed."""
    checkout = _repo(tmp_path / "carameli")
    assert wt_env.checkout_of(checkout) is None
    assert wt_env.main([NULL40, REAL, "1"], root=checkout) == 0
    assert not (checkout / ".env").exists()


def test_a_worktree_with_no_stack_gets_nothing(tmp_path):
    """devkit's own worktrees have no compose file, and a `.env` created there would be a
    file nobody asked for in a repo with nothing to configure."""
    checkout = _repo(tmp_path / "devkit", compose=False)
    tree = _worktree(checkout, tmp_path / "wt")
    assert wt_env.main([NULL40, REAL, "1"], root=tree) == 0
    assert not (tree / ".env").exists()


def test_a_project_that_tracks_its_env_is_left_alone(tmp_path):
    """Creating an untracked file in a repo that does not ignore it would put a permanent
    entry in every `git status`, which is the cost this harness refuses elsewhere."""
    checkout = _repo(tmp_path / "carameli", ignore_env=False)
    tree = _worktree(checkout, tmp_path / "wt")
    assert wt_env.main([NULL40, REAL, "1"], root=tree) == 0
    assert not (tree / ".env").exists()


def test_an_ordinary_branch_checkout_in_an_existing_tree_writes_nothing(tmp_path):
    checkout = _repo(tmp_path / "carameli")
    tree = _worktree(checkout, tmp_path / "wt")
    assert wt_env.main([REAL, REAL, "1"], root=tree) == 0
    assert not (tree / ".env").exists()


def test_the_hook_never_fails_the_command_that_created_the_worktree(tmp_path):
    """git ignores a `post-checkout` status, so the only harm this could do is a
    traceback printed over somebody's `worktree add`. Every path returns 0, including a
    directory that is not a repository at all and a call with no arguments."""
    assert wt_env.main([], root=tmp_path) == 0
    assert wt_env.main([NULL40, REAL, "1"], root=tmp_path / "nowhere") == 0
