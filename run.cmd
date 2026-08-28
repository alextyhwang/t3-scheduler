@echo off
powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0scripts\run.ps1" %*
exit /b %ERRORLEVEL%
