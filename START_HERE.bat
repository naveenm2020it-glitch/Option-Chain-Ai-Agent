@echo off
setlocal
cd /d "%~dp0"
py -3.12 bootstrap.py
if errorlevel 1 (
  echo Could not reconstruct the source. Install Python 3.12 first.
  pause
  exit /b 1
)
call START_NIFTY_AI.bat
