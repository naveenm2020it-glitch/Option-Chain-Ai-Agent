import pandas as pd

from app import REPORT_TYPES, classify_report, index_analysis, options_analysis


def test_has_twelve_expected_report_types():
    assert len(REPORT_TYPES) == 12
    assert "fo_bhavcopy" in REPORT_TYPES


def test_classifies_fno_reports():
    assert classify_report("F&O-UDiFF Common Bhavcopy Final.zip") == "fo_bhavcopy"
    assert classify_report("F&O-Participant wise Open Interest.csv") == "participant_oi"
    assert classify_report("F&O-Daily Settlement Prices.csv") == "fo_settlement"
    assert classify_report("F&O-Daily Volatility.csv") == "fo_volatility"


def test_index_pivot_calculation_uses_latest_date():
    frame = pd.DataFrame([
        {"Date": "10-Nov-2026", "Open": "25000", "High": "25100", "Low": "24900", "Close": "25050"},
        {"Date": "11-Nov-2026", "Open": "25050", "High": "25200", "Low": "25000", "Close": "25150"},
    ])
    result = index_analysis([("nifty.csv", "nifty50", frame)])
    assert result["status"] == "valid"
    assert result["close"] == 25150.0
    assert result["levels"]["PIVOT"] == (25200 + 25000 + 25150) / 3
    assert result["levels"]["R1"] == 2 * result["levels"]["PIVOT"] - 25000


def test_option_analytics_maps_udiff_style_columns():
    frame = pd.DataFrame([
        {"TckrSymb":"NIFTY","XpryDt":"26-Nov-2026","StrkPric":"25000","OptnTp":"CE","OpnIntrst":"1000"},
        {"TckrSymb":"NIFTY","XpryDt":"26-Nov-2026","StrkPric":"25000","OptnTp":"PE","OpnIntrst":"1500"},
        {"TckrSymb":"NIFTY","XpryDt":"26-Nov-2026","StrkPric":"25100","OptnTp":"CE","OpnIntrst":"1200"},
        {"TckrSymb":"NIFTY","XpryDt":"26-Nov-2026","StrkPric":"25100","OptnTp":"PE","OpnIntrst":"1400"},
    ])
    result = options_analysis([("fo.csv","fo_bhavcopy",frame)])
    assert result["status"] == "valid"
    assert result["pcr"] == (1500 + 1400) / (1000 + 1200)
