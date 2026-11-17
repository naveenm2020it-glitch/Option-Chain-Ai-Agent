import csv,hashlib,io,json,zipfile
from datetime import date
from system1.pipeline import build_package,previous_sessions
def make_zip(headers,rows):
    s=io.StringIO(); w=csv.writer(s); w.writerow(headers); w.writerows(rows)
    b=io.BytesIO()
    with zipfile.ZipFile(b,"w",zipfile.ZIP_DEFLATED) as z: z.writestr("sample.csv",s.getvalue())
    return b.getvalue()
def test_previous_sessions_obeys_holiday_and_special_session():
    c={"window_start":"2026-01-01","window_end":"2026-01-31","holidays":["2026-01-01"],"special_trading_sessions":["2026-01-11"]}
    assert previous_sessions(date(2026,1,12),4,c)==["2026-01-12","2026-01-11","2026-01-09","2026-01-08"]
def test_single_date_package_parses_nifty_ce_pe_and_index(tmp_path):
    day="2026-01-07"; d=tmp_path/day; d.mkdir()
    fo=make_zip(["TckrSymb","FinInstrmTp","OptnTp","XpryDt","StrkPric","ClsPric","OpnIntrst","ChngInOpnIntrst","TtlTradgVol"],[["NIFTY","OPTIDX","CE","2026-01-29","25000","120","1000","100","500"],["NIFTY","OPTIDX","PE","2026-01-29","25000","110","1200","-50","600"],["BANKNIFTY","OPTIDX","CE","2026-01-29","50000","200","10","1","20"]])
    idx=b"Index Name,Closing Index Value\nNIFTY 50,25500.25\nINDIA VIX,14.2\n"
    (d/"fo.zip").write_bytes(fo); (d/"index.csv").write_bytes(idx)
    m={"requested_end_date":day,"core_session_date":day,"results":{"fo_udiff_bhavcopy":{"file":"fo.zip","report_date":day,"sha256":hashlib.sha256(fo).hexdigest()},"nifty_index_close_all":{"file":"index.csv","report_date":day,"sha256":hashlib.sha256(idx).hexdigest()}}}
    (d/"manifest.json").write_text(json.dumps(m))
    cal=tmp_path/"calendar.json"; cal.write_text(json.dumps({"window_start":day,"window_end":day,"holidays":[],"special_trading_sessions":[]}))
    result=build_package(tmp_path,date.fromisoformat(day),cal,1); selected=result["sessions"][day]
    assert result["status"]=="ready"
    assert selected["reports"]["fo_udiff_bhavcopy"]["valid_contracts"]==2
    assert {r["option_type"] for r in selected["reports"]["fo_udiff_bhavcopy"]["rows"]}=={"CE","PE"}
    assert selected["reports"]["nifty_index_close_all"]["india_vix_present"]
def test_missing_core_source_blocks_no_zero_fill(tmp_path):
    day="2026-01-07"; d=tmp_path/day; d.mkdir(); (d/"manifest.json").write_text(json.dumps({"results":{}}))
    cal=tmp_path/"calendar.json"; cal.write_text(json.dumps({"window_start":day,"window_end":day,"holidays":[],"special_trading_sessions":[]}))
    result=build_package(tmp_path,date.fromisoformat(day),cal,1)
    assert result["status"]=="blocked" and "fo_udiff_bhavcopy: missing" in result["sessions"][day]["issues"]
