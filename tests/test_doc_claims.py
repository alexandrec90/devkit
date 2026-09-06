"""The docs are vibe-coded too: hold their checkable claims to the repo.

Prose is the one artifact here with no compiler and no test, which makes it the one
place a wrong statement survives indefinitely — and instruction files are *read as
authority*, so a stale one does not merely misinform, it steers the next agent away
from a correct change. The failure has a shape worth naming: an agent proposes a fix,
finds a paragraph a previous agent wrote saying otherwise, and defers to it.

Trimming `CLAUDE.md` surfaced three of these in one pass, all of the same shape and
none of them visible from inside the sentence that made them:

  - two files described `.vscode/tasks.json` as devkit's, months after devkit deleted
    it (and `test_devkit_ships_no_project_level_tasks` started enforcing its absence);
  - the README described `.devkit.toml` as a fixture "turning on the DB and frontend
    tiers", when it describes devkit and turns both off.

So this file checks the three classes of claim that can be checked mechanically:

  - **A cited path exists.** Every path in an inline code span or a Markdown link.
  - **Prose pins no version.** An interpreter or tool version written into a sentence
    is a claim with no owner: the toolchain moves, the sentence does not, and the next
    agent gets a confident contradiction of `pyproject.toml`.
  - **A cited test exists.** A paragraph naming the test that pins a behaviour is the
    reader's shortcut to the evidence, and a rename leaves the shortcut pointing at
    nothing while the paragraph still reads as authority.

Everything else about a doc — whether a rationale is still true, whether a table still
describes the design — is beyond a test, and this file does not pretend otherwise. It
narrows the surface on which prose can be *silently* wrong, which is the part that
compounds.

**Why this is devkit-only and not vendored.** The exemption lists below name devkit's
own deliberate absences, and a vendored copy would ship them into every consumer while
being unable to carry theirs. Moving this into `scripts/hooks/tests/` means putting the
lists behind a `.devkit.toml` field first, the way every other per-project value is
read — worth doing once the false-positive rate here is known, and not before.
"""

import functools
import re
import subprocess
from pathlib import Path

from support import REPO_ROOT, harness_state


def _text(path: Path) -> str:
    """A documented file's content, whether or not the instructions switch has moved it.

    Every read in this module goes through here. `harness-switch.py --off --group
    instructions` moves `CLAUDE.md` and `.claude/rules/*.md` out of the checkout so a
    session stops loading them -- but an instruction file that has moved is still an
    instruction file, and a gate that silently stopped checking one would be worse than a
    gate that failed. Live file first, stash second, "" if it is genuinely neither.
    """
    return harness_state.instruction_text(path)


# --- what counts as a documented file -----------------------------------------
#
# Extension-driven rather than "does the first segment exist", because the miss that
# started this (`.vscode/tasks.json`, in a repo with no `.vscode/`) is invisible to the
# latter: the directory being gone is the whole finding.
_PATH_SUFFIXES = frozenset(
    {
        ".py",
        ".md",
        ".sh",
        ".json",
        ".jsonc",
        ".toml",
        ".yaml",
        ".yml",
        ".cfg",
        ".ini",
        ".txt",
        ".lock",
        ".tmpl",
    }
)

# `.log` is deliberately not in that set: every artifact path under `logs/` is
# gitignored and written at runtime, so requiring one to exist would fail on a clean
# clone -- which is the state CI is always in.

# Directories whose contents are nobody's claim: installed packages, caches, and the
# generated Codex mirror (`sync-codex-context.py` writes it from `.claude/skills/`).
_UNSEARCHED = frozenset(
    {
        ".git",
        ".venv",
        "node_modules",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".hypothesis",
        ".agents",
        # `.claude/worktrees/<name>`, where `claude --worktree` and a Remote Control
        # server started with `--spawn worktree` cut theirs. Unlike the `.worktrees/`
        # box tier -- which lives beside every checkout and so was never in reach --
        # these land *inside* the repo, so every scanner here walks into a whole second
        # copy of it: each nested `CLAUDE.md` reads as an instruction file the root map
        # fails to name, and each nested path answers `_exists` for a file this repo
        # does not own. Gitignoring the directory does not help a walker that reads the
        # filesystem rather than `git ls-files`.
        "worktrees",
    }
)

