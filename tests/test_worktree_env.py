"""`scripts/worktree_env.py`: the global post-checkout hook that keeps a worktree's
compose project off its checkout's.

The decisions are pure and driven as such. `main` is driven against real repos on disk,
because what it asks git -- `--git-common-dir` against `--git-dir`, and `check-ignore` --
is the thing under test, and a stub would only assert that the stub works.
"""

from __future__ import annotations

import shutil
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


def test_name_compose_project_reports_a_write_and_is_silent_on_a_rerun(tmp_path):
    checkout = _repo(tmp_path / "carameli")
    tree = _worktree(checkout, tmp_path / "wt")
    assert "COMPOSE_PROJECT_NAME=carameli-wt" in wt_env.name_compose_project(tree, checkout)
    assert wt_env.name_compose_project(tree, checkout) == ""


# --- the worktree's own interpreter -------------------------------------------
# A `claude --worktree` tree starts with no `.venv`, so every test run in it borrows the
# checkout's interpreter. The agent hooks that used to close that gap are the ones
# `DEVKIT_HOOKS_OFF` switches off; this hook fires whoever cut the tree, and provisions
# under the conditions that keep it seconds rather than minutes.


def _facts(**overrides):
    base = dict(locked=True, own_venv=False, checkout_venv=True, uv="uv")
    base.update(overrides)
    return wt_env.Toolchain(**base)


def test_a_locked_worktree_of_a_provisioned_checkout_gets_a_uv_sync():
    assert _facts().command() == ("uv", "sync", "--all-extras", "--all-groups")


def test_the_manifests_python_pin_reaches_the_sync():
    """`requires-python` in a lock is a floor, so a project pinned to 3.12 resolves
    happily on 3.14 unless the version is passed through -- the same reason
    `worktree.provision_steps` appends it to its own `uv sync`."""
    assert _facts(python_version="3.12").command()[-2:] == ("--python", "3.12")


@pytest.mark.parametrize(
    "overrides, why",
    [
        ({"locked": False}, "not uv-locked: the other ladders are shell strings"),
        ({"own_venv": True}, "already provisioned"),
        ({"checkout_venv": False}, "a cold checkout: nothing says the uv cache is warm"),
        ({"uv": None}, "no uv on PATH"),
        ({"install_command": "make dev"}, "a manifest install_command is a shell string"),
    ],
)
def test_every_other_shape_is_left_to_the_provision_verb(overrides, why):
    assert _facts(**overrides).command() == (), why


def test_the_facts_are_read_off_disk(tmp_path):
    here, checkout = tmp_path / "wt", tmp_path / "co"
    here.mkdir()
    checkout.mkdir()
    (here / "uv.lock").write_text("", encoding="utf-8")
    (checkout / ".venv").mkdir()
    facts = wt_env.Toolchain.observe(here, checkout, uv="C:/tools/uv.exe")
    assert (facts.locked, facts.own_venv, facts.checkout_venv) == (True, False, True)
    assert facts.command()[0] == "C:/tools/uv.exe"


HARNESS_CONFIG_SRC = Path(__file__).resolve().parents[1] / "scripts" / "hooks" / "harness_config.py"


def _vendored(tree: Path, manifest: str) -> Path:
    """A tree carrying the harness, the way a consumer's worktree does."""
    hooks = tree / "scripts" / "hooks"
    hooks.mkdir(parents=True)
    shutil.copy(HARNESS_CONFIG_SRC, hooks / "harness_config.py")
    (tree / ".devkit.toml").write_text(manifest, encoding="utf-8")
    return tree


def test_the_pin_is_read_through_the_trees_own_vendored_harness_config(tmp_path):
    """This hook is installed machine-wide with no repo of its own, so a project's
    `[python]` table is read by the reader that ships beside it -- one copy of the
    defaults, not a TOML parse of our own."""
    tree = _vendored(tmp_path, '[python]\nversion = "3.12"\n')
    assert wt_env.manifest_python(tree) == ("", "3.12")


def test_a_tree_without_a_readable_harness_is_unpinned_rather_than_an_error(tmp_path):
    assert wt_env.manifest_python(tmp_path) == ("", "")
    broken = tmp_path / "broken" / "scripts" / "hooks"
    broken.mkdir(parents=True)
    (broken / "harness_config.py").write_text("def load(root:\n", encoding="utf-8")
    assert wt_env.manifest_python(tmp_path / "broken") == ("", "")
    stale = tmp_path / "stale" / "scripts" / "hooks"
    stale.mkdir(parents=True)
    (stale / "harness_config.py").write_text("def load(root):\n    return object()\n")
    assert wt_env.manifest_python(tmp_path / "stale") == ("", "")


