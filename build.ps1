<#
.SYNOPSIS
    Freeze rumble-chat-chart into two executables, then build the Windows installer.

.DESCRIPTION
    Produces:
      dist\rumble-chat-chart\rumble-chat-chart.exe    console build, the full CLI
      dist\rumble-chat-chartw\rumble-chat-chartw.exe  windowed build, service and dialogs
      dist\rumble-chat-chart-setup-<ver>.exe          the click-to-install installer

    Requires PyInstaller (pip) and Inno Setup 6 (ISCC.exe). Neither is installed
    automatically; run with -Check to see what is missing.

.PARAMETER SkipInstaller
    Freeze the exes but do not build the installer.

.PARAMETER Sign
    Path to a code-signing .pfx. Strongly recommended for anything you
    distribute; see the Distribution notes in README.md.
#>
[CmdletBinding()]
param(
    [switch]$Check,
    [switch]$SkipInstaller,
    [string]$Sign = "",
    [string]$SignPassword = ""
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $here

function Find-Iscc {
    $candidates = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    )
    foreach ($c in $candidates) { if (Test-Path $c) { return $c } }
    $cmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Find-Signtool {
    $tool = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin" -Filter signtool.exe -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match "\\x64\\" } | Select-Object -First 1
    if (-not $tool) { throw "signtool.exe (x64) not found; install the Windows SDK" }
    return $tool.FullName
}

# --- prerequisites --------------------------------------------------------- #
$missing = @()

python -c "import PyInstaller" 2>$null
if (-not $?) {
    $missing += "PyInstaller       ->  python -m pip install pyinstaller"
}

$iscc = Find-Iscc
if (-not $iscc -and -not $SkipInstaller) {
    $missing += "Inno Setup 6      ->  https://jrsoftware.org/isdl.php  (free)"
}

if ($missing.Count -gt 0) {
    Write-Host "Missing build tools:" -ForegroundColor Yellow
    $missing | ForEach-Object { Write-Host "  $_" }
    if (-not $Check) { Pop-Location; throw "install the tools above, then re-run build.ps1" }
}
if ($Check) {
    if ($missing.Count -eq 0) { Write-Host "All build tools present." -ForegroundColor Green }
    Pop-Location
    exit 0
}

# --- freeze ---------------------------------------------------------------- #
Remove-Item -Recurse -Force "$here\dist\rumble-chat-chart", "$here\dist\rumble-chat-chartw" -ErrorAction SilentlyContinue

Write-Host "`n=== freezing console build ===" -ForegroundColor Cyan
python -m PyInstaller --noconfirm --clean --onedir --console `
    --name rumble-chat-chart `
    --workpath "$here\build\cli" --specpath "$here\build" --distpath "$here\dist" `
    --hidden-import tkinter --hidden-import tkinter.simpledialog --hidden-import tkinter.messagebox `
    "$here\rumble_chat_chart.py"
if (-not $?) { Pop-Location; throw "PyInstaller failed on the console build" }

Write-Host "`n=== freezing windowed build ===" -ForegroundColor Cyan
python -m PyInstaller --noconfirm --clean --onedir --noconsole `
    --name rumble-chat-chartw `
    --workpath "$here\build\gui" --specpath "$here\build" --distpath "$here\dist" `
    --hidden-import tkinter --hidden-import tkinter.simpledialog --hidden-import tkinter.messagebox `
    "$here\rumble_chat_chartw.py"
if (-not $?) { Pop-Location; throw "PyInstaller failed on the windowed build" }

# --- smoke test the frozen CLI --------------------------------------------- #
Write-Host "`n=== smoke test ===" -ForegroundColor Cyan
$exe = "$here\dist\rumble-chat-chart\rumble-chat-chart.exe"
& $exe --help | Select-Object -First 3
if (-not $?) { Pop-Location; throw "the frozen exe does not run" }

# --- sign the exes --------------------------------------------------------- #
if ($Sign) {
    $signtool = Find-Signtool
    Write-Host "`n=== signing executables ===" -ForegroundColor Cyan
    $targets = @(
        "$here\dist\rumble-chat-chart\rumble-chat-chart.exe",
        "$here\dist\rumble-chat-chartw\rumble-chat-chartw.exe"
    )
    foreach ($target in $targets) {
        & $signtool sign /f $Sign /p $SignPassword /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 $target
        if (-not $?) { Pop-Location; throw "signing failed for $target" }
    }
}

if ($SkipInstaller) {
    Write-Host "`nFrozen builds are in dist\. Skipping installer." -ForegroundColor Green
    Pop-Location
    exit 0
}

# --- installer ------------------------------------------------------------- #
Write-Host "`n=== building installer ===" -ForegroundColor Cyan
& $iscc "/Qp" "$here\installer\rumble-chat-chart.iss"
if (-not $?) { Pop-Location; throw "Inno Setup failed" }

$setup = Get-ChildItem "$here\dist\rumble-chat-chart-setup-*.exe" | Sort-Object LastWriteTime | Select-Object -Last 1
if ($Sign -and $setup) {
    & (Find-Signtool) sign /f $Sign /p $SignPassword /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 $setup.FullName
}

Write-Host ""
Write-Host "Built $($setup.FullName)" -ForegroundColor Green
Write-Host ("{0:N1} MB" -f ($setup.Length / 1MB))
if (-not $Sign) {
    Write-Host ""
    Write-Host "NOT SIGNED - anyone who downloads this will see a SmartScreen warning." -ForegroundColor Yellow
    Write-Host "See the Distribution section of README.md." -ForegroundColor Yellow
}
Pop-Location
