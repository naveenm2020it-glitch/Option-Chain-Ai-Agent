from nifty_ai.agent_registry import AGENT_COUNT, SPECIALIST_COUNT, public_agent_registry
from nifty_ai.data_quality import report_integrity_summary


def test_agent_registry_is_the_single_count_source():
    registry = public_agent_registry()
    assert SPECIALIST_COUNT == 12
    assert AGENT_COUNT == 13
    assert sum(item["role"] == "specialist" for item in registry) == SPECIALIST_COUNT
    assert sum(item["role"] == "consensus" for item in registry) == 1
    assert registry[-1]["name"] == "Final Consensus"


def test_report_integrity_flags_missing_reports_duplicate_and_date_mismatch():
    reports = [
        {"report_type": "nifty50", "trading_date": "2026-11-10",
         "original_name": "nifty-a.csv", "columns": [
             "Index Date", "Open Index Value", "High Index Value",
             "Low Index Value", "Closing Index Value"]},
        {"report_type": "nifty50", "trading_date": "2026-11-10",
         "original_name": "nifty-b.csv", "columns": [
             "Index Date", "Open Index Value", "High Index Value",
             "Low Index Value", "Closing Index Value"]},
        {"report_type": "fo_bhavcopy", "trading_date": "2026-11-09",
         "original_name": "fo.csv", "columns": ["TckrSymb", "OptnTp", "StrkPric"]},
    ]
    result = report_integrity_summary(reports, "2026-11-10")
    assert result["duplicate_type_dates"] == [
        {"report_type": "nifty50", "trading_date": "2026-11-10", "file_count": 2}
    ]
    assert result["date_mismatches"][0]["report_type"] == "fo_bhavcopy"
    assert "india_vix" in result["missing_report_types"]
    assert result["schema_checks"]["fo_bhavcopy"]["status"] == "PARTIAL"
    assert result["status"] == "CHECK_REQUIRED"


def test_report_integrity_does_not_mark_missing_schema_ready():
    result = report_integrity_summary([
        {"report_type": "fo_bhavcopy", "trading_date": "2026-11-10",
         "original_name": "fo.csv", "columns": ["TckrSymb", "StrkPric"]}
    ], "2026-11-10")
    assert result["schema_checks"]["fo_bhavcopy"]["status"] == "PARTIAL"
    assert "OpnIntrst/Open Interest" in result["schema_checks"]["fo_bhavcopy"]["missing_column_groups"]
