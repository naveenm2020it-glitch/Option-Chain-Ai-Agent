import json
from pathlib import Path

from nifty_ai import analytics, eod_intelligence

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "data" / "nse_reports"


def _latest_report(report_type, folder, extension):
    manifest = json.loads((ARCHIVE / "manifest.json").read_text(encoding="utf-8"))
    dataset = manifest["datasets"][report_type]
    latest_date = dataset["latest_available_date"]
    record = dataset["records"][latest_date]
    path = ROOT / record["filename"]
    assert path.exists()
    return {
        "report_type": report_type,
        "trading_date": latest_date,
        "file_path": str(path),
        "original_name": path.name,
        "row_count": record["rows"],
        "columns": record["columns"],
    }


def test_real_archived_nse_reports_parse_into_institutional_metrics():
    volume = _latest_report("participant_volume", "participant_volume", ".csv")
    fii = _latest_report("fii_derivatives", "fii_derivatives", ".xls")
    reports = [volume, fii]

    participant_summary = analytics.extract_participant_summary(reports)
    volume_data = participant_summary["sources"]["participant_volume"]
    fii_data = participant_summary["sources"]["fii_derivatives"]

    assert any(row["participant"].upper() == "FII" for row in volume_data["participant_totals"])
    nifty_futures = next(row for row in fii_data["nifty_instruments"] if row["instrument"] == "NIFTY FUTURES")
    assert nifty_futures["buy_contracts"] is not None
    assert nifty_futures["sell_contracts"] is not None
    assert nifty_futures["open_interest_contracts"] is not None

    position = eod_intelligence._participant_index_net(reports)
    assert position["available"] is True
    assert any(row["metric"] == "fii_nifty_futures_net_traded_contracts" for row in position["rows"])
