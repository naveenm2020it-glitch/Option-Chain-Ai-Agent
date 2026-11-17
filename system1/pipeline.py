from __future__ import annotations
import csv, gzip, hashlib, io, json, zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CORE_REPORTS = ("fo_udiff_bhavcopy", "nifty_index_close_all")
OPTIONAL_REPORTS = ("cm_udiff_bhavcopy","fo_participant_oi","fo_participant_volume","fo_market_activity","fo_settlement_prices","fo_daily_volatility","fo_contract_master","fo_contract_delta","fo_top10_cm_volume")
ALIASES = {
"symbol":("TckrSymb","Tckr_Symb","Underlying","Symbol","SYMBOL"),
"option_type":("OptnTp","Optn_Tp","OptionType","OPTION_TYP"),
"expiry":("XpryDt","Xpry_Dt","ExpiryDate","EXPIRY_DT"),
"strike":("StrkPric","Strk_Pric","StrikePrice","STRIKE_PR"),
"close":("ClsPric","Cls_Pric","ClosePrice","CLOSE"),
"oi":("OpnIntrst","Opn_Intrst","OpenInterest","OPEN_INT"),
"change_oi":("ChngInOpnIntrst","Chng_In_Opn_Intrst","ChangeInOI","CHG_IN_OI"),
"volume":("TtlTradgVol","Ttl_Tradg_Vol","TotalTradedVolume","CONTRACTS"),
}
def _norm(v): return "".join(c.lower() for c in str(v).strip() if c.isalnum())
def _find(headers,key):
    lookup={_norm(h):h for h in headers}
    return next((lookup[_norm(a)] for a in ALIASES[key] if _norm(a) in lookup),None)
def _number(v):
    if v is None or str(v).strip() in ("","-","NA","N/A","null"): return None
    try: x=float(str(v).replace(",","").strip())
    except (TypeError,ValueError): return None
    return x if x == x and abs(x) != float("inf") else None
def _read_csv_bytes(payload:bytes,filename:str):
    if filename.lower().endswith(".gz"): payload=gzip.decompress(payload)
    if filename.lower().endswith(".zip") or payload[:4]==b"PK\x03\x04":
        with zipfile.ZipFile(io.BytesIO(payload)) as z:
            bad=z.testzip()
            if bad: raise ValueError(f"corrupt ZIP member: {bad}")
            names=[n for n in z.namelist() if not n.endswith("/") and n.lower().endswith((".csv",".txt"))]
            if not names: raise ValueError("ZIP has no CSV/TXT member")
            name=max(names,key=lambda n:z.getinfo(n).file_size)
            payload=z.read(name)
    text=payload.decode("utf-8-sig",errors="strict")
    if "<html" in text[:2000].lower() or "<!doctype" in text[:2000].lower(): raise ValueError("HTML received instead of data")
    try: dialect=csv.Sniffer().sniff(text[:8192],delimiters=",;\t|")
    except csv.Error: dialect=csv.excel
    reader=csv.DictReader(io.StringIO(text),dialect=dialect)
    if not reader.fieldnames or len(reader.fieldnames)<2: raise ValueError("CSV header/delimiter not identified")
    rows=[dict(r) for r in reader if any(str(v or "").strip() for v in r.values())]
    if not rows: raise ValueError("CSV contains no data rows")
    return rows
def load_calendar(path:Path):
    d=json.loads(path.read_text(encoding="utf-8"))
    return {"holidays":set(d.get("holidays",[])),"special":set(d.get("special_trading_sessions",[])),"window_start":d.get("window_start"),"window_end":d.get("window_end")}
def previous_sessions(analysis_date:date,count:int,calendar:dict[str,Any]):
    if count<1: raise ValueError("window_sessions must be at least 1")
    start=date.fromisoformat(calendar["window_start"]) if calendar.get("window_start") else None
    end=date.fromisoformat(calendar["window_end"]) if calendar.get("window_end") else None
    if (start and analysis_date<start) or (end and analysis_date>end): raise ValueError("analysis date outside configured exchange calendar")
    holidays,special=calendar.get("holidays",set()),calendar.get("special",calendar.get("special_trading_sessions",set()))
    out=[]; cur=analysis_date
    while len(out)<count:
        key=cur.isoformat()
        if key in special or (cur.weekday()<5 and key not in holidays): out.append(key)
        cur-=timedelta(days=1)
        if start and cur<start and len(out)<count: raise ValueError("calendar lacks enough prior sessions")
    return out
def _manifest_for(root:Path,session:str):
    p=root/session/"manifest.json"
    if p.is_file(): return p
    p=root/"manifest.json"
    if p.is_file():
        try:
            m=json.loads(p.read_text(encoding="utf-8"))
            if m.get("core_session_date")==session or m.get("requested_end_date")==session: return p
        except (OSError,json.JSONDecodeError): pass
    return None
def _load_report(base:Path,entry:dict[str,Any],session:str):
    filename=entry.get("file")
    if not filename: raise ValueError("manifest entry has no file")
    path=(base/filename).resolve()
    if base.resolve() not in path.parents: raise ValueError("manifest path escapes report directory")
    if not path.is_file(): raise FileNotFoundError(filename)
    payload=path.read_bytes(); digest=hashlib.sha256(payload).hexdigest()
    if entry.get("sha256") and digest!=entry["sha256"]: raise ValueError("SHA-256 mismatch")
    rows=_read_csv_bytes(payload,path.name)
    return rows,{"file":filename,"bytes":len(payload),"sha256":digest,"rows":len(rows),"report_date":entry.get("report_date",session)}
