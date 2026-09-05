"""`scripts/project_python.py` — resolving the interpreter the dev tooling lives in.

The bug these pin is not "a tool was missing". It is that **three surfaces read a
missing tool as a passing one**, and the worst of them said so in a single word:
`lint-all: clean`, printed after every linter had been skipped. So the tests here care
most about the negative space — no venv, a venv that cannot answer, a re-exec that fails
— because each of those is a path that used to end in a false green.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from support import load_script

project_python = load_script("scripts/project_python.py")


def make_venv(root: Path) -> Path:
    """A `.venv` laid out the way this platform's would be, holding a placeholder file.

    Deliberately NOT a working interpreter. Copying the running one is not enough on
    Windows — `python.exe` without the `python3xx.dll` beside it cannot start — and
    building a real venv per test would trade seconds for a fact the last test in this
    module already asserts against the actual checkout. So the layout is real and
    `has_module` is stubbed wherever the *choice* is what is under test.
    """
    subdir, name = ("Scripts", "python.exe") if sys.platform == "win32" else ("bin", "python")
    target = root / ".venv" / subdir / name
    target.parent.mkdir(parents=True)
    target.write_text("placeholder", encoding="utf-8")
    return target


def only(*havers):
    """A `has_module` stub: True for exactly these interpreters.

    Named rather than inlined because the discriminator matters. An earlier version
    tested `".venv" in str(exe)`, which is true of `sys.executable` itself when the suite
    runs from this repo's own virtualenv — so the stub answered True for the interpreter
    it was meant to answer False for, and three tests passed for the wrong reason.
    """
    wanted = {str(h) for h in havers}
    return lambda exe, _module=None: str(exe) in wanted


def make_worktree(main: Path, name: str = "box") -> Path:
    """A linked worktree of `main`, laid out the way `git worktree add` leaves one.

    Only the two things the hop reads are real: the `.git` *file* holding the `gitdir:`
    pointer, and the `<main>/.git/worktrees/<name>` directory it points at. Building an
    actual repository would spend a `git` subprocess per test to assert something about
    string handling.
    """
    (main / ".git" / "worktrees" / name).mkdir(parents=True, exist_ok=True)
    box = main.parent / f"{name}-box"
    box.mkdir(parents=True, exist_ok=True)
    (box / ".git").write_text(f"gitdir: {main / '.git' / 'worktrees' / name}\n", encoding="utf-8")
    return box


# --- finding it -------------------------------------------------------------


def test_a_project_with_no_venv_reports_none(tmp_path):
    """First-class answer, not a failure: a fresh clone and a CI runner both look
    exactly like this, and every caller has a correct behaviour for it."""
    assert project_python.venv_python(tmp_path) is None


def test_the_venv_interpreter_is_found_where_this_platform_puts_it(tmp_path):
    made = make_venv(tmp_path)
    assert project_python.venv_python(tmp_path) == made


def test_a_venv_directory_with_no_interpreter_is_not_a_venv(tmp_path):
    """`uv sync` interrupted partway leaves the directory and not the binary. Reporting
    it as a venv would send every caller at a path that cannot be spawned."""
    (tmp_path / ".venv").mkdir()
    assert project_python.venv_python(tmp_path) is None


# --- finding it from inside a worktree ---------------------------------------


def test_a_worktree_finds_the_checkout_it_was_cut_from(tmp_path):
    """The hop itself: three parents off the `gitdir:` pointer. Everything below depends
    on it, and it is pure string handling, so it is worth pinning on its own."""
    main = tmp_path / "main"
    main.mkdir()
    box = make_worktree(main)
    assert project_python.main_checkout(box) == main


def test_an_ordinary_checkout_is_not_a_worktree(tmp_path):
    """`.git` is a directory here, not a pointer file, which is the common case and has
    to answer None rather than walking three parents up out of the repo."""
    (tmp_path / ".git").mkdir()
    assert project_python.main_checkout(tmp_path) is None


def test_a_submodule_pointer_is_not_mistaken_for_a_worktree(tmp_path):
    """A submodule writes the same kind of `gitdir:` file, at `.git/modules/<name>`.
    Walking up three from that lands outside the checkout entirely, so the shape of the
    two middle segments is checked and not assumed."""
    (tmp_path / ".git").write_text(
        f"gitdir: {tmp_path / '.git' / 'modules' / 'sub'}\n", encoding="utf-8"
    )
    assert project_python.main_checkout(tmp_path) is None


@pytest.mark.parametrize("content", ["", "not a pointer\n", "gitdir:\n"])
def test_a_git_file_that_is_not_a_usable_pointer_answers_none(tmp_path, content):
    """Unreadable, empty, or pointing nowhere all lead to the same fallback, and none of
    them may raise: this runs inside a pre-commit hook."""
    (tmp_path / ".git").write_text(content, encoding="utf-8")
    assert project_python.main_checkout(tmp_path) is None


def test_a_worktree_without_a_venv_falls_back_to_the_main_checkout(tmp_path):
    """The defect this was written for. The box had no `.venv`, the tools were one
    directory up, and every surface reported them missing instead."""
    main = tmp_path / "main"
    main.mkdir()
    made = make_venv(main)
    box = make_worktree(main)
    assert project_python.venv_python(box) == made
    assert project_python.borrowed_from(box) == main


def test_a_worktree_with_its_own_venv_keeps_it(tmp_path):
    """`worktree.py` provisions a box its own venv, and that one wins. Borrowing is the
    fallback for a box that did not get one, never a preference."""
    main = tmp_path / "main"
    main.mkdir()
    make_venv(main)
    box = make_worktree(main)
    own = make_venv(box)
    assert project_python.venv_python(box) == own
    assert project_python.borrowed_from(box) is None


def test_nothing_is_borrowed_when_neither_checkout_has_a_venv(tmp_path):
    """No venv anywhere stays the first-class None it always was, and reports no lender
    to explain -- the message `re_exec` prints must not appear on a fresh clone."""
    main = tmp_path / "main"
    main.mkdir()
    box = make_worktree(main)
    assert project_python.venv_python(box) is None
    assert project_python.borrowed_from(box) is None


def test_re_exec_says_out_loud_which_checkout_it_borrowed_from(tmp_path, monkeypatch, capsys):
    """Borrowing is right but it is not obvious, and a version surprise traced back to a
    line nobody printed costs more than the line does."""
    main = tmp_path / "main"
    main.mkdir()
    made = make_venv(main)
    box = make_worktree(main)
    monkeypatch.setattr(project_python, "has_module", only(made))
    monkeypatch.setattr(
        project_python.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0)
    )
    project_python.re_exec(box, "ruff", ["x.py"], env={})
    message = capsys.readouterr().err
    assert str(main) in message
    assert "provision" in message


def test_re_exec_is_silent_when_the_venv_is_the_projects_own(tmp_path, monkeypatch, capsys):
    """The ordinary path prints nothing. A notice on every run is one everybody learns to
    read past, which would waste the one case it exists for."""
    made = make_venv(tmp_path)
    monkeypatch.setattr(project_python, "has_module", only(made))
    monkeypatch.setattr(
        project_python.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0)
    )
    project_python.re_exec(tmp_path, "ruff", ["x.py"], env={})
    assert capsys.readouterr().err == ""


# --- asking whether it has the tool -----------------------------------------


def test_has_module_answers_for_the_interpreter_it_is_given():
    assert project_python.has_module(sys.executable, "json")
    assert not project_python.has_module(sys.executable, "a_module_that_is_not_installed")


def test_an_interpreter_that_cannot_be_spawned_answers_false(tmp_path):
    """ "Cannot be asked" and "does not have it" lead to the same fallback, so they are
    deliberately not distinguished — a second error path would do nothing different."""
    assert not project_python.has_module(tmp_path / "nope.exe", "json")


# --- choosing between them --------------------------------------------------


def test_the_current_interpreter_wins_when_it_already_has_the_module(tmp_path):
    """The ordering that keeps CI and an activated shell untouched. Both arrive with the
    tool importable, so neither is silently switched onto a venv holding other versions.

    Unstubbed on purpose: `json` really is importable from the running interpreter, so
    this asserts the ordering against a true answer rather than an arranged one.
    """
    make_venv(tmp_path)
    assert project_python.interpreter(tmp_path, "json", current=sys.executable) == sys.executable


def test_the_venv_is_chosen_when_the_current_interpreter_lacks_the_module(tmp_path, monkeypatch):
    """The whole point: `.venv` has pytest, the interpreter on PATH does not."""
    made = make_venv(tmp_path)
    monkeypatch.setattr(project_python, "has_module", only(made))
    chosen = project_python.interpreter(tmp_path, "pytest", current="/nonexistent/python")
    assert Path(chosen) == made


def test_with_no_venv_the_current_interpreter_is_returned_unchanged(tmp_path, monkeypatch):
    """There is nothing better to offer, and returning a path that does not exist would
    turn a clear "no module named x" into an opaque spawn error."""
    monkeypatch.setattr(project_python, "has_module", only())
    assert project_python.interpreter(tmp_path, "pytest", current="/nonexistent/python") == (
        "/nonexistent/python"
    )


def test_a_venv_that_also_lacks_the_module_does_not_win(tmp_path, monkeypatch):
    """Switching interpreters has to buy something. Moving to a venv that cannot import
    the tool either just changes which interpreter reports the failure."""
    make_venv(tmp_path)
    monkeypatch.setattr(project_python, "has_module", only())
    current = "/nonexistent/python"
    assert project_python.interpreter(tmp_path, "pytest", current) == current


def test_with_no_module_named_the_venv_is_preferred_outright(tmp_path):
    """The "run the project's tooling" case: the caller wants the environment, not a
    specific import — so nothing is probed and the venv wins without being asked."""
    made = make_venv(tmp_path)
    assert Path(project_python.interpreter(tmp_path)) == made


# --- re-exec ----------------------------------------------------------------


def test_re_exec_returns_none_when_this_interpreter_is_already_right(tmp_path):
    """None means "carry on here" — the answer on CI, in an activated shell, and on any
    machine with no venv. An int would make the caller exit without doing its work."""
    assert project_python.re_exec(tmp_path, "json", ["x.py"], env={}) is None


def test_the_guard_stops_a_child_re_execing_again(tmp_path, monkeypatch):
    """Not theoretical: `has_module` answers False for a venv interpreter that is present
    but broken, and without the guard each generation would spawn another."""
    made = make_venv(tmp_path)
    monkeypatch.setattr(project_python, "has_module", only(made))
    env = {project_python.REEXEC_GUARD: "1"}
    assert project_python.re_exec(tmp_path, "pytest", ["x.py"], env) is None


def test_a_venv_that_will_not_start_falls_through_rather_than_erroring(tmp_path, monkeypatch):
    """Carrying on under the current interpreter reproduces the original diagnosis. A
    spawn error would replace it with one the reader cannot act on."""
    made = make_venv(tmp_path)

    def explode(*_a, **_kw):
        raise OSError("not executable")

    monkeypatch.setattr(project_python, "has_module", only(made))
    monkeypatch.setattr(project_python.subprocess, "run", explode)
    assert project_python.re_exec(tmp_path, "pytest", ["x.py"], env={}) is None


def test_re_exec_hands_the_child_exit_code_up_unchanged(tmp_path, monkeypatch):
    """The caller's only remaining job once a child has run. Swallowing a non-zero code
    here would make a failing test run report success."""
    made = make_venv(tmp_path)
    monkeypatch.setattr(project_python, "has_module", only(made))
    monkeypatch.setattr(
        project_python.subprocess,
        "run",
        lambda cmd, **_kw: subprocess.CompletedProcess(cmd, 3),
    )
    assert project_python.re_exec(tmp_path, "pytest", ["x.py"], env={}) == 3


def test_the_child_is_marked_so_it_cannot_recurse(tmp_path, monkeypatch):
    made = make_venv(tmp_path)
    seen: dict = {}
    monkeypatch.setattr(project_python, "has_module", only(made))

    def record(cmd, **kwargs):
        seen.update(kwargs.get("env") or {})
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(project_python.subprocess, "run", record)
    project_python.re_exec(tmp_path, "pytest", ["x.py"], env={})
    assert seen[project_python.REEXEC_GUARD] == "1"


# --- the spawn callers build ------------------------------------------------


def test_tool_command_is_the_dash_m_form(tmp_path, monkeypatch):
    made = make_venv(tmp_path)
    monkeypatch.setattr(project_python, "has_module", only(made))
    cmd = project_python.tool_command(tmp_path, "ruff", ["--check"])
    assert Path(cmd[0]) == made
    assert cmd[1:] == ["-m", "ruff", "--check"]


@pytest.mark.parametrize("module", ["ruff", "mypy", "pytest"])
def test_the_tools_this_repo_declares_are_reachable_from_its_own_venv(module):
    """The end-to-end claim, asserted against the real checkout rather than a fixture:
    every dev tool `pyproject.toml` declares is importable from the interpreter this
    module resolves. It is the assertion that would have failed on the machine where
    `lint-all` reported clean having run nothing.

    Asserted unconditionally rather than skipped when there is no `.venv`: CI installs
    these tools into the interpreter it runs as, so `interpreter()` returns that one and
    the claim still holds. A skip here would have made the check disappear on exactly the
    machines where nobody is watching the terminal.
    """
    root = Path(__file__).resolve().parents[1]
    assert project_python.has_module(project_python.interpreter(root, module), module)
