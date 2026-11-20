from __future__ import annotations
import argparse, json
from datetime import date
from pathlib import Path
from system1.pipeline import build_package
ROOT=Path(__file__).resolve().parents[1]
def main():
    p=argparse.ArgumentParser(description="Build System 1 package from already-downloaded NSE reports.")
    p.add_argument("--date",required=True,help="Analysis date YYYY-MM-DD")
    p.add_argument("--reports",default=str(ROOT/"data"/"nse_reports"))
    p.add_argument("--calendar",default=str(ROOT/"data"/"nse_trading_calendar.json"))
    p.add_argument("--window-sessions",type=int,default=23,help="Selected date plus prior sessions; use 1 for diagnostic")
    p.add_argument("--output",default=None)
    a=p.parse_args(); out=Path(a.output) if a.output else None
    package=build_package(Path(a.reports),date.fromisoformat(a.date),Path(a.calendar),a.window_sessions,out)
    selected=package["sessions"][package["analysis_date"]]
    print(json.dumps({"analysis_date":package["analysis_date"],"status":package["status"],"selected_session_ready":package["selected_session_ready"],"window_sessions":package["window_sessions"],"window_ready_sessions":package["window_ready_sessions"],"window_missing_or_blocked_sessions":package["window_missing_or_blocked_sessions"],"input_manifest_hash":package["input_manifest_hash"],"output":str(out) if out else None,"selected_session":selected},indent=2))
    return 0 if package["selected_session_ready"] else 2
if __name__=="__main__": raise SystemExit(main())
