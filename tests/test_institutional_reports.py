import pandas as pd
import pytest

from nifty_ai import analytics
from nifty_ai import eod_intelligence


def test_participant_volume_is_exposed_as_activity_not_position(monkeypatch):
    frame = pd.DataFrame({
        "Client Type": ["FII", "DII", "Client", "Pro", "TOTAL"],
        "Total Long Contracts": [100, 20, 80, 30, 230],
        "Total Short Contracts": [120, 15, 75, 40, 250],
    })
    monkeypatch.setattr(analytics, "load_tables", lambda _: [("volume", frame)])
    report = {
        "report_type": "participant_volume",
        "file_path": "unused.csv",
        "row_count": 5,
    }
    summary = analytics.extract_participant_summary([report])
    volume = summary["sources"]["participant_volume"]
    fii = next(row for row in volume["participant_totals"] if row["participant"] == "FII")
    assert fii["total_long_contracts"] == 100
    assert fii["total_short_contracts"] == 120
    assert fii["net_traded_contracts"] == -20
    assert "not open interest" in volume["interpretation"]


def test_fii_derivatives_summary_extracts_nifty_futures_buy_sell_and_oi(monkeypatch):
    frame = pd.DataFrame({
        "('Unnamed: 0_level_0', 'Unnamed: 0_level_1')": ["NIFTY FUTURES", "NIFTY OPTIONS"],
        "('BUY', 'No. of contracts')": [11298, 4645727],
        "('BUY', 'Amt in Crores')": [1678.82, 694748.98],
        "('SELL', 'No. of contracts')": [27832, 4672979],
        "('SELL', 'Amt in Crores')": [4130.47, 698633.50],
        "('OPEN INTEREST AT THE END OF THE DAY', 'No. of contracts')": [262207, 2794581],
        "('OPEN INTEREST AT THE END OF THE DAY', 'Amt in Crores')": [38724.10, 418251.52],
    })
    monkeypatch.setattr(analytics, "load_tables", lambda _: [("sheet1", frame)])
    report = {
        "report_type": "fii_derivatives",
        "file_path": "unused.xls",
        "row_count": 2,
    }
    summary = analytics.extract_participant_summary([report])
    instruments = summary["sources"]["fii_derivatives"]["nifty_instruments"]
    futures = next(row for row in instruments if row["instrument"] == "NIFTY FUTURES")
    assert futures["net_traded_contracts"] == -16534
    assert futures["open_interest_contracts"] == 262207
    assert futures["net_traded_value_crores"] == pytest.approx(-2451.65)


def test_fii_derivatives_net_flow_is_used_as_separate_institutional_evidence(monkeypatch):
    frame = pd.DataFrame({
        "('Unnamed: 0_level_0', 'Unnamed: 0_level_1')": ["NIFTY FUTURES"],
        "('BUY', 'No. of contracts')": [11298],
        "('SELL', 'No. of contracts')": [27832],
    })
    monkeypatch.setattr(eod_intelligence, "load_tables", lambda _: [("sheet1", frame)])
    result = eod_intelligence._participant_index_net([{
        "report_type": "fii_derivatives",
        "file_path": "unused.xls",
        "original_name": "fii_stats_09-Nov-2026.xls",
    }])
    assert result["available"] is True
    assert result["net"] == -16534
    assert result["rows"][0]["metric"] == "fii_nifty_futures_net_traded_contracts"


def test_participant_volume_alone_does_not_create_directional_bias():
    report = {
        "report_type": "participant_volume",
        "row_count": 5,
        "original_name": "fao_participant_vol_09112026.csv",
        "columns": ["Client Type", "Total Long Contracts", "Total Short Contracts"],
    }
    participant = {
        "sources": {
            "participant_volume": {
                "participant_totals": [{"participant": "FII", "total_long_contracts": 100, "total_short_contracts": 120}]
            }
        }
    }
    result = eod_intelligence._institutional_positioning(
        [report], {"available": False, "bias": "UNAVAILABLE"}, participant, {"available": False}
    )
    assert result["status"] == "partial"
    assert result["bias"] == "NEUTRAL"
    assert result["score"] == 0
    assert result["metrics"]["participant_volume_available"] is True
