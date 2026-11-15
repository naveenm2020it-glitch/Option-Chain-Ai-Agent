import io
import json
import zipfile
from datetime import date

import pytest

from scripts.nse_ml_features import extract_nifty_features


def _nested_archive(day="2026-11-12", symbol="NIFTY"):
    rows = [
        ["2026-11-12", "NIFTY", "IDO", "2026-11-24", 25000, "CE", 100, 110, 1000, 100, 2000, 25000],
        ["2026-11-12", "NIFTY", "IDO", "2026-11-24", 25000, "PE", 120, 130, 1200, 120, 1800, 25000],
        ["2026-11-12", "NIFTY", "IDO", "2026-11-24", 25100, "CE", 90, 95, 900, 80, 1500, 25000],
        ["2026-11-12", "NIFTY", "IDO", "2026-11-24", 25100, "PE", 140, 150, 1300, 150, 1600, 25000],
        ["2026-11-12", "NIFTY", "IDF", "2026-11-24", 0, "", 25050, 25000, 5000, 100, 1000, 25000],
    ]
    headers = "TradDt,TckrSymb,FinInstrmTp,XpryDt,StrkPric,OptnTp,ClsPric,PrvsClsgPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,UndrlygPric"
    csv_data = headers + "\n" + "\n".join(",".join(map(str, row)) for row in rows)
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as zf:
        zf.writestr("BhavCopy.csv", csv_data)
    outer = io.BytesIO()
    participant_headers = "Client Type,Future Index Long,Future Index Short,Future Stock Long,Future Stock Short,Option Index Call Long,Option Index Call Short,Option Index Put Long,Option Index Put Short,Option Stock Call Long,Option Stock Call Short,Option Stock Put Long,Option Stock Put Short,Total Long Contracts,Total Short Contracts"
    participant_oi = participant_headers + "\nFII,100,200,0,0,300,250,400,350,0,0,0,0,800,800\nDII,50,40,0,0,20,10,30,20,0,0,0,0,100,100\nClient,200,100,0,0,400,300,500,400,0,0,0,0,1100,1100\nPro,70,80,0,0,60,70,90,100,0,0,0,0,220,220\nTOTAL,420,420,0,0,780,630,1020,870,0,0,0,0,2220,2220"
    participant_volume = participant_headers + "\nFII,10,20,0,0,30,25,40,35,0,0,0,0,80,80\nDII,5,4,0,0,2,1,3,2,0,0,0,0,10,10\nClient,20,10,0,0,40,30,50,40,0,0,0,0,110,110\nPro,7,8,0,0,6,7,9,10,0,0,0,0,22,22\nTOTAL,42,42,0,0,78,63,102,87,0,0,0,0,222,222"
    index_csv = "Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,Closing Index Value,Points Change,Change(%)\nNIFTY 50,12-Nov-2026,24900,25100,24850,25000,100,0.4\nINDIA VIX,12-Nov-2026,13,14,12,13.5,0.5,3.8"
    delta_csv = "Date,Symbol,Expiry Day,Strike Price,Option Type,Delta Factor\n2026-11-12,NIFTY,24-Nov-2026,25000,CE,0.52\n2026-11-12,NIFTY,24-Nov-2026,25100,CE,0.45\n2026-11-12,NIFTY,24-Nov-2026,25000,PE,-0.48\n2026-11-12,NIFTY,24-Nov-2026,25100,PE,-0.55"
    manifest = {
        "results": {
            "fo_udiff_bhavcopy": {"report_date": day, "file": "fo_bhavcopy.zip"},
            "fo_participant_oi": {"report_date": day, "file": "participant_oi.csv"},
            "fo_participant_volume": {"report_date": day, "file": "participant_volume.csv"},
            "nifty_index_close_all": {"report_date": day, "file": "index_close.csv"},
            "fo_contract_delta": {"report_date": day, "file": "contract_delta.csv"}
        }
    }
    with zipfile.ZipFile(outer, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("fo_bhavcopy.zip", inner.getvalue())
        zf.writestr("participant_oi.csv", participant_oi)
        zf.writestr("participant_volume.csv", participant_volume)
        zf.writestr("index_close.csv", index_csv)
        zf.writestr("contract_delta.csv", delta_csv)
    return outer.getvalue()


def test_extract_nifty_daily_features_and_contract_snapshots():
    result = extract_nifty_features(_nested_archive(), date(2026, 11, 12))
    assert result["session_date"] == "2026-11-12"
    assert result["features"]["spot_close"] == 25000
    assert result["features"]["ce_oi"] == 1900
    assert result["features"]["pe_oi"] == 2500
    assert result["features"]["near_atm_contract_count"] == 4
    assert result["features"]["future_data_available"] == 1
    assert result["features"]["future_return"] == pytest.approx(0.002)
    assert result["features"]["future_basis_pct"] == pytest.approx(0.002)
    assert result["features"]["participant_oi_data_available"] == 1
    assert result["features"]["participant_oi_fii_future_index_net"] == -100
    assert result["features"]["participant_volume_fii_future_index_net"] == -10
    assert result["features"]["india_vix_close"] == pytest.approx(13.5)
    assert result["features"]["spot_high"] == pytest.approx(25100)
    assert result["features"]["spot_low"] == pytest.approx(24850)
    assert result["features"]["delta_data_available"] == 1
    assert result["features"]["delta_match_coverage"] == pytest.approx(1.0)
    assert result["features"]["ce_delta_equiv_oi"] == pytest.approx(925)
    assert result["features"]["pe_delta_equiv_oi"] == pytest.approx(-1291)
    assert result["features"]["net_option_delta_equiv_oi"] == pytest.approx(-366)
    assert result["contracts"][0]["delta_factor"] == pytest.approx(0.52)
    assert len(result["contracts"]) == 4
    assert {row["option_type"] for row in result["contracts"]} == {"CE", "PE"}


def test_extract_rejects_mixed_session_dates():
    with pytest.raises(ValueError, match="manifest date"):
        extract_nifty_features(_nested_archive(day="2026-11-12"), date(2026, 11, 13))
