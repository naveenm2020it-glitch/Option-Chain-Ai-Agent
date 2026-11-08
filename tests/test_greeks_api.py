import app as website_app


def test_greeks_matrix_calculates_ce_and_pe_from_verified_snapshot(monkeypatch):
    expiry = "2026-11-12"
    snapshot = {
        "status": "LIVE",
        "spot": 24000.0,
        "source_timestamp": "2026-11-09T09:30:00+05:30",
        "expiry_dates": [expiry],
        "rows": [{
            "strike": 24000,
            "expiry": expiry,
            "ce": {"lastPrice": 100, "impliedVolatility": 18.0},
            "pe": {"lastPrice": 95, "impliedVolatility": 19.0},
        }],
    }
    monkeypatch.setattr(website_app, "get_market", lambda expiry=None: snapshot)
    result = website_app.greeks(expiry)
    assert result["status"] == "OK"
    assert {item["side"] for item in result["items"]} == {"CE", "PE"}
    assert all("delta" in item and "gamma" in item for item in result["items"])


def test_greeks_matrix_fails_closed_without_verified_data(monkeypatch):
    monkeypatch.setattr(website_app, "get_market", lambda expiry=None: {"status": "UNAVAILABLE"})
    result = website_app.greeks()
    assert result["status"] == "WAIT"
    assert result["items"] == []
