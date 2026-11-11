import pandas as pd

from nifty_ai import analytics, eod_intelligence
from nifty_ai.institutional import summarize_cash_flow_trend


def test_cash_flow_single_session_is_not_accumulation_or_distribution():
    result = summarize_cash_flow_trend([
        {"category": "FII", "net": 1250.0, "trading_date": "2026-11-09"}
    ])
    assert result["FII"]["status"] == "INSUFFICIENT HISTORY"
    assert result["FII"]["classification"] is None
    assert result["FII"]["sessions"] == 1


def test_cash_flow_trend_requires_three_distinct_dated_sessions():
    rows = [
        {"category": "FII", "net": 100.0, "trading_date": "2026-11-04"},
        {"category": "FII", "net": 250.0, "trading_date": "2026-11-05"},
        {"category": "FII", "net": -50.0, "trading_date": "2026-11-06"},
        {"category": "DII", "net": -80.0, "trading_date": "2026-11-04"},
        {"category": "DII", "net": -40.0, "trading_date": "2026-11-05"},
        {"category": "DII", "net": 10.0, "trading_date": "2026-11-06"},
    ]
    result = summarize_cash_flow_trend(rows)
    assert result["FII"]["classification"] == "NET_BUYING_TREND"
    assert result["FII"]["net_total"] == 300.0
    assert result["DII"]["classification"] == "NET_SELLING_TREND"


def test_duplicate_same_day_rows_do_not_fake_extra_sessions():
    rows = [
        {"category": "FII", "net": 100.0, "trading_date": "2026-11-09"},
        {"category": "FII", "net": 150.0, "trading_date": "2026-11-09"},
    ]
    result = summarize_cash_flow_trend(rows)
    assert result["FII"]["sessions"] == 1
    assert result["FII"]["classification"] is None


def test_single_day_cash_flow_does_not_add_institutional_directional_score(monkeypatch):
    frame = pd.DataFrame({
        "Category": ["FII", "DII"],
        "Buy Value": [1000.0, 1200.0],
        "Sell Value": [1500.0, 800.0],
        "Net Value": [-500.0, 400.0],
    })
    monkeypatch.setattr(analytics, "load_tables", lambda _: [("cash", frame)])
    flow = analytics.extract_fii_dii([{
        "report_type": "fii_dii", "file_path": "unused.csv",
        "trading_date": "2026-11-09", "original_name": "cash.csv"
    }])
    assert flow["available"] is True
    assert flow["cash_flow_trend"]["FII"]["classification"] is None
    result = eod_intelligence._institutional_positioning(
        [{"report_type": "fii_dii", "row_count": 2, "original_name": "cash.csv"}],
        flow, {"sources": {}}, {"available": False}
    )
    assert result["score"] == 0
    assert result["bias"] == "NEUTRAL"
    assert any("fewer than three dated sessions" in item for item in result["evidence"])
