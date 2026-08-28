@echo off
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install-task.ps1" %*
exit /b %ERRORLEVEL%
