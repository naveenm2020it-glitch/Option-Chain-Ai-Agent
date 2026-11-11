@echo off
setlocal
cd /d "%~dp0"
title NIFTY AI - Local Intelligence Server
if not exist "INCOMING_REPORTS" mkdir "INCOMING_REPORTS"
if not exist "var" mkdir "var"
if not exist ".venv\Scripts\python.exe" (
  echo [1/3] Creating Python environment...
  py -3.12 -m venv .venv
  if errorlevel 1 (
    echo Python 3.12 was not found. Install Python 3.12 and enable the py launcher.
    pause
    exit /b 1
  )
)
echo [2/3] Installing or checking dependencies...
call ".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo Dependency installation failed. Check your internet connection and Python installation.
  pause
  exit /b 1
)
echo [3/3] Starting NIFTY AI...
start "NIFTY AI Browser" http://127.0.0.1:8000
".venv\Scripts\python.exe" -m uvicorn app:app --host 127.0.0.1 --port 8000
pause