# A token cannot be a path this repo owns if it carries any of these: a glob, a
# placeholder, a shell variable, a Windows separator, a parent traversal, or a scheme.
# `.git/` joins them as a location rather than a spelling: what is under it is Git's
# runtime state, written per clone and committed by nobody.
_NOT_A_PATH = re.compile(r"[\s<>{}*$%|?\"'\\]|\.\.|://|^[/~#]|(?:^|/)\.git/")

# A suffix on its own (`.py`, `.tmpl`) is prose naming a file *type*. Requiring one of
# those to exist asks the repo to contain a file called ".py".
_BARE_SUFFIX = re.compile(r"^\.[A-Za-z0-9]+$")

_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"(?<!!)\[[^]]*]\(<*([^)>\s]+)>*(?:\s+['\"][^)]*)?\)")
_FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)

# Versions belong in the file that installs them. Matched in prose only -- a fenced
# block is a transcript or a config sample, where a literal version is the content.
_VERSION_PIN = re.compile(
    r"""
      \b[Pp]ython\s+\d+\.\d+                 # Python 3.12
    | \bv\d+\.\d+(?:\.\d+)?\b                # v0.7.0
    | (?:>=|<=|==|~=|\^)\s*\d+\.\d+          # >=1.2, ==3.11
    """,
    re.VERBOSE,
)

# A test function named in prose. Six characters past the prefix, because a name short
# enough to be ambiguous (`test_x`) is not one a document points a reader at.
_TEST_NAME = re.compile(r"^test_[a-z0-9_]{6,}$")

_TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(test_[A-Za-z0-9_]+)", re.M)

# Both test trees: half the suite is vendored out of here and lives under `scripts/`.
_TEST_ROOTS = ("tests", "scripts/hooks/tests")


def strip_fences(text: str) -> str:
    """`text` with fenced code blocks removed, so only prose is examined."""
    return _FENCE.sub("", text)


def cited_paths(text: str) -> list[str]:
    """Repo-relative paths a document claims exist.

    Code spans are split on whitespace so a command (`python scripts/run-tests.py`)
    yields the path it names rather than being skipped for containing a space.

    **Nothing inside a fenced block is a claim**, which is what `version_pins` already
    says about the same blocks: a fence is a transcript or a sample. Code spans were
    never read there anyway -- the pattern cannot cross the newlines a fence contains --
    so links were the one construct still leaking out, and they produced a false failure
    the first time anyone wrote PowerShell in this repo. `[xml](Get-Content ...)` is a
    type cast; to the link pattern it is `[text](target)`, so the suite demanded a file
    named `Get-Content`, and the author's only remedy was to rewrite working PowerShell
    to appease a Markdown regex.
    """
    found: list[str] = []
    for span in _CODE_SPAN.findall(text):
        for token in span.split():
            candidate = token.strip("(),;:").rstrip(".")
            if _NOT_A_PATH.search(candidate) or _BARE_SUFFIX.match(candidate):
                continue
            suffix = candidate[candidate.rfind(".") :] if "." in candidate else ""
            if suffix in _PATH_SUFFIXES:
                found.append(candidate)
    for target in _LINK.findall(strip_fences(text)):
        clean = target.split("#", 1)[0]
        # A bare anchor, an external URL, and an absolute path are all somebody
        # else's to keep true.
        if clean and not _NOT_A_PATH.search(clean):
            found.append(clean)
    return found


def version_pins(text: str) -> list[str]:
    """Version literals written into prose (fenced blocks excluded)."""
    return [match.group(0) for match in _VERSION_PIN.finditer(strip_fences(text))]


def cited_test_names(text: str) -> list[str]:
    """Test functions a document claims exist, read from code spans only.

    Split on the punctuation a sentence puts around a name -- a trailing comma, a
    parenthesised fixture, a full stop -- so a citation is found however it was
    written rather than only when it stands alone in its span. Prose that merely
    contains the characters is not a claim; the backticks are what make it one.

    **A name carrying a suffix is a file, and belongs to `cited_paths`.** The split
    leaves an internal dot alone for exactly that reason. Reading
    `test_codex_hooks_contract.py` as a function would have this gate demand a test
    that carameli owns and the README names *because* devkit does not have it -- a
    claim `ALLOWED_MISSING` already keeps, one gate away, with its reason attached.
    """
    return [
        candidate
        for span in _CODE_SPAN.findall(strip_fences(text))
        for token in re.split(r"[\s(),;:]+", span)
        if _TEST_NAME.match(candidate := token.rstrip("."))
    ]