class _Run:
    """A fake `subprocess.run` that records the call and answers as told."""

    def __init__(self, returncode: int = 0, stderr: str = "", raising: BaseException | None = None):
        self.calls: list[tuple[list[str], dict]] = []
        self.returncode = returncode
        self.stderr = stderr
        self.raising = raising

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.raising is not None:
            raise self.raising
        return subprocess.CompletedProcess(argv, self.returncode, "", self.stderr)


def _provisionable(tmp_path: Path) -> tuple[Path, Path]:
    """A uv-locked project whose checkout is provisioned, and a fresh worktree of it."""
    checkout = _repo(tmp_path / "proj", compose=False)
    (checkout / ".venv").mkdir()
    tree = _worktree(checkout, tmp_path / "wt")
    (tree / "uv.lock").write_text("", encoding="utf-8")
    return checkout, tree


def test_the_sync_runs_in_the_worktree_and_says_so(tmp_path):
    checkout, tree = _provisionable(tmp_path)
    run = _Run()
    line = wt_env.provision(tree, checkout, runner=run, environ={}, uv="uv")
    [(argv, kwargs)] = run.calls
    assert argv == ["uv", "sync", "--all-extras", "--all-groups"]
    assert Path(kwargs["cwd"]) == tree
    assert kwargs["timeout"] == wt_env.PROVISION_TIMEOUT
    assert line.startswith("devkit: .venv provisioned by `uv sync")


def test_a_failed_sync_relays_its_tail_and_the_command_to_rerun(tmp_path):
    checkout, tree = _provisionable(tmp_path)
    run = _Run(1, stderr="resolving\nerror: no index\n")
    line = wt_env.provision(tree, checkout, runner=run, environ={}, uv="uv")
    assert "failed" in line
    assert "error: no index" in line
    assert "uv sync --all-extras --all-groups" in line


def test_a_sync_that_hangs_or_cannot_start_is_named_not_waited_for(tmp_path):
    """The timeout is for the day the network is gone: `git worktree add` still
    returns, and the line says what to run once it is back."""
    checkout, tree = _provisionable(tmp_path)
    hang = subprocess.TimeoutExpired(cmd="uv", timeout=1)
    line = wt_env.provision(tree, checkout, runner=_Run(raising=hang), environ={}, uv="uv")
    assert "did not finish" in line and "by hand" in line
    gone = _Run(raising=FileNotFoundError("uv"))
    assert "could not run" in wt_env.provision(tree, checkout, runner=gone, environ={}, uv="uv")


def test_the_opt_out_runs_nothing(tmp_path):
    checkout, tree = _provisionable(tmp_path)
    run = _Run()
    off = {wt_env.SKIP_PROVISION_VAR: "1"}
    assert wt_env.provision(tree, checkout, runner=run, environ=off, uv="uv") == ""
    assert run.calls == []


def test_a_checkout_without_a_venv_gets_no_sync_at_worktree_time(tmp_path):
    """The cold path. Without the checkout's `.venv` nothing says this machine has the
    project's uv cache, and a cold sync inside `git worktree add` is minutes;
    `ship.py --preflight` still names the command for whoever can spend them."""
    checkout = _repo(tmp_path / "proj", compose=False)
    tree = _worktree(checkout, tmp_path / "wt")
    (tree / "uv.lock").write_text("", encoding="utf-8")
    run = _Run()
    assert wt_env.provision(tree, checkout, runner=run, environ={}, uv="uv") == ""
    assert run.calls == []


def test_main_provisions_the_tree_alongside_naming_its_stack(tmp_path, monkeypatch, capsys):
    """One hook, two lines: the compose name and the venv, each only when its own
    conditions hold, and neither able to stop the other."""
    checkout = _repo(tmp_path / "carameli")
    (checkout / ".venv").mkdir()
    tree = _worktree(checkout, tmp_path / "wt")
    (tree / "uv.lock").write_text("", encoding="utf-8")
    monkeypatch.setattr(wt_env.shutil, "which", lambda name: "uv")
    run = _Run()
    assert wt_env.main([NULL40, REAL, "1"], root=tree, runner=run, environ={}) == 0
    out = capsys.readouterr().out
    assert "COMPOSE_PROJECT_NAME=carameli-wt" in out
    assert ".venv provisioned" in out
    assert len(run.calls) == 1
