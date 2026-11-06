@echo off
setlocal
title NIFTY LAB Local NSE Connector
cd /d "%~dp0"

echo ==========================================
echo       NIFTY LAB LOCAL CONNECTOR
echo ==========================================
echo.
echo This sends genuine NSE responses to Render.
echo It does not bypass NSE restrictions or invent data.
echo.

set "NIFTYLAB_RENDER_URL=https://niftylab-indian-option-mcp-preview.onrender.com"
set /p "NIFTYLAB_INGEST_TOKEN=Paste the same NIFTYLAB_INGEST_TOKEN configured in Render: "

if "%NIFTYLAB_INGEST_TOKEN%"=="" (
  echo Token is required.
  pause
  exit /b 1
)

where py >nul 2>nul
if %errorlevel%==0 (
  py -3.12 -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Python 3.12 setup failed. Check Python installation.
    pause
    exit /b 1
  )
  py -3.12 local_connector.py
) else (
  python -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Python setup failed. Check Python installation.
    pause
    exit /b 1
  )
  python local_connector.py
)

echo.
echo Connector test finished.
pause