@functools.lru_cache(maxsize=1)
def _defined_test_names() -> frozenset[str]:
    """Every test function `def`ined in either tree, plus every test module's stem.

    Names, not file text. The looser rule -- "the name occurs somewhere under
    `tests/`" -- reads a *mention* as an existence proof, and the first thing it
    excused was this file: the gate below recounts the rename that motivated it, so a
    docstring naming the retired test would have kept that test citable forever. A dead
    name has to be dead everywhere, or the gate launders its own history.

    Stems are in because prose names a module (`test_repo_contract`) about as often as
    a function, and `cited_paths` only sees the spelling that carries a directory.
    """
    found: set[str] = set()
    for root in _TEST_ROOTS:
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            found.add(path.stem)
            found.update(_TEST_DEF.findall(path.read_text(encoding="utf-8", errors="replace")))
    return frozenset(found)


def _exists(relpath: str) -> bool:
    """True when `relpath` names something in the repo.

    A bare basename is resolved by search rather than from the root: prose refers to
    `stop.py` far more often than to `scripts/hooks/stop.py`, and demanding the full
    path would tax every sentence to catch nothing extra.
    """
    if "/" in relpath:
        return (REPO_ROOT / relpath).exists()
    return any(
        not any(part in _UNSEARCHED for part in match.relative_to(REPO_ROOT).parts)
        for match in REPO_ROOT.rglob(relpath)
    )


@functools.lru_cache(maxsize=1)
def _tracked_paths() -> frozenset[str]:
    """Every path git tracks, repo-relative and forward-slashed."""
    listing = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return frozenset(entry for entry in listing.split("\0") if entry)


def _is_tracked(relpath: str) -> bool:
    """True when a *clone* of this repo would contain `relpath`.

    `_exists` reads the working tree, which is the right question for "is this cited
    path real" and the wrong one for "is this exemption still needed". The two differ
    for anything generated: `logs/` is gitignored, so `logs/plug-menu.json` is present
    for anyone who has run the plug menu or `worktree.py reconcile` and absent in a
    fresh checkout — which is exactly what its own exemption says. Asking the working
    tree made `test_exemptions_are_still_needed` fail on a developer machine and pass
    in CI on the same commit, and the entry it demanded be dropped was the one entry
    whose stated reason predicted the failure.

    Basenames match by name, as in `_exists`, and get their `_UNSEARCHED` filter for
    free: nothing tracks a `.venv`, so `pyvenv.cfg` is untracked without a list saying
    where not to look.
    """
    if "/" in relpath:
        return relpath in _tracked_paths()
    return any(entry.rsplit("/", 1)[-1] == relpath for entry in _tracked_paths())


def _claude_md_files() -> list:
    """Every `CLAUDE.md` in the repo, not only the root one.

    The instruction tier is split by what it governs — a directory's own `CLAUDE.md`
    loads when an agent works in it — and a claim is no less a claim for being written
    one level down. `templates/` is skipped for the same reason ruff and mypy skip it:
    what is in there is content awaiting a render, and its paths are the *generated*
    project's, not devkit's.
    """
    skipped = _UNSEARCHED | {"templates"}
    found = {
        path
        for path in REPO_ROOT.rglob("CLAUDE.md")
        if not any(part in skipped for part in path.relative_to(REPO_ROOT).parts)
    }
    # Switched-off files by their LIVE path, so everything downstream -- the citation
    # check, the `relative_to` in every message -- keeps talking about the repo. The
    # listing has to include them, not just the reads: a glob finds nothing once the
    # group is off, and `test_every_claude_md_is_checked_not_only_the_root_one` would
    # then pass by checking nothing.
    found |= {
        REPO_ROOT / relpath
        for relpath, _held in harness_state.instruction_sources(REPO_ROOT)
        if Path(relpath).name == "CLAUDE.md"
    }
    return sorted(found)


