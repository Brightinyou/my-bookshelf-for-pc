' My Bookshelf launcher (native window, no console). Stop by closing the app window.
' NOTE: keep this file ASCII-only — Korean text breaks in ANSI-parsed VBS.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("Wscript.Shell")
dir_ = fso.GetParentFolderName(WScript.ScriptFullName)
repoDir = fso.GetParentFolderName(fso.GetParentFolderName(dir_))
If fso.FileExists(repoDir & "\core\desktop.py") Then
    dir_ = repoDir
End If
sh.CurrentDirectory = dir_

If Not fso.FileExists(dir_ & "\.venv\Scripts\pythonw.exe") Then
    msg = "Setup did not complete." & vbCrLf & vbCrLf & _
          "Run setup.bat in this folder." & vbCrLf & _
          "If setup fails again, check install.log in the same folder."
    If Not fso.FileExists(dir_ & "\install.log") Then
        msg = "Setup did not complete." & vbCrLf & vbCrLf & _
              "Run setup.bat in this folder."
    End If
    MsgBox msg, vbExclamation, "My Bookshelf"
    WScript.Quit
End If

' Launch the native window app (desktop.py). pythonw = no console window.
' desktop.py starts the Streamlit server hidden, waits for it, then shows the window.
sh.Run """.venv\Scripts\pythonw.exe"" core\desktop.py", 0, False
