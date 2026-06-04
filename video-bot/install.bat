@echo off
setlocal enabledelayedexpansion
title Video Bot — Installer
color 0A

echo.
echo  ============================================================
echo   VIDEO BOT INSTALLER
echo   This will set up everything you need. Takes ~5 minutes.
echo  ============================================================
echo.

:: ── Check Git ────────────────────────────────────────────────────────────
echo [1/6] Checking Git...
where git >nul 2>&1
if %errorlevel% neq 0 (
    echo  ERROR: Git is not installed.
    echo  Please install it from: https://git-scm.com/download/win
    echo  Then re-run this installer.
    pause & exit /b 1
)
echo  OK: Git found.

:: ── Check Node ───────────────────────────────────────────────────────────
echo [2/6] Checking Node.js...
node -v >nul 2>&1
if %errorlevel% neq 0 (
    echo  ERROR: Node.js is not installed.
    echo  Please install it from: https://nodejs.org  (download LTS)
    echo  Then close and reopen this terminal and re-run install.bat
    pause & exit /b 1
)
for /f "tokens=*" %%v in ('node -v') do set NODE_VER=%%v
echo  OK: Node.js %NODE_VER% found.

:: ── Check Python ─────────────────────────────────────────────────────────
echo [3/6] Checking Python...
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo  ERROR: Python is not installed.
    echo  Please install it from: https://python.org/downloads
    echo  IMPORTANT: Check "Add Python to PATH" during install.
    echo  Then re-run this installer.
    pause & exit /b 1
)
for /f "tokens=*" %%v in ('python --version') do set PY_VER=%%v
echo  OK: %PY_VER% found.

:: ── Check FFmpeg ─────────────────────────────────────────────────────────
echo [4/6] Checking FFmpeg...
where ffmpeg >nul 2>&1
if %errorlevel% neq 0 (
    echo  WARNING: FFmpeg not found on PATH.
    echo.
    echo  To install:
    echo    1. Go to https://ffmpeg.org/download.html
    echo    2. Click Windows ^> ffmpeg-release-essentials.zip
    echo    3. Extract to C:\ffmpeg
    echo    4. Add C:\ffmpeg\bin to your system PATH
    echo    5. Re-run this installer
    echo.
    echo  You can continue without FFmpeg but some features may not work.
    set /p SKIP_FFMPEG="Continue anyway? (y/n): "
    if /i "!SKIP_FFMPEG!" neq "y" exit /b 1
) else (
    echo  OK: FFmpeg found.
)

:: ── Install faster-whisper + audio tools ─────────────────────────────────
echo [5/6] Installing transcription + audio tools...
pip show faster-whisper >nul 2>&1
if %errorlevel% neq 0 (
    echo  Installing faster-whisper...
    pip install faster-whisper
    if %errorlevel% neq 0 (
        echo  WARNING: faster-whisper install failed.
        echo  No problem - you can use Premiere Pro transcription instead.
        echo  In Claude Code type: /transcribe-premiere
    )
) else (
    echo  OK: faster-whisper already installed.
)
pip install pydub Pillow --quiet
echo  OK: pydub + Pillow installed.

:: ── npm install ───────────────────────────────────────────────────────────
echo [6/6] Installing project dependencies...
cd /d "%~dp0"
if not exist node_modules (
    call npm install
    if %errorlevel% neq 0 (
        echo  ERROR: npm install failed.
        pause & exit /b 1
    )
) else (
    echo  OK: node_modules already exists.
)

:: ── Create workspace folder ───────────────────────────────────────────────
if not exist workspace mkdir workspace
if not exist premiere_imports\graphics mkdir premiere_imports\graphics
if not exist premiere_imports\overlays  mkdir premiere_imports\overlays

:: ── Done ─────────────────────────────────────────────────────────────────
echo.
echo  ============================================================
echo   INSTALL COMPLETE!
echo  ============================================================
echo.
echo  Next step: Double-click run.bat and follow the prompts.
echo.
pause
