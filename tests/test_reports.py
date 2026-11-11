from nifty_ai.reports import identify_report


def test_identifies_fo_bhavcopy():
    assert identify_report("BhavCopy_NSE_FO_0_0_0_20261110_F_0000.csv.zip") == "fo_bhavcopy"


def test_identifies_participant_oi():
    assert identify_report("F&O-Participant wise Open Interest (csv).csv") == "participant_oi"


def test_identifies_fii_stats():
    assert identify_report("fii_stats_10-Nov-2026.xls") == "fii_derivatives"


def test_identifies_volatility_types():
    assert identify_report("FOVOLT_10112026.csv") == "fo_volatility"
    assert identify_report("CMVOLT_10112026.csv") == "cm_volatility"
