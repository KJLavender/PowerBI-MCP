@echo off
rem Double-click to start PRISM and open it in the browser (the first run installs what it needs).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-prism.ps1" %*
if errorlevel 1 pause
