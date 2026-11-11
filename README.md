# NIFTY LAB — Next-Day AI Intelligence

Personal, local-first NIFTY 50 end-of-day research application. Import your 12 NSE reports, validate them, calculate previous-session levels, run six independent analytical agents, consolidate evidence, and keep a forecast journal.

## What is included

- Dark navy / cyan glass trading dashboard with optional light theme.
- Twelve-category report centre supporting CSV, ZIP, XLS, XLSX, DAT and TXT.
- Browser multi-file / folder upload for Render or any hosted deployment.
- Windows-local folder watcher for files copied into INCOMING_REPORTS.
- SQLite report inventory, raw-file storage, analysis journal and CSV export.
- NIFTY OHLC pivot, CPR, PDH, PDL, PDC, R1/R2/R3 and S1/S2/S3 calculations.
- NIFTY option Bhavcopy analysis for CE/PE OI, PCR, Max Pain and strike OI concentrations when the UDiFF columns can be mapped.
- Six independent agents: Price Action, Options Intelligence, Institutional Flow, Market Breadth, Volatility and ML Pattern Engine.
- Random Forest pattern model only after a minimum history gate; chronological holdout accuracy is reported separately.
- Explicit missing / unclassified / malformed report statuses. No fabricated prices or demo signal values.

## The 12 expected report categories

1. NIFTY 50 historical index OHLC.
2. India VIX historical index OHLC.
3. CM UDiFF Common Bhavcopy final.
4. Security-wise Delivery Positions.
5. CM Daily Volatility.
6. FII/FPI and DII activity.
7. F&O UDiFF Common Bhavcopy final.
8. Participant-wise Open Interest.
9. Participant-wise Trading Volumes.
10. FII Derivatives Statistics.
11. F&O Daily Settlement Prices.
12. F&O Daily Volatility.

The importer identifies reports from filenames and column headers. NSE report labels / filenames can change; use the Data Centre report inventory to spot any file that needs manual review. The current version does not silently coerce an unknown file into a known category.

## Run on Windows (recommended for your personal workflow)

1. Download the repository ZIP from GitHub and extract it.
2. Install Python 3.12 (64-bit).
3. Double-click START_NIFTY_AI.bat.
4. Copy your 12 reports into INCOMING_REPORTS (subfolders are supported).
5. Open http://127.0.0.1:8000 if it does not open automatically.
6. The local watcher scans the folder every five seconds. You can also use Data Centre → Select report folder or Browse files.
7. Click Run analysis after all expected reports have been imported.

The Windows-local mode can read the local folder because the Python backend runs on your computer. A normal website hosted on Render cannot directly see C:\ or your browser's Downloads folder.

## Run manually

~~~powershell
py -3.12 -m venv .venv
.\\.venv\\Scripts\\Activate.ps1
python -m pip install -r requirements.txt
uvicorn app:app --host 127.0.0.1 --port 8000
~~~

Open http://127.0.0.1:8000.

## Render deployment

The repository includes render.yaml for a Python 3.12 web service. The existing Render web service connected to this repository can deploy from main on commit.

On Render:
- Use pip install -r requirements.txt.
- Start command: uvicorn app:app --host 0.0.0.0 --port $PORT.
- Health endpoint: /api/health.
- Upload reports in the browser using Data Centre → Browse files or Select report folder.

**Hosted/local distinction:** Render cannot watch your Windows folders. Browser uploads are required on Render. Render free service disks and local SQLite storage are ephemeral; do not treat the hosted instance as a permanent personal research archive. Use the Windows-local mode for durable data history and folder monitoring.

## Data interpretation and safeguards

- The market-level engine uses the last dated NIFTY 50 OHLC row after sorting by report date. It will not substitute spot or options prices for NIFTY index OHLC.
- Classic pivots and CPR are calculated from the imported prior completed NIFTY session.
- Options PCR / Max Pain are calculated only when NIFTY option contracts and the required strike, option type and OI fields are available.
- OI change requires comparing the current Bhavcopy with the previous trading session. The initial import provides a snapshot, not a historical delta series.
- Participant-wise and FII/DII data are treated as broad positioning context, not exact NIFTY-only directional positions.
- The current institutional directional mapper is conservative and may show UNAVAILABLE until the imported report schema is verified.
- The ML agent requires sufficient historical daily NIFTY observations. A single day's report set cannot train a meaningful model.
- Any probability / confidence display must be based on out-of-sample calibration. The present rules-based evidence blend is explicitly labelled uncalibrated.
- No trading orders, broker API, live data scraping, or automatic NSE website download is implemented.

## API

- GET /api/health — service health and runtime mode.
- GET /api/dashboard — current report inventory and latest analysis.
- GET /api/report-types — expected report categories.
- POST /api/import — multipart upload; repeat files form field for each report.
- POST /api/import-folder — scan local INCOMING_REPORTS.
- POST /api/analyze — run agents and persist analysis.
- GET /api/history — saved analysis journal.
- GET /api/export — prediction history CSV.
- PATCH /api/reports/{id} — manually assign a report category using report_type form field.
- DELETE /api/reports/{id} — remove an imported report.

Research tool only; not investment advice.
