@echo off
chcp 65001 >nul
cd /d "%~dp0"
REM My Bookshelf 실행 스크립트(윈도우) — 더블클릭하면 브라우저에 앱이 열립니다.
REM (먼저 setup.bat 로 설치를 한 번 마쳐야 합니다.)

if not exist .venv\Scripts\python.exe (
    echo [실패] 설치가 안 되어 있습니다. setup.bat 를 먼저 실행해 주세요.
    pause
    exit /b 1
)

echo [My Bookshelf] 시작합니다 — 잠시 후 브라우저가 열립니다.
echo 끝낼 때는 이 창에서 Ctrl+C 를 누르거나 창을 닫으세요.
.venv\Scripts\python -m streamlit run pipeline_app.py --server.port 8501 --browser.gatherUsageStats false
pause