def _sibling_references() -> list:
    """Reference material a `CLAUDE.md` extracts to a file beside it.

    The 500-line cap makes this the sanctioned way to shed depth — `authoring.md`
    prescribes it — so the overflow has to stay as gated as the file it left. It was
    not: `.claude/skills/*/*.md` already swept a skill's siblings in, and nothing swept
    a `CLAUDE.md`'s, so `scripts/windowless-jobs.md` was the first 105 lines in this
    repo that no doc gate read. Splitting a file is exactly when its paths are most
    likely to go stale, which makes the un-gated moment the wrong one.
    """
    return sorted(
        sibling
        for parent in {path.parent for path in _claude_md_files()}
        for sibling in parent.glob("*.md")
        if sibling.name != "CLAUDE.md"
    )


def _documented_files() -> list:
    roots = [REPO_ROOT / "README.md", REPO_ROOT / "RELEASING.md"]
    roots += _claude_md_files()
    roots += _sibling_references()
    roots += _rule_files()
    roots += sorted((REPO_ROOT / ".claude" / "skills").glob("*/*.md"))
    seen: dict = {}
    for path in roots:
        if harness_state.instruction_exists(path):
            seen.setdefault(path.resolve(), path)
    return list(seen.values())


def _rule_files() -> list:
    """`.claude/rules/*.md` by live path, switched off or not. See `_claude_md_files`."""
    found = set((REPO_ROOT / ".claude" / "rules").glob("*.md"))
    found |= {
        REPO_ROOT / relpath
        for relpath, _held in harness_state.instruction_sources(REPO_ROOT)
        if relpath.startswith(".claude/rules/")
    }
    return sorted(found)


def _instruction_files() -> list:
    """The tier read as instruction. The README and RELEASING.md are documentation:
    they describe releases and pinned revs, where a version literal *is* the fact."""
    return [
        path
        for path in _documented_files()
        if path.name != "README.md" and path.name != "RELEASING.md"
    ]


# Paths named on purpose that are not here. Each entry is a claim in its own right --
# "this is deliberately absent" or "this belongs to another repo" -- so each carries
# the reason it is exempt, and an entry that stops being true fails the last test in
# this file rather than lingering.
ALLOWED_MISSING = {
    ".vscode/tasks.json": "the file devkit does NOT own -- its absence is the rule, and "
    "test_devkit_ships_no_project_level_tasks enforces it",
    "check-lock-markers.py": "a project-owned Stop tier devkit has no lockfiles for; "
    "its absence is an explicit skip, documented in test_repo_contract.py",
    "test_codex_hooks_contract.py": "carameli's, cited as the coupling the vendored "
    "tier exists to avoid",
    "dependabot-lock-repair.yml": "carameli's, cited among the workflows deliberately "
    "not normalized",
    ".codex/config.toml": "an optional trusted-project location for the fallback; "
    "devkit configures it at user scope instead",
    "AGENTS.md": "the README explicitly rejects a repository copy; the generic Claude-rule "
    "bridge lives in the user's ~/.codex/AGENTS.md",
    "DEVKIT_FILES.json": "the receipt `--pull` writes in a *consumer*; devkit is the "
    "source, so it has none",
    "known-fixes.md": "a consumer's project-owned sibling that retirement must not delete",
    "writing-conventions.md": "an illustrative filename in the progressive-disclosure "
    "example, not a file",
    ".devkit-workspace-render.json": "the render stamp, written beside the LIVE "
    "workspace file -- which lives outside every repo, so the stamp can never be in one; "
    "test_the_stamp_sits_beside_the_live_file_not_under_devkit_logs pins where it goes",
    "logs/plug-menu.json": "the plug/unplug checklist's cached options -- a generated "
    "file under the gitignored `logs/`, written by `--refresh-menu` and by `worktree.py "
    "reconcile`, so it exists on a machine that has run either and never in a clone",
    "pyvenv.cfg": "the file that makes a directory a virtualenv, named as the thing "
    "`devkit_schtasks.windowless` reads at runtime; it exists in every `.venv`, all of "
    "which are untracked, and never at the root of the repo",
}

# Version literals prose is allowed to carry, with the reason each is not a pin that
# can rot. Empty by intent: the first entry has to argue for itself.
ALLOWED_VERSION_PINS: dict[str, str] = {}

