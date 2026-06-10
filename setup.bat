@echo off
chcp 65001 >nul
cd /d "%~dp0"
REM My Bookshelf 설치 스크립트(윈도우) — 더블클릭으로 실행하세요.
REM 하는 일: 파이썬 확인 -> 전용 가상환경(.venv) 생성 -> 필요 패키지 설치.
REM 인터넷 연결 필요. Docling(PDF 변환 엔진)이 커서 처음 설치는 10~20분 걸릴 수 있습니다.

echo [My Bookshelf] 설치를 시작합니다.
echo.

REM ── 1. 파이썬 3.10+ 찾기 ──────────────────────────────────
set "PYCMD="
py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
if not errorlevel 1 set "PYCMD=py -3"
if not defined PYCMD (
    python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
    if not errorlevel 1 set "PYCMD=python"
)
if not defined PYCMD (
    echo [실패] 파이썬 3.10 이상이 필요합니다.
    echo        https://www.python.org/downloads/ 에서 최신 파이썬을 설치한 뒤
    echo        이 스크립트를 다시 실행해 주세요.
    echo        ※ 설치 첫 화면에서 "Add python.exe to PATH" 를 꼭 체크하세요.
    start https://www.python.org/downloads/
    pause
    exit /b 1
)
for /f "delims=" %%v in ('%PYCMD% --version') do echo [확인] 파이썬: %%v

REM ── 2. 가상환경 + 패키지 설치 ─────────────────────────────
if not exist .venv (
    echo [진행] 가상환경^(.venv^) 생성 중...
    %PYCMD% -m venv .venv
    if errorlevel 1 ( echo [실패] 가상환경 생성 실패 & pause & exit /b 1 )
)
echo [진행] 패키지 설치 중 — Docling이 커서 10~20분 걸릴 수 있습니다. 창을 닫지 마세요.
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt
if errorlevel 1 (
    echo [실패] 패키지 설치 실패 — 인터넷 연결을 확인하고 다시 실행해 주세요.
    pause
    exit /b 1
)

echo.
echo [완료] 설치 끝! 이제 start.bat 를 더블클릭하면 앱이 열립니다.
echo        (옵시디언이 없다면 install-obsidian.bat 도 실행하세요.)
pause
