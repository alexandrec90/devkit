#!/usr/bin/env python3
"""Global Git branch-lifecycle policy installed by Devkit.

The policy is deliberately local: GitHub Free cannot enforce protected branches in
private repositories. It blocks the mistakes that matter before Git changes anything
remotely:

* commits while detached, on the remote default branch, or on main/master;
* pushes to those protected branches, or to a branch name whose GitHub PR merged;
* pushes that create or move a release tag, which only a release workflow may do.

After the policy passes, the dispatcher runs the repository's pre-commit framework
configuration -- the commit stage for ``pre-commit``, the pre-push stage for a
``pre-push`` that publishes a branch -- and an optional ``.githooks/<hook>``. Everything
is stdlib-only because the hook runs before a project environment is guaranteed.

**Why this is a package.** It was one 837-line module, and the structure ratchet's
`file_lines` ceiling for it had been raised on five consecutive branches -- which
`.claude/rules/engineering.md` makes a defect report rather than a raise. Nothing in it
was a god function; the largest was 43 lines. What filled it was three responsibilities
that had never been cut apart, and every raise fed one of them without moving the seam:

* `branch` -- may this ref take a commit, may it take a push. Pure decisions over a
  `Runner`, which is what makes the whole tier testable without a repository.
* `framework` -- where is this project's `pre-commit`, and what environment does it need.
* `dispatch` -- what git actually calls. The only tier that prints or exits.
* `_core` -- the constants, the small frozen types, and the single spawn point. Imports
  none of the others.

**Why a package rather than sibling modules**, which is the part that took the design.
This code is imported as ``git_policy`` from the checkout and as ``devkit_git_policy``
from ``~/.devkit/git-hooks``, where ``install-git-policy.py`` copies it under that name
without rewriting a single import. Siblings would have had to be found under both
spellings, at the one point in the harness where a failed import blocks every commit on
the machine. A package's *relative* imports do not care what the package is called, so
``from ._core import ...`` is correct under either name and there is no dual-name
lookup anywhere. A directory also wins over a same-named flat module on ``sys.path``, so
a ``devkit_git_policy.py`` left behind by an older install is shadowed rather than
preferred -- the upgrade needs no cleanup to be safe.

This module re-exports the surface callers already used, so `import git_policy` keeps
meaning what it meant: `scripts/git-merge-default.py` and the tests are unchanged by the
cut.
"""

from __future__ import annotations

# The submodules are bound as attributes by these imports, and that is load-bearing for
# the tests: a name must be patched where it is *looked up*, so a test that replaces
# `_pre_commit_command` patches `git_policy.framework`, not this module. Re-exporting a
# function does not make this module the place it is resolved from.
from . import _core, branch, dispatch, framework
from ._core import (
    ALWAYS_PROTECTED,
    DEFAULT_PROJECT_HOOKS,
    DEFAULT_REMOTE,
    FAIL_CLOSED_KEY,
    NO_WINDOW,
    PROJECT_HOOKS_KEY,
    PROTECTED_BRANCH_KEY,
    RELEASE_TAG_RE,
    REMOTE_KEY,
    SKIP_ENV_VAR,
    SUPPORTED_HOOKS,
    ZERO_OID_RE,
    Decision,
    MergedPR,
    PushUpdate,
    Runner,
    TagUpdate,
    console_python,
    run_command,
)
from .branch import (
    default_branch,
    evaluate_pre_commit,
    evaluate_pre_push,
    github_repo,
    merged_pr,
    parse_push_updates,
    parse_tag_updates,
    policy_skipped,
    protected_branches,
    release_tag_decision,
)
from .dispatch import main, run_hook
from .framework import framework_env

__all__ = [
    "ALWAYS_PROTECTED",
    "DEFAULT_PROJECT_HOOKS",
    "DEFAULT_REMOTE",
    "FAIL_CLOSED_KEY",
    "NO_WINDOW",
    "PROJECT_HOOKS_KEY",
    "PROTECTED_BRANCH_KEY",
    "RELEASE_TAG_RE",
    "REMOTE_KEY",
    "SKIP_ENV_VAR",
    "SUPPORTED_HOOKS",
    "ZERO_OID_RE",
    "Decision",
    "MergedPR",
    "PushUpdate",
    "Runner",
    "TagUpdate",
    "_core",
    "branch",
    "console_python",
    "default_branch",
    "dispatch",
    "evaluate_pre_commit",
    "evaluate_pre_push",
    "framework",
    "framework_env",
    "github_repo",
    "main",
    "merged_pr",
    "parse_push_updates",
    "parse_tag_updates",
    "policy_skipped",
    "protected_branches",
    "release_tag_decision",
    "run_command",
    "run_hook",
]
