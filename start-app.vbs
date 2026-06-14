' My Bookshelf launcher (no console window). Stop with stop-app.bat.
' NOTE: keep this file ASCII-only — Korean text breaks in ANSI-parsed VBS.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("Wscript.Shell")
dir_ = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir_

If Not fso.FileExists(dir_ & "\.venv\Scripts\python.exe") Then
    MsgBox "Setup is not done yet. Please run install again.", vbExclamation, "My Bookshelf"
    WScript.Quit
End If

' Already running? Just open browser.
Dim http : Set http = CreateObject("MSXML2.XMLHTTP")
On Error Resume Next
http.Open "GET", "http://localhost:8501", False
http.Send
On Error GoTo 0
If http.Status = 200 Then
    sh.Run "cmd /c start http://localhost:8501", 0, False
    WScript.Quit
End If

' Start Streamlit (hidden window)
sh.Run "cmd /c "".venv\Scripts\python.exe"" -m streamlit run core\pipeline_app.py --server.port 8501 --browser.gatherUsageStats false", 0, False

' Wait for Streamlit to be ready, then open browser
Dim i
For i = 1 To 20
    WScript.Sleep 1000
    On Error Resume Next
    http.Open "GET", "http://localhost:8501", False
    http.Send
    On Error GoTo 0
    If http.Status = 200 Then
        sh.Run "cmd /c start http://localhost:8501", 0, False
        WScript.Quit
    End If
Next

' Fallback: open browser anyway after 20s
sh.Run "cmd /c start http://localhost:8501", 0, False