# Names in `test_`-shape that are not tests. Same contract as the two lists above: an
# entry is a claim, it carries its reason, and it fails once it stops being true.
ALLOWED_TEST_NAMES = {
    "test_a_lost_decision_is_refuse0": "not a test but a pytest `tmp_path` basename, "
    "truncated by the fixture -- documented as such in scripts/hooks/tests/conftest.py, "
    "which is the whole point of the paragraphs that cite it",
}


def test_no_skill_wraps_a_command_the_gate_does_not_block():
    """A wrapper on a command nothing blocks is pure noise, and it spreads by copying.

    Both harnesses are covered by one assertion now that `enforce-capped-bash.py` is a
    blocklist: `python scripts/...` was never on it, so wrapping it buys no second bound
    in Claude Code either. The earlier version of this test pinned one sentence of the
    check-logs preamble and could only speak for Codex.
    """
    offenders: list[str] = []
    for skill in sorted(REPO_ROOT.glob(".claude/skills/*/SKILL.md")):
        text = skill.read_text(encoding="utf-8")
        for match in re.finditer(r'invoke-capped\.py --command "([^"]+)"', text):
            command = match.group(1)
            if command.startswith(("python ", "python3 ")):
                offenders.append(f"{skill.parent.name}: {command}")
    assert not offenders, "wrapped commands the gate does not block: " + "; ".join(offenders)


def test_the_repo_carries_no_agents_md():
    """Codex reads this repo's `CLAUDE.md` files; a repo-level `AGENTS.md` is a fork.

    `project_doc_fallback_filenames = ["CLAUDE.md"]` at user scope is what points Codex
    at the real instruction tier, and `~/.codex/AGENTS.md` carries the one generic bridge
    for the rules Codex cannot discover. README says the rest outright: copying rule
    bodies into an `AGENTS.md` creates a second instruction tree that can drift.

    This is a regression test with a specific incident behind it. On 2026-09-02 an
    `AGENTS.md` appeared in this checkout, untracked, produced by running the root
    `CLAUDE.md` through `CLAUDE.md`->`AGENTS.md` and a case-insensitive `claude`->`Codex`
    -- which is why it opened "the portable agent-coding harness for Codex / Codex" and
    pointed at nine paths (`.Codex/rules/engineering.md`, `scripts/AGENTS.md`, ...) that
    have never existed. Nothing generated it and nothing would have regenerated it.

    `test_documented_paths_exist` caught that one only by accident, through those dead
    paths. A *correct* find-and-replace would have produced a file with every path
    resolving and no gate with anything to say -- an authoritative second tree, silently.
    So the invariant worth asserting is the file's absence, not its contents.
    """
    found = sorted(
        path.relative_to(REPO_ROOT).as_posix()
        for path in REPO_ROOT.rglob("AGENTS.md")
        if not any(part in _UNSEARCHED for part in path.relative_to(REPO_ROOT).parts)
    )
    assert not found, (
        "a second instruction tree: " + ", ".join(found) + "\nCodex reads CLAUDE.md here "
        "(project_doc_fallback_filenames). Delete it rather than maintaining a mirror."
    )


def test_the_gate_still_sees_a_file_the_instructions_switch_has_moved(monkeypatch, tmp_path):
    """The reversion check for `_text` and the two listing helpers.

    `harness-switch.py --off --group instructions` moves this repo's own `CLAUDE.md` and
    rules out of the checkout. Without the switch-aware listing every assertion in this
    module would still pass -- over an empty set, which is the one failure mode a gate
    cannot report about itself.
    """
    held = tmp_path / "held.md"
    held.write_text("stashed body naming `scripts/worktree.py`\n", encoding="utf-8")
    monkeypatch.setattr(
        harness_state,
        "instruction_sources",
        lambda _root, _ledger=None: [("nested/CLAUDE.md", held), (".claude/rules/x.md", held)],
    )
    monkeypatch.setattr(
        harness_state, "instruction_text", lambda path: _stashed_or_live(path, held)
    )

    assert REPO_ROOT / "nested" / "CLAUDE.md" in _claude_md_files()
    assert REPO_ROOT / ".claude" / "rules" / "x.md" in _rule_files()
    assert "worktree.py" in _text(REPO_ROOT / "nested" / "CLAUDE.md")


