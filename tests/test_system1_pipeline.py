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
    fo=make_zip(["TckrSymb","FinInstrmTp","OptnTp","TradDt","XpryDt","StrkPric","ClsPric","OpnIntrst","ChngInOpnIntrst","TtlTradgVol"],[["NIFTY","OPTIDX","CE","2026-01-07","2026-01-29","25000","120","1000","100","500"],["NIFTY","OPTIDX","PE","2026-01-07","2026-01-29","25000","110","1200","-50","600"],["BANKNIFTY","OPTIDX","CE","2026-01-07","2026-01-29","50000","200","10","1","20"]])
    idx=b"Index Name,Index Date,Closing Index Value\nNIFTY 50,07-01-2026,25500.25\nINDIA VIX,07-01-2026,14.2\n"
    (d/"fo.zip").write_bytes(fo); (d/"index.csv").write_bytes(idx)
    m={"requested_end_date":day,"core_session_date":day,"results":{"fo_udiff_bhavcopy":{"file":"fo.zip","report_date":day,"sha256":hashlib.sha256(fo).hexdigest()},"nifty_index_close_all":{"file":"index.csv","report_date":day,"sha256":hashlib.sha256(idx).hexdigest()}}}
    (d/"manifest.json").write_text(json.dumps(m))
    cal=tmp_path/"calendar.json"; cal.write_text(json.dumps({"window_start":day,"window_end":day,"holidays":[],"special_trading_sessions":[]}))
    result=build_package(tmp_path,date.fromisoformat(day),cal,1); selected=result["sessions"][day]
    assert result["status"]=="ready"
    assert selected["reports"]["fo_udiff_bhavcopy"]["valid_contracts"]==2
    assert {r["option_type"] for r in selected["reports"]["fo_udiff_bhavcopy"]["contracts"]}=={"CE","PE"}
    assert selected["reports"]["nifty_index_close_all"]["india_vix_present"]
def test_missing_core_source_blocks_no_zero_fill(tmp_path):
    day="2026-01-07"; d=tmp_path/day; d.mkdir(); (d/"manifest.json").write_text(json.dumps({"results":{}}))
    cal=tmp_path/"calendar.json"; cal.write_text(json.dumps({"window_start":day,"window_end":day,"holidays":[],"special_trading_sessions":[]}))
    result=build_package(tmp_path,date.fromisoformat(day),cal,1)
    assert result["status"]=="blocked" and "fo_udiff_bhavcopy: missing" in result["sessions"][day]["issues"]


def test_deep_validator_accepts_real_tab_delimiter():
    from scripts.system1_deep_data_validation import inspect_csv

    payload = "Report\tTrade Date\tValue\nNIFTY\t17-12-2025\t25000\n".encode()
    result = inspect_csv(payload, "tab_fixture")
    assert result["rows"] == 1
    assert result["detected_delimiter"] == "tab"
    assert result["observed_dates"] == ["2025-12-17"]


def test_deep_validator_accepts_comma_delimiter():
    from scripts.system1_deep_data_validation import inspect_csv

    payload = b"Report,Trade Date,Value\nNIFTY,17-12-2025,25000\n"
    result = inspect_csv(payload, "comma_fixture")
    assert result["rows"] == 1
    assert result["detected_delimiter"] == ","


def make_market_activity_zip(day="2025-12-17", include_options=True, empty_futures=False):
    import io
    import zipfile

    dmy = date.fromisoformat(day).strftime("%d%m%Y")
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED) as archive:
        futures_rows = [] if empty_futures else [["NIFTY", "2025-12-17", "25000", "12"]]
        futures = io.StringIO()
        writer = csv.writer(futures)
        writer.writerow(["Instrument", "Trade Date", "Value", "Volume"])
        writer.writerows(futures_rows)
        archive.writestr(f"futidx{dmy}.csv", futures.getvalue())

        if include_options:
            options = io.StringIO()
            writer = csv.writer(options)
            writer.writerow(["Instrument", "Trade Date", "Value", "Volume"])
            writer.writerow(["NIFTY", "2025-12-17", "25000", "100"])
            archive.writestr(f"optidx{dmy}.csv", options.getvalue())

        # NSE's optional VIX-futures member may contain a title and header only.
        archive.writestr(
            f"futivx{dmy}.csv",
            "NSE Futures VIX Report\\nInstrument,Trade Date,Value\\n",
        )
        # NSE's help/legend file is documentation, not a tabular data source.
        archive.writestr("fohelp.txt", "FO report help\\nField definitions\\nNotes without columns")
    return payload.getvalue()


