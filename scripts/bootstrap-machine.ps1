<#
.SYNOPSIS
  Provision a fresh Windows workstation for devkit, from nothing to self-maintaining.

.DESCRIPTION
  The one thing a new machine cannot get from a VS Code task, because every task in this
  workspace lives in the workspace file -- which arrives with the clone this script makes.
  That is the whole reason it exists, and the reason it is PowerShell rather than Python
  like everything else under scripts/: it runs before Python is installed.

  It does the six steps a workstation needed by hand, none of which was written down:

    1. winget the prerequisites: git, Python, uv, VS Code.
    2. clone devkit.
    3. persist DEVKIT_DIR, without which the harness ledger silently no-ops.
    4. run install-installers-schedule.py --yes -- the ONE installer a machine ever runs
       by hand. It registers devkit-installers, which from then on discovers every other
       scripts/install-*.py by glob and keeps it current, daily and at logon.
    5. install the two VS Code extensions the workspace tasks resolve their inputs
       through. Without them roughly twenty tasks fail with
       "command 'extension.commandvariable.pickStringRemember' not found", which names a
       command rather than a package and so cannot be searched for.
    6. report whatever is left that only a human can answer -- notably the git identity.

  Idempotent: every step checks before it acts, so re-running it repairs a machine rather
  than doubling anything up. Dry by default, like every installer in this repo; -Yes
  applies. Nothing here is devkit-specific magic -- the same steps by hand are in the
  "New workstation" section of README.md.

  NOTE ON STEP 5. scripts/vscode_extensions.py is emphatic that "a recommendations entry
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
    @{ Command = 'python'; Id = 'Python.Python.3.13';     Name = 'Python' }
    @{ Command = 'uv';   Id = 'astral-sh.uv';             Name = 'uv' }
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

# --- 1. prerequisites ---------------------------------------------------------

Step 'Prerequisites'
if ($SkipPrerequisites) {
    Note 'skipped (-SkipPrerequisites)'
} elseif (-not (Test-Command 'winget')) {
    Warn 'winget is not on PATH -- install App Installer from the Microsoft Store, or install git, Python, uv and VS Code yourself.'
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
        }
    }
    # winget edits the machine PATH, which this already-running process does not see.
    if ($Yes) {
        $env:PATH = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                    [Environment]::GetEnvironmentVariable('Path', 'User')
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

# --- 5. VS Code extensions ----------------------------------------------------

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

# --- 6. what only a human can answer ------------------------------------------

Step 'Left for you'
if (Test-Command 'git') {
    foreach ($key in @('user.name', 'user.email')) {
        $value = git config --global --get $key 2>$null
        if (-not $value) {
            Warn "git $key is unset -- every commit fails with 'Author identity unknown'. Fix: git config --global $key ..."
        }
    }
}
Note "Open the workspace: $Path\..\alex-projects.code-workspace (or the one this machine uses)"
Note 'Then: the tray icon reports every scheduled job, and "Machine: Scheduled Jobs" manages them.'

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
