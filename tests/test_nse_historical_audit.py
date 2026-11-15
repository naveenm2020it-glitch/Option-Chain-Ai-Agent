from datetime import date
import json

import pytest
from botocore.exceptions import ClientError

from scripts import nse_historical_audit as audit


def test_expected_calendar_has_495_sessions_and_one_sunday_special():
    sessions = audit.expected_sessions(date(2024, 11, 12), date(2026, 11, 12))
    assert len(sessions) == 495
    assert date(2026, 11, 8) in sessions
    assert date(2024, 11, 15) not in sessions
    assert date(2025, 10, 21) in sessions
    assert date(2024, 11, 20) in sessions


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
        pass

    catalog = {"collections": [
        {"collection_date": day.isoformat(), "archive_sha256": str(index), "archive_bytes": 100}
        for index, day in enumerate(audit.expected_sessions(start, end))
    ]}
    monkeypatch.setattr(audit, "b2_client", lambda: FakeClient())
    monkeypatch.setattr(audit, "read_catalog", lambda client: catalog)
    monkeypatch.setattr(audit, "validate_remote_archive",
                        lambda client, bucket, day, entry: (calls.append(day) or True, "verified_4_report_files"))
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
        pass

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
