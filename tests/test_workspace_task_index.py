"""`scripts/workspace-task-index.py` — the task block rendered as a reviewable table.

The script asserts nothing about the workspace file, so nothing it gets wrong is caught
by another gate: a column that quietly drops a picker, or a wrapper it fails to see
through, reads as "the workspace does not do that" to whoever is reviewing. So the
tests here are mostly about *not losing information* — every task, every input, every
`${input:...}` reference wherever it hides — rather than about formatting.

The formatting half has exactly one property worth pinning: a wrapped cell must stay
inside its column. A table whose long detail spills sideways is the illegible thing the
script was written to replace.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from support import REPO_ROOT, load_script, needs_live_workspace

index = load_script("scripts/workspace-task-index.py")


# --- fixture ------------------------------------------------------------------

# Small, but every shape the real file has: a dispatched task, a wrapped one, a
# PowerShell launcher, a task with no group, an input reached through `options.cwd`,
# each of the three picker types, and an input no task asks for.
FIXTURE_BLOCK = {
    "version": "2.0.0",
    "tasks": [
        {
            "label": "Test: Run Suite",
            "detail": "Runs the suite.",
            "type": "process",
            "command": "python",
            "args": [
                "${workspaceFolder:devkit}/scripts/devkit_project.py",
                "--project",
                "${input:project}",
                "test",
                "${input:testScope}",
            ],
            "group": {"kind": "test", "isDefault": True},
        },
        {
            "label": "Workspace: Check Drift",
            "detail": "Read-only comparison.",
            "type": "process",
            "command": "python",
            "args": [
                "scripts/notify-wrap.py",
                "Workspace: Check Drift",
                "--",
                "python",
                "scripts/log-wrap.py",
                "Workspace: Check Drift",
                "--",
                "python",
                "scripts/devkit_project.py",
                "--check-workspace",
            ],
            "options": {"cwd": "${workspaceFolder:devkit}"},
            "group": "build",
        },
        {
            "label": "Agents: Open Tabs",
            "detail": "Opens terminal tabs.",
            "type": "process",
            "command": "pwsh.exe",
            "args": [
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                "${workspaceFolder:devkit}/scripts/launch-agent-tabs.ps1",
                "-Agent",
                "${input:agentCli}",
            ],
            "runOptions": {"instanceLimit": 4},
        },
        {
            "label": "Unprefixed",
            "type": "shell",
            "command": "echo",
            "args": ["hi"],
            "options": {"cwd": "${input:daemonProject}"},
        },
    ],
    "inputs": [
        {
            "id": "project",
            "type": "command",
            "command": "extension.commandvariable.pickStringRemember",
            "args": {
                "description": "Which checkout(s)?",
                "multiPick": True,
                "optionGroups": [{"options": ["carameli", "devkit"]}],
            },
        },
        {
            "id": "testScope",
            "type": "pickString",
            "description": "How much of the suite",
            "options": [
                {"label": "Full suite", "value": ""},
                {"label": "Changed only", "value": "--changed"},
            ],
            "default": "",
        },
        {
            "id": "daemonProject",
            "type": "pickString",
            "description": "Which checkout supplies the override",
            "options": ["carameli", "devkit"],
            "default": "carameli",
        },
        {
            "id": "agentCli",
            "type": "promptString",
            "description": "Which CLI",
            "default": "",
        },
        {
            "id": "previewRow",
            "type": "command",
            "command": "extension.commandvariable.pickStringRemember",
            "args": {
                "description": "Which branch?",
                "multiPick": True,
                "fileName": "${workspaceFolder:devkit}/logs/preview-menu.json",
                "pickStringRemember": {"previewProject": {"description": "Which checkout?"}},
            },
        },
        {
            "id": "orphan",
            "type": "pickString",
            "description": "Nothing asks this",
            "options": ["a"],
        },
    ],
}


@pytest.fixture
def block() -> dict:
    return json.loads(json.dumps(FIXTURE_BLOCK))


@pytest.fixture
def workspace_file(tmp_path: Path) -> Path:
    """The fixture written out in VS Code's dialect — comments and a trailing comma."""
    body = json.dumps({"folders": [], "tasks": FIXTURE_BLOCK}, indent="\t")
    path = tmp_path / "fixture.code-workspace"
    path.write_text(
        "// a comment VS Code allows\n" + body.replace("}\n}", "},\n}"), encoding="utf-8"
    )
    return path


