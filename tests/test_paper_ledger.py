from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
from uuid import UUID

import paper_ledger


class FakeResult:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(self):
        self.quotes = {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split()).lower()
        if normalized.startswith("insert into paper_quotes"):
            quote_id = params[0]
            self.quotes[quote_id] = (
                quote_id, params[1], date.fromisoformat(params[2]), params[3], params[4],
                params[5], params[6], params[7], params[8], params[9],
            )
            return FakeResult((quote_id,))
        if normalized.startswith("select quote_id from paper_quotes"):
            return FakeResult((params[0],) if params[0] in self.quotes else None)
        if normalized.startswith("select quote_id,symbol,expiry,strike"):
            return FakeResult(self.quotes.get(params[0]))
        if "current_database()" in normalized:
            return FakeResult(("paper_test", "public", "127.0.0.1"))
        return FakeResult()


def test_save_quote_returns_id_that_can_be_read_back(monkeypatch):
    conn = FakeConnection()
    monkeypatch.setattr(paper_ledger, "_connect", lambda: conn)
    now = datetime.now(timezone.utc)
    payload = {
        "symbol": "NIFTY",
        "expiry": (now.date() + timedelta(days=7)).isoformat(),
        "strike": "26000",
        "option_type": "CE",
        "bid": "100",
        "ask": "101",
        "timestamp": now.isoformat(),
        "lot_size": 65,
        "source": "unit-test",
    }

    saved = paper_ledger.save_quote(payload, now=now)
    loaded = paper_ledger.get_quote(saved["quote_id"])

    assert UUID(saved["quote_id"])
    assert loaded is not None
    assert loaded["quote_id"] == saved["quote_id"]
    assert loaded["bid"] == "100"
    assert loaded["ask"] == "101"
    assert loaded["database_fingerprint"] == saved["database_fingerprint"]


def test_get_quote_rejects_invalid_uuid():
    import pytest

    with pytest.raises(ValueError, match="valid UUID"):
        paper_ledger.get_quote("not-a-uuid")


def test_get_quote_returns_none_for_unknown_id(monkeypatch):
    conn = FakeConnection()
    monkeypatch.setattr(paper_ledger, "_connect", lambda: conn)
    assert paper_ledger.get_quote("15462493-c588-4348-9d3b-6bf9f979a979") is None
