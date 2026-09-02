"""`scripts/guard_interpreter.py` — what a program handed to an interpreter writes.

The behaviour is exercised end-to-end through the guard in `tests/test_worktree_guard.py`;
this module names the seams directly, which is what the untested-symbols gate asks for and
what stops a helper changing meaning unnoticed while its caller's arithmetic still lands on
the same answer.
"""

from __future__ import annotations

import pytest
from support import load_script

gi = load_script("scripts/guard_interpreter.py")


# --- heredoc bodies ---------------------------------------------------------


def test_heredoc_bodies_are_the_complement_of_stripping_them():
    command = "cat <<'EOF'\nbody line\nEOF\necho after"
    assert gi.heredoc_bodies(command) == ["body line"]


def test_no_heredoc_means_no_bodies():
    assert gi.heredoc_bodies("echo hello") == []


def test_an_unterminated_heredoc_takes_the_rest():
    """What a shell does with it too, so the tier sees the text the interpreter would."""
    assert gi.heredoc_bodies("python - <<'PY'\na = 1\nb = 2") == ["a = 1\nb = 2"]


# --- inline code ------------------------------------------------------------


def test_inline_code_is_read_for_an_interpreter():
    assert gi.inline_snippets("python -c 'x = 1'") == ["x = 1"]
    assert gi.inline_snippets('python3 -c "x = 1"') == ["x = 1"]


def test_a_flag_on_a_non_interpreter_is_not_a_program():
    """`grep -e pattern` and `sed -e s/a/b/` both take the flag and neither is code.
    Reading them as code would treat the filename after it as the program's target."""
    assert gi.inline_snippets("grep -e 'x = 1' a.py") == []
    assert gi.inline_snippets("sed -e s/a/b/ a.py") == []


def test_a_flag_with_no_value_is_not_read_past_the_end():
    assert gi.inline_snippets("python -c") == []


def test_an_interpreter_is_recognised_however_it_is_spelled():
    """A first word may be a bare name or a path, on either platform's separator."""
    assert gi.inline_snippets("/usr/bin/python3 -c 'x'") == ["x"]
    assert gi.inline_snippets("C:\\Python\\python.exe -c 'x'") == ["x"]


def test_only_the_interpreters_statement_is_read():
    """The split exists so the verb check is per statement, not per command line."""
    assert gi.inline_snippets("grep -e 'nope' a.py && python -c 'yes'") == ["yes"]


# --- the sink gate ----------------------------------------------------------


def test_a_path_without_a_write_is_not_a_target():
    """The property the whole tier rests on. `open('a.py')` and `open('a.py','w')` name
    the same literal, and reading is most of what a guarded session does here."""
    assert gi.code_write_targets("open('a.py').read()") == []


@pytest.mark.parametrize(
    "code",
    [
        "open('a.py','w')",
        "pathlib.Path('a.py').write_text('x')",
        "shutil.copy('b.txt', 'a.py')",
        "fs.writeFileSync('a.py', 'x')",
    ],
)
def test_a_write_sink_makes_its_literals_targets(code):
    assert "a.py" in gi.code_write_targets(code)


def test_a_version_number_is_not_a_path():
    """`3.14` is `<name>.<ext>` by shape. Routing a write for it is the false positive
    the letter-first extension rule prevents."""
    assert gi.code_write_targets("pathlib.Path(x).write_text('3.14')") == []


def test_targets_are_deduplicated_in_order():
    code = "open('a.py','w'); open('b.py','w'); open('a.py','w')"
    assert gi.code_write_targets(code) == ["a.py", "b.py"]


# --- the entry point --------------------------------------------------------


def test_write_targets_reads_both_shapes():
    command = "python -c \"open('a.py','w')\" && python - <<'PY'\nopen('b.py','w')\nPY"
    assert gi.write_targets(command) == ["b.py", "a.py"]


def test_a_leading_cd_is_not_followed():
    """Deliberate, and documented on `write_targets`: paths come back relative and the
    guard resolves them against the tool call's own cwd. That is exactly what the
    command-line tier already does for any `cd` form it cannot follow, and for the same
    reason — a relative name still resolves into the checkout, the conservative direction.
    """
    assert gi.write_targets("cd sub && python - <<'PY'\nopen('a.py','w')\nPY") == ["a.py"]


def test_write_targets_is_empty_for_a_command_that_only_reads():
    command = "python - <<'PY'\nprint(open('README.md').read())\nPY"
    assert gi.write_targets(command) == []


def test_a_heredoc_body_that_is_not_code_costs_nothing():
    """Bodies are read without checking the verb, because the readers vary. Nothing fires
    without a sink, so a body of plain data is simply not a target."""
    assert gi.write_targets("cat <<'EOF'\njust text about a.py\nEOF") == []