@pytest.fixture
def rows(block: dict) -> list:
    return index.task_rows(block)


# --- reading ------------------------------------------------------------------


def test_resolve_workspace_prefers_the_live_file_then_the_canonical_copy(tmp_path: Path):
    """`--canonical` and an explicit path both win over the default, and the default
    falls back rather than failing: the live file sits beside the checkouts, so it does
    not exist in a CI clone or a fresh machine, where a report is still worth having."""
    given = tmp_path / "given.code-workspace"
    assert index.resolve_workspace(given, False) == (given, "given")
    assert index.resolve_workspace(given, True) == (given, "given")
    assert index.resolve_workspace(None, True) == (index.CANONICAL_WORKSPACE, "canonical")
    path, which = index.resolve_workspace(None, False)
    if index.LIVE_WORKSPACE.is_file():
        assert (path, which) == (index.LIVE_WORKSPACE, "live")
    else:
        assert (path, which) == (index.CANONICAL_WORKSPACE, "canonical")


def test_load_task_block_reads_both_nestings(workspace_file: Path, tmp_path: Path):
    """A `.code-workspace` nests the block under `tasks`; a `tasks.json` is the block."""
    nested = index.load_task_block(workspace_file)
    assert next(task["label"] for task in nested["tasks"]) == "Test: Run Suite"

    bare = tmp_path / "tasks.json"
    bare.write_text(json.dumps(FIXTURE_BLOCK), encoding="utf-8")
    assert index.load_task_block(bare)["tasks"] == nested["tasks"]


def test_load_task_block_rejects_a_file_that_is_not_a_workspace(tmp_path: Path):
    path = tmp_path / "list.json"
    path.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ValueError):
        index.load_task_block(path)


# --- one task -----------------------------------------------------------------


def test_unwrap_command_sees_through_both_wrappers(block: dict):
    """`notify-wrap -> log-wrap -> the script` is what every workspace-scoped task
    carries, and it is identical on all of them — so reporting it as "what the task
    runs" would fill the column with the one thing that distinguishes nothing."""
    task = block["tasks"][1]
    tokens = [task["command"], *task["args"]]
    assert index.unwrap_command(tokens) == [
        "python",
        "scripts/devkit_project.py",
        "--check-workspace",
    ]


def test_unwrap_command_leaves_an_unwrapped_command_alone():
    assert index.unwrap_command(["python", "scripts/sweep.py"]) == ["python", "scripts/sweep.py"]


def test_unwrap_command_survives_a_wrapper_with_no_separator():
    """Malformed rather than impossible: returning the tokens beats raising, because a
    report that dies on one odd task tells you nothing about the other forty."""
    assert index.unwrap_command(["python", "scripts/log-wrap.py", "Label"]) == [
        "python",
        "scripts/log-wrap.py",
        "Label",
    ]


def test_describe_command_condenses_a_dispatch(block: dict):
    assert index.describe_command(block["tasks"][0]) == (
        "python devkit_project.py --project <project> test <testScope>"
    )


def test_describe_command_drops_shell_boilerplate(block: dict):
    """Every `pwsh.exe` task carries the same four flags. They are noise in a column
    whose job is to say what is different about this task."""
    assert index.describe_command(block["tasks"][2]) == (
        "pwsh.exe launch-agent-tabs.ps1 -Agent <agentCli>"
    )


def test_task_group_marks_the_default(block: dict):
    assert index.task_group(block["tasks"][0]) == "test*"
    assert index.task_group(block["tasks"][1]) == "build"
    assert index.task_group(block["tasks"][2]) == ""


def test_task_parameters_finds_inputs_outside_args(block: dict):
    """An input can reach a task through `options.cwd` as well as `args`. Reading only
    `args` would have shown the last fixture task as asking for nothing."""
    assert index.task_parameters(block["tasks"][0]) == ("project", "testScope")
    assert index.task_parameters(block["tasks"][3]) == ("daemonProject",)
    assert index.task_parameters(block["tasks"][1]) == ()


def test_task_parameters_dedupes_and_keeps_order():
    task = {"args": ["${input:b}", "${input:a}", "${input:b}"]}
    assert index.task_parameters(task) == ("b", "a")


