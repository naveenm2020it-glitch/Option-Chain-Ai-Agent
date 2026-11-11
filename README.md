# NIFTY Next-Day AI Intelligence

A personal, local-first NIFTY end-of-day research application. The repository source is stored in a split ZIP bundle; run `python bootstrap.py` to reconstruct it.

## Windows quick start
1. Download this repository with **Code → Download ZIP** (or clone it).
2. Install Python 3.12 (64-bit).
3. Double-click `START_HERE.bat`. It reconstructs the app and launches it.
4. Open `http://127.0.0.1:8000`.
5. Put manually downloaded NSE reports in `INCOMING_REPORTS/`, or upload them through Data Centre.
6. Review the validation checklist and run the six-agent analysis.

## Render
The root `render.yaml` defines a free Python service in Singapore. Set a strong unique `NIFTY_APP_PASSWORD` in Render. Render disables local folder watching and supports browser uploads. Free service storage is temporary; keep originals and long-term history on your PC. The local Windows mode is the persistent personal workspace.

## Features
- Import and audit the 12 selected NSE EOD report types.
- Folder monitoring locally, ZIP extraction, duplicate fingerprints, data-date checks and import logs.
- NIFTY OHLC pivots, R/S levels and CPR when valid spot OHLC is available.
- NIFTY options OI, PCR, max CE/PE OI strike candidates and Max Pain when the bhavcopy schema is recognized.
- Six transparent specialist analytics modules and a master evidence summary.
- SQLite report and prediction history.
- Dark navy/cyan glass dashboard inspired by the supplied references.

The application does not scrape NSE or automatically download exchange reports. Missing values are not fabricated. The engine withholds predictions when required data is incomplete. ML performance is not claimed until sufficient historical inputs and chronological walk-forward validation exist. Check the permitted use of downloaded market data. Research only; no orders are placed.
