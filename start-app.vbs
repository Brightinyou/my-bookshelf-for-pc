' My Bookshelf launcher (no console window). Stop with stop-app.bat.
' NOTE: keep this file ASCII-only — Korean text breaks in ANSI-parsed VBS.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("Wscript.Shell")
dir_ = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir_

If Not fso.FileExists(dir_ & "\.venv\Scripts\python.exe") Then
    MsgBox "Setup is not done yet. Please run setup.bat first.", vbExclamation, "My Bookshelf"
    WScript.Quit
End If

sh.Run "cmd /c "".venv\Scripts\python.exe"" -m streamlit run pipeline_app.py --server.port 8501 --browser.gatherUsageStats false", 0, False
