#!/usr/bin/env python3
"""Deterministic Phase 2 normalization of the Phase 1 NSE EOD archive."""
from __future__ import annotations
import argparse, csv, hashlib, io, json, math, re, zipfile
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

OPTION_FIELDS = "session_date instrument_id symbol expiry_date strike option_type instrument_name open high low close last previous_close underlying_price settlement_price open_interest oi_change traded_volume traded_value trade_count lot_size contract_delta_factor previous_close_oi previous_close_premium premium_change_pct oi_change_pct volume_to_oi".split()
INDEX_FIELDS = "session_date open high low close points_change change_pct volume turnover_rs_cr pe pb dividend_yield".split()
PARTICIPANT_FIELDS = "session_date report_type participant future_index_long future_index_short future_stock_long future_stock_short option_index_call_long option_index_put_long option_index_call_short option_index_put_short option_stock_call_long option_stock_put_long option_stock_call_short option_stock_put_short total_long_contracts total_short_contracts".split()
FEATURE_FIELDS = "session_date nifty_open nifty_high nifty_low nifty_close nifty_change_pct nifty_range_points nifty_range_pct nifty_option_contracts ce_contracts pe_contracts ce_open_interest pe_open_interest ce_oi_change pe_oi_change ce_volume pe_volume ce_positive_oi_change_contracts pe_positive_oi_change_contracts participant_oi_available participant_volume_available".split()

def dte(v):
    if not v or not v.strip(): return None
    v=v.strip()
    for fmt in ("%Y-%m-%d","%d-%m-%Y","%d/%m/%Y","%Y/%m/%d"):
        try: return datetime.strptime(v[:10],fmt).date().isoformat()
        except ValueError: pass
    return date.fromisoformat(v[:10]).isoformat()

def num(v):
    if v is None: return None
    v=v.strip().replace(",","")
    if not v or v in {"-","--","NA","N/A","null","None"}: return None
    n=float(v)
    return n if math.isfinite(n) else None

def integer(v):
    n=num(v)
    return int(n) if n is not None else None

def readcsv(raw):
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig",errors="replace"))))

def report(z, session, suffix):
    names=[n for n in z.namelist() if n.startswith(f"reports/{session}/") and n.endswith(suffix)]
    return z.read(sorted(names)[0]) if names else None

def fo_rows(z, session):
    raw=report(z,session,"fo_udiff_bhavcopy.zip")
    if raw is None: raise ValueError(f"Missing FO Bhavcopy: {session}")
    with zipfile.ZipFile(io.BytesIO(raw)) as inner:
        names=[n for n in inner.namelist() if n.lower().endswith(".csv")]
        if len(names)!=1: raise ValueError(f"Expected one FO CSV for {session}")
        return readcsv(inner.read(names[0]))

def participant_rows(z, session, filename):
    raw=report(z,session,filename)
    if raw is None: return []
    lines=raw.decode("utf-8-sig",errors="replace").splitlines()
    i=next((i for i,line in enumerate(lines) if line.strip().startswith("Client Type,")),None)
    if i is None: return []
    return [{(k or "").strip():(v or "").strip() for k,v in row.items()} for row in csv.DictReader(io.StringIO("\n".join(lines[i:])))]

def delta_map(z, session):
    raw=report(z,session,"fo_contract_delta.csv")
    out={}
    if raw is None: return out
    for r in readcsv(raw):
        if r.get("Symbol","").strip().upper()!="NIFTY": continue
        k=(dte(r.get("Date")),dte(r.get("Expiry day")),num(r.get("Strike Price")),r.get("Option Type","").strip().upper())
        v=num(r.get("Delta Factor"))
        if v is not None: out[k]=v
    return out

