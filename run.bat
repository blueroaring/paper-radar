@echo off
REM Paper Radar quick start (Windows).
REM
REM NOTE FOR MAINTAINERS: keep this file PURE ASCII (no BOM, no Chinese).
REM cmd.exe reads a .bat with the current OEM code page, and chcp only takes
REM effect after it runs, so any non-ASCII text placed before the chcp line
REM is decoded with the wrong code page.
REM
REM Double-click this file to start the local console plus the built-in daily
REM scheduler. Pass extra arguments to the serve command, e.g.:
REM     run.bat --port 8899
REM     run.bat --no-scheduler

setlocal
cd /d "%~dp0"
chcp 65001 >nul 2>&1
set PYTHONIOENCODING=utf-8

if not exist config.json (
  echo [i] config.json not found - copying config.example.json ...
  copy /y config.example.json config.json >nul
)

python -m paper_radar serve %*
if errorlevel 1 (
  echo.
  echo [!] Failed to start. Make sure Python 3.10+ is installed and on PATH.
  pause
)
endlocal
