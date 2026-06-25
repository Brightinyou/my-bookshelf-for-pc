@echo off
chcp 65001 >nul
cd /d "%~dp0"
REM My Bookshelf 실행 스크립트(윈도우) — 더블클릭하면 앱 창이 열립니다.
REM (먼저 setup.bat 로 설치를 한 번 마쳐야 합니다.)

if not exist .venv\Scripts\python.exe (
    echo [실패] 설치가 안 되어 있습니다. setup.bat 를 먼저 실행해 주세요.
    echo 아무 키나 누르면 창이 닫힙니다.
    pause >nul
    exit /b 1
)

REM 네이티브 창(PyWebView) 지원 여부 확인
.venv\Scripts\python -c "import webview" 2>nul
if %errorlevel%==0 (
    echo [My Bookshelf] 시작합니다 — 잠시 후 앱 창이 열립니다.
    echo 앱 창을 닫으면 자동으로 종료됩니다.
    .venv\Scripts\python core\desktop.py
    exit /b
) else (
    echo [경고] 네이티브 창 모듈^(pywebview^)이 없어 브라우저로 엽니다.
    echo setup.bat 를 다시 실행하면 네이티브 창을 쓸 수 있습니다.
    .venv\Scripts\python -m streamlit run core\pipeline_app.py --server.port 8501 --browser.gatherUsageStats false
    echo 아무 키나 누르면 창이 닫힙니다.
    pause >nul
    exit /b
)
