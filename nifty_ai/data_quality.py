"""Report-level integrity checks for EOD-only analysis."""
from collections import Counter

from .reports import REPORTS

# Minimum columns needed to make the corresponding core feature meaningful.
REQUIRED_COLUMNS = {
    "nifty50": (("Index Date", "IndexDate", "Date"), ("Open Index Value", "Open"), 
                ("High Index Value", "High"), ("Low Index Value", "Low"),
                ("Closing Index Value", "Close")),
    "india_vix": (("Date", "Index Date", "IndexDate"), ("Close", "Closing Index Value")),
    "fo_bhavcopy": (("TckrSymb", "Symbol"), ("OptnTp", "Option Type"),
                    ("StrkPric", "Strike Price"), ("OpnIntrst", "Open Interest")),
    "fii_dii": (("Date", "Date1", "Trading Date"),),
    "participant_oi": (("Client Type", "ClientType"),),
    "participant_volume": (("Client Type", "ClientType"),),
}


def _normal(value):
    return "".join(ch.lower() for ch in str(value) if ch.isalnum())


def _missing_columns(columns, groups):
    available = {_normal(value) for value in (columns or [])}
    return ["/".join(group) for group in groups
            if not any(_normal(candidate) in available for candidate in group)]


def report_integrity_summary(reports, trading_date=None):
    """Summarise date alignment, duplicate report types and schema coverage.

    This does not infer data from absent reports and does not mark a report READY
    merely because its file exists.
    """
    rows = list(reports or [])
    by_type_date = Counter(
        (item.get("report_type"), item.get("trading_date"))
        for item in rows if item.get("report_type") and item.get("trading_date")
    )
    duplicates = [
        {"report_type": key[0], "trading_date": key[1], "file_count": count}
        for key, count in sorted(by_type_date.items()) if count > 1
    ]
    misaligned = [
        {"name": item.get("original_name"), "report_type": item.get("report_type"),
         "trading_date": item.get("trading_date")}
        for item in rows if trading_date and item.get("trading_date")
        and item.get("trading_date") != trading_date
    ]
    available = {item.get("report_type") for item in rows}
    schema = {}
    for report_type, required_groups in REQUIRED_COLUMNS.items():
        candidates = [item for item in rows if item.get("report_type") == report_type]
        columns = []
        for item in candidates:
            columns.extend(item.get("columns") or [])
        missing = _missing_columns(columns, required_groups)
        schema[report_type] = {
            "status": "READY" if candidates and not missing else
                      "PARTIAL" if candidates else "INSUFFICIENT DATA",
            "files": len(candidates),
            "missing_column_groups": missing,
        }
    missing_reports = [key for key in REPORTS if key not in available]
    warnings = []
    if duplicates:
        warnings.append("Multiple files exist for the same report type and trading date; verify versions before analysis.")
    if misaligned:
        warnings.append("One or more report trading dates do not match the selected analysis date.")
    if missing_reports:
        warnings.append("Some of the 12 NSE EOD report types are not available for this analysis date.")
    if schema and any(item["status"] != "READY" for item in schema.values()):
        warnings.append("One or more core report schemas are incomplete or unavailable.")
    return {
        "trading_date": trading_date,
        "report_files": len(rows),
        "available_report_types": len(available),
        "expected_report_types": len(REPORTS),
        "missing_report_types": missing_reports,
        "duplicate_type_dates": duplicates,
        "date_mismatches": misaligned,
        "schema_checks": schema,
        "status": "CHECK_REQUIRED" if duplicates or misaligned or missing_reports else "REVIEW_SCHEMA",
        "warnings": warnings,
        "policy": "EOD_REPORTS_ONLY_NO_INTRADAY_CANDLES",
    }
