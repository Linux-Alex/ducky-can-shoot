<#
.SYNOPSIS
    Publish a Ducky Can Shoot release from a Windows machine.

.DESCRIPTION
    The Linux artefacts (wheel, sdist, .deb, AppImage) are built by GitHub
    Actions, not on this machine.  This script pushes the release commit and
    the v<version> tag that starts .github/workflows/release.yml, waits for
    that workflow with the GitHub CLI, and then checks that the published
    release carries every expected asset.

    It does not bump the version.  Run scripts/bump-version.py on a Linux
    checkout (or commit the bump by hand), push the commit, and then run this
    from the same checkout - usually with Git for Windows and the GitHub CLI
    installed.

.PARAMETER Version
    Release version, for example 0.3.0.  Defaults to the value in
    src/dcs/version.py, and must match pyproject.toml.

.PARAMETER DryRun
    Print every git and gh command instead of running it.

.PARAMETER NoWatch
    Do not wait for the workflow or check the release assets.

.PARAMETER Force
    Move an existing tag.  Dangerous: a published tag and its release already
    point somewhere, and moving it confuses everybody who downloaded it.

.PARAMETER Retrigger
    The tag is already on origin but no release was published for it (the
    common case being a workflow that never started, for example because
    GitHub reported "your account is locked due to a billing issue").  Delete
    the remote tag and push it again, which is what starts the release
    workflow a second time.  Refused when a release already exists.

.EXAMPLE
    pwsh -File scripts/release-windows.ps1

.EXAMPLE
    pwsh -File scripts/release-windows.ps1 -Version 0.3.1 -DryRun
#>
[CmdletBinding()]
param(
    [string]$Version = "",
    [switch]$DryRun,
    [switch]$NoWatch,
    [switch]$Force,
    [switch]$Retrigger
)

$ErrorActionPreference = 'Stop'

function Say([string]$Text) { Write-Host "==> $Text" -ForegroundColor Cyan }
function Warn([string]$Text) { Write-Host "warning: $Text" -ForegroundColor Yellow }
function Fail([string]$Text) { Write-Host "error: $Text" -ForegroundColor Red; exit 2 }

function Invoke-Step([string]$Exe, [string[]]$Arguments) {
    if ($DryRun) {
        Write-Host ("    " + $Exe + " " + ($Arguments -join " ")) -ForegroundColor DarkGray
        return ""
    }
    $output = & $Exe @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        $output | Write-Host
        Fail ("{0} {1} exited with {2}" -f $Exe, ($Arguments -join " "), $LASTEXITCODE)
    }
    return ($output -join [Environment]::NewLine)
}

function Git([string[]]$Arguments) { return Invoke-Step "git" $Arguments }
function Gh([string[]]$Arguments) { return Invoke-Step "gh" $Arguments }

# ---------------------------------------------------------------- environment
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $repo
Say "repository: $repo"

foreach ($tool in @('git', 'gh')) {
    $found = Get-Command $tool -ErrorAction SilentlyContinue
    if (-not $found) { Fail "$tool is not on PATH (install Git for Windows and the GitHub CLI)" }
}

if (-not $DryRun) {
    & gh auth status *> $null
    if ($LASTEXITCODE -ne 0) { Fail "gh is not logged in; run: gh auth login" }
}

# ------------------------------------------------------------------- version
if (-not $Version) {
    $match = Select-String -Path (Join-Path $repo 'src/dcs/version.py') -Pattern '__version__\s*=\s*"([^"]+)"'
    if (-not $match) { Fail "cannot read __version__ from src/dcs/version.py" }
    $Version = $match.Matches[0].Groups[1].Value
}
if ($Version -notmatch '^[0-9]+\.[0-9]+\.[0-9]+$') { Fail "'$Version' is not a version like 0.3.0" }

$pyproject = Select-String -Path (Join-Path $repo 'pyproject.toml') -Pattern '^version\s*=\s*"([^"]+)"'
if (-not $pyproject) { Fail "cannot read version from pyproject.toml" }
if ($pyproject.Matches[0].Groups[1].Value -ne $Version) {
    Fail ("pyproject.toml says {0} but src/dcs/version.py says {1}; bump both" -f $pyproject.Matches[0].Groups[1].Value, $Version)
}

$tag = "v$Version"
$branch = (& git rev-parse --abbrev-ref HEAD)
Say "release $Version (tag $tag) from branch $branch"

# --------------------------------------------------------------------- checks
$dirty = (& git status --porcelain)
if ($dirty) {
    $dirty | Write-Host
    Fail "the working tree is not clean; commit the release changes first"
}
if ($branch -ne 'main') { Warn "you are on '$branch', not main; a release is usually tagged on main" }

Git @('fetch', 'origin', '--tags') | Out-Null
$localHead = (& git rev-parse HEAD)
$remoteHead = (& git rev-parse "origin/$branch" 2>$null)
if ($LASTEXITCODE -eq 0 -and $remoteHead -and $localHead -ne $remoteHead) {
    Fail "HEAD is not what origin/$branch points at; pull or push first (git status)"
}

