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

:: ── Check install was done ────────────────────────────────────────────────
if not exist node_modules (
    echo  ERROR: Please run install.bat first!
    pause & exit /b 1
)

:: ── Choose profile ────────────────────────────────────────────────────────
echo  What type of video are you editing?
echo.
echo    [1] Talking head (+ auto Vox animations when not showing screen)
echo    [2] YouTube essay (16:9 full frame)
echo    [3] Shorts / Reels only (9:16, no screen recording)
echo    [4] B-roll + floating face cam
echo.
set /p PROFILE_CHOICE="Enter number (1-4): "

if "%PROFILE_CHOICE%"=="1" set PROFILE=talking-head
if "%PROFILE_CHOICE%"=="2" set PROFILE=youtube
if "%PROFILE_CHOICE%"=="3" set PROFILE=shorts
if "%PROFILE_CHOICE%"=="4" set PROFILE=floating-cam
if not defined PROFILE set PROFILE=talking-head

echo  Profile selected: %PROFILE%
echo.

:: ── Talking head video ────────────────────────────────────────────────────
echo  ── TALKING HEAD VIDEO ──────────────────────────────────────
echo  Drag your talking head .mp4 into this window and press Enter.
echo  (Or type the full path manually)
echo.
set /p RAW_VIDEO="Video path: "

:: Strip quotes if drag-dropped
set RAW_VIDEO=!RAW_VIDEO:"=!

if not exist "!RAW_VIDEO!" (
    echo  ERROR: File not found: !RAW_VIDEO!
    pause & exit /b 1
)

:: Copy to workspace
echo  Copying to workspace...
copy /Y "!RAW_VIDEO!" workspace\raw.mp4 >nul
echo  OK.

:: ── Screen recording (optional) ───────────────────────────────────────────
set SCREEN_ARGS=
if "%PROFILE%"=="talking-head" (
    echo.
    echo  ── SCREEN RECORDING (optional) ─────────────────────────────
    echo  Did you also record your screen? (y/n)
    set /p HAS_SCREEN="Answer: "

    if /i "!HAS_SCREEN!"=="y" (
        echo  Drag your screen recording .mp4 into this window and press Enter.
        set /p SCREEN_VIDEO="Screen recording path: "
        set SCREEN_VIDEO=!SCREEN_VIDEO:"=!

        if exist "!SCREEN_VIDEO!" (
            copy /Y "!SCREEN_VIDEO!" workspace\screen.mp4 >nul
            echo  OK: Screen recording copied.

            echo.
            echo  How many seconds into your talking head did you start the screen recording?
            echo  (If you started both at the same time, enter 0)
            set /p SCREEN_OFFSET="Seconds offset (default 0): "
            if "!SCREEN_OFFSET!"=="" set SCREEN_OFFSET=0

            set SCREEN_ARGS=--screen workspace/screen.mp4 --screen-offset !SCREEN_OFFSET!
        ) else (
            echo  WARNING: Screen recording not found, continuing without it.
        )
    )
)

:: ── Run the pipeline ──────────────────────────────────────────────────────
echo.
echo  ============================================================
echo   RUNNING PIPELINE — this may take several minutes
echo  ============================================================
echo.

python auto_edit.py workspace/raw.mp4 --profile %PROFILE% %SCREEN_ARGS%

if %errorlevel% neq 0 (
    echo.
    echo  ERROR: Pipeline failed. Check the output above for details.
    pause & exit /b 1
)

:: ── Done ─────────────────────────────────────────────────────────────────
echo.
echo  ============================================================
echo   DONE!
echo  ============================================================
echo.
echo  Your files are ready:
echo.
echo    workspace\final_timeline.xml   ^<-- import this into Premiere
echo    workspace\timeline_cut.xml     ^<-- V1 rough cut only (if needed)
echo.
echo  In Premiere Pro:
echo    File ^> Import ^> workspace\final_timeline.xml
echo    Export: File ^> Export ^> Media ^> H.264 ^> Instagram Reels
echo.

:: Open workspace folder for convenience
explorer workspace

pause
