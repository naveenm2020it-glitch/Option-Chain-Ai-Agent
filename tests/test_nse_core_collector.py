from datetime import date
from io import BytesIO
import zipfile

import pandas as pd

from scripts import nse_core_eod_collector as collector
from nifty_ai.reports import identify_report


class FakeResponse:
    def __init__(self, content=b"", status_code=200, json_data=None):
        self.content = content
        self.status_code = status_code
        self._json_data = json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return self._json_data


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        return self.responses.pop(0)


def zip_csv(csv_text):
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("BhavCopy.csv", csv_text)
    return buffer.getvalue()


def index_csv():
    return (
        "Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,Closing Index Value\n"
        "NIFTY 50,11-Nov-2026,25000,25100,24900,25050\n"
        "INDIA VIX,11-Nov-2026,13.0,14.0,12.0,13.5\n"
    ).encode()


def test_udiff_zip_validation_for_cash_and_fo():
    cm = zip_csv("TckrSymb,SctySrs,ClsPric\nRELIANCE,EQ,1500\n")
    fo = zip_csv("TckrSymb,OptnTp,StrkPric,OpnIntrst\nNIFTY,CE,25000,1000\n")
    assert collector._parse_udiff_zip(cm, "cm_bhavcopy")[0] == 1
    assert collector._parse_udiff_zip(fo, "fo_bhavcopy")[0] == 1


def test_udiff_cash_filename_is_recognized_by_app_importer():
    assert identify_report("BhavCopy_NSE_CM_0_0_0_20261111_F_0000.csv.zip") == "cm_bhavcopy"
    assert identify_report("BhavCopy_NSE_FO_0_0_0_20261111_F_0000.csv.zip") == "fo_bhavcopy"


def test_index_file_is_split_into_nifty_and_vix(tmp_path, monkeypatch):
    monkeypatch.setattr(collector, "DATA_ROOT", tmp_path / "nse_reports")
    session = FakeSession([FakeResponse(index_csv())])
    manifest = {"schema_version": 1, "source": "NSE official", "datasets": {}}
    collector._collect_indices(session, date(2026, 11, 11), manifest, lambda: None)
    assert manifest["datasets"]["nifty50"]["records"]["2026-11-11"]["status"] == "available"
    assert manifest["datasets"]["india_vix"]["records"]["2026-11-11"]["status"] == "available"
    nifty = pd.read_csv(tmp_path / "nse_reports/nifty50/nifty50_11112026.csv")
    vix = pd.read_csv(tmp_path / "nse_reports/india_vix/india_vix_11112026.csv")
    assert nifty.iloc[0]["Closing Index Value"] == 25050
    assert vix.iloc[0]["Closing Index Value"] == 13.5


def test_fii_dii_endpoint_is_normalized_to_app_csv(tmp_path, monkeypatch):
    monkeypatch.setattr(collector, "DATA_ROOT", tmp_path / "nse_reports")
    session = FakeSession([FakeResponse(json_data=[
        {"date": "11-Nov-2026", "category": "FII/FPI", "buyValue": "100", "sellValue": "90", "netValue": "10"},
        {"date": "11-Nov-2026", "category": "DII", "buyValue": "80", "sellValue": "85", "netValue": "-5"},
    ])])
    manifest = {"schema_version": 1, "source": "NSE official", "datasets": {}}
    collector._collect_fii_dii(session, manifest)
    path = tmp_path / "nse_reports/fii_dii/fii_dii_20261111.csv"
    frame = pd.read_csv(path)
    assert list(frame["Category"]) == ["FII/FPI", "DII"]
    assert frame.iloc[0]["Net Value"] == 10
    assert manifest["datasets"]["fii_dii"]["records"]["2026-11-11"]["status"] == "available"


def test_full_core_collector_archives_five_report_types(tmp_path, monkeypatch):
    monkeypatch.setattr(collector, "DATA_ROOT", tmp_path / "nse_reports")
    monkeypatch.setattr(collector, "MANIFEST_PATH", tmp_path / "nse_reports/manifest.json")
    cm = zip_csv("TckrSymb,SctySrs,ClsPric\nRELIANCE,EQ,1500\n")
    fo = zip_csv("TckrSymb,OptnTp,StrkPric,OpnIntrst\nNIFTY,CE,25000,1000\n")
    fii = [
        {"date": "11-Nov-2026", "category": "FII/FPI", "buyValue": "100", "sellValue": "90", "netValue": "10"},
        {"date": "11-Nov-2026", "category": "DII", "buyValue": "80", "sellValue": "85", "netValue": "-5"},
    ]
    fake = FakeSession([
        FakeResponse(cm), FakeResponse(fo), FakeResponse(index_csv()),
        FakeResponse(json_data=fii),
    ])
    result = collector.collect(
        lookback_calendar_days=1, end_date=date(2026, 11, 11),
        pause_seconds=0, session_factory=lambda: fake,
    )
    for kind in ("nifty50", "india_vix", "cm_bhavcopy", "fo_bhavcopy", "fii_dii"):
        assert result["datasets"][kind]["latest_available_date"] == "2026-11-11"
        assert result["datasets"][kind]["records"]["2026-11-11"]["status"] == "available"
    assert len(fake.calls) == 4
    assert (tmp_path / "nse_reports/manifest.json").exists()
