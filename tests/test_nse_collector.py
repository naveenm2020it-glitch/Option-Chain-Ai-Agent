from datetime import date

import pytest

from scripts import nse_eod_collector as collector


class FakeResponse:
    def __init__(self, content=b"ok", status_code=200):
        self.content = content
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(str(self.status_code))


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def csv_payload():
    return b"Client Type,Future Index Long,Future Index Short\nFII,100,50\nPRO,20,10\n"


def test_official_url_date_formats():
    day = date(2026, 11, 11)
    volume_date = collector._report_date("participant_volume", day)
    fii_date = collector._report_date("fii_derivatives", day)
    assert collector.REPORTS["participant_volume"]["url"].format(date=volume_date).endswith(
        "fao_participant_vol_11112026.csv"
    )
    assert collector.REPORTS["fii_derivatives"]["url"].format(date=fii_date).endswith(
        "fii_stats_11-Nov-2026.xls"
    )


def test_csv_validation_rejects_html_and_empty():
    rows, cols = collector._parse_report("participant_volume", csv_payload())
    assert rows == 2
    assert "Client Type" in cols
    with pytest.raises(ValueError):
        collector._parse_report("participant_volume", b"<html>403 forbidden</html>" * 5)


def test_fii_xls_validation_rejects_html():
    with pytest.raises(ValueError, match="genuine XLS"):
        collector._parse_report("fii_derivatives", b"<html>Access denied</html>" * 5)


def test_download_404_is_not_available():
    session = FakeSession([FakeResponse(b"", 404)])
    assert collector._download(session, "https://example.invalid/report") is None


def test_download_retries_transient_error():
    import requests
    session = FakeSession([requests.Timeout("timeout"), FakeResponse(b"valid")])
    result = collector._download(
        session, "https://example.invalid/report", attempts=2, sleep=lambda _: None
    )
    assert result == b"valid"
    assert len(session.calls) == 2


def test_collect_writes_only_valid_reports_and_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(collector, "DATA_ROOT", tmp_path / "nse_reports")
    monkeypatch.setattr(collector, "MANIFEST_PATH", tmp_path / "nse_reports" / "manifest.json")
    fake = FakeSession([FakeResponse(csv_payload()), FakeResponse(b"", 404)])
    result = collector.collect(
        lookback_calendar_days=1, end_date=date(2026, 11, 11),
        session_factory=lambda: fake, sleep=lambda _: None,
    )
    volume = result["datasets"]["participant_volume"]
    fii = result["datasets"]["fii_derivatives"]
    assert volume["latest_available_date"] == "2026-11-11"
    assert volume["records"]["2026-11-11"]["rows"] == 2
    assert fii["latest_available_date"] is None
    assert fii["records"]["2026-11-11"]["status"] == "not_available"
    assert list((tmp_path / "nse_reports" / "participant_volume").glob("*.csv"))
    assert not list((tmp_path / "nse_reports" / "fii_derivatives").glob("*.xls"))


def test_invalid_manifest_is_not_silently_overwritten(tmp_path, monkeypatch):
    monkeypatch.setattr(collector, "MANIFEST_PATH", tmp_path / "manifest.json")
    collector.MANIFEST_PATH.write_text("not json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="manifest is invalid"):
        collector._load_manifest()


def test_lookback_bounds_are_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr(collector, "DATA_ROOT", tmp_path / "nse_reports")
    monkeypatch.setattr(collector, "MANIFEST_PATH", tmp_path / "nse_reports" / "manifest.json")
    with pytest.raises(ValueError):
        collector.collect(lookback_calendar_days=0, session_factory=lambda: FakeSession([]))


def test_session_does_not_depend_on_blocked_homepage(monkeypatch):
    fake = FakeSession([])
    monkeypatch.setattr(collector.requests, "Session", lambda: fake)
    session = collector._session()
    assert session is fake
    assert session.calls == []
    assert session.headers["User-Agent"].startswith("Mozilla/")



def test_participant_volume_parser_uses_second_row_as_header():
    payload = (
        b'""Participant wise Trading Volume as on Nov 09, 2026"",,,,,\n'
        b'Client Type,Future Index Long,Future Index Short,Total Long Contracts,Total Short Contracts\n'
        b'Client,100,90,200,190\nDII,20,10,30,20\nFII,50,40,90,80\nPro,30,50,80,100\nTOTAL,200,200,400,400\n'
    )
    rows, columns = collector._parse_report("participant_volume", payload)
    assert rows == 5
    assert "Client Type" in columns
    assert "Future Index Long" in columns
    assert not any(column.startswith("Unnamed") for column in columns)


def test_fii_xls_parser_uses_second_row_as_header(monkeypatch):
    import pandas as pd
    observed = {}
    frame = pd.DataFrame(
        [["Index Futures", 100, 80], ["Index Options", 200, 150]],
        columns=["Instrument", "Buy", "Sell"],
    )

    def fake_read_excel(*args, **kwargs):
        observed.update(kwargs)
        return {"Sheet1": frame}

    monkeypatch.setattr(collector.pd, "read_excel", fake_read_excel)
    payload = bytes.fromhex("D0CF11E0A1B11AE1") + b"\x00" * 64
    rows, columns = collector._parse_report("fii_derivatives", payload)
    assert observed["header"] == [1, 2]
    assert rows == 2
    assert columns == ["Instrument", "Buy", "Sell"]
