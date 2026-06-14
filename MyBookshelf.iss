; MyBookshelf.iss — Inno Setup 6.x 설치 스크립트
; 컴파일: https://jrsoftware.org/isinfo.php (무료)
; 결과물: dist\MyBookshelf-Setup.exe

#define MyAppName      "My Bookshelf"
#define MyAppVersion   "0.4.4"
#define MyAppExe       "start-app.vbs"

[Setup]
AppId={{3F8A9C12-B47D-4E21-A56F-82C310D4F1AB}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=My Bookshelf
DefaultDirName={localappdata}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename=MyBookshelf-Setup
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
MinVersion=10.0
ArchitecturesInstallIn64BitMode=x64
PrivilegesRequired=lowest
; 공증 없는 동료 배포 — 서명 생략

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Tasks]
Name: "desktopicon"; Description: "바탕화면에 바로가기 만들기"; GroupDescription: "추가 옵션:"

[Files]
; ── 앱 핵심 파일 (core/) ──────────────────────────────────
Source: "core\pipeline_app.py";    DestDir: "{app}"; Flags: ignoreversion
Source: "core\config.py";          DestDir: "{app}"; Flags: ignoreversion
Source: "core\llm_providers.py";   DestDir: "{app}"; Flags: ignoreversion
Source: "core\gemini_wiki.py";     DestDir: "{app}"; Flags: ignoreversion
Source: "core\chapter_wiki.py";    DestDir: "{app}"; Flags: ignoreversion
Source: "core\ocr_windows.py";     DestDir: "{app}"; Flags: ignoreversion
Source: "core\requirements.txt";   DestDir: "{app}"; Flags: ignoreversion
; ── 실행·종료 스크립트 ────────────────────────────────────
Source: "start-app.vbs";           DestDir: "{app}"; Flags: ignoreversion
Source: "start.bat";               DestDir: "{app}"; Flags: ignoreversion
Source: "stop-app.bat";            DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName} 시작";    Filename: "{sys}\wscript.exe"; Parameters: """{app}\start-app.vbs"""; WorkingDir: "{app}"
Name: "{group}\{#MyAppName} 종료";    Filename: "{app}\stop-app.bat"; WorkingDir: "{app}"
Name: "{group}\프로그램 제거";         Filename: "{uninstallexe}"
Name: "{userdesktop}\{#MyAppName}"; Filename: "{sys}\wscript.exe"; Parameters: """{app}\start-app.vbs"""; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
; 패키지 설치 (pip — 10~20분 소요, 진행 창 표시)
Filename: "cmd.exe"; \
    Parameters: "/k cd /d ""{app}"" && python -m venv .venv && .venv\Scripts\python.exe -m pip install --upgrade pip && .venv\Scripts\python.exe -m pip install -r requirements.txt && echo. && echo 설치 완료! 이 창을 닫으세요. && pause"; \
    StatusMsg: "패키지를 설치합니다 (10~20분 소요) — 창을 닫지 마세요."; \
    Flags: waituntilterminated

; 설치 완료 후 앱 즉시 실행 (선택)
Filename: "{sys}\wscript.exe"; \
    Parameters: """{app}\start-app.vbs"""; \
    WorkingDir: "{app}"; \
    Flags: nowait postinstall skipifsilent; \
    Description: "지금 My Bookshelf 시작"

[UninstallRun]
; 제거 시 실행 중인 앱 종료
Filename: "powershell.exe"; \
    Parameters: "-NoProfile -Command ""Get-Process python -ErrorAction SilentlyContinue | Where-Object {{ $_.MainWindowTitle -eq '' }} | Stop-Process -Force"""; \
    Flags: runhidden
