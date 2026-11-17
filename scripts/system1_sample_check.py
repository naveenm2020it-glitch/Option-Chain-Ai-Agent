from __future__ import annotations
import argparse, hashlib, json, tempfile
from datetime import date
from pathlib import Path
import sys
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.nse_download_check import fetch_report, report_specs
from system1.pipeline import build_package

def main():
    parser=argparse.ArgumentParser(description="Download and parse the two mandatory NSE reports for one historical session.")
    parser.add_argument("--date",default="2026-01-07")
    args=parser.parse_args()
    session_date=date.fromisoformat(args.date)
    specs={name:(name,path,kind) for name,path,kind in report_specs(session_date)}
    required=("fo_udiff_bhavcopy","nifty_index_close_all")
    client=requests.Session()
    client.headers.update({
        "User-Agent":"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Accept":"*/*","Referer":"https://www.nseindia.com/",
    })
    with tempfile.TemporaryDirectory(prefix="system1-sample-") as temp:
        root=Path(temp); folder=root/session_date.isoformat(); folder.mkdir()
        results={}
        for name in required:
            payload,meta=fetch_report(client,*specs[name])
            ext={"zip":"zip","csv":"csv","gzip":"csv.gz"}[specs[name][2]]
            filename=f"{name}_{session_date.isoformat()}.{ext}"
            (folder/filename).write_bytes(payload)
            results[name]={"file":filename,"report_date":session_date.isoformat(),"sha256":hashlib.sha256(payload).hexdigest(),"download_url":meta["url"],"bytes":len(payload),"http_status":meta["http_status"]}
        manifest={"core_session_date":session_date.isoformat(),"requested_end_date":session_date.isoformat(),"results":results}
        (folder/"manifest.json").write_text(json.dumps(manifest,indent=2),encoding="utf-8")
        calendar=root/"calendar.json"
        calendar.write_text(json.dumps({"window_start":session_date.isoformat(),"window_end":session_date.isoformat(),"holidays":[],"special_trading_sessions":[]}),encoding="utf-8")
        package=build_package(root,session_date,calendar,1)
        selected=package["sessions"][session_date.isoformat()]
        summary={
            "analysis_date":package["analysis_date"],
            "status":package["status"],
            "downloaded_reports":{k:{"bytes":v["bytes"],"sha256":v["sha256"],"http_status":v["http_status"]} for k,v in results.items()},
            "validated_reports":{k:{"status":v["status"],"rows":v.get("rows"),"sha256":v.get("sha256")} for k,v in selected["reports"].items() if k in required},
            "nifty_option_contracts":selected["reports"].get("fo_udiff_bhavcopy",{}).get("valid_contracts"),
            "rejected_option_rows":selected["reports"].get("fo_udiff_bhavcopy",{}).get("rejected_rows"),
            "duplicate_contract_keys":selected["reports"].get("fo_udiff_bhavcopy",{}).get("duplicate_contract_keys"),
            "nifty50_and_vix":selected["reports"].get("nifty_index_close_all",{}).get("rows"),
            "issues":selected.get("issues",[]),
            "b2_calls":0,
            "render_calls":0,
        }
        print(json.dumps(summary,indent=2))
        return 0 if package["selected_session_ready"] else 2
if __name__=="__main__":
    raise SystemExit(main())
