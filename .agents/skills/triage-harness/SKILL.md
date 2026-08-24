---
name: triage-harness
description: Work the harness-defect backlog -- the agent reports and failed box spawns every harnessed project files on this machine's central ledger -- verifying each against current code, fixing what is still real, and recording what retired it.
argument-hint: 'Optional: a project name, an event name, or a group id to work on first'
---

# Work the harness-defect backlog

`.claude/rules/engineering.md` requires an agent to *report* a harness defect rather than
route around it, and `scripts/hooks/report-harness-defect.py` gives that report a durable
home. Nothing consumed the reports. This skill is the other end: it is why filing one is
worth an agent's turn.

Devkit-only, deliberately. Every defect on this ledger is a defect in **devkit** — the
guard, the gates, the hooks — whatever project the session that hit it was scoped to. A
consumer repo has nothing to fix; it has a vendored copy to `--pull` once the fix ships
here. That is also why this skill is not in `sync-devkit.py`'s `MANIFEST`.

> Every command below is issued bare. None is on the Bash blocklist, and a wrapper here
> buys no second bound.

## 1. Read the backlog as groups, not as lines

```bash
python scripts/harness_triage.py
```

It prints, and writes `logs/harness-triage.log` — read from the file, per the
failure-artifact rule. Both are grouped by `(event, project, detail)`, most recurrences
first, because a backlog read flat stops being read: 24 of this machine's first 39 open
items were **one** spawn race recorded 24 times.

Two event names reach it, and they want different treatment:

| Event | What it is | Where the diagnosis starts |
| --- | --- | --- |
| `agent-report` | an agent's *judgment* — a false-positive block, an instruction that dead-ended | the `command=` field: re-run it against today's code |
| `guard-spawn-failed` | an edit was blocked and no box could be cut for it | the `detail=` field: it carries the exception |

Everything else on the ledger — `guard-route`, `guard-block`, `capped-bash-block`,
`lint-fix-block` — is forensics, not a backlog. `check-logs` §7 covers reading those.

`project=` is normalised on read, so rows written from a box (`devkit--some-task-0824`)
group with the project they were cut from. Do not filter on the raw field.

## 2. Verify each group before believing it

**A report is evidence that an agent was blocked, not that the block was wrong**, and
this ledger is append-only: the oldest entries predate several fixes. So for each group,
in this order — the first answer that lands ends it:

1. **Is it already fixed?** `git log --oneline -20 -- <the file the report names>`, and
   `gh pr list --state merged --limit 20 --search "<keyword>"`. A merged PR that changed
   the behaviour retires the group; go to step 4 with its number.
2. **Does it still reproduce?** Run the `command=` field, or the equivalent, against the
   working tree. A capped-Bash or guard block reproduces in one call.
3. **Was the report itself wrong?** An agent that wrapped a command the blocklist never
   named, or renamed a branch to get past a check, filed a defect against a gate doing
   its job. Retire it with a note saying so — that is a real resolution, not a dismissal.
4. **`version=` says whether the reporter's copy was current.** A consumer at
   `DEVKIT_VERSION` weeks behind may be reporting something `main` already fixed. Check
   before triaging it as live.

Never resolve a group you have not answered one of those four for. Ageing out was the
hole this replaced; a note that says "probably fine" is the same hole with a command in
front of it.

## 3. Fix what survives, in one box, in the same turn

The execution default applies unchanged: what is worth naming is worth doing. Fix the
live groups, with a test each per `.claude/rules/engineering.md` — for a false-positive
block, the regression test is the exact command the report named.

Two things stay the user's call, because both are irreversible and neither is yours to
assume: **discarding work that exists only in a box**, and **a fix that has to be
released** rather than merged (a vendored-tier change reaches consumers only through
`sync-devkit.py --pull` against a tag — say so in the PR, and see `RELEASING.md`).

If a group's fix is genuinely out of scope for one change, leave it open and say why in
the report. An open item costs nothing; a laundered one costs the next agent.

## 4. Record what retired it — this is the step that makes the list shrink

```bash
python scripts/harness_triage.py --resolve-like <id> --note "fixed in #202: redirect_targets is quote-aware" --pr 202
```

`--resolve-like` takes one id and retires **every open item sharing its signature**, which
is the whole point of the grouping — one note for one defect, not 24. `--resolve` takes
literal ids when a group needs splitting.

- The note is **required**. `--note` refuses to be blank, exactly as `.devkit-untested.txt`
  refuses to be seeded over.
- A resolution is itself a ledger event, so the ledger stays append-only and single-file:
  nothing is edited, nothing can go stale against a second state file.
- Ids are content-addressed, not line numbers — a resolution written today still names
  its event after a thousand appends.

Resolve **after** the fix is pushed, not before. A note naming a PR that does not exist
yet is the one claim on this ledger nothing can check.

## Reporting

Give the user the shape, then the work: how many groups were open, how many were already
fixed (and by what), how many are now fixed here, and what is left open with the reason.
A count that only went down because things were retired is worth saying out loud — it is
the failure mode this tool was built to make visible.