def _stashed_or_live(path: Path, held: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else held.read_text(encoding="utf-8")


def test_documented_paths_exist():
    """A path a document names is a claim the repo has to keep."""
    missing: list[str] = []
    for path in _documented_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        for cited in cited_paths(_text(path)):
            if cited in ALLOWED_MISSING or _exists(cited):
                continue
            missing.append(f"{rel}: {cited}")
    assert not missing, (
        "documentation cites paths that do not exist:\n  "
        + "\n  ".join(sorted(set(missing)))
        + "\nFix the sentence, or add the path to ALLOWED_MISSING with the reason it "
        "is named while absent."
    )


def test_instruction_prose_pins_no_versions():
    """An interpreter or tool version in prose outranks nothing and rots on its own.

    The version lives in the file that installs it, where a bump changes it. Written
    into a paragraph it becomes a second source of truth that no upgrade touches, and
    the next agent has to guess which one is current.
    """
    pinned: list[str] = []
    for path in _instruction_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        for pin in version_pins(_text(path)):
            if pin not in ALLOWED_VERSION_PINS:
                pinned.append(f"{rel}: {pin!r}")
    assert not pinned, (
        "instruction prose pins a version:\n  "
        + "\n  ".join(sorted(set(pinned)))
        + "\nName the file that owns the version instead (pyproject.toml, a lock, a "
        "workflow), or add it to ALLOWED_VERSION_PINS with the reason it cannot rot."
    )


def test_documented_test_names_exist():
    """A test named in prose is a pointer to the evidence, and a rename breaks it.

    This is the class the other two gates could not see, and it surfaced only because
    someone asked an unrelated question. `.claude/engineering-evidence.md` told every
    reader that the branch tier was exempt from `DEVKIT_HOOKS_OFF`, and named the test
    that asserted the exemption. The exemption had since been inverted and that test
    renamed to `test_the_branch_tier_consults_the_harness_kill_switch` -- so the
    paragraph was telling agents their work would still be routed into a box with the
    switch on, when standing the harness down now stands the tier down with it. Every
    path in it existed and it pinned no version: both other gates were green throughout.

    The retired name is deliberately not written here. See `_defined_test_names`.
    """
    stale: list[str] = []
    for path in _documented_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        for name in cited_test_names(_text(path)):
            if name in ALLOWED_TEST_NAMES or name in _defined_test_names():
                continue
            stale.append(f"{rel}: {name}")
    assert not stale, (
        "documentation cites tests that are not in the suite:\n  "
        + "\n  ".join(sorted(set(stale)))
        + "\nPoint at the test that carries the behaviour now -- and read the paragraph "
        "around the citation, because a renamed test usually means the claim moved too. "
        "A name that was never a test goes in ALLOWED_TEST_NAMES with the reason."
    )


def test_exemptions_are_still_needed():
    """An exemption that has become true is an exemption nobody will remove.

    Each list is documentation of a deliberate gap; a stale entry turns them into
    noise, and then into cover for a real miss. So an entry has to stay *both* absent
    and cited — the second half matters as much, because an exemption for a sentence
    nobody writes any more is drift of exactly the kind this file exists to catch, one
    level up.

    It has already paid for itself: four of the first ten entries were wrong. This test
    named them.

    "Has become true" is read off what git tracks and not off the disk, per `_is_tracked`
    — an exemption is a claim about a clone, and several entries name files that a
    machine which has run the harness legitimately has.
    """
    resurrected = sorted(path for path in ALLOWED_MISSING if _is_tracked(path))
    assert not resurrected, (
        f"ALLOWED_MISSING names paths this repo now tracks: {resurrected}. Drop the entries."
    )
    everything_cited = {cited for path in _documented_files() for cited in cited_paths(_text(path))}
    uncited = sorted(set(ALLOWED_MISSING) - everything_cited)
    assert not uncited, (
        f"ALLOWED_MISSING exempts paths no document names: {uncited}. Drop the entries."
    )
    unused = sorted(
        pin
        for pin in ALLOWED_VERSION_PINS
        if not any(pin in version_pins(_text(path)) for path in _instruction_files())
    )
    assert not unused, f"ALLOWED_VERSION_PINS names pins no longer written: {unused}."
    defined = sorted(name for name in ALLOWED_TEST_NAMES if name in _defined_test_names())
    assert not defined, (
        f"ALLOWED_TEST_NAMES exempts names the suite now defines: {defined}. Drop the entries."
    )
    everything_named = {
        name for path in _documented_files() for name in cited_test_names(_text(path))
    }
    unnamed = sorted(set(ALLOWED_TEST_NAMES) - everything_named)
    assert not unnamed, (
        f"ALLOWED_TEST_NAMES exempts names no document cites: {unnamed}. Drop the entries."
    )


def test_a_generated_file_on_disk_does_not_retire_its_exemption():
    """Running the harness must not redden the suite for having been run.

    `logs/` is gitignored, so `logs/plug-menu.json` appears the first time anyone opens
    the plug menu or lets `worktree.py reconcile` fire — and from then on
    `test_exemptions_are_still_needed` demanded the removal of an exemption whose own
    sentence says the file "exists on a machine that has run either and never in a
    clone". The test contradicted the entry it was checking, and only ever off a clone,
    so CI could not see it.

    Written to fail deterministically wherever it runs: it puts the file there itself.
    """
    generated = REPO_ROOT / "logs" / "plug-menu.json"
    ours = not generated.exists()
    generated.parent.mkdir(parents=True, exist_ok=True)
    if ours:
        generated.write_text("{}", encoding="utf-8")
    try:
        assert _exists("logs/plug-menu.json"), "precondition: it is on disk"
        assert not _is_tracked("logs/plug-menu.json"), "and still not in a clone"
        test_exemptions_are_still_needed()
    finally:
        if ours:
            generated.unlink()


def test_every_claude_md_is_checked_not_only_the_root_one():
    """The split instruction tier has to be covered by the checks it is subject to.

    Moving a paragraph from the root file into `scripts/CLAUDE.md` must not move it out
    of this file's reach — that would make decomposition a way to launder an unchecked
    claim, and the whole point of the split is that the same discipline applies at every
    level. `templates/` stays out: its paths belong to the project a render produces.
    """
    covered = {path.relative_to(REPO_ROOT).as_posix() for path in _documented_files()}
    nested = {name for name in covered if name.endswith("CLAUDE.md") and "/" in name}
    assert "CLAUDE.md" in covered
    assert nested, "no nested CLAUDE.md is being checked; did the tier collapse back?"
    assert not any(name.startswith("templates/") for name in covered)


def test_material_extracted_beside_a_claude_md_is_checked_too():
    """The overflow from a 500-line split stays as gated as the file it left.

    `test_every_claude_md_is_checked_not_only_the_root_one` closes the same hole one
    axis over — moving a paragraph *down* the tier — and this closes moving it
    *sideways*, into a sibling that is not named `CLAUDE.md`. Both are the sanctioned
    remedy for an oversized instruction file, so neither can be the way a claim stops
    being checked. A skill's siblings were already swept in by the `*/*.md` glob; a
    `CLAUDE.md`'s were not, and the first extraction to exercise that was 105 lines no
    gate read.
    """
    covered = {path.relative_to(REPO_ROOT).as_posix() for path in _documented_files()}
    siblings = {path.relative_to(REPO_ROOT).as_posix() for path in _sibling_references()}
    assert siblings <= covered
    assert siblings <= {path.relative_to(REPO_ROOT).as_posix() for path in _instruction_files()} | {
        "README.md",
        "RELEASING.md",
    }, "a sibling reference must be read as instruction"
    assert len(covered) == len(_documented_files()), "a file is listed twice"
    assert not any(name.startswith(".pytest_cache/") for name in covered)


def test_the_root_file_names_every_other_instruction_file():
    """A rule nobody points at is a rule only one of the two agents can find.

    Codex reads `CLAUDE.md` and reads straight past `.claude/rules/`, so the root file's
    map is the only route from a Codex session to a rule's existence — and a nested
    `CLAUDE.md` only loads once someone is already working in that directory. Neither
    absence is visible from inside the file that should have named it, which is the
    same shape as every other miss this module catches.
    """
    root = _text(REPO_ROOT / "CLAUDE.md")
    cited = set(cited_paths(root))
    expected = {path.relative_to(REPO_ROOT).as_posix() for path in _claude_md_files()}
    expected |= {path.relative_to(REPO_ROOT).as_posix() for path in _rule_files()}
    missing = sorted(expected - cited - {"CLAUDE.md"})
    assert not missing, (
        f"CLAUDE.md's map does not name: {missing}. Add a row, or the file is reachable "
        "only by an agent that already knows it exists."
    )


# --- the extractors themselves ------------------------------------------------


def test_cited_paths_reads_spans_links_and_commands():
    text = (
        "Run `python scripts/run-tests.py` after `stop.py`, see [rules](.claude/x.md).\n"
        'The wrapper takes `--command "<the command>"` and writes `logs/run.log`.\n'
        "Skip `origin/main`, `terminal.ansiBright*`, `Path(__file__)` and `hook.CFG`.\n"
    )
    assert cited_paths(text) == [
        "scripts/run-tests.py",
        "stop.py",
        ".claude/x.md",
    ]


def test_cited_paths_ignores_urls_anchors_and_absolute_targets():
    text = "[a](https://example.com/x.md) [b](#anchor) [c](/etc/hosts) [d](../up.md)"
    assert cited_paths(text) == []


def test_cited_paths_does_not_read_a_powershell_cast_as_a_link():
    """Lived, on the first skill in this repo to document PowerShell. `[xml](Get-Content
    ...)` is a type cast; to a link pattern it is `[text](target)`, so the suite demanded
    a file called `Get-Content` and the only way to green was to rewrite the sample."""
    text = "```powershell\n$doc = [xml](Get-Content 'x.xml' -Raw)\n```\n"
    assert cited_paths(text) == []


def test_cited_paths_reads_nothing_at_all_out_of_a_fenced_block():
    """The rule the case above generalises to, and the one a reader should hold: a fence
    is a sample. A path worth checking is named in prose, in a span or a link."""
    text = "```bash\npython scripts/run-tests.py\nsee [rules](.claude/x.md)\n```\n"
    assert cited_paths(text) == []


def test_cited_paths_strips_trailing_sentence_punctuation():
    assert cited_paths("Read `tests/support.py`, then `CLAUDE.md`.") == [
        "tests/support.py",
        "CLAUDE.md",
    ]


def test_version_pins_finds_prose_and_spares_fenced_samples():
    text = (
        "The language is Python 3.12 and the pin is v0.7.0.\n"
        "```yaml\nrev: v0.5.0\npython: '3.13'\n```\n"
        "Requires >=1.4 of the runner.\n"
    )
    assert version_pins(text) == ["Python 3.12", "v0.7.0", ">=1.4"]


def test_version_pins_ignores_line_counts_and_rule_codes():
    assert version_pins("Keep files under 500 lines; E501 stays off; 12 tests failed.") == []


def test_cited_test_names_reads_spans_however_they_are_punctuated():
    text = (
        "The gate is `test_documented_paths_exist`.\n"
        "See `test_one_thing` and `test_two_things(tmp_path)`, then stop.\n"
        "```\ntest_inside_a_fence_is_a_transcript\n```\n"
    )
    assert cited_test_names(text) == [
        "test_documented_paths_exist",
        "test_one_thing",
        "test_two_things",
    ]


def test_cited_test_names_leaves_a_test_file_to_the_path_gate():
    """`foo.py` is a claim about a file, and the two gates must not both make it."""
    assert cited_test_names("Carameli's `test_codex_hooks_contract.py` stays there.") == []
    assert cited_test_names("Run `python -m pytest tests/test_doc_claims.py`.") == []


def test_cited_test_names_needs_a_span_and_a_name_worth_citing():
    """Unbackticked prose is not a claim, and `test_x` is not a name."""
    assert cited_test_names("test_the_thing_that_is_not_in_a_span") == []
    assert cited_test_names("`test_x`") == []


def test_defined_names_hold_functions_and_stems_and_nothing_else():
    """A stem and a function both count; a name that only appears *inside* a test file
    does not, which is the property that stops this file excusing its own history."""
    defined = _defined_test_names()
    assert "test_repo_contract" in defined  # a module stem
    assert "test_documented_paths_exist" in defined  # a function
    assert "test_a_lost_decision_is_refuse0" not in defined  # quoted, never defined


def test_strip_fences_leaves_prose_intact():
    assert strip_fences("before\n```\nfenced\n```\nafter") == "before\n\nafter"
