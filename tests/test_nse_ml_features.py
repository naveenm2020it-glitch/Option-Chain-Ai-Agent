import io
import json
import zipfile
from datetime import date

import pytest

from scripts.nse_ml_features import extract_nifty_features


def _nested_archive(day="2026-11-12", symbol="NIFTY"):
    rows = [
        ["2026-11-12", "NIFTY", "2026-11-24", 25000, "CE", 100, 110, 1000, 100, 2000, 25000],
        ["2026-11-12", "NIFTY", "2026-11-24", 25000, "PE", 120, 130, 1200, 120, 1800, 25000],
        ["2026-11-12", "NIFTY", "2026-11-24", 25100, "CE", 90, 95, 900, 80, 1500, 25000],
        ["2026-11-12", "NIFTY", "2026-11-24", 25100, "PE", 140, 150, 1300, 150, 1600, 25000],
    ]
    headers = "TradDt,TckrSymb,XpryDt,StrkPric,OptnTp,ClsPric,PrvsClsgPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,UndrlygPric"
    csv_data = headers + "\n" + "\n".join(",".join(map(str, row)) for row in rows)
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as zf:
        zf.writestr("BhavCopy.csv", csv_data)
    outer = io.BytesIO()
    manifest = {
        "results": {
            "fo_udiff_bhavcopy": {
                "report_date": day,
                "file": "fo_bhavcopy.zip",
            }
        }
    }
    with zipfile.ZipFile(outer, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("fo_bhavcopy.zip", inner.getvalue())
    return outer.getvalue()


def test_extract_nifty_daily_features_and_contract_snapshots():
    result = extract_nifty_features(_nested_archive(), date(2026, 11, 12))
    assert result["session_date"] == "2026-11-12"
    assert result["features"]["spot_close"] == 25000
    assert result["features"]["ce_oi"] == 1900
    assert result["features"]["pe_oi"] == 2500
    assert result["features"]["near_atm_contract_count"] == 4
    assert len(result["contracts"]) == 4
    assert {row["option_type"] for row in result["contracts"]} == {"CE", "PE"}


def test_extract_rejects_mixed_session_dates():
    with pytest.raises(ValueError, match="manifest date"):
        extract_nifty_features(_nested_archive(day="2026-11-12"), date(2026, 11, 13))
