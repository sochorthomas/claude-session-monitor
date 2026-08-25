' Silently launch the floating widget (no console window).
' Double-click to run, or drop a shortcut into the Startup folder (shell:startup)
' to launch it at login.
'
' This file must stay ASCII-only (WScript reads .vbs as ANSI). It only locates
' scripts\start-monitor.ps1 below itself and runs it hidden; all path handling
' lives in the .ps1, so the tool works from any location, including paths with
' spaces.
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
ps1 = fso.BuildPath(fso.BuildPath(scriptDir, "scripts"), "start-monitor.ps1")

q = Chr(34)
Set sh = CreateObject("WScript.Shell")
sh.Run "powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File " & q & ps1 & q, 0, False
