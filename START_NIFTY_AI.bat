@echo off
setlocal
cd /d "%~dp0"
title NIFTY LAB - Next-Day Intelligence

where py >nul 2>nul
if errorlevel 1 (
  echo Python launcher was not found. Install Python 3.12 and enable Add to PATH.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating Python 3.12 environment...
  py -3.12 -m venv .venv
  if errorlevel 1 (
    echo Could not create Python 3.12 environment.
    pause
    exit /b 1
  )
)

echo Installing or checking required packages...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo Dependency installation failed. Check your internet connection and Python version.
  pause
  exit /b 1
)

if not exist "INCOMING_REPORTS" mkdir "INCOMING_REPORTS"
if not exist "data" mkdir "data"

echo.
echo NIFTY LAB is starting at http://127.0.0.1:8000
echo Place your NSE files in the INCOMING_REPORTS folder.
echo Keep this window open while using the application.
start "" http://127.0.0.1:8000
".venv\Scripts\python.exe" -m uvicorn app:app --host 127.0.0.1 --port 8000
pause
