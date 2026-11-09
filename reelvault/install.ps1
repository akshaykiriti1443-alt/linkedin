# ReelVault installer for Windows.
#
# Paste this ONE line into PowerShell (Start menu > type "PowerShell" > open it):
#   irm https://raw.githubusercontent.com/akshaykiriti1443-alt/linkedin/claude/kind-dijkstra-0o03in/reelvault/install.ps1 | iex
#
# It installs (for your user only, no admin needed):
#   - Python 3.12 (if missing)          - ffmpeg (video tools)
#   - Claude Code (if missing)          - ReelVault itself
# Then it puts a "ReelVault" icon on your desktop and opens the app.

param(
    [string]$Branch = "claude/kind-dijkstra-0o03in",
    [string]$Source = "",        # a local reelvault folder instead of downloading (used by the test run)
    [switch]$NoLaunch,
    [switch]$SkipClaude
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # makes downloads much faster
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$RV    = Join-Path $env:USERPROFILE "ReelVault"
$Tools = Join-Path $RV "tools"
$Env_  = Join-Path $RV "app-env"
New-Item -ItemType Directory -Force -Path $RV, $Tools | Out-Null

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
        # Skip the Microsoft Store placeholder that only opens the Store.
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
$FfBin = Join-Path $Tools "ffmpeg\bin"
if (-not (Test-Path (Join-Path $FfBin "ffmpeg.exe"))) {
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
}
Ok "ffmpeg ready"

# ---------------------------------------------------------------- Claude Code
Step "Checking Claude Code"
$ClaudeExe = Join-Path $env:USERPROFILE ".local\bin\claude.exe"
$HaveClaude = (Test-Path $ClaudeExe) -or (Get-Command claude -ErrorAction SilentlyContinue)
if (-not $HaveClaude -and -not $SkipClaude) {
    Write-Host "    Installing Claude Code..."
    try {
        Invoke-RestMethod https://claude.ai/install.ps1 | Invoke-Expression
    } catch {
        Write-Host "    Claude Code did not install: $_" -ForegroundColor Yellow
        Write-Host "    ReelVault still opens; it will show how to fix this." -ForegroundColor Yellow
    }
}
$ClaudeCmd = if (Test-Path $ClaudeExe) { $ClaudeExe } else { (Get-Command claude -ErrorAction SilentlyContinue).Source }
if ($ClaudeCmd -and -not $SkipClaude) {
    # Newer Claude models need a recent Claude Code; update it every time.
    Write-Host "    Updating Claude Code..."
    try { & $ClaudeCmd update 2>&1 | Select-Object -Last 1 | ForEach-Object { Write-Host "    $_" } } catch {}
}
if ($ClaudeCmd) { Ok ("Claude Code ready: " + ((& $ClaudeCmd --version) 2>$null)) }

# ---------------------------------------------------------------- ReelVault
Step "Installing ReelVault"
# Close a ReelVault that is still running, so the new version really starts.
try { Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8765/api/quit -ContentType "application/json" -Body "{}" -TimeoutSec 5 | Out-Null } catch {}
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*reelvault.app*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 1
if (-not (Test-Path (Join-Path $Env_ "Scripts\python.exe"))) {
    & $Py -m venv $Env_
}
$VPy  = Join-Path $Env_ "Scripts\python.exe"
$VPyw = Join-Path $Env_ "Scripts\pythonw.exe"
& $VPy -m pip install --upgrade pip --quiet --disable-pip-version-check

if ($Source) {
    $Pkg = (Resolve-Path $Source).Path
} else {
    $zip = Join-Path $env:TEMP "reelvault-src.zip"
    $src = Join-Path $RV "source"
    $url = "https://github.com/akshaykiriti1443-alt/linkedin/archive/refs/heads/$Branch.zip"
    Invoke-WebRequest $url -OutFile $zip
    if (Test-Path $src) { Remove-Item $src -Recurse -Force }
    Expand-Archive $zip -DestinationPath $src -Force
    Remove-Item $zip -ErrorAction SilentlyContinue
    $Pkg = Join-Path (Get-ChildItem $src -Directory | Select-Object -First 1).FullName "reelvault"
}
Write-Host "    Installing packages (a few minutes the first time)..."
& $VPy -m pip install --upgrade $Pkg --quiet --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { throw "Package install failed. Run this installer again." }
# Always replace ReelVault's own files, even when the version number looks the same.
& $VPy -m pip install --force-reinstall --no-deps $Pkg --quiet --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { throw "Package install failed. Run this installer again." }
$Ver = & $VPy -c "import reelvault; print(reelvault.__version__)"
# Set up the ReelVault folder now (settings + the Chrome extension folder),
# so it is ready even before the app is opened.
& $VPy -c "import reelvault.app as a; a.prepare_home()"
if (-not (Test-Path (Join-Path $RV "chrome-extension\manifest.json"))) { throw "Could not create the Chrome extension folder." }
Ok "ReelVault $Ver installed"

# ---------------------------------------------------------------- Shortcuts
Step "Adding the ReelVault icon"
$shell = New-Object -ComObject WScript.Shell
$targets = @(
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "ReelVault.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Programs")) "ReelVault.lnk")
)
foreach ($t in $targets) {
    $lnk = $shell.CreateShortcut($t)
    $lnk.TargetPath = $VPyw
    $lnk.Arguments = "-m reelvault.app"
    $lnk.WorkingDirectory = $RV
    $lnk.Description = "ReelVault: your saved reels, broken down"
    $lnk.Save()
}
Ok "Icon added to your desktop and Start menu"

Write-Host ""
Write-Host "All done. ReelVault $Ver is opening in your browser." -ForegroundColor Green
Write-Host "Next time, just double-click the ReelVault icon on your desktop."
if (-not $NoLaunch) { Start-Process $VPyw -ArgumentList "-m reelvault.app" -WorkingDirectory $RV }