$tagExists = -not [string]::IsNullOrWhiteSpace((& git ls-remote --tags origin $tag))
$releaseExists = $false
if ($tagExists) {
    & gh release view $tag --json tagName *> $null
    if ($LASTEXITCODE -eq 0) { $releaseExists = $true }
}

if ($tagExists -and $releaseExists -and -not $Force) {
    Fail "$tag is already published; bump the version to release something new"
}
if ($tagExists -and -not $releaseExists -and -not $Force -and -not $Retrigger) {
    Fail ("the tag {0} exists on origin but no release was published for it - a workflow that never started looks exactly like this. Fix the reason (for example an account/billing problem in GitHub Settings), then re-run with -Retrigger." -f $tag)
}
if ($Retrigger -and $releaseExists) {
    Fail "a release for $tag already exists; -Retrigger would not change it"
}
if ($Retrigger -and -not $tagExists) {
    Warn "nothing to retrigger: $tag is not on origin, so a normal run will create it"
}

# ---------------------------------------------------------------- the release
if ($DryRun) { Say "dry run: nothing will be pushed, tagged or released" }

Say "pushing the release commit"
Git @('push', 'origin', 'HEAD') | Out-Null

if ($Force) {
    Say "moving the tag $tag (forced)"
    Git @('tag', '-f', '-a', $tag, '-m', "Ducky Can Shoot $Version") | Out-Null
    Git @('push', '--force', 'origin', $tag) | Out-Null
} elseif ($Retrigger -and $tagExists) {
    Say "deleting and re-pushing $tag to start the release workflow again"
    Git @('push', 'origin', ":refs/tags/$tag") | Out-Null
    Git @('tag', '-f', '-a', $tag, '-m', "Ducky Can Shoot $Version") | Out-Null
    Git @('push', 'origin', $tag) | Out-Null
} else {
    Say "creating and pushing the tag $tag"
    Git @('tag', '-a', $tag, '-m', "Ducky Can Shoot $Version") | Out-Null
    Git @('push', 'origin', $tag) | Out-Null
}

if ($NoWatch) {
    Say "not waiting: the release workflow was started by the tag push"
    Say "check it with: gh run list --workflow release.yml --limit 3"
    exit 0
}

if ($DryRun) {
    Say "would wait for the release workflow here"
    exit 0
}

# ------------------------------------------------------------------ the watch
Say "waiting for the release workflow"
$started = (Get-Date).AddMinutes(-5)
$runId = $null
for ($attempt = 0; $attempt -lt 40 -and -not $runId; $attempt++) {
    Start-Sleep -Seconds 3
    $json = (& gh run list --workflow release.yml --limit 10 --json databaseId,event,createdAt,status,conclusion)
    if ($LASTEXITCODE -ne 0) { continue }
    $runs = $json | ConvertFrom-Json
    if (-not $runs) { continue }
    $candidate = $runs |
        Where-Object { $_.event -eq 'push' -and [datetime]$_.createdAt -gt $started } |
        Sort-Object { [datetime]$_.createdAt } -Descending |
        Select-Object -First 1
    if ($candidate) { $runId = $candidate.databaseId }
}
if (-not $runId) { Fail "the release workflow did not start; look at https://github.com/Linux-Alex/ducky-can-shoot/actions" }

Say "watching run $runId (about ten minutes: wheel, .deb, AppImage, attestation)"
& gh run watch $runId --exit-status
if ($LASTEXITCODE -ne 0) {
    Fail "the release workflow failed; open the run and read the failing step: gh run view $runId --log-failed"
}

# --------------------------------------------------------------- the release
Say "checking the published release"
$releaseJson = $null
for ($attempt = 0; $attempt -lt 20 -and -not $releaseJson; $attempt++) {
    $output = (& gh release view $tag --json tagName,assets 2>$null)
    if ($LASTEXITCODE -eq 0 -and $output) { $releaseJson = $output }
    else { Start-Sleep -Seconds 3 }
}
if (-not $releaseJson) { Fail "the workflow finished but release $tag is not visible yet; try gh release view $tag" }

$release = $releaseJson | ConvertFrom-Json
$names = @($release.assets | ForEach-Object { $_.name })
$expected = @(
    "ducky_can_shoot-$Version-py3-none-any.whl",
    "ducky_can_shoot-$Version.tar.gz",
    "ducky-can-shoot_$Version-1_all.deb",
    "SHA256SUMS"
)
$missing = @()
foreach ($name in $expected) {
    if ($names -notcontains $name) { $missing += $name }
}
$appimage = $names | Where-Object { $_ -like 'DuckyCanShoot-*.AppImage' }
if (-not $appimage) { $missing += 'DuckyCanShoot-<arch>.AppImage' }

if ($missing.Count -gt 0) {
    Fail ("the release is missing: " + ($missing -join ', '))
}

Say "release $tag is published with:"
$names | Sort-Object | ForEach-Object { Write-Host "    $_" }
Say "verify a download with: gh attestation verify <file> -R Linux-Alex/ducky-can-shoot"
Say "done: https://github.com/Linux-Alex/ducky-can-shoot/releases/tag/$tag"
exit 0
