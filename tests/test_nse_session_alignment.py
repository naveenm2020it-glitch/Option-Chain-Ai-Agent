from datetime import date

from scripts import nse_download_check as downloader


class FakeSession:
    def __init__(self):
        self.headers = {}

    def get(self, *args, **kwargs):
        return type("Response", (), {"status_code": 200})()


def fake_save_report(output, name, day, spec, payload, meta):
    yield name, {"report_date": day.isoformat(), "file": f"{name}.csv"}


def test_core_reports_fall_back_to_one_shared_session(monkeypatch, tmp_path):
    monkeypatch.setattr(downloader.requests, "Session", FakeSession)
    monkeypatch.setattr(downloader, "save_report", fake_save_report)

    def fake_fetch(session, name, path, kind):
        # Market activity for 12 Nov is unavailable, while the other three
        # core files exist. The downloader must reject that mixed set.
        if name == "fo_market_activity" and "12112026" in path:
            raise RuntimeError("not published yet")
        return b"validated report payload", {"bytes": 24}

    monkeypatch.setattr(downloader, "fetch_report", fake_fetch)
    manifest = downloader.run(tmp_path, date(2026, 11, 12), lookback_days=3)

    assert manifest["core_session_status"] == "aligned"
    assert manifest["core_session_date"] == "2026-11-11"
    for name in downloader.CORE_REPORT_NAMES:
        assert manifest["results"][name]["report_date"] == "2026-11-11"


def test_missing_one_core_report_blocks_the_whole_session(monkeypatch, tmp_path):
    monkeypatch.setattr(downloader.requests, "Session", FakeSession)
    monkeypatch.setattr(downloader, "save_report", fake_save_report)

    def fake_fetch(session, name, path, kind):
        if name == "fo_market_activity":
            raise RuntimeError("not available")
        return b"validated report payload", {"bytes": 24}

    monkeypatch.setattr(downloader, "fetch_report", fake_fetch)
    manifest = downloader.run(tmp_path, date(2026, 11, 12), lookback_days=2)

    assert manifest["core_session_status"] == "blocked_no_common_session"
    assert manifest["core_session_date"] is None
    for name in downloader.CORE_REPORT_NAMES:
        assert manifest["results"][name]["status"] == "no_common_session"
        assert manifest["results"][name]["report_date"] is None
