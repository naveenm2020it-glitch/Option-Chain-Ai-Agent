from __future__ import annotations

import argparse
import io
import json
import os
import zipfile
from datetime import date
from pathlib import Path

import pandas as pd
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))

from storage import b2_client, read_catalog
from scripts.nse_ml_features import extract_nifty_features
from scripts.nse_historical_backfill import is_b2_cap_error


TARGET_REPORTS = (
    "fo_participant_oi",
    "fo_participant_volume",
    "nifty_index_close_all",
    "fo_market_activity",
    "fo_udiff_bhavcopy",
)


def csv_profile(payload: bytes, filename: str) -> dict:
    frame = pd.read_csv(io.BytesIO(payload), dtype=str, encoding="utf-8-sig", nrows=1000, low_memory=False)
    result = {"file": filename, "columns": [str(c).strip() for c in frame.columns], "rows_sampled": len(frame)}
    for candidate in ("Client Type", "ClientType", "Participant", "Participant Type", "Index Name", "Index"):
        if candidate in frame.columns:
            result["category_values"] = frame[candidate].dropna().astype(str).str.strip().drop_duplicates().head(10).tolist()
            break
    return result


def inspect_archive(archive_bytes: bytes, session: date) -> dict:
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        bad = archive.testzip()
        if bad:
            raise ValueError(f"ZIP CRC failure: {bad}")
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        results = manifest.get("results", {})
        reports = {}
        for key in TARGET_REPORTS:
            item = results.get(key)
            if not isinstance(item, dict) or not item.get("file"):
                reports[key] = {"present": False, "manifest_status": item.get("status") if isinstance(item, dict) else "missing"}
                continue
            name = item["file"]
            if Path(name).name != name or name not in archive.namelist():
                reports[key] = {"present": False, "error": "unsafe_or_missing_member"}
                continue
            payload = archive.read(name)
            profile = csv_profile(payload, name) if name.lower().endswith(".csv") else {
                "file": name, "zip_members": [Path(x).name for x in zipfile.ZipFile(io.BytesIO(payload)).namelist()[:30]]
            }
            reports[key] = {"present": True, "report_date": item.get("report_date"), **profile}
        features = extract_nifty_features(archive_bytes, session)["features"]
        coverage = {
            key: features.get(key, 0.0)
            for key in (
                "index_data_available",
                "participant_oi_data_available",
                "participant_volume_data_available",
                "future_data_available",
            )
        }
        return {
            "session_date": session.isoformat(),
            "core_session_status": manifest.get("core_session_status"),
            "core_session_date": manifest.get("core_session_date"),
            "manifest_report_keys": sorted(results),
            "reports": reports,
            "extracted_feature_coverage": coverage,
            "diagnostic_only": True,
            "b2_writes": 0,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect exactly one existing NIFTY B2 archive; read-only.")
    parser.add_argument("--session-date", required=True)
    args = parser.parse_args()
    session = date.fromisoformat(args.session_date)
    client = b2_client()
    bucket = os.environ["B2_BUCKET_NAME"]
    catalog = read_catalog(client)
    entry = next(
        (x for x in catalog.get("collections", []) if x.get("collection_date") == session.isoformat()),
        None,
    )
    if not entry:
        print(json.dumps({"session_date": session.isoformat(), "status": "not_in_catalog", "b2_object_reads": 0, "b2_writes": 0}, indent=2))
        return 2
    try:
        response = client.get_object(Bucket=bucket, Key=f"nse/daily/{session.isoformat()}/reports.zip")
        payload = response["Body"].read()
    except ClientError as exc:
        if is_b2_cap_error(exc):
            print(json.dumps({"status": "paused_b2_cap", "message": "B2 read cap reached; no retry attempted.", "b2_writes": 0}, indent=2))
            return 3
        raise
    report = inspect_archive(payload, session)
    report.update({"status": "completed", "b2_catalog_reads": 1, "b2_archive_reads": 1, "b2_writes": 0})
    print(json.dumps(report, indent=2))
    Path("single-archive-diagnostic.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