def test_market_activity_zip_accepts_header_only_optional_futivx():
    from scripts.system1_deep_data_validation import inspect_payload

    result = inspect_payload(
        "fo_market_activity",
        make_market_activity_zip(),
        "zip",
        "2025-12-17",
    )
    assert result["rows"] == 2
    assert result["empty_optional_members"] == ["futivx17122025.csv"]
    assert result["metadata_members"] == ["fohelp.txt"]
    statuses = {m["member"].split("/")[-1]: m.get("status") for m in result["members"]}
    assert statuses["futivx17122025.csv"] == "empty_optional"
    assert statuses["futidx17122025.csv"] == "content_validated"
    assert statuses["optidx17122025.csv"] == "content_validated"


def test_market_activity_zip_fails_when_mandatory_options_member_missing():
    import pytest
    from scripts.system1_deep_data_validation import inspect_payload

    with pytest.raises(ValueError, match="missing mandatory.*optidx"):
        inspect_payload(
            "fo_market_activity",
            make_market_activity_zip(include_options=False),
            "zip",
            "2025-12-17",
        )


def test_market_activity_zip_fails_when_mandatory_futures_member_has_no_rows():
    import pytest
    from scripts.system1_deep_data_validation import inspect_payload

    with pytest.raises(ValueError, match="mandatory member futidx.*no data rows"):
        inspect_payload(
            "fo_market_activity",
            make_market_activity_zip(empty_futures=True),
            "zip",
            "2025-12-17",
        )


def test_option_normalizer_blocks_wrong_session_date(tmp_path):
    import pytest
    from system1.pipeline import _normalise_options
    rows=[{"TckrSymb":"NIFTY","OptnTp":"CE","TradDt":"2026-01-06","XpryDt":"2026-01-29","StrkPric":"25000","ClsPric":"100","OpnIntrst":"10","ChngInOpnIntrst":"1","TtlTradgVol":"5"}]
    with pytest.raises(ValueError, match="mismatched trade date"):
        _normalise_options(rows,"2026-01-07")


def test_option_normalizer_blocks_invalid_expiry_and_negative_oi():
    import pytest
    from system1.pipeline import _normalise_options
    base={"TckrSymb":"NIFTY","OptnTp":"CE","TradDt":"2026-01-07","XpryDt":"bad-date","StrkPric":"25000","ClsPric":"100","OpnIntrst":"10","ChngInOpnIntrst":"1","TtlTradgVol":"5"}
    with pytest.raises(ValueError, match="invalid NIFTY option rows"):
        _normalise_options([base],"2026-01-07")
    base["XpryDt"]="2026-01-29"
    base["OpnIntrst"]="-10"
    with pytest.raises(ValueError, match="invalid NIFTY option rows"):
        _normalise_options([base],"2026-01-07")


def test_option_normalizer_canonicalizes_expiry_and_counts_both_sides():
    from system1.pipeline import _normalise_options
    base={"TckrSymb":"NIFTY","OptnTp":"CE","TradDt":"07-Jan-2026","XpryDt":"29-Jan-2026","StrkPric":"25000","ClsPric":"100","OpnIntrst":"10","ChngInOpnIntrst":"1","TtlTradgVol":"5"}
    pe={**base,"OptnTp":"PE"}
    result=_normalise_options([base,pe],"2026-01-07")
    assert result["ce_contracts"]==1 and result["pe_contracts"]==1
    assert result["expiries"]==["2026-01-29"]


def test_normalized_package_audit_requires_both_ce_and_pe_and_valid_expiry():
    from scripts.system1_deep_data_validation import audit_normalized_package
    package={"window_sessions":1,"sessions":{"2026-01-07":{"status":"ready","reports":{"fo_udiff_bhavcopy":{"contracts":[
        {"underlying":"NIFTY","option_type":"CE","expiry":"2026-01-29","strike":25000.0,"close":100.0,"oi":10.0,"change_oi":1.0,"volume":5.0}
    ],"duplicate_contract_keys":0}}}}}
    assert audit_normalized_package(package)["status"]=="FAIL"
    package["sessions"]["2026-01-07"]["reports"]["fo_udiff_bhavcopy"]["contracts"].append(
        {"underlying":"NIFTY","option_type":"PE","expiry":"2026-01-29","strike":25000.0,"close":100.0,"oi":10.0,"change_oi":1.0,"volume":5.0}
    )
    assert audit_normalized_package(package)["status"]=="PASS"


def test_frozen_package_fingerprint_ignores_generation_timestamp_only():
    from scripts.system1_deep_data_validation import frozen_package_fingerprint
    a={"analysis_date":"2026-01-07","generated_at_utc":"2026-01-07T10:00:00Z","sessions":{"x":{"status":"ready"}}}
    b={**a,"generated_at_utc":"2026-01-07T10:01:00Z"}
    assert frozen_package_fingerprint(a)==frozen_package_fingerprint(b)
    b["sessions"]={"x":{"status":"blocked"}}
    assert frozen_package_fingerprint(a)!=frozen_package_fingerprint(b)
