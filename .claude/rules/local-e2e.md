---
description: Tests only this workstation can run -- the nightly local end-to-end tier and its scripts/e2e.py contract
paths:
  - "**/test_*.py"
  - "**/*_test.py"
  - "**/*.test.*"
  - "**/*.spec.*"
  - scripts/e2e.py
---

# Rule: The local end-to-end tier

Vendored from devkit, like `engineering.md`: change it there.

A test that needs this workstation cannot run in CI, and a skip there is not coverage.
That means a test that needs the live workspace file, a GUI, the Docker stack, a browser
or a credential. Before this tier such tests ran only when a person happened to run the
suite on the default branch.

**Reach them from `scripts/e2e.py`.** devkit's `devkit-local-e2e` job runs that
script in this checkout, fast-forwarded to origin's default-branch tip, once the PC has
been idle for a while. A red run reaches the fix pass as this project's red default branch, and its
fixer is told to run the script again.

| The contract | |
| --- | --- |
| exit code | 0 green, anything else red |
| each failure | printed as pytest's `FAILED <id>` line: the signature the fix pass groups and dedupes by. The id is read up to the first whitespace, so a runner whose test names have spaces must replace them |
| cwd | the checkout root |
| environment | `DEVKIT_E2E=1` under the scheduler; the checkout's own `.venv` interpreter, else the machine's Python, so a project with no `.venv` keeps the script stdlib-only |
| a run that executed nothing | exits non-zero, since a green from zero tests is a skip reported as a pass |

The usual shape is a pytest marker that `addopts` deselects and `scripts/e2e.py`
selects; devkit's is `local`. Another runner works the same way, for example a Vitest
config whose include pattern the default run never matches.

**Deselection keeps a tier test out of CI, so the test needs no skip of its own.** Where
the machine is missing what it needs, fail and name what is missing (`pytest.fail`, say).
`scripts/hooks/structure_check.py` refuses a new skip here as anywhere, through its
`skipped_tests` counter, which `--record` cannot pass. And under `DEVKIT_E2E` a skip in
the tier is a failure: the checkout is the one the tests were written for, so nothing
has a reason left to skip.

**What a hosted runner can run goes to CI, not to this tier.** A service container, Docker
on `ubuntu-latest`, a headless browser or a `windows-latest` desktop is not this
workstation; its signed-in accounts, credentials, hardware and live data are. Mark the
first kind `ci_e2e`, deselected by `addopts` beside `local`, and select it with
`scripts/e2e.py --ci` from an `E2E` job that runs on the default branch only, inside the
`PR Gate` workflow: the fix pass reads a red from no other. When no
`local` test is left, `e2e = "ci"` under `[delivery]` in `.devkit.toml` tells the PC's
job to record the project as run in CI rather than run it.

**Every project owes the script.** One whose default branch on origin has none is red
at the next run, as a missing tier, and the fix pass sends its fixer to write one. It is
never a skip: a skip reads as green, which is the state this tier exists to end.

By hand: `python scripts/e2e.py` here. From devkit's checkout,
`python scripts/e2e-pass.py run --no-wait <project>` shows what the night will see.
