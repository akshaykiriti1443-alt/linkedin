@echo off
setlocal enabledelayedexpansion
title Video Bot — Run
color 0B
cd /d "%~dp0"

echo.
echo  ============================================================
echo   VIDEO BOT — AI Video Editor
echo  ============================================================
echo.

:: ── Check install ────────────────────────────────────────────────────────
if not exist node_modules (
    echo  ERROR: Please run install.bat first!
    pause & exit /b 1
)

:: ── Session status ────────────────────────────────────────────────────────
if exist workspace\project.md (
    echo  Existing session found in workspace\project.md
    echo.
    python auto_edit.py --status 2>nul
    echo.
    echo    [1] Continue from where I left off
    echo    [2] Start fresh (re-run everything)
    echo.
    set /p SESSION_CHOICE="Enter number (1-2): "
    if "!SESSION_CHOICE!"=="2" set RESET_FLAG=--reset
)

:: ── Video type ────────────────────────────────────────────────────────────
echo.
echo  What type of video?
echo.
echo    [1] Talking head  (face + auto Vox 3D when talking, screen PiP when showing)
echo    [2] YouTube essay (16:9 full frame)
echo    [3] Shorts / Reels only
echo    [4] B-roll + floating face cam
echo.
set /p PROFILE_CHOICE="Enter number (1-4): "

if "%PROFILE_CHOICE%"=="1" set PROFILE=talking-head
if "%PROFILE_CHOICE%"=="2" set PROFILE=youtube
if "%PROFILE_CHOICE%"=="3" set PROFILE=shorts
if "%PROFILE_CHOICE%"=="4" set PROFILE=floating-cam
if not defined PROFILE set PROFILE=talking-head

:: ── Quality ───────────────────────────────────────────────────────────────
echo.
echo  Output quality?
echo.
echo    [1] Draft   — fast, 720p  (good for checking the edit)
echo    [2] Preview — 1080p       (good for review)
echo    [3] Final   — 1080p max   (for publishing)
echo.
set /p QUALITY_CHOICE="Enter number (1-3, default=1): "

if "%QUALITY_CHOICE%"=="2" set QUALITY=preview
if "%QUALITY_CHOICE%"=="3" set QUALITY=final
if not defined QUALITY set QUALITY=draft

:: ── Talking head video ────────────────────────────────────────────────────
echo.
echo  Drag your talking head .mp4 here and press Enter:
set /p RAW_VIDEO="Video: "
set RAW_VIDEO=!RAW_VIDEO:"=!

if not exist "!RAW_VIDEO!" (
    echo  ERROR: File not found.
    pause & exit /b 1
)
copy /Y "!RAW_VIDEO!" workspace\raw.mp4 >nul
echo  Copied to workspace\raw.mp4

:: ── Screen recording ─────────────────────────────────────────────────────
set SCREEN_ARGS=
if "%PROFILE%"=="talking-head" (
    echo.
    echo  Did you also record your screen? (y/n)
    set /p HAS_SCREEN="Answer: "
    if /i "!HAS_SCREEN!"=="y" (
        echo  Drag your screen recording here:
        set /p SCREEN_VIDEO="Screen: "
        set SCREEN_VIDEO=!SCREEN_VIDEO:"=!
        if exist "!SCREEN_VIDEO!" (
            copy /Y "!SCREEN_VIDEO!" workspace\screen.mp4 >nul
            echo  How many seconds into your talking head did the screen recording start? (0 if same time)
            set /p SCR_OFF="Offset (default 0): "
            if "!SCR_OFF!"=="" set SCR_OFF=0
            set SCREEN_ARGS=--screen workspace/screen.mp4 --screen-offset !SCR_OFF!
        )
    )
)

:: ── Run ──────────────────────────────────────────────────────────────────
echo.
echo  ============================================================
echo   RUNNING  (a timeline preview will open automatically)
echo  ============================================================
echo.

python auto_edit.py workspace/raw.mp4 --profile %PROFILE% --quality %QUALITY% %SCREEN_ARGS% %RESET_FLAG%

if %errorlevel% neq 0 (
    echo.
    echo  ERROR: Pipeline failed. See output above.
    pause & exit /b 1
)

:: ── Open outputs ─────────────────────────────────────────────────────────
echo.
echo  Opening workspace folder...
explorer workspace

echo.
echo  ============================================================
echo   DONE! Import workspace\final_timeline.xml into Premiere.
echo  ============================================================
echo.
pause
