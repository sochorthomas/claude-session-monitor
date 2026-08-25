@echo off
rem Double-click entry point for install.ps1.
rem
rem Double-clicking a .ps1 opens it in an editor rather than running it, so this
rem thin wrapper exists purely to be clickable. It keeps the console open
rem afterwards: install.ps1 ends by smoke-testing the hook, and that result is
rem the reason to run it at all.
rem
rem ASCII only - a .cmd is read in the console codepage.
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1"
set "rc=%ERRORLEVEL%"
echo.
pause
exit /b %rc%
