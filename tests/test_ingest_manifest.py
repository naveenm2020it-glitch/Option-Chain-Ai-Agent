import json

from nifty_ai import ingest


def test_scan_manifest_imports_verified_metadata_without_reparsing(tmp_path, monkeypatch):
    root = tmp_path
    archive = root / "data" / "nse_reports"
    report = archive / "nifty50" / "nifty50_09112026.csv"
    report.parent.mkdir(parents=True)
    report.write_text("Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,Closing Index Value\n"
                      "NIFTY 50,09-Nov-2026,25000,25100,24900,25050\n", encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "datasets": {
            "nifty50": {
                "records": {
                    "2026-11-09": {
                        "status": "available",
                        "filename": "data/nse_reports/nifty50/nifty50_09112026.csv",
                        "sha256": "abc123",
                        "bytes": report.stat().st_size,
                        "rows": 1,
                        "columns": ["Index Name", "Index Date", "Open Index Value",
                                    "High Index Value", "Low Index Value", "Closing Index Value"],
                    },
                    "2026-11-10": {"status": "not_available"},
                }
            }
        }
    }
    (archive / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(ingest, "ROOT", root)
    captured = []
    monkeypatch.setattr(ingest, "add_report", lambda item: captured.append(item) or item)
    monkeypatch.setattr(ingest, "inspect_report", lambda *args: (_ for _ in ()).throw(
        AssertionError("manifest import must not parse every archived file")
    ))

    added = ingest.scan_manifest(archive)

    assert len(added) == 1
    assert captured[0]["report_type"] == "nifty50"
    assert captured[0]["trading_date"] == "2026-11-09"
    assert captured[0]["row_count"] == 1
    assert captured[0]["fingerprint"] == "abc123"
    assert captured[0]["status"] == "imported"


def test_scan_manifest_ignores_missing_and_unavailable_files(tmp_path, monkeypatch):
    archive = tmp_path / "data" / "nse_reports"
    archive.mkdir(parents=True)
    (archive / "manifest.json").write_text(json.dumps({
        "schema_version": 1,
        "datasets": {"fo_bhavcopy": {"records": {
            "2026-11-10": {"status": "not_available", "filename": "missing.zip"},
            "2026-11-11": {"status": "available", "filename": "missing.zip"},
        }}}
    }), encoding="utf-8")
    monkeypatch.setattr(ingest, "ROOT", tmp_path)
    monkeypatch.setattr(ingest, "add_report", lambda item: item)
    assert ingest.scan_manifest(archive) == []