def _normalise_options(rows,session):
    headers=list(rows[0]); cols={k:_find(headers,k) for k in ALIASES}
    required=("symbol","option_type","expiry","strike","close","oi","change_oi","volume")
    missing=[k for k in required if not cols.get(k)]
    if missing: raise ValueError("UDiFF required columns missing: "+", ".join(missing))
    out=[]; rejected=0
    for row in rows:
        if str(row.get(cols["symbol"],"")).strip().upper()!="NIFTY" or str(row.get(cols["option_type"],"")).strip().upper() not in ("CE","PE"):
            rejected+=1; continue
        vals={k:_number(row.get(cols[k])) for k in ("strike","close","oi","change_oi","volume")}
        if any(v is None for v in vals.values()): rejected+=1; continue
        out.append({"underlying":"NIFTY","option_type":str(row[cols["option_type"]]).strip().upper(),"expiry":str(row.get(cols["expiry"],"")).strip(),**vals})
    keys=[(r["expiry"],r["strike"],r["option_type"]) for r in out]
    duplicates=len(keys)-len(set(keys))
    if not out: raise ValueError("No valid NIFTY CE/PE contracts in UDiFF")
    return {"rows":out,"valid_contracts":len(out),"rejected_rows":rejected,"duplicate_contract_keys":duplicates,"columns":cols}
def _normalise_index(rows):
    headers=list(rows[0])
    sym=next((h for h in headers if _norm(h) in ("indexname","index")),None)
    close=next((h for h in headers if _norm(h) in ("closingindexvalue","close","indexclose")),None)
    if not sym or not close: raise ValueError("Index report requires index name and closing index value")
    out=[]
    for row in rows:
        name=str(row.get(sym,"")).strip().upper()
        if name in ("NIFTY 50","INDIA VIX"):
            value=_number(row.get(close))
            if value is not None: out.append({"index":name,"close":value})
    if not any(x["index"]=="NIFTY 50" for x in out): raise ValueError("NIFTY 50 close missing")
    return {"rows":out,"nifty50_present":True,"india_vix_present":any(x["index"]=="INDIA VIX" for x in out)}
def build_package(report_root:Path,analysis_date:date,calendar_path:Path,window_sessions:int=23,output_path:Path|None=None):
    calendar=load_calendar(calendar_path); sessions=previous_sessions(analysis_date,window_sessions,calendar)
    results={}; aggregate=hashlib.sha256()
    for session in reversed(sessions):
        mp=_manifest_for(report_root,session)
        if not mp: results[session]={"status":"missing_manifest","reports":{}}; continue
        try: manifest=json.loads(mp.read_text(encoding="utf-8"))
        except (OSError,json.JSONDecodeError) as exc: results[session]={"status":"invalid_manifest","error":str(exc),"reports":{}}; continue
        report_results=manifest.get("results",{}); report_data={}; issues=[]
        for name in (*CORE_REPORTS,*OPTIONAL_REPORTS):
            entry=report_results.get(name)
            if not isinstance(entry,dict) or not entry.get("file"):
                report_data[name]={"status":"missing"}
                if name in CORE_REPORTS: issues.append(f"{name}: missing")
                continue
            try:
                rows,meta=_load_report(mp.parent,entry,session)
                if str(meta["report_date"])!=session: raise ValueError(f"report date {meta['report_date']} != {session}")
                if name=="fo_udiff_bhavcopy":
                    parsed=_normalise_options(rows,session); meta.update(parsed)
                    if parsed["duplicate_contract_keys"]: issues.append(f"{name}: duplicate contract keys={parsed['duplicate_contract_keys']}")
                elif name=="nifty_index_close_all": meta.update(_normalise_index(rows))
                report_data[name]={"status":"validated",**meta}
                aggregate.update(name.encode()); aggregate.update(meta["sha256"].encode())
            except Exception as exc:
                report_data[name]={"status":"invalid","error":str(exc)}
                if name in CORE_REPORTS: issues.append(f"{name}: {exc}")
        ready=not issues and all(report_data.get(n,{}).get("status")=="validated" for n in CORE_REPORTS)
        results[session]={"status":"ready" if ready else "blocked","reports":report_data,"issues":issues}
    selected=results[analysis_date.isoformat()]
    blocked=[s for s,v in results.items() if v["status"]!="ready"]
    package={"schema_version":"1.0","system":"system_1_data_intelligence","analysis_date":analysis_date.isoformat(),"window_sessions":window_sessions,"session_dates_newest_first":sessions,"window_policy":"selected date plus prior trading sessions; no calendar-day substitution","status":"ready" if selected["status"]=="ready" else "blocked","selected_session_ready":selected["status"]=="ready","window_ready_sessions":sum(v["status"]=="ready" for v in results.values()),"window_missing_or_blocked_sessions":blocked,"sessions":results,"input_manifest_hash":aggregate.hexdigest(),"generated_at_utc":datetime.now(timezone.utc).isoformat(),"limitations":["Participant OI/volume are broad-market context, not proof of NIFTY-specific seller identity.","Historical IV, Greeks, bid/ask and intraday quotes are not assumed available unless separately validated.","Missing data remains missing; no silent zero-fill."]}
    if output_path:
        output_path.parent.mkdir(parents=True,exist_ok=True); output_path.write_text(json.dumps(package,indent=2),encoding="utf-8")
    return package
