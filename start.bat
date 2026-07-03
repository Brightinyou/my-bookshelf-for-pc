@echo off
setlocal
cd /d "%~dp0"

if exist "%~dp0start-app.vbs" (
    start "" wscript.exe "%~dp0start-app.vbs"
    exit /b 0
)

if exist "%~dp0MyBookshelf.exe" (
    start "" "%~dp0MyBookshelf.exe"
    exit /b 0
)

exit /b 1
