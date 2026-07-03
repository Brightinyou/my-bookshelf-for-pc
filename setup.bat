@echo off
setlocal EnableExtensions
chcp 65001 >nul

if exist "%~dp0..\..\core\requirements.txt" (
    cd /d "%~dp0..\.."
) else (
    cd /d "%~dp0"
)

set "INSTALLER_MODE=0"
set "SKIP_OBSIDIAN=0"
set "NO_PAUSE=0"
set "REQ_FILE="
set "PYCMD="

:parse_args
if "%~1"=="" goto args_done
if /i "%~1"=="--installer" (
    set "INSTALLER_MODE=1"
    set "SKIP_OBSIDIAN=1"
    set "NO_PAUSE=1"
) else if /i "%~1"=="--skip-obsidian" (
    set "SKIP_OBSIDIAN=1"
) else if /i "%~1"=="--no-pause" (
    set "NO_PAUSE=1"
)
shift
goto parse_args

:args_done
if exist "core\requirements.txt" (
    set "REQ_FILE=core\requirements.txt"
) else if exist "requirements.txt" (
    set "REQ_FILE=requirements.txt"
) else (
    echo [ERROR] requirements.txt was not found.
    goto :fail
)

echo [My Bookshelf] Installing runtime...
echo.

py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
if not errorlevel 1 set "PYCMD=py -3"
if not defined PYCMD (
    python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
    if not errorlevel 1 set "PYCMD=python"
)
if not defined PYCMD (
    echo [ERROR] Python 3.10 or newer is required.
    echo         Install Python from https://www.python.org/downloads/
    if "%INSTALLER_MODE%"=="0" start https://www.python.org/downloads/
    goto :fail
)
for /f "delims=" %%v in ('%PYCMD% --version') do echo [OK] %%v

if exist ".venv" if not exist ".venv\Scripts\python.exe" (
    echo [WARN] Broken virtual environment found. Recreating it...
    rmdir /s /q ".venv"
)

if not exist ".venv\Scripts\python.exe" (
    echo [STEP] Creating virtual environment...
    %PYCMD% -m venv ".venv"
    if errorlevel 1 (
        echo [ERROR] Failed to create .venv
        goto :fail
    )
)

echo [STEP] Upgrading pip...
call ".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 (
    echo [ERROR] Failed to upgrade pip.
    goto :fail
)

echo [STEP] Installing packages from %REQ_FILE% ...
call ".venv\Scripts\python.exe" -m pip install -r "%REQ_FILE%"
if errorlevel 1 (
    echo [ERROR] Failed to install required packages.
    echo         Check your internet connection and Python installation.
    goto :fail
)

if not exist "%USERPROFILE%\.streamlit" mkdir "%USERPROFILE%\.streamlit" >nul 2>nul
if not exist "%USERPROFILE%\.streamlit\credentials.toml" (
    > "%USERPROFILE%\.streamlit\credentials.toml" echo [general]
    >> "%USERPROFILE%\.streamlit\credentials.toml" echo email = ""
)

if "%SKIP_OBSIDIAN%"=="1" goto :success

echo.
if exist "%LOCALAPPDATA%\Programs\Obsidian\Obsidian.exe" goto :obsidian_ready
if exist "%LOCALAPPDATA%\Obsidian\Obsidian.exe" goto :obsidian_ready
choice /c YN /m "[Question] Obsidian is not installed. Install it now?"
if errorlevel 2 (
    echo [INFO] You can install it later with install-obsidian.bat
    goto :success
)
where winget >nul 2>nul
if errorlevel 1 (
    echo [INFO] Opening the Obsidian download page...
    start https://obsidian.md/download
    goto :success
)
echo [STEP] Installing Obsidian with winget...
winget install -e --id Obsidian.Obsidian --accept-source-agreements --accept-package-agreements
if errorlevel 1 (
    echo [INFO] Automatic Obsidian install failed. Opening the download page...
    start https://obsidian.md/download
)
goto :success

:obsidian_ready
echo [OK] Obsidian is already installed.

:success
echo.
echo [DONE] Installation finished.
echo        Start the app with MyBookshelf.exe or start-app.vbs
if "%NO_PAUSE%"=="0" pause
exit /b 0

:fail
echo.
echo [FAILED] Installation did not complete.
if "%NO_PAUSE%"=="0" pause
exit /b 1
