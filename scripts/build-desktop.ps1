# =====================================================================================
# Build the NEXUS desktop client as a standalone Windows executable.
# =====================================================================================
#   & .\scripts\build-desktop.ps1
#   & .\scripts\build-desktop.ps1 -OutputRoot "E:\NEXUS-build"
#   & .\scripts\build-desktop.ps1 -Console -SkipSelfCheck
#
# If Windows refuses to run the file ("not digitally signed"), allow it for this session only:
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
#
# TWO ROOTS, ON PURPOSE
#   -BuildRoot  where PyInstaller runs. Defaults to <repo>\build-output, i.e. inside the project.
#   -OutputRoot where the finished application is published (dist only). Defaults to the first
#               roomy drive that is not the system drive, so a build artefact does not consume
#               the system drive.
#
# Why the split: on this machine the project's virtual-environment interpreter can create files
# only inside the workspace, while other processes can write anywhere. Building directly onto
# another drive therefore fails with "Access is denied" from deep inside PyInstaller, which looks
# like a permissions problem in the project but is not. Building in the workspace and copying the
# result out works, keeps the bulky part off the system drive, and is verified by running the
# published executable.
# =====================================================================================

[CmdletBinding()]
param(
    # Where the finished application is published (created if missing).
    [string]$OutputRoot,

    # Where PyInstaller does its work. Keep this inside the project unless you know your
    # interpreter can write elsewhere.
    [string]$BuildRoot,

    # Build a console binary (useful when a start-up failure needs a traceback).
    [switch]$Console,

    # Skip the post-build self-check (the check needs the backend running).
    [switch]$SkipSelfCheck,

    # Backend the self-check talks to.
    [string]$ApiUrl = "http://127.0.0.1:8000",
    [string]$ApiKey = $env:NEXUS_API_KEY
)

$ErrorActionPreference = "Stop"
# Build tools (uv, pytest, PyInstaller) write progress to stderr. With PowerShell 7.4's
# $PSNativeCommandUseErrorActionPreference default that turns a successful command into a
# terminating error under ErrorActionPreference='Stop'. Exit codes are checked explicitly instead.
$PSNativeCommandUseErrorActionPreference = $false

$repoRoot = Split-Path -Parent $PSScriptRoot
$desktopDir = Join-Path $repoRoot "desktop"

