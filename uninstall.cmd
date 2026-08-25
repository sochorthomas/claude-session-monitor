@echo off
rem Double-click entry point for uninstall.ps1.
rem
rem The confirmation lives here rather than in the .ps1 so that the script
rem itself stays non-interactive and safe to drive from anything else. A
rem double-click should not silently delete your settings; a scripted call
rem should not stop and ask.
rem
rem ASCII only - a .cmd is read in the console codepage.
setlocal
echo Claude Session Monitor - uninstall
echo.
echo This will:
echo    - close the widget
echo    - remove its hooks from your Claude Code settings
echo    - delete the session-status folder and saved settings under .claude
echo    - remove a Startup shortcut pointing at this folder
echo.
echo Your own hooks and every other Claude Code setting are left alone,
echo and this folder is not deleted.
echo.
set "flags=#"
set /p "answer=Type Y to uninstall, K to uninstall but keep your settings: "
if /i "%answer%"=="Y" set "flags="
if /i "%answer%"=="K" set "flags=-KeepData"
if "%flags%"=="#" (
    echo.
    echo Cancelled - nothing was changed.
    echo.
    pause
    exit /b 1
)
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\uninstall.ps1" %flags%
set "rc=%ERRORLEVEL%"
echo.
pause
exit /b %rc%
