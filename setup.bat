@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Goal 3 - setup

echo ============================================================
echo   Goal 3 Minibattle Analytics - first-time setup
echo ============================================================
echo.

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if (3,10) <= sys.version_info[:2] <= (3,14) else 1)" >nul 2>nul
    if errorlevel 1 (
        echo Existing .venv uses an unsupported Python:
        ".venv\Scripts\python.exe" --version
        echo Removing the incompatible .venv...
        rmdir /s /q ".venv"
        if exist ".venv" (
            echo ERROR: could not remove .venv. Close programs using it and retry.
            pause
            exit /b 1
        )
    )
)

set "PYTHON_EXE="
call :try_py 3.12
if not defined PYTHON_EXE call :try_py 3.11
if not defined PYTHON_EXE call :try_py 3.10
if not defined PYTHON_EXE call :try_py 3.13
if not defined PYTHON_EXE call :try_py 3.14
if not defined PYTHON_EXE call :try_python
if not defined PYTHON_EXE call :try_common
if not defined PYTHON_EXE (
    echo ERROR: no supported Python installation was found.
    echo Supported versions: Python 3.10 - 3.14. Recommended: Python 3.12/3.13.
    echo Python 3.15 is too new for this stack:
    echo PyTorch, CTranslate2 and tokenizers do not have compatible wheels yet.
    echo.
    echo Install Python 3.12 from https://www.python.org/downloads/
    echo or run: winget install --id Python.Python.3.12 -e
    echo Enable "Add Python to PATH" and the Python launcher during installation.
    echo.
    pause
    exit /b 1
)

echo Using Python:
"%PYTHON_EXE%" --version

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment .venv ...
    "%PYTHON_EXE%" -m venv .venv
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

:try_py
py -%1 --version >nul 2>nul
if errorlevel 1 exit /b 1
for /f "delims=" %%P in ('py -%1 -c "import sys; print(sys.executable)"') do set "PYTHON_EXE=%%P"
exit /b 0

:try_python
python -c "import sys; raise SystemExit(0 if (3,10) <= sys.version_info[:2] <= (3,14) else 1)" >nul 2>nul
if errorlevel 1 exit /b 1
for /f "delims=" %%P in ('python -c "import sys; print(sys.executable)"') do set "PYTHON_EXE=%%P"
exit /b 0

:try_common
for %%D in (314 313 312 311 310) do (
    if not defined PYTHON_EXE call :try_exe "%LOCALAPPDATA%\Programs\Python\Python%%D\python.exe"
)
exit /b 0

:try_exe
if not exist %1 exit /b 1
%1 -c "import sys; raise SystemExit(0 if (3,10) <= sys.version_info[:2] <= (3,14) else 1)" >nul 2>nul
if errorlevel 1 exit /b 1
set "PYTHON_EXE=%~1"
exit /b 0
