# NIFTY Next-Day AI Intelligence

The repository contains a source archive split into small binary parts for the GitHub deployment upload. The application source is reconstructed by `bootstrap.py`.

## Start locally on Windows

1. Download this repository using **Code → Download ZIP**, or clone it.
2. Install Python 3.12 (64-bit) with the `py` launcher.
3. Double-click `START_HERE.bat`. It reconstructs the source and launches the local app.
4. Open `http://127.0.0.1:8000` if it does not open automatically.
5. Put your manually downloaded NSE reports into `INCOMING_REPORTS/`, or choose Data centre → Import files.
6. Confirm the report checklist, then run the six-agent analysis.

## Render

The root `render.yaml` creates a free Python web service in Singapore. Add a strong unique `NIFTY_APP_PASSWORD` in the Render environment. Browser report uploads are supported. The free Render filesystem is temporary; uploaded files and SQLite history can be lost after restart or redeploy. Keep original reports and long-term history on your PC. The local Windows mode is the persistent personal workspace.

## What it does

- Imports the 12 NSE EOD report types selected by the user: NIFTY 50 historical index, India VIX, CM UDiFF Bhavcopy, CM delivery, CM volatility, FII/DII activity, F&O UDiFF Bhavcopy, participant OI, participant volumes, FII derivatives statistics, F&O settlement prices and F&O volatility.
- Detects files in the local `INCOMING_REPORTS/` folder, fingerprints duplicates, extracts ZIP contents and audits each report.
- Calculates previous-day classic pivot, R1/R2/R3, S1/S2/S3 and CPR from genuine NIFTY OHLC.
- Calculates NIFTY option OI aggregates, PCR, max CE/PE OI strike candidates and Max Pain where the UDiFF contract schema is recognized.
- Produces six transparent specialist analytics outputs and one master evidence summary.
- Saves reports, daily index prices and analysis history locally.

A scheduled GitHub Actions collector fetches the official NSE F&O Participant-wise Trading Volumes CSV and FII Derivatives Statistics XLS archives after market hours. It validates file formats and tabular contents, retries temporary failures, records unavailable dates honestly, retains raw source files, and backfills a rolling 60-calendar-day window (typically enough for at least 20 trading sessions). The latest 20 available sessions are indexed in `data/nse_reports/manifest.json`.

On Render startup, verified archived CSV/XLS reports are automatically imported into the analysis database; no manual upload is required for these two reports. Other reports remain importable manually. The collector does not fabricate missing market values. NSE publication delays or access blocks are reported as missing/error states and retried on the next scheduled run.

GitHub is the durable raw archive. Render's free filesystem remains temporary, so the SQLite database and user-uploaded files can be lost after restart or redeploy; the committed NSE archive is retained in GitHub. Check NSE's data terms for the permitted use of downloaded reports. This is a personal research tool, not investment advice or order execution.

## Render build

`python bootstrap.py && pip install -r requirements.txt`

Start: `uvicorn app:app --host 0.0.0.0 --port $PORT`

Health: `/api/health`
