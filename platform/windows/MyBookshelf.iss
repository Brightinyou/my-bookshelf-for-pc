; MyBookshelf.iss — Inno Setup 6.x 설치 스크립트
; 컴파일: https://jrsoftware.org/isinfo.php (무료)
; 결과물: dist\windows\Setup.exe

#define MyAppName      "My Bookshelf"
#define MyAppVersion   "0.6.8"
#define MyAppExe       "start-app.vbs"

[Setup]
AppId={{3F8A9C12-B47D-4E21-A56F-82C310D4F1AB}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=My Bookshelf
DefaultDirName={localappdata}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\..\dist\windows
OutputBaseFilename=Setup
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
MinVersion=10.0
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
SetupIconFile=MyBookshelf.ico
; 공증 없는 동료 배포 — 서명 생략

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Tasks]
Name: "desktopicon";   Description: "바탕화면에 바로가기 만들기"; GroupDescription: "추가 옵션:"
Name: "uninstallicon"; Description: "바탕화면에 제거 바로가기 만들기"; GroupDescription: "추가 옵션:"

[Files]
; ── 앱 핵심 파일 → {app}\core\ (start-app.vbs 경로 기준) ──
Source: "..\..\core\pipeline_app.py";    DestDir: "{app}\core"; Flags: ignoreversion
Source: "..\..\core\desktop.py";         DestDir: "{app}\core"; Flags: ignoreversion
Source: "..\..\core\config.py";          DestDir: "{app}\core"; Flags: ignoreversion
Source: "..\..\core\llm_providers.py";   DestDir: "{app}\core"; Flags: ignoreversion
Source: "..\..\core\version.py";         DestDir: "{app}\core"; Flags: ignoreversion
Source: "..\..\core\gemini_wiki.py";     DestDir: "{app}\core"; Flags: ignoreversion
Source: "..\..\core\chapter_wiki.py";    DestDir: "{app}\core"; Flags: ignoreversion
Source: "..\..\core\requirements.txt";   DestDir: "{app}"; Flags: ignoreversion
; ── 실행·종료 스크립트 ────────────────────────────────────
Source: "MyBookshelf.exe";         DestDir: "{app}"; Flags: ignoreversion
Source: "MyBookshelf.ico";         DestDir: "{app}"; Flags: ignoreversion
Source: "..\mac\MyBookshelf.icns";        DestDir: "{app}"; Flags: ignoreversion
Source: "..\mac\MyBookshelf.iconset\*";   DestDir: "{app}\MyBookshelf.iconset"; Flags: ignoreversion
Source: "start-app.vbs";           DestDir: "{app}"; Flags: ignoreversion
Source: "start.bat";               DestDir: "{app}"; Flags: ignoreversion
Source: "stop-app.bat";            DestDir: "{app}"; Flags: ignoreversion

[Icons]
; 시작 메뉴 최상위 — Windows 11 "모든 앱"에서 M 항목으로 바로 보임
Name: "{userprograms}\{#MyAppName}";          Filename: "{app}\MyBookshelf.exe"; IconFilename: "{app}\MyBookshelf.ico"; WorkingDir: "{app}"
; 시작 메뉴 서브폴더 — 종료·제거 항목
Name: "{userprograms}\{#MyAppName} (폴더)\{#MyAppName} 시작";  Filename: "{app}\MyBookshelf.exe"; IconFilename: "{app}\MyBookshelf.ico"; WorkingDir: "{app}"
Name: "{userprograms}\{#MyAppName} (폴더)\{#MyAppName} 종료";  Filename: "{app}\stop-app.bat"; WorkingDir: "{app}"
Name: "{userprograms}\{#MyAppName} (폴더)\프로그램 제거";       Filename: "{uninstallexe}"
; 바탕화면
Name: "{userdesktop}\{#MyAppName}";           Filename: "{app}\MyBookshelf.exe"; IconFilename: "{app}\MyBookshelf.ico"; WorkingDir: "{app}"
Name: "{userdesktop}\{#MyAppName} 제거";      Filename: "{uninstallexe}"; Tasks: uninstallicon

[Run]
; 패키지 설치 (pip — 10~20분 소요, 진행 창 표시)
Filename: "cmd.exe"; \
    Parameters: "/c cd /d ""{app}"" && taskkill /f /im pythonw.exe >nul 2>nul & taskkill /f /im python.exe >nul 2>nul & if exist .venv (echo 이전 설치 제거 중... && rmdir /s /q .venv) && python -m venv .venv && .venv\Scripts\python.exe -m pip install --upgrade pip && .venv\Scripts\python.exe -m pip install -r requirements.txt && (echo. && echo 설치가 완료되었습니다. 5초 후 창이 닫힙니다. && powershell -NoProfile -Command ""Start-Sleep 5"") || (echo. && echo 오류가 발생했습니다. 위 내용을 확인하세요. && pause)"; \
    StatusMsg: "패키지를 설치합니다 (10~20분 소요)..."; \
    Flags: waituntilterminated

; 설치 완료 후 앱 즉시 실행 (선택)
Filename: "{sys}\wscript.exe"; \
    Parameters: """{app}\start-app.vbs"""; \
    WorkingDir: "{app}"; \
    Flags: nowait postinstall skipifsilent; \
    Description: "지금 My Bookshelf 시작"

[UninstallDelete]
; pip으로 설치된 패키지(.venv) 및 앱 생성 파일 완전 제거
Type: filesandordirs; Name: "{app}\.venv"
Type: filesandordirs; Name: "{app}\__pycache__"
Type: dirifempty;     Name: "{app}"

[UninstallRun]
; 제거 전 실행 중인 앱 종료
Filename: "powershell.exe"; \
    Parameters: "-NoProfile -Command ""Get-Process python -ErrorAction SilentlyContinue | Where-Object {{ $_.MainWindowTitle -eq '' }} | Stop-Process -Force"""; \
    Flags: runhidden; RunOnceId: "KillPython"
