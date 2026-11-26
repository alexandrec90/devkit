"""`scripts/project_registry.py`'s writers: `projects.toml` edited a line at a time.

Kept apart from `tests/test_project_registry.py`, which covers the readers, so the two
halves of the module's migration can change without touching one file.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import project_registry as pr

REPO_ROOT = Path(__file__).resolve().parents[1]

SAMPLE = """# The registry, with comments the edit keeps.

projects = [
    "alpha",  # the first
    # a comment between
    "beta",
]

# settings every PC shares
[settings]
"""


def _with(*lines: str) -> str:
    """SAMPLE with `lines` added at the end of the array."""
    added = "".join(f'    "{line}",\n' for line in lines)
    return SAMPLE.replace('    "beta",\n]', f'    "beta",\n{added}]')


# --- register_text ---------------------------------------------------------------------


def test_a_name_is_appended_at_the_end_of_the_array():
    edited = pr.register_text(SAMPLE, "gamma")
    assert edited == _with("gamma")
    assert pr.parse_registry(edited) == ["alpha", "beta", "gamma"]


def test_several_names_land_in_the_order_given():
    edited = pr.register_text(SAMPLE, "gamma", "delta")
    assert pr.parse_registry(edited) == ["alpha", "beta", "gamma", "delta"]


def test_registering_keeps_every_comment_and_the_settings_table():
    edited = pr.register_text(SAMPLE, "gamma")
    for kept in ("# The registry", "# the first", "# a comment between", "# settings every"):
        assert kept in edited
    assert pr.parse_shared_settings(edited) == {}


def test_registering_twice_is_registering_once():
    once = pr.register_text(SAMPLE, "gamma")
    assert pr.register_text(once, "gamma") == once
    assert pr.register_text(SAMPLE, "alpha") == SAMPLE, "already registered: unchanged"
    assert pr.register_text(SAMPLE, "gamma", "gamma") == once


def test_registering_nothing_changes_nothing():
    assert pr.register_text(SAMPLE) == SAMPLE


def test_a_last_name_with_no_comma_gets_one():
    text = 'projects = [\n    "alpha"  # kept\n]\n'
    edited = pr.register_text(text, "beta")
    assert edited == 'projects = [\n    "alpha",  # kept\n    "beta",\n]\n'


def test_a_name_goes_before_a_reference_checkout():
    """Where `devkit_project.insert_folder` puts a folder, so the registries keep one order."""
    edited = pr.register_text(SAMPLE, "gamma", before=frozenset({"beta"}))
    assert pr.parse_registry(edited) == ["alpha", "gamma", "beta"]


def test_a_one_line_array_is_laid_out_one_name_per_line():
    edited = pr.register_text("projects = []\n\n[settings]\n", "alpha")
    assert edited == 'projects = [\n    "alpha",\n]\n\n[settings]\n'
    edited = pr.register_text('projects = ["alpha"]  # why\n', "beta")
    assert edited == 'projects = [  # why\n    "alpha",\n    "beta",\n]\n'


def test_a_file_with_no_projects_gets_them_ahead_of_its_first_table():
    edited = pr.register_text("# head\n\n[settings]\n", "alpha")
    assert edited == '# head\n\nprojects = [\n    "alpha",\n]\n\n[settings]\n'
    assert pr.register_text("", "alpha") == 'projects = [\n    "alpha",\n]\n'


def test_an_indent_already_in_use_is_kept():
    edited = pr.register_text('projects = [\n\t"alpha",\n]\n', "beta")
    assert edited == 'projects = [\n\t"alpha",\n\t"beta",\n]\n'


@pytest.mark.parametrize(
    ("text", "name", "why"),
    [
        ("projects = [\n", "x", "not TOML"),
        ('projects = [\n    "a", "b",\n]\n', "c", "two names on one line"),
        (SAMPLE, 'quo"te', "a name TOML would need escaping for"),
        (SAMPLE, "", "an empty name"),
        (SAMPLE, "has#hash", "a name the line parser would read as a comment"),
    ],
)
def test_an_edit_it_cannot_make_cleanly_is_refused(text, name, why):
    with pytest.raises(pr.RegistryError):
        pr.register_text(text, name)  # `why` names the case in the test id


# --- unregister_text -------------------------------------------------------------------


def test_a_name_is_dropped_and_its_neighbours_comments_kept():
    edited = pr.unregister_text(SAMPLE, "beta")
    assert pr.parse_registry(edited) == ["alpha"]
    assert "# a comment between" in edited and "# the first" in edited
    assert edited == SAMPLE.replace('    "beta",\n', "")


def test_a_dropped_name_takes_its_own_trailing_comment():
    edited = pr.unregister_text(SAMPLE, "alpha")
    assert pr.parse_registry(edited) == ["beta"]
    assert "# the first" not in edited


def test_unregistering_is_the_inverse_of_registering():
    assert pr.unregister_text(pr.register_text(SAMPLE, "gamma"), "gamma") == SAMPLE


def test_unregistering_an_absent_name_changes_nothing():
    assert pr.unregister_text(SAMPLE, "never-here") == SAMPLE
    assert pr.unregister_text(SAMPLE) == SAMPLE


def test_unregistering_from_a_one_line_array():
    edited = pr.unregister_text('projects = ["alpha", "beta"]\n', "alpha")
    assert pr.parse_registry(edited) == ["beta"]


def test_unregistering_from_a_shape_it_cannot_edit_is_refused():
    with pytest.raises(pr.RegistryError):
        pr.unregister_text('projects = [\n    "a", "b",\n]\n', "a")


def test_the_committed_registry_round_trips():
    """The shipped file is the shape every writer meets: an add and a drop leave it as
    it was, byte for byte."""
    text = (REPO_ROOT / pr.REGISTRY_NAME).read_text(encoding="utf-8")
    added = pr.register_text(text, "zz-probe")
    assert pr.parse_registry(added) == [*pr.parse_registry(text), "zz-probe"]
    assert pr.unregister_text(added, "zz-probe") == text


# --- the file-level writers ------------------------------------------------------------


def test_register_file_writes_and_says_so(tmp_path):
    path = tmp_path / pr.REGISTRY_NAME
    path.write_text(SAMPLE, encoding="utf-8")
    assert pr.register_file(path, "gamma") is True
    assert path.read_text(encoding="utf-8") == _with("gamma")
    assert pr.register_file(path, "gamma") is False, "a re-run writes nothing"


def test_unregister_file_writes_and_says_so(tmp_path):
    path = tmp_path / pr.REGISTRY_NAME
    path.write_text(SAMPLE, encoding="utf-8")
    assert pr.unregister_file(path, "beta") is True
    assert pr.parse_registry(path.read_text(encoding="utf-8")) == ["alpha"]
    assert pr.unregister_file(path, "beta") is False


def test_a_missing_registry_is_left_missing(tmp_path):
    """While there is no `projects.toml` the workspace file is the registry, and a file
    created holding one name would shadow every other project."""
    path = tmp_path / pr.REGISTRY_NAME
    assert pr.register_file(path, "gamma") is False
    assert pr.unregister_file(path, "gamma") is False
    assert not path.exists()


def test_a_refused_edit_leaves_the_file_as_it_was(tmp_path):
    path = tmp_path / pr.REGISTRY_NAME
    text = 'projects = [\n    "a", "b",\n]\n'
    path.write_text(text, encoding="utf-8")
    with pytest.raises(pr.RegistryError):
        pr.register_file(path, "c")
    assert path.read_text(encoding="utf-8") == text


def test_what_a_writer_writes_the_readers_read(tmp_path):
    root = tmp_path / "projects"
    devkit = root / "devkit"
    devkit.mkdir(parents=True)
    (devkit / pr.REGISTRY_NAME).write_text(SAMPLE, encoding="utf-8")
    pr.register_file(devkit / pr.REGISTRY_NAME, "gamma")
    assert pr.all_projects(devkit) == ["alpha", "beta", "gamma"]
