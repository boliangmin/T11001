Option Explicit

Dim shell, fso, appDir, pythonLauncher, pythonwPath, scriptPath, command

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

appDir = fso.GetParentFolderName(WScript.ScriptFullName)
scriptPath = fso.BuildPath(appDir, "stock_price_ui.py")

If Not fso.FileExists(scriptPath) Then
    MsgBox "The UI script was not found:" & vbCrLf & scriptPath, vbCritical, "Startup failed"
    WScript.Quit 1
End If

' Prefer the Windows Python launcher so the newest installed Python 3 is used.
pythonLauncher = FindPythonLauncher()
If pythonLauncher <> "" Then
    command = Chr(34) & pythonLauncher & Chr(34) & " -3 " & Chr(34) & scriptPath & Chr(34)
Else
    pythonwPath = FindNewestPythonw()
    If pythonwPath = "" Then
        MsgBox "No Python 3.10 or newer GUI runtime (pyw.exe or pythonw.exe) was found.", _
            vbCritical, "Startup failed"
        WScript.Quit 1
    End If
    command = Chr(34) & pythonwPath & Chr(34) & " " & Chr(34) & scriptPath & Chr(34)
End If

shell.CurrentDirectory = appDir

' Window style 0 hides any console; the Tkinter window remains visible.
shell.Run command, 0, False

Function FindPythonLauncher()
    Dim candidate
    FindPythonLauncher = ""
    For Each candidate In Array( _
        shell.ExpandEnvironmentStrings("%SystemRoot%\pyw.exe"), _
        shell.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Launcher\pyw.exe"))
        If fso.FileExists(candidate) Then
            FindPythonLauncher = candidate
            Exit Function
        End If
    Next
End Function

' Fallback: the highest Python 3.10+ pythonw.exe in the default install folders.
Function FindNewestPythonw()
    Dim versionPattern, root, folder, matches, minor, bestMinor, candidate
    Set versionPattern = New RegExp
    versionPattern.Pattern = "^Python3(\d+)(-32)?$"
    versionPattern.IgnoreCase = True
    FindNewestPythonw = ""
    bestMinor = 9
    For Each root In Array( _
        shell.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python"), _
        shell.ExpandEnvironmentStrings("%ProgramFiles%"))
        If fso.FolderExists(root) Then
            For Each folder In fso.GetFolder(root).SubFolders
                Set matches = versionPattern.Execute(folder.Name)
                If matches.Count = 1 Then
                    minor = CInt(matches(0).SubMatches(0))
                    candidate = fso.BuildPath(folder.Path, "pythonw.exe")
                    If minor > bestMinor And fso.FileExists(candidate) Then
                        bestMinor = minor
                        FindNewestPythonw = candidate
                    End If
                End If
            Next
        End If
    Next
End Function
