import io
import zipfile
import pandas as pd

from nifty_ai.reports import identify_report, infer_report_type, load_tables


def test_identifies_fo_bhavcopy():
    assert identify_report("BhavCopy_NSE_FO_0_0_0_20261110_F_0000.csv.zip") == "fo_bhavcopy"


def test_identifies_participant_oi():
    assert identify_report("F&O-Participant wise Open Interest (csv).csv") == "participant_oi"


def test_identifies_fii_stats():
    assert identify_report("fii_stats_10-Nov-2026.xls") == "fii_derivatives"


def test_identifies_volatility_types():
    assert identify_report("FOVOLT_10112026.csv") == "fo_volatility"
    assert identify_report("CMVOLT_10112026.csv") == "cm_volatility"


def test_detects_fo_bhavcopy_from_udiff_columns_without_filename():
    frame = pd.DataFrame(columns=["BizDt", "TckrSymb", "OptnTp", "StrkPric", "OpnIntrst", "ChngInOpnIntrst"])
    assert infer_report_type("download.zip", [frame]) == "fo_bhavcopy"


def test_reads_nse_fo_bhavcopy_zip():
    csv = (
        "BizDt,TckrSymb,OptnTp,StrkPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,ClsPric,XpryDt\\n"
        "20261110,NIFTY,CE,26000,1000,100,500,120,20261117\\n"
        "20261110,NIFTY,PE,26000,1200,150,600,130,20261117\\n"
    ).encode("utf-8")
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("BhavCopy_NSE_FO_20261110.csv", csv)
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("daily_bhavcopy.zip", inner.getvalue())
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "download.zip"
        p.write_bytes(outer.getvalue())
        tables = load_tables(str(p))
        assert tables
        assert "OptnTp" in tables[0][1].columns
        assert len(tables[0][1]) == 2
