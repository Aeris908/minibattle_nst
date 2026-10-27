@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Goal 3 - launcher

if not exist ".venv\Scripts\python.exe" (
    echo Virtual environment not found. Running setup first...
    call "%~dp0setup.bat"
    if errorlevel 1 (
        pause
        exit /b 1
    )
)

echo Opening dashboard and worker in two windows...
start "Goal 3 dashboard" cmd /k call "%~dp0start_dashboard.bat"
timeout /t 2 /nobreak >nul
start "Goal 3 worker" cmd /k call "%~dp0start_worker.bat"
echo.
echo Done. Use the dashboard to add a video URL.
timeout /t 3 /nobreak >nul
exit /b 0
