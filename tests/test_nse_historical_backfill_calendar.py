from datetime import date
import hashlib
import io
import json
import zipfile

from scripts.nse_historical_backfill import dates_between, validate_remote_archive
from scripts.nse_download_check import CORE_REPORT_NAMES


def test_weekdays_and_official_weekend_special_session_are_included():
    days = list(dates_between(date(2026, 11, 6), date(2026, 11, 9)))
    assert days == [
        date(2026, 11, 6),
        date(2026, 11, 8),
        date(2026, 11, 9),
    ]


def test_official_muhurat_sunday_session_is_included():
    days = list(dates_between(date(2026, 11, 7), date(2026, 11, 9)))
    assert days == [date(2026, 11, 8), date(2026, 11, 9)]


def test_unlisted_sunday_is_not_included():
    days = list(dates_between(date(2026, 11, 1), date(2026, 11, 1)))
    assert days == []


def _valid_archive_fixture(day_text="2026-11-12"):
    files = {}
    manifest_results = {}
    for name in CORE_REPORT_NAMES:
        if name in {"fo_udiff_bhavcopy", "fo_market_activity"}:
            inner = io.BytesIO()
            with zipfile.ZipFile(inner, "w") as zf:
                zf.writestr("sample.csv", "symbol,price,volume\nNIFTY,24000,1000\n")
            payload = inner.getvalue()
        else:
            payload = b"symbol,price,volume\nNIFTY,24000,1000\n"
        extension = "zip" if name in {"fo_udiff_bhavcopy", "fo_market_activity"} else "csv"
        filename = f"{name}_{day_text}.{extension}"
        files[filename] = payload
        manifest_results[name] = {
            "report_date": day_text,
            "file": filename,
            "sha256": hashlib.sha256(payload).hexdigest(),
        }

    archive_buffer = io.BytesIO()
    manifest = {
        "core_session_status": "aligned",
        "core_session_date": day_text,
        "results": manifest_results,
    }
    with zipfile.ZipFile(archive_buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for filename, payload in files.items():
            zf.writestr(filename, payload)
        zf.writestr("manifest.json", json.dumps(manifest))
    archive_bytes = archive_buffer.getvalue()
    catalog_entry = {
        "archive_bytes": len(archive_bytes),
        "archive_sha256": hashlib.sha256(archive_bytes).hexdigest(),
    }

    class Body:
        def __init__(self, data):
            self.data = data

        def read(self):
            return self.data

    class Client:
        def __init__(self):
            self.get_count = 0

        def get_object(self, Bucket, Key):
            self.get_count += 1
            assert Key.endswith("reports.zip")
            return {"Body": Body(archive_bytes)}

    return Client(), catalog_entry, manifest


def test_remote_archive_integrity_checks_hash_crc_and_manifest_files():
    client, catalog_entry, manifest = _valid_archive_fixture()
    valid, reason = validate_remote_archive(
        client, "nifty-history", date(2026, 11, 12), catalog_entry, manifest
    )
    assert valid is True
    assert reason.startswith("verified_4_report_files")
    assert client.get_count == 1


def test_remote_archive_rejects_catalog_hash_mismatch():
    client, catalog_entry, manifest = _valid_archive_fixture()
    catalog_entry["archive_sha256"] = "0" * 64
    valid, reason = validate_remote_archive(
        client, "nifty-history", date(2026, 11, 12), catalog_entry, manifest
    )
    assert valid is False
    assert reason == "archive_sha256_mismatch_or_missing_catalog_hash"
