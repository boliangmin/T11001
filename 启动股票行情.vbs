Option Explicit

Dim shell, fso, appDir, pythonwPath, scriptPath, command

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

appDir = fso.GetParentFolderName(WScript.ScriptFullName)
pythonwPath = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & _
    "\Programs\Python\Python313\pythonw.exe"
scriptPath = fso.BuildPath(appDir, "stock_price_ui.py")

If Not fso.FileExists(pythonwPath) Then
    MsgBox "Python GUI launcher was not found:" & vbCrLf & pythonwPath, vbCritical, "Startup failed"
    WScript.Quit 1
End If

If Not fso.FileExists(scriptPath) Then
    MsgBox "The UI script was not found:" & vbCrLf & scriptPath, vbCritical, "Startup failed"
    WScript.Quit 1
End If

shell.CurrentDirectory = appDir
command = Chr(34) & pythonwPath & Chr(34) & " " & Chr(34) & scriptPath & Chr(34)

' Window style 0 hides any console; the Tkinter window remains visible.
shell.Run command, 0, False