function Resolve-OutputRoot {
    param([string]$Requested)
    if ($Requested) { return $Requested }
    # Prefer a drive that is not the system drive and has room to spare.
    foreach ($candidate in @("E:\", "G:\", "D:\")) {
        if (Test-Path $candidate) {
            $free = (Get-PSDrive -Name $candidate.Substring(0, 1) -ErrorAction SilentlyContinue).Free
            if ($free -and $free -gt 5GB) { return (Join-Path $candidate "NEXUS-build") }
        }
    }
    return (Join-Path $repoRoot "build-output")
}

$buildRoot = if ($BuildRoot) { $BuildRoot } else { Join-Path $repoRoot "build-output" }
$outRoot = Resolve-OutputRoot $OutputRoot
$work = Join-Path $buildRoot "work"
$buildDist = Join-Path $buildRoot "dist"
$publishDist = Join-Path $outRoot "dist"
$report = Join-Path $buildRoot "selfcheck.json"

Write-Host "NEXUS desktop build" -ForegroundColor Cyan
Write-Host "  source    : $desktopDir"
Write-Host "  build root: $buildRoot  (PyInstaller work)"
Write-Host "  publish to: $outRoot  (final application)"
$driveLetter = (Split-Path -Qualifier $buildRoot).TrimEnd(':')
$freeGB = [math]::Round((Get-PSDrive -Name $driveLetter).Free / 1GB, 1)
Write-Host "  free      : $freeGB GB on ${driveLetter}:"
if ($freeGB -lt 3) {
    throw "Not enough free space on ${driveLetter}: (need ~3 GB, found $freeGB GB). Use -BuildRoot."
}

Push-Location $desktopDir
try {
    Write-Host "`n[1/5] Ensuring build dependencies..." -ForegroundColor Cyan
    uv sync --quiet
    # Idempotent: adds PyInstaller to the dev group the first time, leaves it alone afterwards.
    uv add --dev --quiet pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "Could not install PyInstaller." }

    Write-Host "`n[2/5] Running the test suite first (never package a broken build)..." -ForegroundColor Cyan
    $env:QT_QPA_PLATFORM = "offscreen"
    uv run pytest -q
    if ($LASTEXITCODE -ne 0) { throw "Tests failed; refusing to build." }

    Write-Host "`n[3/5] Building with PyInstaller (work: $buildRoot)..." -ForegroundColor Cyan
    if ($Console) { $env:NEXUS_BUILD_CONSOLE = "1" } else { Remove-Item Env:NEXUS_BUILD_CONSOLE -ErrorAction SilentlyContinue }
    New-Item -ItemType Directory -Force -Path $buildDist, $work | Out-Null

    # PyInstaller writes progress to stderr. Two things go wrong if that is not handled here:
    #   1. with $ErrorActionPreference='Stop', merged stderr becomes a terminating error;
    #   2. redirecting this script's streams from the caller triggers the same thing, which makes
    #      a build fail for a reason that has nothing to do with the build.
    # So the output is captured here, into a log the caller can read, with errors non-terminating.
    $pyiLog = Join-Path $buildRoot "pyinstaller.log"
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        uv run pyinstaller nexus.spec --distpath $buildDist --workpath $work --noconfirm 2>&1 |
            Tee-Object -FilePath $pyiLog | Out-Null
        $pyiExit = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previous
    }
    if ($pyiExit -ne 0) {
        Write-Host "      PyInstaller failed; full output: $pyiLog" -ForegroundColor Red
        Get-Content $pyiLog -Tail 15 | ForEach-Object { "        $_" }
        throw "PyInstaller failed (exit $pyiExit)."
    }

    $builtApp = Join-Path $buildDist "NEXUS"
    $builtExe = Join-Path $builtApp "NEXUS.exe"
    if (-not (Test-Path $builtExe)) { throw "Expected executable not found at $builtExe" }
    $builtMB = [math]::Round((Get-ChildItem $builtApp -Recurse -File |
        Measure-Object Length -Sum).Sum / 1MB, 1)
    Write-Host "      built: $builtExe ($builtMB MB)" -ForegroundColor Green

    Write-Host "`n[4/5] Publishing the application to $publishDist..." -ForegroundColor Cyan
    New-Item -ItemType Directory -Force -Path $publishDist | Out-Null
    # /MIR keeps the destination an exact copy without deleting anything outside it.
    $null = robocopy $builtApp (Join-Path $publishDist "NEXUS") /MIR /NFL /NDL /NJH /NJS /NP
    if ($LASTEXITCODE -ge 8) { throw "robocopy failed with exit code $LASTEXITCODE" }
    $publishedExe = Join-Path $publishDist "NEXUS\NEXUS.exe"
    if (-not (Test-Path $publishedExe)) { throw "Published executable missing at $publishedExe" }
    Write-Host "      published: $publishedExe" -ForegroundColor Green

    # Ship the human-facing extras with the application: the Persian quick-start (written for the
    # project owner, not for the repository, whose documentation is English) and a double-click
    # launcher. Copying them from the repository keeps one source of truth; editing them by hand in
    # the output folder would be lost on the next build.
    $extras = Join-Path $desktopDir "packaging"
    $extraTarget = Join-Path $publishDist "NEXUS"
    if (Test-Path $extras) {
        Copy-Item (Join-Path $extras "*") $extraTarget -Force
        $shipped = (Get-ChildItem $extras -File | ForEach-Object { $_.Name }) -join ", "
        Write-Host "      extras   : $shipped" -ForegroundColor Green
    }

    # Ship the licence and the third-party notices. This is not optional politeness: the build
    # redistributes Qt, which is LGPL-3.0, and LGPL section 4b requires the licence texts to
    # accompany the work. A build that omits them is not redistributable.
    $legal = @(
        @{ Source = (Join-Path $repoRoot "LICENSE");                  Target = $extraTarget },
        @{ Source = (Join-Path $repoRoot "NOTICE");                   Target = $extraTarget },
        @{ Source = (Join-Path $repoRoot "THIRD-PARTY-NOTICES.md");   Target = $extraTarget },
        @{ Source = (Join-Path $repoRoot "licenses");                 Target = (Join-Path $extraTarget "licenses") }
    )
    foreach ($item in $legal) {
        if (Test-Path $item.Source) {
            Copy-Item $item.Source $item.Target -Recurse -Force
        } else {
            throw "Required legal file is missing: $($item.Source)"
        }
    }
    $licenceFiles = (Get-ChildItem (Join-Path $extraTarget "licenses") -File |
        ForEach-Object { $_.Name }) -join ", "
    Write-Host "      licences : LICENSE, NOTICE, THIRD-PARTY-NOTICES.md, licenses\{$licenceFiles}" -ForegroundColor Green

    if ($SkipSelfCheck) {
        Write-Host "`n[5/5] Self-check skipped (-SkipSelfCheck)." -ForegroundColor Yellow
        # Exit explicitly: a script invoked with '&' or 'pwsh -File' does not set $LASTEXITCODE for
        # the caller unless it calls exit, so a caller would otherwise read a stale value from some
        # earlier native command and misread a successful build as a failure.
        exit 0
    }

    Write-Host "`n[5/5] Running the published executable's self-check..." -ForegroundColor Cyan
    Write-Host "      exe   : $publishedExe"
    Write-Host "      report: $report"
    $env:NEXUS_DESKTOP_API_URL = $ApiUrl
    if ($ApiKey) { $env:NEXUS_DESKTOP_API_KEY = $ApiKey }
    # Start-Process with -Wait -PassThru, NOT the call operator.
    #
    # $LASTEXITCODE is not set when the call operator invokes a GUI-subsystem executable. Reading
    # $LASTEXITCODE after `& NEXUS.exe …` therefore returns whatever an earlier command left
    # behind, which made a perfectly healthy windowed build look like a failure (it reported 1
    # while Start-Process reported 0). For a GUI binary, ask the process object.
    $process = Start-Process -FilePath $publishedExe `
        -ArgumentList "--self-check", $report `
        -Wait -PassThru -NoNewWindow
    $code = $process.ExitCode
    if (Test-Path $report) {
        Write-Host "      report:" -ForegroundColor DarkGray
        Get-Content $report | ForEach-Object { "        $_" }
    }
    if ($code -ne 0) {
        Write-Host "`nSelf-check reported a problem (exit $code)." -ForegroundColor Red
        Write-Host "Is the backend running at $ApiUrl ?" -ForegroundColor Yellow
        exit $code
    }
    Write-Host "`nBuild verified: the published client started from $outRoot, built every page and reached the API." -ForegroundColor Green
    exit 0
}
finally {
    Pop-Location
}
