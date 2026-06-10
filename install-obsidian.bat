@echo off
chcp 65001 >nul
REM 옵시디언(위키 노트 열람용) 설치 도우미 — winget 자동 설치, 없으면 다운로드 페이지.
if exist "%LOCALAPPDATA%\Programs\Obsidian\Obsidian.exe" goto :obs_have
if exist "%LOCALAPPDATA%\Obsidian\Obsidian.exe" goto :obs_have
where winget >nul 2>nul
if errorlevel 1 (
    echo [안내] 옵시디언 다운로드 페이지를 엽니다. Windows용 설치 파일을 받아 실행하세요.
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
echo [안내] 옵시디언에서 "Open folder as vault"로
echo        문서\My Bookshelf\wiki 폴더를 열면 생성된 노트가 보입니다.
pause
