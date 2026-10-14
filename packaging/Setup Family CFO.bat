@echo off
setlocal
cd /d "%~dp0"
title Family CFO - setup
echo.
echo  Setting up Family CFO. This takes a minute or two and needs the internet.
echo.

rem ---- find Python 3.10 or newer (newest first; an older one may also be installed)
set "PY="
for %%V in (3.14 3.13 3.12 3.11 3.10) do (
  if not defined PY (
    py -%%V -c "import sys" >nul 2>nul && set "PY=py -%%V"
  )
)
if not defined PY (
  python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo  Python 3.10 or newer was not found.
  echo.
  echo  1. Download it from https://www.python.org/downloads/
  echo  2. When installing, TICK "Add python.exe to PATH".
  echo  3. Then double-click "Setup Family CFO" again.
  echo.
  pause
  exit /b 1
)

rem ---- a private Python environment inside this folder
if not exist ".venv\Scripts\python.exe" (
  echo  Creating the app's Python environment...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo  Could not create it. Try running this file again.
    pause
    exit /b 1
  )
)
echo  Installing the libraries it uses...
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
".venv\Scripts\python.exe" -m pip install --quiet "pypdf>=5"
if errorlevel 1 (
  echo  The install did not finish - check the internet connection and run this file again.
  pause
  exit /b 1
)
rem Only needed if Claude is chosen for the chat; Groq needs nothing extra.
".venv\Scripts\python.exe" -m pip install --quiet "anthropic>=1.11" >nul 2>nul || echo  (Claude chat library skipped - the free Groq chat still works.)

rem ---- a "Family CFO" shortcut on the desktop
if not defined FCFO_NO_SHORTCUT (
  powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$dir = (Get-Location).Path; $desk = [Environment]::GetFolderPath('Desktop');" ^
    "$s = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $desk 'Family CFO.lnk'));" ^
    "$s.TargetPath = Join-Path $dir '.venv\Scripts\python.exe'; $s.Arguments = 'run_dashboard.py';" ^
    "$s.WorkingDirectory = $dir; $s.WindowStyle = 7; $s.Description = 'Family CFO'; $s.Save()"
  echo  Added a "Family CFO" shortcut to your desktop.
)

echo.
echo  All set. Family CFO now opens in your browser.
echo  Next time, just double-click "Family CFO" on your desktop.
echo.
if not defined FCFO_NO_START start "Family CFO" /min ".venv\Scripts\python.exe" run_dashboard.py
pause
