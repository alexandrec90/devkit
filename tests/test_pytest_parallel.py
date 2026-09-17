"""Tests for `scripts/pytest_parallel.py`, the gate's parallel-run decision.

The decision is pure and the probe is separable, so nothing here starts a worker: a
test that spent eight processes proving the suite can spend eight processes would be
the cost this module exists to remove.
"""

from support import load_script

parallel = load_script("scripts/pytest_parallel.py")


def test_the_flag_is_added_when_xdist_is_installed():
    assert parallel.args(environ={}, have_xdist=True) == ["-n", "auto"]


def test_a_project_without_xdist_keeps_the_serial_run():
    """The flag is a speedup, never a requirement. Handing `-n` to a pytest with no
    xdist is a usage error, which would fail the gate over an optimisation."""
    assert parallel.args(environ={}, have_xdist=False) == []


def test_the_worker_count_is_settable():
    assert parallel.args(workers="4", environ={}, have_xdist=True) == ["-n", "4"]


def test_the_operator_can_switch_it_off_even_with_xdist_present():
    """For a suite that has grown a shared resource, or to read a failure in order."""
    environ = {parallel.DISABLE_ENV: "1"}
    assert parallel.args(environ=environ, have_xdist=True) == []


def test_values_that_read_as_off_do_not_switch_parallelism_off():
    """Same asymmetry as `git_policy`'s skip variable: `DEVKIT_NO_XDIST=0` means "do not
    disable" to whoever typed it, and reading it as "disable" inverts their intent."""
    for value in ("", "0", "false", "no", "off", "OFF", "  False  "):
        assert parallel.args(environ={parallel.DISABLE_ENV: value}, have_xdist=True) == [
            "-n",
            "auto",
        ], value


def test_anything_else_set_does_switch_it_off():
    for value in ("1", "true", "yes", "please"):
        assert parallel.disabled({parallel.DISABLE_ENV: value}) is True, value


def test_availability_is_probed_without_importing_the_plugin():
    """`find_spec`, not an import: the answer is wanted before pytest starts, and this
    process is about to spawn a different one. It must answer rather than raise."""
    assert parallel.available() in (True, False)


def test_the_default_worker_count_is_xdists_own_spelling():
    """`auto` is one worker per CPU. Named as a constant because two runners and their
    tests reach for it, and a bare string in three places is three places to disagree."""
    assert parallel.DEFAULT_WORKERS == "auto"
    assert parallel.args(environ={}, have_xdist=True) == ["-n", parallel.DEFAULT_WORKERS]
