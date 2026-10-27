@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Goal 3 - dashboard

if not exist ".venv\Scripts\python.exe" (
    echo Virtual environment not found. Running setup...
    call "%~dp0setup.bat"
    if errorlevel 1 exit /b 1
)

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONDONTWRITEBYTECODE=1"

echo ============================================================
echo   Goal 3 dashboard
echo   Browser URL: http://localhost:8501
echo   Keep this window open. Press Ctrl+C to stop.
echo ============================================================
echo.

".venv\Scripts\python.exe" -m streamlit run app.py --server.address localhost --server.port 8501

echo.
echo Dashboard stopped.
pause
exit /b 0
