---
name: check-logs
description: Audit this machine's background and automated processes -- the scheduled jobs, the artifacts they leave, and the boxes reconcile manages -- and tell a stale record apart from a live failure.
---

# Check the automated processes

> Depends on Windows Task Scheduler (`schtasks`) and a devkit checkout on this machine.
> Every Bash call needs an output cap -- route each through
> `scripts/hooks/invoke-capped.py`, per `.claude/rules/engineering.md`.

Answer in this order. It is not a preference: each step decides whether the next one's
evidence means anything.

## 1. Ask the scheduler before you open a single file

```bash
python scripts/hooks/invoke-capped.py --command "python scripts/workspace-status.py"
```

`scripts/schedule_health.py`'s docstring owns why this comes first, and it is worth
reading once: three of the four ways a job dies are invisible in its own output, because
the job that would have written the log is the job that did not run.

Silence here means healthy. A line means one job needs the rest of this skill.

## 2. A `Last Result` can outlive the command that produced it

**Check this before diagnosing anything.** Windows carries a task's run history across
the `/Create /F` that replaces it, so a failure can survive the fix that already landed
for it. That is not a corner case -- it is what the last audit found:
`devkit-docker-prune` reported `exit 1` at 11:58, and the corrected task was registered
at 16:18 the same day. The job was fine. Nothing about the failure line said so.

Two timestamps settle it. The task document's mtime is when the current definition was
registered:

```powershell
Get-Item 'C:\Windows\System32\Tasks\<task-name>' | Select-Object LastWriteTime
Get-ScheduledTask -TaskName 'devkit-*' | Get-ScheduledTaskInfo |
  Select-Object TaskName,LastRunTime,LastTaskResult,NextRunTime
```

If `LastRunTime` predates `LastWriteTime`, **the result describes a command that no
longer exists**. Say so and stop; there is no failure to fix. Confirm what changed with
`git log` on the installer -- `scripts/install-docker-prune.py`,
`scripts/install-reconcile-task.py`, `scripts/install-upgrade-schedule.py`,
`scripts/install-vanillaland-merge.py`.

## 3. Read the registered command against its installer

`tests/test_scheduled_jobs.py` asserts every job's contract from CI, and its docstring
names the one thing CI cannot see: whether *this machine's* registered task still matches
the installer in the repo. Nothing reports that drift, so read it.

```powershell
$raw = Get-Content 'C:\Windows\System32\Tasks\<task-name>' -Raw
$doc = [xml] $raw
$doc.Task.Actions.Exec.Arguments
```

Compare against the installer's argument builder. A hand-registered task is the failure
this whole tier was built after: no artifact, and none of the settings a laptop needs.
Re-running an installer is idempotent, so `python scripts/install-<job>.py --yes` is the
cheap way to make the machine agree with the repo again.

## 4. Then the artifacts, knowing what empty means

`scripts/schedule_health.py`'s `ARTIFACTS` maps each job to the file it leaves. Read
those, not a directory listing.

| what you find | what it means |
| --- | --- |
| empty file | **passed** -- `scripts/log-wrap.py` empties its artifact on success |
| empty file, and the job runs unattended with `--always` | it has not run since it last passed |
| stamped `# exit: 0` | passed, at the time in the header |
| missing file | not a diagnosis. Go back to step 2 |

A missing artifact and a job that never ran are the same empty result, which is why the
session-start line names the absence rather than pointing at the path.

## 5. Verify a wrapper without running the job

Never fire a scheduled job to clear a stale status. `scripts/docker-maint.py` runs
`wsl --shutdown` and prunes every unused image; the reconcile pass destroys boxes; the
VanillaLand merge stashes a working tree somebody may be editing right now. Each is
correct in the small hours and wrong as a diagnostic.

To prove the wrapper writes what it claims, run the *registered* command with a scratch
directory as the working directory and a trivial child, and read the artifact it leaves
there. `scripts/log-wrap.py` resolves `logs/` from the cwd, so nothing touches the repo:

```powershell
Start-Process -FilePath <pythonw> -WorkingDirectory <scratch> -Wait -NoNewWindow `
  -ArgumentList '<repo>/scripts/log-wrap.py','--always','"<label>"','--','<pythonw>','-c','"import sys; sys.exit(1)"'
```

This is also the check that matters after editing a wrapper: an unattended job runs
windowless, where `sys.stdout` is `None` rather than a discarded stream.

## 6. The other automated tier: boxes and checkouts

```bash
python scripts/hooks/invoke-capped.py --command "python scripts/worktree.py list"
```

`logs/reconcile.log` is the scheduled pass's own account -- disk headroom, boxes reaped,
checkouts synced. Two readings that are easy to get backwards:

- **A box holding work is not an error.** The refusal to reap it is the guarantee that
  tier exists for. Report it; do not force past it.
- **A box whose PR was *closed* rather than merged is different**, and worth surfacing:
  its commits exist nowhere else, and no schedule will ever clear it. Check with
  `gh pr view <n> --json state,mergedAt` before calling it abandoned.

## Reporting

Say which jobs are healthy, not only which are not -- "every job green but one, and that
one is a stale record" is the answer; a list of everything wrong reads as a broken
machine.

Fix what you find in the same turn, per the execution default. Two things are the user's
call, because both are irreversible and neither is yours to assume: discarding work that
exists only in a box, and running a job whose side effects reach outside this repo.
