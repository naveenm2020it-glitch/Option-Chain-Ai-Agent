from __future__ import annotations

from collections import Counter
from typing import Any

# These reports must describe the same exchange session before an EOD
# NIFTY derivatives view can be calculated. Ancillary reports may legitimately
# publish on a different schedule and are displayed separately.
CORE_REPORTS = (
    "fo_udiff_bhavcopy",
    "fo_participant_oi",
    "fo_participant_volume",
    "fo_market_activity",
)


def assess_manifest(manifest: dict[str, Any] | None) -> dict[str, Any]:
    """Fail closed when core files are absent, invalid, or from mixed dates."""
    if not isinstance(manifest, dict) or not isinstance(manifest.get("results"), dict):
        return {
            "status": "blocked",
            "aligned": False,
            "session_date": None,
            "core_reports": {},
            "issues": ["NSE manifest or results section is missing."],
        }

    results = manifest["results"]
    core: dict[str, dict[str, Any]] = {}
    issues: list[str] = []
    dates: list[str] = []

    for name in CORE_REPORTS:
        item = results.get(name)
        if not isinstance(item, dict):
            core[name] = {"status": "missing", "report_date": None}
            issues.append(f"{name}: missing from manifest.")
            continue

        report_date = item.get("report_date")
        status = item.get("status", "downloaded")
        has_payload = bool(item.get("file")) and status in ("downloaded", "extracted", "ok")
        if not report_date or not has_payload:
            core[name] = {"status": status, "report_date": report_date}
            issues.append(f"{name}: no validated file/report date (status={status}).")
            continue

        core[name] = {"status": status, "report_date": report_date}
        dates.append(str(report_date))

    counts = Counter(dates)
    session_date = counts.most_common(1)[0][0] if counts else None
    mismatched = {name: item["report_date"] for name, item in core.items()
                  if item.get("report_date") and item["report_date"] != session_date}
    if mismatched:
        details = ", ".join(f"{name}={day}" for name, day in sorted(mismatched.items()))
        issues.append(f"Core NSE reports are from different sessions: {details}.")

    aligned = len(core) == len(CORE_REPORTS) and not issues and len(set(dates)) == 1
    return {
        "status": "ready" if aligned else "blocked",
        "aligned": aligned,
        "session_date": session_date if aligned else None,
        "core_reports": core,
        "issues": issues,
    }
