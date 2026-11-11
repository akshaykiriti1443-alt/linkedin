# ReelVault Members on your own computer (localhost) - Windows installer.
#
# Paste this ONE line into PowerShell (Start menu > type "PowerShell" > open it):
#   irm https://raw.githubusercontent.com/akshaykiriti1443-alt/linkedin/claude/kind-dijkstra-0o03in/reelvault/install-members.ps1 | iex
#
# It installs (for your user only, no admin needed) into %USERPROFILE%\ReelVault-Members:
#   - Python 3.12 (if missing), ffmpeg (reuses the ReelVault desktop app's copy if present)
#   - ReelVault Members (server + member web app) and its models
#   - the Chrome extension built for http://localhost:8800
# Then it puts a "ReelVault Members" icon on your desktop and opens http://localhost:8800.

param(
    [string]$Branch = "claude/kind-dijkstra-0o03in",
    [string]$Source = "",        # a local reelvault folder instead of downloading (used by the test run)
    [switch]$NoLaunch,
    [switch]$NoWhisper           # skip the speech models (test runs only)
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$RVM   = Join-Path $env:USERPROFILE "ReelVault-Members"
$Tools = Join-Path $RVM "tools"
$Env_  = Join-Path $RVM "env"
New-Item -ItemType Directory -Force -Path $RVM, $Tools | Out-Null

function Step($msg) { Write-Host ""; Write-Host "==> $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "    $msg" -ForegroundColor Green }

# ---------------------------------------------------------------- Python
function Find-Python {
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"
    )
    foreach ($c in $candidates) { if (Test-Path $c) { return $c } }
    foreach ($name in @("python", "python3")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source -notlike "*WindowsApps*") {
            $v = & $cmd.Source -c "import sys; print(sys.version_info >= (3, 11))" 2>$null
            if ($v -eq "True") { return $cmd.Source }
        }
    }
    return $null
}

Step "Checking Python"
$Py = Find-Python
if (-not $Py) {
    Write-Host "    Installing Python 3.12 (about 25 MB)..."
    $installer = Join-Path $env:TEMP "python-3.12.8-amd64.exe"
    Invoke-WebRequest "https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe" -OutFile $installer
    Start-Process $installer -ArgumentList "/quiet InstallAllUsers=0 PrependPath=1 Include_launcher=0 Include_test=0" -Wait
    Remove-Item $installer -ErrorAction SilentlyContinue
    $Py = Find-Python
    if (-not $Py) { throw "Python did not install. Install it from python.org, then run this again." }
}
Ok "Python: $Py"

# ---------------------------------------------------------------- ffmpeg
Step "Checking video tools (ffmpeg)"
$DesktopFf = Join-Path $env:USERPROFILE "ReelVault\tools\ffmpeg\bin\ffmpeg.exe"
$FfBin = Join-Path $Tools "ffmpeg\bin"
if ((Test-Path $DesktopFf) -or (Test-Path (Join-Path $FfBin "ffmpeg.exe"))) {
    Ok "ffmpeg ready"
} else {
    Write-Host "    Downloading ffmpeg (about 90 MB, takes a minute)..."
    $zip = Join-Path $env:TEMP "ffmpeg.zip"
    Invoke-WebRequest "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip" -OutFile $zip
    $tmp = Join-Path $env:TEMP "ffmpeg-unzip"
    if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
    Expand-Archive $zip -DestinationPath $tmp -Force
    $inner = Get-ChildItem $tmp -Directory | Select-Object -First 1
    New-Item -ItemType Directory -Force -Path $FfBin | Out-Null
    Copy-Item (Join-Path $inner.FullName "bin\ffmpeg.exe"), (Join-Path $inner.FullName "bin\ffprobe.exe") $FfBin -Force
    Remove-Item $zip, $tmp -Recurse -Force -ErrorAction SilentlyContinue
    Ok "ffmpeg ready"
}

# ---------------------------------------------------------------- stop a running copy
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*reelvault.members_local*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# ---------------------------------------------------------------- ReelVault Members
Step "Installing ReelVault Members (a few minutes the first time)"
if (-not (Test-Path (Join-Path $Env_ "Scripts\python.exe"))) { & $Py -m venv $Env_ }
$VPy  = Join-Path $Env_ "Scripts\python.exe"
$VPyw = Join-Path $Env_ "Scripts\pythonw.exe"
& $VPy -m pip install --upgrade pip --quiet --disable-pip-version-check

if ($Source) {
    $Pkg = (Resolve-Path $Source).Path
} else {
    $zip = Join-Path $env:TEMP "reelvault-members-src.zip"
    $src = Join-Path $RVM "source"
    Invoke-WebRequest "https://github.com/akshaykiriti1443-alt/linkedin/archive/refs/heads/$Branch.zip" -OutFile $zip
    if (Test-Path $src) { Remove-Item $src -Recurse -Force }
    Expand-Archive $zip -DestinationPath $src -Force
    Remove-Item $zip -ErrorAction SilentlyContinue
    $Pkg = Join-Path (Get-ChildItem $src -Directory | Select-Object -First 1).FullName "reelvault"
}
& $VPy -m pip install --upgrade "$Pkg[server]" --quiet --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { throw "Package install failed. Run this installer again." }
& $VPy -m pip install --force-reinstall --no-deps $Pkg --quiet --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { throw "Package install failed. Run this installer again." }
Ok "ReelVault Members installed"

Step "Downloading models and building the Chrome extension"
$setupArgs = @("-m", "reelvault.members_local", "--setup", "--source", $Pkg)
if ($NoWhisper) { $setupArgs += "--no-whisper" }
& $VPy @setupArgs
if ($LASTEXITCODE -ne 0) { throw "Setup did not finish. Run this installer again." }

# ---------------------------------------------------------------- Shortcut
Step "Adding the ReelVault Members icon"
$shell = New-Object -ComObject WScript.Shell
foreach ($t in @((Join-Path ([Environment]::GetFolderPath("Desktop")) "ReelVault Members.lnk"),
                 (Join-Path ([Environment]::GetFolderPath("Programs")) "ReelVault Members.lnk"))) {
    $lnk = $shell.CreateShortcut($t)
    $lnk.TargetPath = $VPyw
    $lnk.Arguments = "-m reelvault.members_local"
    $lnk.WorkingDirectory = $RVM
    $lnk.Description = "ReelVault Members on this computer (http://localhost:8800)"
    $lnk.Save()
}
Ok "Icon added to your desktop and Start menu"

Write-Host ""
Write-Host "All done. ReelVault Members opens at http://localhost:8800" -ForegroundColor Green
Write-Host "Your login is saved in: $RVM\login.txt"
Get-Content (Join-Path $RVM "login.txt") | ForEach-Object { Write-Host "    $_" }
Write-Host "Chrome extension folder (Load unpacked): $RVM\chrome-extension"
if (-not $NoLaunch) { Start-Process $VPyw -ArgumentList "-m reelvault.members_local" -WorkingDirectory $RVM }
