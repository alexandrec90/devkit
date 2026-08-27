"""Tests for `scripts/task_input.py` -- recognising a dismissed VS Code task input.

The property under test is narrow and the reason for it is not: VS Code aborts a task
run when one of *its own* inputs is escaped, and cannot when the input is a `command`
one, so the literal `${input:<id>}` reaches the script as if it were an answer. Every
multi-select picker in `workspace.jsonc` is a command input. What these tests pin is
that the literal is recognised wherever it lands in an argument list, and that nothing
else is mistaken for it -- a false positive here would silently refuse to run a task
that was answered.
"""

from support import task_input

cancel_report = task_input.cancel_report
cancelled_inputs = task_input.cancelled_inputs


def test_an_answered_task_reports_no_cancellation():
    assert cancelled_inputs(["--project", "devkit,carameli", "test", "--changed"]) == ()


def test_the_literal_is_found_as_a_whole_argument():
    """The common shape: `--project ${input:project}`, the picker VS Code never resolved."""
    assert cancelled_inputs(["--project", "${input:project}", "lint"]) == ("project",)


def test_the_literal_is_found_when_it_is_embedded_in_an_argument():
    """`Ingest: Run Source` spells one as `--arg=${input:ingestArg}`.

    A whole-argument comparison would pass that straight through to the script, which is
    the failure this looks for rather than the one it is obviously about.
    """
    assert cancelled_inputs(["ingest", "--arg=${input:ingestArg}"]) == ("ingestArg",)


def test_every_dismissed_prompt_is_named_once_and_in_order():
    """A compound task has several, and a repeat is one prompt, not two."""
    argv = ["--project", "${input:project}", "e2e", "${input:e2eMode}", "${input:project}"]
    assert cancelled_inputs(argv) == ("project", "e2eMode")


def test_a_bare_dollar_brace_is_not_a_dismissed_input():
    """VS Code substitutes plenty of other variables, and they resolve fine.

    Claiming those would turn a working task into a silent no-op -- the more expensive
    direction of the two, because nothing would be printed that named the cause.
    """
    argv = ["${workspaceFolder:devkit}/scripts/x.py", "${env:ProgramFiles}\\v\\v.exe", "${input}"]
    assert cancelled_inputs(argv) == ()


def test_non_string_arguments_are_tolerated():
    """`main(argv)` is called with lists built by tests and by callers, not only by VS Code."""
    assert cancelled_inputs([1, None, "${input:project}"]) == ("project",)


def test_the_report_names_the_program_and_every_dismissed_prompt():
    line = cancel_report("devkit_project", ("project", "testScope"))
    assert line.startswith("devkit_project: cancelled")
    assert "project" in line and "testScope" in line
