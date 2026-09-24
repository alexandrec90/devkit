---
description: What a coding session leaves to the fix pass — no commit, push or PR, no full test suite — and the fixer sessions that are exempt
---

# Rule: Stop at the change

Deliberately **unscoped**, and vendored from devkit like `engineering.md`: change it
there, not here.

A coding session makes the change and describes it. It does **not**:

- `git commit`, `git push`, or open a PR (`gh pr create`) — finish with the `/ship`
  skill, which writes the commit message to a file and stops;
- run the whole test suite, or wait on a CI gate.

Running the tests for what you touched is fine when it helps you, and never required.
The scheduled fix pass commits, pushes, opens the PR and runs the full gate in CI; a red
gate comes back to a fresh session with the failures named.

**Fixer sessions are exempt.** A session the fix pass dispatched — its opening prompt
names a failing PR, a refused commit or a merge conflict — follows that prompt, including
where it says to commit, push or run the tests: that is its job, and this rule does not
override it.