def writecsv(path, fields, rows):
    with path.open("w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=fields,extrasaction="ignore"); w.writeheader()
        w.writerows({k:r.get(k) for k in fields} for r in rows)

WINDOW_FIELDS = "session_date current_nifty_change_pct prior_5_sessions_available prior_5_avg_nifty_change_pct prior_5_avg_ce_oi_change prior_5_avg_pe_oi_change prior_5_ce_volume prior_5_pe_volume prior_22_sessions_available prior_22_avg_nifty_change_pct prior_22_avg_ce_oi_change prior_22_avg_pe_oi_change prior_22_ce_volume prior_22_pe_volume".split()

def window_rows(features):
    """For each date, summarize only preceding completed sessions (no look-ahead)."""
    out=[]
    for i,current in enumerate(features):
        row={"session_date":current["session_date"],"current_nifty_change_pct":current.get("nifty_change_pct")}
        for n in (5,22):
            prior=features[max(0,i-n):i]
            complete=len(prior)==n
            row[f"prior_{n}_sessions_available"]=len(prior)
            row[f"prior_{n}_sessions_complete"]=complete
            for source,target in (("nifty_change_pct","avg_nifty_change_pct"),("ce_oi_change","avg_ce_oi_change"),("pe_oi_change","avg_pe_oi_change")):
                vals=[r.get(source) for r in prior if r.get(source) is not None]
                row[f"prior_{n}_{target}"]=round(sum(vals)/len(vals),6) if complete and len(vals)==n else None
            for source,target in (("ce_volume","ce_volume"),("pe_volume","pe_volume")):
                vals=[r.get(source) for r in prior if r.get(source) is not None]
                row[f"prior_{n}_{target}"]=sum(vals) if complete and len(vals)==n else None
        out.append(row)
    return out

def normalize(archive: Path, output: Path, expected_sessions=22):
    output.mkdir(parents=True,exist_ok=True)
    z=zipfile.ZipFile(archive)
    manifests=[n for n in z.namelist() if re.search(r"reports/trial-manifest\.json$",n,re.I)]
    if not manifests: raise ValueError("Missing reports/trial-manifest.json")
    source=json.loads(z.read(sorted(manifests)[0]))
    sessions=sorted(s["session_date"] for s in source.get("sessions",[]) if s.get("status")=="aligned")
    if len(sessions)!=expected_sessions: raise ValueError(f"Expected {expected_sessions} sessions, got {len(sessions)}")
    if len(set(sessions))!=len(sessions): raise ValueError("Duplicate session dates")
    options=[]; indexes=[]; participants=[]; counts={}; missing=defaultdict(list); seen=set()
    for day in sessions:
        dm=delta_map(z,day); daily=[]
        for r in fo_rows(z,day):
            typ=r.get("OptnTp","").strip().upper()
            if r.get("TckrSymb","").strip().upper()!="NIFTY" or typ not in {"CE","PE"}: continue
            expiry=dte(r.get("XpryDt")); strike=num(r.get("StrkPric"))
            if expiry is None or strike is None: continue
            iid=(r.get("FinInstrmId") or "").strip(); key=(day,iid)
            if key in seen: raise ValueError(f"Duplicate contract key: {key}")
            seen.add(key)
            daily.append({"session_date":day,"instrument_id":iid,"symbol":"NIFTY","expiry_date":expiry,"strike":strike,"option_type":typ,"instrument_name":r.get("FinInstrmNm"),"open":num(r.get("OpnPric")),"high":num(r.get("HghPric")),"low":num(r.get("LwPric")),"close":num(r.get("ClsPric")),"last":num(r.get("LastPric")),"previous_close":num(r.get("PrvsClsgPric")),"underlying_price":num(r.get("UndrlygPric")),"settlement_price":num(r.get("SttlmPric")),"open_interest":integer(r.get("OpnIntrst")),"oi_change":integer(r.get("ChngInOpnIntrst")),"traded_volume":integer(r.get("TtlTradgVol")),"traded_value":num(r.get("TtlTrfVal")),"trade_count":integer(r.get("TtlNbOfTxsExctd")),"lot_size":integer(r.get("NewBrdLotQty")),"contract_delta_factor":dm.get((day,expiry,strike,typ))})
        daily.sort(key=lambda x:(x["expiry_date"],x["strike"],x["option_type"]))
        ce=sum(x["option_type"]=="CE" for x in daily); pe=sum(x["option_type"]=="PE" for x in daily)
        if not ce or not pe: raise ValueError(f"Missing CE or PE NIFTY contracts: {day}")
        counts[day]={"nifty_option_rows":len(daily),"ce":ce,"pe":pe}; options.extend(daily)
        raw=report(z,day,"nifty_index_close_all.csv")
        if raw is None: missing["nifty_index_close_all"].append(day)
        else:
            idx=next((x for x in readcsv(raw) if x.get("Index Name","").strip().upper()=="NIFTY 50"),None)
            if idx is None: missing["nifty_50_row"].append(day)
            else: indexes.append({"session_date":day,"open":num(idx.get("Open Index Value")),"high":num(idx.get("High Index Value")),"low":num(idx.get("Low Index Value")),"close":num(idx.get("Closing Index Value")),"points_change":num(idx.get("Points Change")),"change_pct":num(idx.get("Change(%)")),"volume":integer(idx.get("Volume")),"turnover_rs_cr":num(idx.get("Turnover (Rs. Cr.)")),"pe":num(idx.get("P/E")),"pb":num(idx.get("P/B")),"dividend_yield":num(idx.get("Div Yield"))})
        for kind,filename in (("oi","fo_participant_oi.csv"),("volume","fo_participant_volume.csv")):
            rows=participant_rows(z,day,filename)
            if not rows: missing[f"participant_{kind}"].append(day)
            for p in rows:
                participants.append({"session_date":day,"report_type":kind,"participant":p.get("Client Type"),"future_index_long":integer(p.get("Future Index Long")),"future_index_short":integer(p.get("Future Index Short")),"future_stock_long":integer(p.get("Future Stock Long")),"future_stock_short":integer(p.get("Future Stock Short")),"option_index_call_long":integer(p.get("Option Index Call Long")),"option_index_put_long":integer(p.get("Option Index Put Long")),"option_index_call_short":integer(p.get("Option Index Call Short")),"option_index_put_short":integer(p.get("Option Index Put Short")),"option_stock_call_long":integer(p.get("Option Stock Call Long")),"option_stock_put_long":integer(p.get("Option Stock Put Long")),"option_stock_call_short":integer(p.get("Option Stock Call Short")),"option_stock_put_short":integer(p.get("Option Stock Put Short")),"total_long_contracts":integer(p.get("Total Long Contracts")),"total_short_contracts":integer(p.get("Total Short Contracts"))})
    # Match exact expiry/strike/type across consecutive observations; never synthesize a baseline.
    prior={}
    for r in sorted(options,key=lambda x:(x["expiry_date"],x["strike"],x["option_type"],x["session_date"])):
        k=(r["expiry_date"],r["strike"],r["option_type"]); prev=prior.get(k)
        r["previous_close_oi"]=prev.get("open_interest") if prev else None
        r["previous_close_premium"]=prev.get("close") if prev else None
        r["premium_change_pct"]=round((r["close"]/prev["close"]-1)*100,6) if prev and prev.get("close") not in (None,0) and r.get("close") is not None else None
        r["oi_change_pct"]=round((r["open_interest"]/prev["open_interest"]-1)*100,6) if prev and prev.get("open_interest") not in (None,0) and r.get("open_interest") is not None else None
        oi=r.get("open_interest"); vol=r.get("traded_volume")
        r["volume_to_oi"]=round(vol/oi,8) if oi not in (None,0) and vol is not None else None
        prior[k]=r
    ib={x["session_date"]:x for x in indexes}; ob=defaultdict(list)
    for r in options: ob[r["session_date"]].append(r)
    pdates={(r["session_date"],r["report_type"]) for r in participants}; features=[]
    for day in sessions:
        idx=ib.get(day,{}); rows=ob[day]; ce=[r for r in rows if r["option_type"]=="CE"]; pe=[r for r in rows if r["option_type"]=="PE"]
        total=lambda rs,k:sum((r.get(k) or 0) for r in rs)
        hi,lo=idx.get("high"),idx.get("low"); close=idx.get("close")
        features.append({"session_date":day,"nifty_open":idx.get("open"),"nifty_high":hi,"nifty_low":lo,"nifty_close":close,"nifty_change_pct":idx.get("change_pct"),"nifty_range_points":round(hi-lo,4) if hi is not None and lo is not None else None,"nifty_range_pct":round((hi-lo)/close*100,6) if hi is not None and lo is not None and close else None,"nifty_option_contracts":len(rows),"ce_contracts":len(ce),"pe_contracts":len(pe),"ce_open_interest":total(ce,"open_interest"),"pe_open_interest":total(pe,"open_interest"),"ce_oi_change":total(ce,"oi_change"),"pe_oi_change":total(pe,"oi_change"),"ce_volume":total(ce,"traded_volume"),"pe_volume":total(pe,"traded_volume"),"ce_positive_oi_change_contracts":sum((r.get("oi_change") or 0)>0 for r in ce),"pe_positive_oi_change_contracts":sum((r.get("oi_change") or 0)>0 for r in pe),"participant_oi_available":(day,"oi") in pdates,"participant_volume_available":(day,"volume") in pdates})
    windows=window_rows(features)\n    writecsv(output/"nifty_options_eod.csv",OPTION_FIELDS,options); writecsv(output/"nifty_index_eod.csv",INDEX_FIELDS,indexes); writecsv(output/"participant_positioning_eod.csv",PARTICIPANT_FIELDS,participants); writecsv(output/"session_feature_summary.csv",FEATURE_FIELDS,features); writecsv(output/"window_feature_summary.csv",WINDOW_FIELDS,windows)
    manifest={"schema_version":1,"pipeline":"phase2_eod_normalization","source_archive":archive.name,"source_archive_sha256":hashlib.sha256(archive.read_bytes()).hexdigest(),"source_analysis_date":source.get("analysis_date"),"session_count":len(sessions),"sessions":sessions,"selected_expiry_in_phase1_trial":source.get("selected_expiry"),"contract_universe_source":"fo_bhavcopy","contract_master_used":False,"option_contract_rows":len(options),"index_rows":len(indexes),"participant_rows":len(participants),"session_counts":counts,"missing_reports":dict(missing),"outputs":{"nifty_options_eod.csv":len(options),"nifty_index_eod.csv":len(indexes),"participant_positioning_eod.csv":len(participants),"session_feature_summary.csv":len(features),"window_feature_summary.csv":len(windows)},"latest_prior_5_sessions":windows[-1]["prior_5_sessions_available"],"latest_prior_22_sessions":windows[-1]["prior_22_sessions_available"],"prior_22_context_complete":windows[-1]["prior_22_sessions_complete"],"phase2_normalization_complete":True,"phase2_feature_context_complete":windows[-1]["prior_22_sessions_complete"],"limitations":["Participant reports are market-wide aggregates, not NIFTY-only positions.","Delta Factor is retained as contract_delta_factor; it is not option Greek delta.","EOD snapshots cannot establish intraday sequence or trigger crossings.","Stale contract-master files are excluded.","No seller entry premium is inferred from aggregate OI."],"created_utc":datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")}
    (output/"dataset_manifest.json").write_text(json.dumps(manifest,indent=2),encoding="utf-8")
    return manifest

def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument("--archive",required=True,type=Path); p.add_argument("--output",required=True,type=Path); p.add_argument("--expected-sessions",type=int,default=22); a=p.parse_args()
    m=normalize(a.archive,a.output,a.expected_sessions)
    print(json.dumps({k:m[k] for k in ("session_count","option_contract_rows","index_rows","participant_rows","outputs","missing_reports")},indent=2))
if __name__=="__main__": main()