def test_task_rows_splits_the_domain_off_the_label(rows: list):
    assert [(row.domain, row.name) for row in rows[:3]] == [
        ("Test", "Run Suite"),
        ("Workspace", "Check Drift"),
        ("Agents", "Open Tabs"),
    ]


def test_task_rows_files_an_unprefixed_label_under_other(rows: list):
    assert (rows[3].domain, rows[3].name) == ("Other", "Unprefixed")


def test_task_rows_says_when_a_task_may_run_twice(rows: list):
    """`instanceLimit` is invisible in the file and decides whether two previews can be
    compared side by side, which is exactly the kind of thing a reviewer is looking for."""
    assert "up to 4 runs at once" in rows[2].detail


def test_task_rows_flags_a_missing_detail(rows: list):
    """Every task is supposed to carry one — `tests/test_devkit_project.py` gates it.
    A blank cell would read as "no cost worth stating" rather than as a gap."""
    assert "no detail" in rows[3].detail


def test_task_row_is_a_row(rows: list):
    assert isinstance(rows[0], index.TaskRow)


def test_domains_are_listed_in_file_order(rows: list):
    assert index.domains(rows) == ["Test", "Workspace", "Agents", "Other"]


# --- one input ----------------------------------------------------------------


def test_input_kind_names_how_the_picker_asks(block: dict):
    kinds = {spec["id"]: index.input_kind(spec) for spec in block["inputs"]}
    assert kinds["testScope"] == "pick one"
    assert kinds["agentCli"] == "free text"
    assert kinds["project"] == "pick many, remembered"
    assert kinds["previewRow"] == "pick many, remembered, from a file, after a first pick"


def test_input_question_reads_a_command_inputs_own_args(block: dict):
    assert index.input_question(block["inputs"][0]) == "Which checkout(s)?"
    assert index.input_question(block["inputs"][1]) == "How much of the suite"


def test_input_choices_marks_the_default(block: dict):
    choices = index.input_choices(block["inputs"][1])
    assert choices[0].startswith("* Full suite")
    assert choices[1].startswith("  Changed only")


def test_input_choices_shows_the_value_when_it_differs_from_the_label(block: dict):
    """The label is what the picker shows; the value is what reaches argparse. A table
    that printed only the label could not answer "what does this actually pass"."""
    assert "'--changed'" in index.input_choices(block["inputs"][1])[1]


def test_input_choices_flattens_option_groups(block: dict):
    assert [line.strip() for line in index.input_choices(block["inputs"][0])] == [
        "carameli",
        "devkit",
    ]


def test_input_choices_says_when_the_menu_comes_from_a_file(block: dict):
    """A file-backed picker is only as fresh as whatever last wrote that file — the
    property `.claude/rules/vscode-tasks.md` says every such picker has to pay for."""
    lines = index.input_choices(block["inputs"][4])
    assert any("logs/preview-menu.json" in line for line in lines)
    assert any("asks previewProject first: Which checkout?" in line for line in lines)


def test_input_choices_reads_file_backed_option_groups():
    """Regression, found by `test_the_live_workspace_renders` going red on a task
    another session had just added. `optionGroups` is a **list** of groups or a
    **mapping** naming the file to read them from; only the list was modelled, so the
    loop walked the mapping's keys and died on `"fileName".get`. One input's spelling
    took the whole report with it, including every task that parses fine."""
    spec = {
        "id": "plugSelection",
        "type": "command",
        "command": "extension.commandvariable.pickStringRemember",
        "args": {
            "description": "Which plug?",
            "optionGroups": {
                "fileName": "${workspaceFolder:devkit}/logs/plug-menu.json",
                "fileFormat": "load",
            },
        },
    }
    lines = index.input_choices(spec)
    assert any("logs/plug-menu.json" in line for line in lines)


def test_input_choices_survives_a_group_that_is_not_a_mapping():
    """The same crash one level down. A hand-edited workspace file is the input here,
    and a report that dies on it tells you nothing about the other forty tasks."""
    spec = {"args": {"optionGroups": ["carameli", {"options": ["devkit"]}]}}
    assert [line.strip() for line in index.input_choices(spec)] == ["devkit"]


def test_input_choices_spells_out_a_blank_default(block: dict):
    assert index.input_choices(block["inputs"][3]) == ("  (blank)",)


