@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Goal 3 - setup

echo ============================================================
echo   Goal 3 Minibattle Analytics - first-time setup
echo ============================================================
echo.

set "PYTHON_CMD="
python --version >nul 2>nul
if not errorlevel 1 set "PYTHON_CMD=python"
if not defined PYTHON_CMD (
    py -3 --version >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=py -3"
)
if not defined PYTHON_CMD (
    echo ERROR: Python 3.10+ was not found.
    echo Install it from https://www.python.org/downloads/
    echo IMPORTANT: enable "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

%PYTHON_CMD% -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
if errorlevel 1 (
    echo ERROR: Python 3.10 or newer is required.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment .venv ...
    %PYTHON_CMD% -m venv .venv
    if errorlevel 1 (
        echo ERROR: could not create the virtual environment.
        pause
        exit /b 1
    )
) else (
    echo Virtual environment already exists.
)

echo.
echo Installing Python dependencies. This may take 10-30 minutes...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo ERROR: dependency installation failed.
    echo Check the internet connection and run setup.bat again.
    pause
    exit /b 1
)

echo.
where ffmpeg >nul 2>nul
if errorlevel 1 (
    if defined GOAL3_FFMPEG (
        if exist "%GOAL3_FFMPEG%" goto ffmpeg_ok
    )
    echo WARNING: ffmpeg was not found in PATH.
    echo Install it or set these variables, for example:
    echo   set GOAL3_FFMPEG=C:\ffmpeg\bin\ffmpeg.exe
    echo   set GOAL3_FFPROBE=C:\ffmpeg\bin\ffprobe.exe
    echo   set GOAL3_FFMPEG_LOCATION=C:\ffmpeg\bin
    echo.
) else (
    echo ffmpeg found.
)
:ffmpeg_ok

echo.
echo ============================================================
echo   Setup finished.
echo.
echo   Next step: calibrate the stream layout once:
echo   calibrate.bat "C:\path\to\video.mp4" 45
echo.
echo   Then launch everything:
echo   start_all.bat
echo ============================================================
echo.
pause
exit /b 0
