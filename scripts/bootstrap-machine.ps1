<#
.SYNOPSIS
  Provision a fresh Windows workstation for devkit, from nothing to self-maintaining.

.DESCRIPTION
  The one thing a new machine cannot get from a VS Code task, because every task in this
  workspace lives in the workspace file -- which arrives with the clone this script makes.
  That is the whole reason it exists, and the reason it is PowerShell rather than Python
  like everything else under scripts/: it runs before Python is installed.

  It does the seven steps a workstation needed by hand, none of which was written down:

    1. winget the prerequisites: git, uv, the GitHub CLI, VS Code -- then Python from uv,
       so `python3` is a real interpreter and not the Microsoft Store alias every devkit
       hook would otherwise hit.
    2. clone devkit.
    3. persist DEVKIT_DIR, without which the harness ledger silently no-ops.
    4. run install-installers-schedule.py --yes -- the ONE installer a machine ever runs
       by hand. It registers devkit-installers, which from then on discovers every other
       scripts/install-*.py by glob and keeps it current, daily and at logon.
    5. render the live workspace file from workspace.jsonc. Every VS Code task lives in
       it, and nothing else creates it until devkit-workspace-status's first daily pass.
    6. install the two VS Code extensions the workspace tasks resolve their inputs
       through. Without them roughly twenty tasks fail with
       "command 'extension.commandvariable.pickStringRemember' not found", which names a
       command rather than a package and so cannot be searched for.
    7. report whatever is left that only a human can answer -- notably the git identity,
       `gh auth login`, and restarting a VS Code that was open during the installs.

  Idempotent: every step checks before it acts, so re-running it repairs a machine rather
  than doubling anything up. Dry by default, like every installer in this repo; -Yes
  applies. Nothing here is devkit-specific magic -- the same steps by hand are in the
  "New workstation" section of README.md.

  NOTE ON STEP 6. scripts/vscode_extensions.py is emphatic that "a recommendations entry
  is a prompt, never an install", and that stands: a daily *reporter* must never install
  software behind the operator. This is the opposite context -- an explicit,
  operator-invoked provisioning run whose entire purpose is to put the machine in a
  working state -- so it installs them and says that it did. The reporter keeps prompting.

.PARAMETER Path
  Where to clone devkit. Default: ~\vs-code\devkit.

.PARAMETER Repo
  The clone URL. Default: devkit's public repo.

.PARAMETER Yes
  Apply. Without it every step prints what it would do and changes nothing.

.PARAMETER SkipPrerequisites
  Do not call winget. For a machine whose software is managed some other way.