def test_input_rows_name_the_tasks_that_ask(block: dict, rows: list):
    by_id = {row.ident: row for row in index.input_rows(block, rows)}
    assert by_id["project"].used_by == ("Test: Run Suite",)
    assert by_id["daemonProject"].used_by == ("Unprefixed",)
    assert isinstance(by_id["project"], index.InputRow)


def test_input_rows_leave_an_orphan_picker_visible(block: dict, rows: list):
    """An input no task names is dead weight, and nothing else in the workspace would
    ever point at it."""
    by_id = {row.ident: row for row in index.input_rows(block, rows)}
    assert by_id["orphan"].used_by == ()


# --- tables -------------------------------------------------------------------


def test_wrap_cell_honours_newlines():
    """`textwrap.wrap` treats a newline as ordinary whitespace, which ran a 13-option
    picker's answers together into one paragraph."""
    assert index.wrap_cell("a\nb", 40) == ["a", "b"]


def test_wrap_cell_of_an_empty_string_is_one_blank_line():
    assert index.wrap_cell("", 10) == [""]


def test_column_widths_give_the_last_column_the_remainder():
    widths = index.column_widths(("A", "B"), [("xx", "yyyy")], (10,), 40)
    assert widths[0] == 2
    assert sum(widths) + 2 == 40


def test_column_widths_never_starve_the_prose_column():
    widths = index.column_widths(("Task", "Detail"), [("x" * 60, "y")], (60,), 20)
    assert widths[-1] == index.MIN_DETAIL_WIDTH


def test_render_row_keeps_every_cell_inside_its_column():
    """The one formatting property that matters: a long detail must wrap rather than
    push its neighbours sideways, which is the illegibility this script replaces."""
    lines = index.render_row(("short", "a b c d e f g h"), [8, 6])
    assert len(lines) > 1
    for line in lines:
        assert len(line) <= 8 + 2 + 6
    assert all(line[:8].strip() in ("short", "") for line in lines)


def test_render_table_separates_rows(rows: list):
    lines = index.render_table(("Task", "What"), [(r.name, r.detail) for r in rows], (20,), 80)
    assert set(lines[1]) == {"-", " "}
    assert lines[-1] == ""


def test_md_cell_escapes_pipes_and_folds_newlines():
    """An unescaped pipe ends the cell early, silently shifting every column after it."""
    assert index.md_cell("a | b\nc") == "a \\| b<br>c"


def test_md_table_emits_a_header_separator():
    lines = index.md_table(("A", "B"), [("1", "2")])
    assert lines[0] == "| A | B |"
    assert lines[1].startswith("|")
    assert lines[2] == "| 1 | 2 |"


# --- whole report -------------------------------------------------------------


def test_header_lines_say_which_copy_was_read(tmp_path: Path):
    header = index.header_lines(tmp_path / "w.code-workspace", "live", [], [])
    assert "live" in header[0]
    assert "0 tasks, 0 parameters" in header[2]


def test_render_text_keeps_every_task_and_every_input(block: dict, rows: list):
    inputs = index.input_rows(block, rows)
    report = index.render_text(rows, inputs, ["header"], 100)
    for row in rows:
        assert row.name in report
    for spec in inputs:
        assert spec.ident in report
    assert "(unused)" in report


def test_render_text_never_exceeds_the_requested_width(block: dict, rows: list):
    """Above the floor the fixed columns leave the prose column enough room; below it
    the floor wins and the table is wider than asked, which
    `test_column_widths_never_starve_the_prose_column` covers."""
    inputs = index.input_rows(block, rows)
    for width in (110, 120, 160):
        for line in index.render_text(rows, inputs, ["header"], width).splitlines():
            assert len(line) <= width, line


def test_render_markdown_keeps_every_task_and_every_input(block: dict, rows: list):
    inputs = index.input_rows(block, rows)
    report = index.render_markdown(rows, inputs, ["header"])
    assert report.startswith("# VS Code workspace tasks")
    for row in rows:
        assert row.name in report
    for spec in inputs:
        assert spec.ident in report


def test_build_report_reads_a_file_end_to_end(workspace_file: Path):
    report = index.build_report(workspace_file, "given", "text", 100)
    assert "4 tasks, 6 parameters" in report
    assert "=== Parameters (6)" in report


def test_output_path_picks_the_extension_from_the_format():
    assert index.output_path("text", None).name.endswith(".txt")
    assert index.output_path("markdown", None).name.endswith(".md")
    assert index.output_path("markdown", Path("x.out")) == Path("x.out")


