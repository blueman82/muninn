@echo off
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0muninn.ps1" %*
exit /b %errorlevel%
