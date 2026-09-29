@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  powershell.exe -NoProfile -File "%~dp0scripts\bootstrap-error.ps1"
  echo Run scripts\setup.ps1 first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -c "import sys" >nul 2>&1
if errorlevel 1 (
  powershell.exe -NoProfile -File "%~dp0scripts\bootstrap-error.ps1"
  echo Python could not start. Run scripts\setup.ps1 or check the environment.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m videocut.launcher --open
if errorlevel 1 pause
