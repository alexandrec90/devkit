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


def test_json_dumped_to_the_processs_own_stdout_is_not_a_sink():
    """`json.dump(report, sys.stdout)` is how a measuring script prints its answer, and
    the literals beside it are the files it measured, not files it wrote. A block here
    was reported as a false positive on a read-only PowerShell `python -c`."""
    code = (
        "sizes = {p: len(open(p).read()) for p in ['scripts/a.py']}; json.dump(sizes, sys.stdout)"
    )
    assert gi.code_write_targets(code) == []
    assert gi.code_write_targets("json.dump(x, open('out.json', 'w'))") == ["out.json"]


# --- pathlib joins ----------------------------------------------------------


def test_two_joined_literals_are_one_path():
    """`Path('scripts') / 'x.py'` writes `scripts/x.py`. Read literal by literal it yields
    `x.py` -- a real filename with the wrong parent, resolved against the cwd."""
    code = "(pathlib.Path('scripts') / 'x.py').write_text('')"
    assert gi.code_write_targets(code) == ["scripts/x.py"]


def test_a_chain_of_joined_literals_collapses_to_one_path():
    assert gi.join_path_literals("'a' / 'b' / 'c.py'") == "'a/b/c.py'"
    assert gi.join_path_literals("Path('a') / 'b' / 'c.py'") == "Path('a/b/c.py')"


def test_a_segment_joined_onto_a_variable_is_not_a_target():
    """The reported shape: `BOX / 'scripts' / 'schedule_health.py'` with `BOX` an absolute
    path into the session's own box. The tail literal was returned alone, the guard
    resolved it against the static checkout, and the remedy named `<box>/schedule_health.py`
    -- a file that does not exist. A root this tier cannot read is not a path it can judge,
    which is how the command-line tier already treats `$VAR/x.py`."""
    code = "(BOX / 'scripts' / 'schedule_health.py').write_text(body)"
    assert gi.code_write_targets(code) == []
    code = "(pathlib.Path(__file__).parent / 'x.py').write_text('')"
    assert gi.code_write_targets(code) == []


def test_a_slash_inside_a_literal_is_not_a_join():
    """`re.sub('/', '_', name)` puts a slash between two quotes with a comma in the way,
    and a division by a literal does not happen in code anyone runs."""
    code = "open('a.py', 'w').write(re.sub('/', '_', 'b.py'))"
    assert gi.code_write_targets(code) == ["a.py", "b.py"]


# --- the entry point --------------------------------------------------------


def test_write_targets_reads_both_shapes():
    command = "python -c \"open('a.py','w')\" && python - <<'PY'\nopen('b.py','w')\nPY"
    assert gi.write_targets(command) == ["b.py", "a.py"]


def test_a_plain_leading_cd_rebases_the_targets():
    """The command-line tier follows a plain `cd` for its operands because not doing so was
    its most-reported false positive; this tier had the same report against it --
    `cd <box> && python - <<PY` refused as a write to the static checkout -- so it follows
    the same closed list of spellings, and no more.
    """
    assert gi.write_targets("cd sub && python - <<'PY'\nopen('a.py','w')\nPY") == ["sub/a.py"]
    command = (
        "cd \"C:\\ws\\.worktrees\\devkit--build-0902\" && python - <<'PY'\nopen('a.py','w')\nPY"
    )
    assert gi.write_targets(command) == ["C:/ws/.worktrees/devkit--build-0902/a.py"]


def test_leading_cd_reads_the_plain_spellings_only():
    """One operand, at the head of the line, terminated by `&&`, `;` or the line's end; the
    quotes come off and a trailing separator goes, so the base joins cleanly."""
    assert gi.leading_cd("cd sub && python x.py") == "sub"
    assert gi.leading_cd('Set-Location "C:\\ws\\box\\"; python x.py') == "C:/ws/box"
    assert gi.leading_cd("pushd 'a b'\npython x.py") == "a b"
    assert gi.leading_cd("cd $BOX && python x.py") == ""
    assert gi.leading_cd("echo x && cd sub && python x.py") == ""
    assert gi.leading_cd("") == ""


def test_a_rooted_target_ignores_the_cd():
    command = "cd sub && python - <<'PY'\nopen('/opt/a.py','w'); open('C:\\\\t\\\\b.py','w')\nPY"
    assert gi.write_targets(command) == ["/opt/a.py", "C:\\\\t\\\\b.py"]


@pytest.mark.parametrize(
    "prefix",
    [
        "cd $BOX && ",
        "cd ~ && ",
        "cd - && ",
        "cd /d C:\\x && ",
        "cd a b && ",
        "echo x && cd sub && ",
    ],
)
def test_a_cd_this_tier_cannot_follow_leaves_the_targets_relative(prefix):
    """A variable, a switch, two operands or a `cd` that is not at the head: the base is
    left where the tool call's cwd put it, which is the conservative direction -- a
    relative name still resolves into the checkout."""
    assert gi.write_targets(prefix + "python - <<'PY'\nopen('a.py','w')\nPY") == ["a.py"]


def test_write_targets_is_empty_for_a_command_that_only_reads():
    command = "python - <<'PY'\nprint(open('README.md').read())\nPY"
    assert gi.write_targets(command) == []


def test_a_heredoc_body_that_is_not_code_costs_nothing():
    """Bodies are read without checking the verb, because the readers vary. Nothing fires
    without a sink, so a body of plain data is simply not a target."""
    assert gi.write_targets("cat <<'EOF'\njust text about a.py\nEOF") == []
