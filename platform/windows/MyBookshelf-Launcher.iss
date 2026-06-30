; MyBookshelf-Launcher.iss — 앱 실행용 독립 EXE 빌더
; 컴파일: ISCC.exe MyBookshelf-Launcher.iss
; 결과물: dist\windows\MyBookshelf.exe  (더블클릭 → 앱 실행, 콘솔 창 없음)

[Setup]
AppName=My Bookshelf Launcher
AppVersion=1.0
CreateAppDir=no
Uninstallable=no
OutputDir=..\..\dist\windows
OutputBaseFilename=MyBookshelf
WizardStyle=modern
DisableWelcomePage=yes
DisableReadyPage=yes
DisableFinishedPage=yes
MinVersion=10.0
PrivilegesRequired=lowest
SetupIconFile=MyBookshelf.ico

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Code]
function InitializeSetup(): Boolean;
var
  InstallDir, PyPath, ScriptPath: String;
  ResultCode: Integer;
begin
  InstallDir := ExpandConstant('{localappdata}\My Bookshelf');
  PyPath     := InstallDir + '\.venv\Scripts\pythonw.exe';
  ScriptPath := '"' + InstallDir + '\core\desktop.py"';

  if FileExists(PyPath) then
    Exec(PyPath, ScriptPath, InstallDir, SW_HIDE, ewNoWait, ResultCode)
  else
    MsgBox(
      'My Bookshelf가 설치되어 있지 않습니다.' + #13#10 +
      'MyBookshelf-Setup.exe를 먼저 실행해 주세요.',
      mbError, MB_OK);

  Result := False;
end;
