@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Goal 3 - worker

if not exist ".venv\Scripts\python.exe" (
    echo Virtual environment not found. Running setup...
    call "%~dp0setup.bat"
    if errorlevel 1 exit /b 1
)

where ffmpeg >nul 2>nul
if errorlevel 1 (
    if defined GOAL3_FFMPEG (
        if exist "%GOAL3_FFMPEG%" goto ffmpeg_ok
    )
    echo WARNING: ffmpeg was not found.
    echo Set GOAL3_FFMPEG and GOAL3_FFPROBE, or add ffmpeg to PATH.
    echo.
    pause
)
:ffmpeg_ok

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONDONTWRITEBYTECODE=1"

echo ============================================================
echo   Goal 3 worker
echo   It will wait for new videos in the queue.
echo   Keep this window open. Press Ctrl+C to stop.
echo ============================================================
echo.

".venv\Scripts\python.exe" worker.py --loop
set "WORKER_EXIT=%ERRORLEVEL%"

echo.
echo Worker stopped. Exit code: %WORKER_EXIT%
pause
exit /b %WORKER_EXIT%
