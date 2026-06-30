; MyBookshelf-Uninstall.iss — 별도 배포용 제거 도우미
; 컴파일: ISCC.exe MyBookshelf-Uninstall.iss
; 결과물: dist\windows\Uninstall.exe

[Setup]
AppName=My Bookshelf Uninstaller
AppVersion=1.0
CreateAppDir=no
Uninstallable=no
OutputDir=..\..\dist\windows
OutputBaseFilename=Uninstall
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
  UninstPath: String;
  ResultCode: Integer;
begin
  UninstPath := ExpandConstant('{localappdata}\My Bookshelf\unins000.exe');
  if FileExists(UninstPath) then begin
    if MsgBox('My Bookshelf를 완전히 제거하시겠습니까?' + #13#10 +
              '(.venv 패키지 폴더도 함께 삭제됩니다.)',
              mbConfirmation, MB_YESNO) = IDYES then
      Exec(UninstPath, '/NORESTART', '', SW_SHOW, ewWaitUntilTerminated, ResultCode);
  end else
    MsgBox('My Bookshelf가 이 컴퓨터에 설치되어 있지 않습니다.', mbInformation, MB_OK);
  Result := False;
end;
