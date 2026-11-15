from datetime import date
import io
import json

import pytest
from botocore.exceptions import ClientError

from scripts import nse_historical_audit as audit


def test_expected_calendar_has_494_sessions_and_one_sunday_special():
    sessions = audit.expected_sessions(date(2024, 11, 12), date(2026, 11, 12))
    assert len(sessions) == 494
    assert date(2026, 11, 8) in sessions
    assert date(2024, 11, 15) not in sessions
    assert date(2025, 10, 21) in sessions
    assert date(2024, 11, 20) not in sessions


def test_checkpoint_rejects_different_date_window(tmp_path):
    path = tmp_path / "checkpoint.json"
    path.write_text(json.dumps({
        "schema_version": audit.CHECKPOINT_VERSION,
        "start_date": "2024-11-12",
        "end_date": "2026-11-12",
        "verified": {},
        "failed": {},
    }))
    with pytest.raises(ValueError, match="date window"):
        audit._load_checkpoint(path, date(2024, 11, 13), date(2026, 11, 12))


def test_audit_resumes_verified_dates_without_redownloading(monkeypatch, tmp_path):
    start, end = date(2024, 11, 12), date(2024, 11, 14)
    checkpoint_path = tmp_path / "checkpoint.json"
    calls = []

    class FakeClient:
        def get_object(self, **_kwargs):
            return {"Body": io.BytesIO(b"fake-zip")}

    catalog = {"collections": [
        {"collection_date": day.isoformat(), "archive_sha256": str(index), "archive_bytes": 100}
        for index, day in enumerate(audit.expected_sessions(start, end))
    ]}
    monkeypatch.setattr(audit, "b2_client", lambda: FakeClient())
    monkeypatch.setattr(audit, "read_catalog", lambda client: catalog)
    monkeypatch.setattr(audit, "validate_remote_archive",
                        lambda client, bucket, day, entry: (calls.append(day) or True, "verified_4_report_files"))
    monkeypatch.setattr(audit, "extract_nifty_features",
                        lambda archive_bytes, day: {"session_date": day.isoformat(),
                                                    "feature_schema_version": audit.FEATURE_SCHEMA_VERSION,
                                                    "features": {"spot_close": 25000.0}, "contracts": []})
    monkeypatch.setenv("B2_BUCKET_NAME", "test-bucket")

    first = audit.run_audit(start, end, checkpoint_path, max_archives=2)
    assert first["status"] == "audit_in_progress"
    assert first["verified_session_count"] == 2
    assert len(calls) == 2

    second = audit.run_audit(start, end, checkpoint_path, max_archives=2)
    assert second["status"] == "archive_integrity_complete_calendar_reconciled"
    assert second["verified_session_count"] == 3
    assert len(calls) == 3


def test_b2_cap_pauses_without_retrying(monkeypatch, tmp_path):
    start, end = date(2024, 11, 12), date(2024, 11, 13)
    checkpoint_path = tmp_path / "checkpoint.json"

    class FakeClient:
        def get_object(self, **_kwargs):
            raise error

    catalog = {"collections": [
        {"collection_date": day.isoformat(), "archive_sha256": "a" * 64, "archive_bytes": 100}
        for day in audit.expected_sessions(start, end)
    ]}
    error = ClientError({"Error": {"Code": "TooManyRequests", "Message": "Class B cap exceeded"}}, "GetObject")
    monkeypatch.setattr(audit, "b2_client", lambda: FakeClient())
    monkeypatch.setattr(audit, "read_catalog", lambda client: catalog)
    monkeypatch.setattr(audit, "validate_remote_archive", lambda *args: (_ for _ in ()).throw(error))
    monkeypatch.setenv("B2_BUCKET_NAME", "test-bucket")

    report = audit.run_audit(start, end, checkpoint_path, max_archives=2)
    assert report["status"] == "paused_b2_class_b_cap"
    assert report["b2_cap_blocked"] is True
    assert report["model_training_allowed"] is False


def test_catalog_read_b2_cap_is_reported_without_losing_checkpoint(monkeypatch, tmp_path):
    start, end = date(2024, 11, 12), date(2024, 11, 12)
    checkpoint_path = tmp_path / "checkpoint.json"
    error = ClientError({"Error": {"Code": "TooManyRequests", "Message": "Class B cap exceeded"}}, "GetObject")
    monkeypatch.setattr(audit, "b2_client", lambda: object())
    monkeypatch.setattr(audit, "read_catalog", lambda client: (_ for _ in ()).throw(error))
    monkeypatch.setenv("B2_BUCKET_NAME", "test-bucket")

    report = audit.run_audit(start, end, checkpoint_path, max_archives=1)
    assert report["status"] == "paused_b2_class_b_cap"
    assert report["catalog_snapshot_current"] is False
    assert report["model_training_allowed"] is False


def test_archive_batch_size_is_hard_capped_for_b2_quota():
    assert audit.clamp_archive_batch_size(25) == 25
    assert audit.clamp_archive_batch_size(1000) == 25
    assert audit.clamp_archive_batch_size(0) == 1
    assert audit.clamp_archive_batch_size(-10) == 1
