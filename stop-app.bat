@echo off
chcp 65001 >nul
REM My Bookshelf 종료 스크립트 — start-app.vbs(창 없음)로 실행한 앱을 끕니다.
echo [My Bookshelf] 앱을 종료합니다...
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*streamlit*' -and $_.CommandLine -like '*pipeline_app.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo [완료] 종료되었습니다. 이 창은 잠시 후 닫힙니다.
timeout /t 3 >nul
