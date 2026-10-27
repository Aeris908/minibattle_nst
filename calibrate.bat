@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Goal 3 - calibration

if not exist ".venv\Scripts\python.exe" (
    echo Virtual environment not found. Running setup...
    call "%~dp0setup.bat"
    if errorlevel 1 exit /b 1
)

set "VIDEO=%~1"
set "TIME=%~2"

if "%VIDEO%"=="" (
    echo Drag and drop a video file here, or paste its full path,
    echo then press Enter:
    set /p "VIDEO=Video path: "
)
if "%TIME%"=="" (
    set /p "TIME=Frame time in seconds (default 45): "
)
if "%TIME%"=="" set "TIME=45"

if "%VIDEO%"=="" (
    echo ERROR: video path is empty.
    pause
    exit /b 1
)

set "PYTHONUTF8=1"
echo.
echo Starting calibration at %TIME% seconds...
echo The tool will print which digit templates are still missing.
echo.
".venv\Scripts\python.exe" tools\calibrate_ui.py --video "%VIDEO%" --time %TIME%
echo.
echo Calibration finished. For missing digits run:
echo   .venv\Scripts\python.exe tools\calibrate_ui.py --capture-digit N --video "%VIDEO%" --time SECONDS
echo.
pause
exit /b 0
