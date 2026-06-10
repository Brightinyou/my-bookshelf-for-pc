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
    echo 아무 키나 누르면 창이 닫힙니다.
    pause >nul
    exit /b 1
)
for /f "delims=" %%v in ('%PYCMD% --version') do echo [확인] 파이썬: %%v

REM ── 2. 가상환경 + 패키지 설치 ─────────────────────────────
if not exist .venv (
    echo [진행] 가상환경^(.venv^) 생성 중...
    %PYCMD% -m venv .venv
    if errorlevel 1 ( echo [실패] 가상환경 생성 실패 — 아무 키나 누르면 닫힙니다. & pause >nul & exit /b 1 )
)
echo [진행] 패키지 설치 중 — Docling이 커서 10~20분 걸릴 수 있습니다. 창을 닫지 마세요.
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt
if errorlevel 1 (
    echo [실패] 패키지 설치 실패 — 인터넷 연결을 확인하고 다시 실행해 주세요.
    echo 아무 키나 누르면 창이 닫힙니다.
    pause >nul
    exit /b 1
)

REM ── Streamlit 첫 실행 영문 환영문(이메일 입력) 건너뛰기 ──
if not exist "%USERPROFILE%\.streamlit" mkdir "%USERPROFILE%\.streamlit"
if not exist "%USERPROFILE%\.streamlit\credentials.toml" (
    echo [general]> "%USERPROFILE%\.streamlit\credentials.toml"
    echo email = "">> "%USERPROFILE%\.streamlit\credentials.toml"
)

REM ── 3. 옵시디언(위키 노트 열람용) 확인 ─────────────────────
echo.
if exist "%LOCALAPPDATA%\Programs\Obsidian\Obsidian.exe" goto :obs_have
if exist "%LOCALAPPDATA%\Obsidian\Obsidian.exe" goto :obs_have
choice /c YN /m "[질문] 위키 노트 열람용 옵시디언이 없습니다. 지금 설치할까요"
if errorlevel 2 (
    echo [안내] 나중에 install-obsidian.bat 를 실행하면 설치할 수 있습니다.
    goto :done
)
where winget >nul 2>nul
if errorlevel 1 (
    echo [안내] 옵시디언 다운로드 페이지를 엽니다 — 설치 파일을 받아 실행하세요.
    start https://obsidian.md/download
    goto :done
)
echo [진행] winget으로 옵시디언 설치 중...
winget install -e --id Obsidian.Obsidian --accept-source-agreements --accept-package-agreements
if errorlevel 1 (
    echo [안내] 자동 설치 실패 — 다운로드 페이지를 엽니다.
    start https://obsidian.md/download
)
goto :done

:obs_have
echo [확인] 옵시디언이 이미 설치되어 있습니다.

:done
echo.
echo [완료] 설치 끝! 이제 start.bat 를 더블클릭하면 앱이 열립니다.
echo 아무 키나 누르면 창이 닫힙니다.
pause >nul
