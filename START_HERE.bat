@echo off
cd /d "%~dp0"
py -3.12 bootstrap.py
if errorlevel 1 (
  echo.
  echo Source setup failed. Install Python 3.12 and retry.
  pause
  exit /b 1
)
call START_NIFTY_AI.bat
