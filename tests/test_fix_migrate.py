"""`scripts/fix_migrate.py`: a database checkout's dev database brought to its merged head.

Real git, in throwaway repositories, because the gate is a question about the checkout's
branch and tree. The project's `scripts/db-upgrade.py` is the faked half: a runner that
answers `--check` and the upgrade from a table and records what it was asked.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fix_cycle
import fix_migrate as fm


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return done.stdout.strip()


def _commit(repo: Path, path: str, text: str) -> None:
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text(text, encoding="utf-8", newline="\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", path)


@pytest.fixture
def checkout(tmp_path):
    """`carameli` beside a workspace file, on `main`, level with its origin, shipping
    its upgrade script."""
    origin, checkout = tmp_path / "carameli.git", tmp_path / "ws" / "carameli"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    _git(tmp_path, "clone", str(origin), str(checkout))
    for key, value in (("user.email", "t@t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(checkout, "config", key, value)
    _git(checkout, "config", "core.hooksPath", str(tmp_path / "no-hooks"))
    _commit(checkout, fm.SCRIPT, "")
    _git(checkout, "push", "origin", "main")
    _git(checkout, "remote", "set-head", "origin", "main")
    return checkout


class Script:
    """`scripts/db-upgrade.py`, answering from `codes` and recording every call."""

    def __init__(self, check: int = fm.BEHIND, upgrade: int = 0, said: str = "") -> None:
        self.codes = {"--check": check, "": upgrade}
        self.said = said
        self.calls: list[str] = []

    def __call__(self, argv, **kwargs):
        assert argv[1] == fm.SCRIPT and kwargs["cwd"].name == "carameli"
        verb = " ".join(argv[2:])
        self.calls.append(verb)
        return subprocess.CompletedProcess(argv, self.codes[verb], "", self.said)


def test_a_database_behind_its_merged_head_is_upgraded(checkout):
    script = Script()
    line = fm.tend_one("carameli", checkout, fix_cycle.DISPATCH, script)
    assert line == "carameli -- database upgraded to head"
    assert script.calls == ["--check", ""]


def test_a_plan_pass_says_what_it_would_do_and_does_not(checkout):
    script = Script()
    line = fm.tend_one("carameli", checkout, fix_cycle.PLAN, script)
    assert line == "carameli -- would upgrade its database to head"
    assert script.calls == ["--check"]


@pytest.mark.parametrize("answer", [fm.CURRENT, fm.UNKNOWN], ids=["current", "database down"])
def test_a_current_or_unreachable_database_is_silence(checkout, answer):
    """Down is not a fault: the pass after it starts does the upgrade."""
    script = Script(check=answer)
    assert fm.tend_one("carameli", checkout, fix_cycle.DISPATCH, script) == ""
    assert script.calls == ["--check"]


def test_a_failed_upgrade_says_failed_with_what_alembic_said(checkout):
    script = Script(upgrade=1, said='psycopg.errors.DuplicateTable: relation "x" exists\n')
    line = fm.tend_one("carameli", checkout, fix_cycle.DISPATCH, script)
    assert line == (
        'carameli -- FAILED to upgrade its database: psycopg.errors.DuplicateTable: relation "x" exists'
    )


def test_a_check_that_answers_outside_its_contract_is_a_failure(checkout):
    """An exit that is not 0, 1 or 2 is a broken script, not a database to upgrade."""
    script = Script(check=3, said="Traceback ...\nImportError: docker_common")
    line = fm.tend_one("carameli", checkout, fix_cycle.DISPATCH, script)
    assert line == "carameli -- FAILED to check its database: ImportError: docker_common"
    assert script.calls == ["--check"]


def test_a_checkout_without_the_script_says_so_every_pass(checkout):
    """The point is that nobody has to remember: a project that never wrote its half is
    in the record, not quietly never migrated."""
    (checkout / fm.SCRIPT).unlink()
    script = Script()
    line = fm.tend_one("carameli", checkout, fix_cycle.DISPATCH, script)
    assert line == f"carameli -- not migrated: it has no {fm.SCRIPT}"
    assert "FAILED" not in line, "a missing half is reported, not filed every pass"
    assert script.calls == []


def test_nothing_on_a_task_branch_is_applied(checkout):
    """A revision applied from a branch stamps the database with an id a rewrite can
    remove, after which every Alembic command there fails."""
    _git(checkout, "checkout", "-b", "agent/new-table")
    assert fm.ready(checkout) == "not on main"
    script = Script()
    assert fm.tend_one("carameli", checkout, fix_cycle.DISPATCH, script) == ""
    assert script.calls == []


def test_nothing_uncommitted_is_applied(checkout):
    """An unsaved revision file under `versions/` would be applied with the rest."""
    (checkout / "alembic" / "versions").mkdir(parents=True)
    (checkout / "alembic" / "versions" / "abc_draft.py").write_text("", encoding="utf-8")
    assert fm.ready(checkout) == "uncommitted changes"
    script = Script()
    assert fm.tend_one("carameli", checkout, fix_cycle.DISPATCH, script) == ""
    assert script.calls == []


def test_a_commit_origin_does_not_have_is_not_applied(checkout):
    _commit(checkout, "alembic/versions/abc_local.py", "")
    assert fm.ready(checkout) == "HEAD is not on origin/main"
    assert fm.tend_one("carameli", checkout, fix_cycle.DISPATCH, Script()) == ""


def test_a_checkout_behind_origin_still_migrates_what_it_has(checkout):
    """Behind is a subset of what merged, so it is safe; reconcile fast-forwards it."""
    _git(checkout, "checkout", "-b", "ahead")
    _commit(checkout, "alembic/versions/def_next.py", "")
    _git(checkout, "push", "origin", "ahead:main")
    _git(checkout, "checkout", "main")
    _git(checkout, "fetch", "origin")
    assert fm.ready(checkout) == ""


def test_a_run_that_cannot_start_or_finish_comes_back_failed(checkout):
    def times_out(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    def missing(argv, **kwargs):
        raise FileNotFoundError("python.exe")

    done = fm.call(checkout, [], fm.UPGRADE_TIMEOUT, times_out)
    assert (done.returncode, fm.last_said(done)) == (-1, f"ran past {fm.UPGRADE_TIMEOUT}s")
    done = fm.call(checkout, ["--check"], fm.CHECK_TIMEOUT, missing)
    assert done.returncode == -1 and fm.last_said(done).startswith("could not start")


def test_last_said_prefers_stderr_and_falls_back_to_the_exit_code():
    def done(code, out, err):
        return subprocess.CompletedProcess([], code, out, err)

    assert fm.last_said(done(1, "upgrading\n", "boom\n\n")) == "boom"
    assert fm.last_said(done(1, "upgrading\nfailed here\n", "")) == "failed here"
    assert fm.last_said(done(7, "", "")) == "exit 7"


def test_only_the_registered_database_checkouts_are_tended(checkout, monkeypatch):
    """By name, as `db-revision` is scoped: devkit has no database, and a project absent
    from this machine has no checkout to look in."""
    monkeypatch.setattr(fm.devkit_project, "DB_PROJECTS", ("carameli", "ibkr_trader"))
    workspace = checkout.parent / "test.code-workspace"
    script = Script()
    lines = fm.tend(workspace, ["devkit", "carameli", "ibkr_trader"], fix_cycle.DISPATCH, script)
    assert lines == ["carameli -- database upgraded to head"]
    assert script.calls == ["--check", ""]
