from datetime import date

import pytest

from scripts import nse_extended_eod_collector as collector


def test_csv_report_validation_accepts_tabular_payload():
    rows, columns = collector._validate("participant_oi", b"Client Type,Future Index Long,Future Index Short\nFII,10,5\n")
    assert rows == 1
    assert "Client Type" in columns


def test_csv_report_validation_rejects_html_error_page():
    with pytest.raises(ValueError, match="HTML"):
        collector._validate("fo_settlement", b"<html><body>Access denied</body></html>")


def test_mto_validation_rejects_empty_or_single_line_payload():
    with pytest.raises(ValueError):
        collector._validate("cm_delivery", b"only one line with no rows\n")


def test_report_discovery_uses_official_api_filename_and_path():
    discovered = {
        "2026-11-11": [{
            "display": "F&O-Daily Settlement Prices (csv)",
            "url": "https://nsearchives.nseindia.com/archives/nsccl/sett/FOSett_prce_11112026.csv",
            "filename": "FOSett_prce_11112026.csv",
            "api": "https://www.nseindia.com/api/daily-reports?key=FO",
        }]
    }
    result = collector._match_discovered("fo_settlement", date(2026, 11, 11), discovered)
    assert result is not None
    assert result[1] == "FOSett_prce_11112026.csv"
    assert result[0].startswith("https://nsearchives.nseindia.com/")
