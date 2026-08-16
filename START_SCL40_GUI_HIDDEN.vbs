Option Explicit

Dim shell, fso, scriptDir, launcher
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
launcher = fso.BuildPath(scriptDir, "START_SCL40_GUI.cmd")

shell.Run "cmd.exe /c """ & launcher & """", 0, False