.EXAMPLE
  # Inspect first (the default):
  & ([scriptblock]::Create((irm https://raw.githubusercontent.com/alexandrec90/devkit/main/scripts/bootstrap-machine.ps1)))

.EXAMPLE
  # Then apply:
  & ([scriptblock]::Create((irm https://raw.githubusercontent.com/alexandrec90/devkit/main/scripts/bootstrap-machine.ps1))) -Yes
#>

[CmdletBinding()]
param(
    [string] $Path = (Join-Path $HOME 'vs-code\devkit'),
    [string] $Repo = 'https://github.com/alexandrec90/devkit.git',
    [switch] $Yes,
    [switch] $SkipPrerequisites
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:Planned = @()
$script:Problems = @()
$script:SoftwareInstalled = $false

function Step($message) { Write-Host "==> $message" -ForegroundColor Cyan }
function Note($message) { Write-Host "    $message" }
function Warn($message) { Write-Host "    $message" -ForegroundColor Yellow }

function Would($message) {
    $script:Planned += $message
    if (-not $Yes) { Note "would $message" }
}

# winget package ids, paired with the command each one puts on PATH. The command is what
# is actually tested: a package can be "installed" per-machine and still not be reachable
# from this shell, and PATH is what every later step depends on.
$Prerequisites = @(
    @{ Command = 'git';  Id = 'Git.Git';                  Name = 'Git' }
    @{ Command = 'uv';   Id = 'astral-sh.uv';             Name = 'uv' }
    # Every PR, gate and fix-pass read goes through it; the fix pass refuses to start without it.
    @{ Command = 'gh';   Id = 'GitHub.cli';               Name = 'GitHub CLI' }
    @{ Command = 'code'; Id = 'Microsoft.VisualStudioCode'; Name = 'VS Code' }
)

# Only rioj7.command-variable supplies pickStringRemember/multiPick, which roughly twenty
# of the workspace's tasks resolve their checkout through; tasks-shell-input supplies
# shellCommand.execute, which the agent tasks use. Kept in step with workspace.jsonc's
# own "recommendations" list -- vscode_extensions.py reads that list as the source of
# truth once the clone exists, and reports anything missing on the daily pass.
$Extensions = @('rioj7.command-variable', 'augustocdias.tasks-shell-input')

function Test-Command($name) {
    $null -ne (Get-Command $name -ErrorAction SilentlyContinue)
}

# Found is not enough for Python: a fresh Windows has `python.exe` and `python3.exe` in
# WindowsApps as Microsoft Store aliases, which print "Python was not found" and exit 9009.
function Test-Runs($name) {
    if (-not (Test-Command $name)) { return $false }
    # The alias writes to stderr, which Windows PowerShell 5.1 makes terminating under Stop.
    $ErrorActionPreference = 'Continue'
    & $name -c 'import sys' *> $null
    $LASTEXITCODE -eq 0
}

# --- 1. prerequisites ---------------------------------------------------------

Step 'Prerequisites'
if ($SkipPrerequisites) {
    Note 'skipped (-SkipPrerequisites)'
} elseif (-not (Test-Command 'winget')) {
    Warn 'winget is not on PATH -- install App Installer from the Microsoft Store, or install git, uv, gh and VS Code yourself.'
    $script:Problems += 'winget unavailable; prerequisites not checked'
} else {
    foreach ($tool in $Prerequisites) {
        if (Test-Command $tool.Command) {
            Note "$($tool.Name): already on PATH"
            continue
        }
        Would "winget install $($tool.Id)  ($($tool.Name))"
        if ($Yes) {
            Note "installing $($tool.Name)..."
            winget install --exact --id $tool.Id --accept-source-agreements --accept-package-agreements --silent
            if ($LASTEXITCODE -ne 0) { $script:Problems += "winget failed for $($tool.Id)" }
            else { $script:SoftwareInstalled = $true }
        }
    }
    # winget edits the machine PATH, which this already-running process does not see.
    if ($Yes) {
        $env:PATH = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                    [Environment]::GetEnvironmentVariable('Path', 'User')
    }
}

# --- 1b. Python, from uv ------------------------------------------------------
#
# Not from winget: python.org's installer ships no python3.exe, and every devkit git hook
# and every pre-commit `language: script` entry starts `#!/usr/bin/env python3`. On Windows
# that name then falls through to the Microsoft Store alias, which exits 9009. The global
# post-checkout hook fails, so `git worktree add` exits non-zero ("could not cut ...
# nothing opened"), and every ruff hook refuses the commit. `uv python install --default`
# puts a real python.exe and python3.exe in uv's bin directory. That directory has to come
# before WindowsApps on the user PATH, because WindowsApps is where the aliases live.

Step 'Python'
if ((Test-Runs 'python3') -and (Test-Runs 'python')) {
    Note 'python and python3: both run'
} elseif (-not (Test-Command 'uv')) {
    Warn 'python/python3 are missing or only the Store aliases, and uv is not on PATH to install them -- every devkit hook will exit 9009.'
    $script:Problems += 'no python3 that runs'
} else {
    Would 'uv python install --default   # a real python.exe and python3.exe'
    if ($Yes) {
        uv python install --default
        if ($LASTEXITCODE -ne 0) { $script:Problems += 'uv python install --default failed' }
        else { $script:SoftwareInstalled = $true }
    }
    $bin = (uv python dir --bin | Out-String).Trim().TrimEnd('\')
    $parts = @([Environment]::GetEnvironmentVariable('Path', 'User') -split ';' | Where-Object { $_ })
    $binAt = -1; $aliasAt = -1
    for ($i = 0; $i -lt $parts.Count; $i++) {
        if ($binAt -lt 0 -and $parts[$i].TrimEnd('\') -eq $bin) { $binAt = $i }
        if ($aliasAt -lt 0 -and $parts[$i] -like '*\Microsoft\WindowsApps*') { $aliasAt = $i }
    }
    if ($binAt -lt 0 -or ($aliasAt -ge 0 -and $binAt -gt $aliasAt)) {
        Would "put $bin first on the user PATH, ahead of the WindowsApps aliases"
        if ($Yes) {
            $rest = @($parts | Where-Object { $_.TrimEnd('\') -ne $bin })
            [Environment]::SetEnvironmentVariable('Path', ((@($bin) + $rest) -join ';'), 'User')
            $env:PATH = "$bin;$env:PATH"
            $script:SoftwareInstalled = $true
        }
    }
    if ($Yes -and -not (Test-Runs 'python3')) {
        $script:Problems += 'python3 still does not run after uv python install --default'
    }
}

# --- 2. the clone -------------------------------------------------------------

Step "Devkit checkout at $Path"
$gitDir = Join-Path $Path '.git'
if (Test-Path $gitDir) {
    Note 'already a git checkout; leaving it alone'
} else {
    Would "git clone $Repo $Path"
    if ($Yes) {
        if (-not (Test-Command 'git')) {
            throw 'git is still not on PATH -- open a new terminal and re-run; winget PATH changes need one.'
        }
        $parent = Split-Path -Parent $Path
        if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
        git clone $Repo $Path
        if ($LASTEXITCODE -ne 0) { throw "git clone failed with exit $LASTEXITCODE" }
    }
}

# --- 3. DEVKIT_DIR ------------------------------------------------------------
#
# The harness ledger, the drift check and the triage backlog all resolve through it, and
# every one of them is a silent no-op without it rather than an error -- which is the
# worst shape for a missing setting to have.

Step 'DEVKIT_DIR'
$current = [Environment]::GetEnvironmentVariable('DEVKIT_DIR', 'User')
if ($current -eq $Path) {
    Note "already set to $Path"
} else {
    if ($current) { Note "currently $current" }
    Would "setx DEVKIT_DIR $Path"
    if ($Yes) {
        [Environment]::SetEnvironmentVariable('DEVKIT_DIR', $Path, 'User')
        $env:DEVKIT_DIR = $Path
    }
}

# --- 4. the one installer -----------------------------------------------------

Step 'Scheduled jobs'
$installer = Join-Path $Path 'scripts\install-installers-schedule.py'
if (-not (Test-Path $installer)) {
    if ($Yes) {
        $script:Problems += "no installer at $installer -- the clone did not land"
        Warn 'skipped: the clone is not there yet'
    } else {
        Note 'would run install-installers-schedule.py --yes (after the clone above)'
    }
} else {
    Would "python $installer --yes   # registers devkit-installers; it registers the rest"
    if ($Yes) {
        python $installer --yes
        if ($LASTEXITCODE -ne 0) { $script:Problems += "install-installers-schedule.py exited $LASTEXITCODE" }
        # One immediate pass, so the machine is fully provisioned when this returns
        # rather than at the next logon.
        Note 'running the first maintenance pass...'
        python (Join-Path $Path 'scripts\installers.py') maintain
        if ($LASTEXITCODE -gt 1) { $script:Problems += "installers.py maintain exited $LASTEXITCODE" }
    }
}

# --- 5. the workspace file ----------------------------------------------------
#
# The step the summary used to skip while telling you to open its result. The render
# only ever writes a missing file or one devkit wrote itself, and refuses over a hand
# edit, so a re-run on a set-up machine is a no-op rather than a clobber.

Step 'Workspace file'
$render = Join-Path $Path 'scripts\devkit_project.py'
# Named here only for the summary; the render resolves the path itself
# (sweep.WORKSPACE_FILE_NAME, beside the checkout), so a stale name here could mislead
# the note but never the write. tests/test_bootstrap_machine.py pins the two together.
$workspace = Join-Path (Split-Path -Parent $Path) 'alex-projects.code-workspace'
if (-not (Test-Path $render)) {
    if ($Yes) {
        $script:Problems += "no renderer at $render -- the clone did not land"
        Warn 'skipped: the clone is not there yet'
    } else {
        Note 'would run devkit_project.py --render-workspace (after the clone above)'
    }
} else {
    Would "python $render --render-workspace   # creates $workspace"
    if ($Yes) {
        python $render --render-workspace
        if ($LASTEXITCODE -ne 0) { $script:Problems += "devkit_project.py --render-workspace exited $LASTEXITCODE" }
    }
}

# --- 6. VS Code extensions ----------------------------------------------------

Step 'VS Code extensions'
if (-not (Test-Command 'code')) {
    Warn 'the `code` CLI is not on PATH -- VS Code will prompt for these when you open the workspace.'
} else {
    $installed = @(code --list-extensions 2>$null)
    foreach ($extension in $Extensions) {
        if ($installed -contains $extension) {
            Note "$extension : already installed"
            continue
        }
        Would "code --install-extension $extension"
        if ($Yes) {
            code --install-extension $extension | Out-Null
            if ($LASTEXITCODE -ne 0) { $script:Problems += "could not install $extension" }
        }
    }
}

# --- 7. what only a human can answer ------------------------------------------

Step 'Left for you'
if (Test-Command 'git') {
    foreach ($key in @('user.name', 'user.email')) {
        $value = git config --global --get $key 2>$null
        if (-not $value) {
            Warn "git $key is unset -- every commit fails with 'Author identity unknown'. Fix: git config --global $key ..."
        }
    }
}
if (Test-Command 'gh') {
    # Logged out, gh writes to stderr: see Test-Runs for why that needs Continue.
    $ErrorActionPreference = 'Continue'
    gh auth status *> $null
    $loggedIn = $LASTEXITCODE -eq 0
    $ErrorActionPreference = 'Stop'
    if (-not $loggedIn) {
        Warn 'gh is not logged in -- every PR and gate read fails. Fix: gh auth login'
    }
}
# A VS Code task inherits the PATH VS Code was launched with, so a tool installed while it
# was open is invisible to every task until it is fully quit -- "Agent: Fix what is red"
# then dies on a FileNotFoundError that names no program.
if ($script:SoftwareInstalled -and (Get-Process -Name 'Code' -ErrorAction SilentlyContinue)) {
    Warn 'VS Code was running while software was installed -- quit every window and reopen it, or its tasks will not find the new tools.'
}
Note "Open the workspace: $workspace"
Note 'Then run "Workspace: Plug / Unplug Projects" to clone the projects registered from other PCs.'
Note 'The tray icon reports every scheduled job, and "Machine: Scheduled Jobs" manages them.'

# --- the summary --------------------------------------------------------------

Write-Host ''
if (-not $Yes) {
    Write-Host "Dry run -- $($script:Planned.Count) step(s) would run. Nothing changed." -ForegroundColor Yellow
    Write-Host 'Re-run with -Yes to apply.' -ForegroundColor Yellow
    exit 0
}
if ($script:Problems.Count -gt 0) {
    Write-Host "Finished with $($script:Problems.Count) problem(s):" -ForegroundColor Red
    foreach ($problem in $script:Problems) { Write-Host "  - $problem" -ForegroundColor Red }
    exit 1
}
Write-Host 'Done. This machine now maintains itself: devkit-installers runs daily and at logon.' -ForegroundColor Green
exit 0
