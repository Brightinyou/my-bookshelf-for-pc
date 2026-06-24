' My Bookshelf launcher (native window, no console). Stop by closing the app window.
' NOTE: keep this file ASCII-only — Korean text breaks in ANSI-parsed VBS.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("Wscript.Shell")
dir_ = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir_

If Not fso.FileExists(dir_ & "\.venv\Scripts\pythonw.exe") Then
    MsgBox "Setup is not done yet. Please run install again.", vbExclamation, "My Bookshelf"
    WScript.Quit
End If

' Launch the native window app (desktop.py). pythonw = no console window.
' desktop.py starts the Streamlit server hidden, waits for it, then shows the window.
sh.Run """.venv\Scripts\pythonw.exe"" core\desktop.py", 0, False