# --- cli ----------------------------------------------------------------------


def test_main_writes_the_report_and_prints_it(workspace_file: Path, tmp_path: Path, capsys):
    out = tmp_path / "nested" / "report.txt"
    code = index.main(["--workspace", str(workspace_file), "--out", str(out)])
    assert code == 0
    written = out.read_text(encoding="utf-8")
    assert "Test: Run Suite".removeprefix("Test: ") in written
    assert written.rstrip() in capsys.readouterr().out


def test_main_reports_a_missing_workspace_rather_than_raising(tmp_path: Path, capsys):
    """A missing live file is the ordinary state in a clone, not a crash."""
    code = index.main(["--workspace", str(tmp_path / "absent.code-workspace")])
    assert code == 2
    assert "no workspace file" in capsys.readouterr().err


def test_main_reports_an_unparseable_workspace(tmp_path: Path, capsys):
    path = tmp_path / "broken.code-workspace"
    path.write_text("{ not json", encoding="utf-8")
    assert index.main(["--workspace", str(path)]) == 2
    assert "cannot read" in capsys.readouterr().err


# --- against the repo ---------------------------------------------------------


def test_the_canonical_workspace_renders():
    """devkit's own copy is the fixture that matters: it has every picker shape, a
    thousand-character detail, and forty tasks, and it is the file a reviewer will
    actually point this at.

    Names are checked against the Markdown render because the text one wraps them; the
    width guarantee is checked against the text one, past the header, whose first line
    is an absolute path and so is as long as the machine makes it.
    """
    block = index.load_task_block(index.CANONICAL_WORKSPACE)
    markdown = index.build_report(index.CANONICAL_WORKSPACE, "canonical", "markdown", 120)
    for task in block["tasks"]:
        label = task["label"]
        assert index.md_cell(label.partition(": ")[2] or label) in markdown
    for spec in block["inputs"]:
        assert spec["id"] in markdown

    body = index.build_report(index.CANONICAL_WORKSPACE, "canonical", "text", 120).split("\n\n", 1)
    for line in body[1].splitlines():
        assert len(line) <= 120, line


@needs_live_workspace
def test_the_live_workspace_renders():
    """The default target. Skipped in CI, where the live file does not exist."""
    report = index.build_report(index.LIVE_WORKSPACE, "live", "markdown", 120)
    assert "## Parameters" in report


def test_the_output_directory_is_ignored_by_git():
    """The report is a generated view of a file already in the repo. Committing it
    would be a second copy with nothing keeping it honest — so it goes under `logs/`,
    which `.gitignore` already covers."""
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert any(line.strip().rstrip("/") == "logs" for line in ignored)
    assert index.output_path("text", None).parent == REPO_ROOT / "logs"


def test_the_markdown_report_cannot_fail_a_commit():
    """`--format markdown` writes into `logs/`, and markdownlint's `globs` are absolute
    rather than "whatever pre-commit staged" — so before `logs/**` was ignored, running
    this script once left a generated file that blocked every later commit, including
    the commit of the script that wrote it. Wrapped table cells carry `<br>`, which is
    MD033, so the artifact is unfixable as well as unreviewed."""
    config = (REPO_ROOT / ".markdownlint-cli2.yaml").read_text(encoding="utf-8")
    ignores = config.split("ignores:", 1)[1].split("\nconfig:", 1)[0]
    assert '"logs/**"' in ignores
    assert index.output_path("markdown", None).parent == REPO_ROOT / "logs"


def test_no_document_spells_the_generated_report_path():
    """`test_doc_claims.test_documented_paths_exist` reads a path in prose as a claim
    that the file is there — and this one is there only after a run. So the README
    naming it passed on any machine that had run the script and failed in CI, which is
    the worst way round: the gate that caught it is the one whose artifact has to be
    downloaded to read. Asserted here instead, where the answer does not depend on
    whether `logs/` happens to be populated."""
    from test_doc_claims import _documented_files, cited_paths

    stems = {f"{index.OUTPUT_STEM}.txt", f"{index.OUTPUT_STEM}.md"}
    for document in _documented_files():
        cited = cited_paths(document.read_text(encoding="utf-8"))
        named = [path for path in cited if Path(path).name in stems]
        assert not named, f"{document.name} names {named}; say `logs/` and stop there"
