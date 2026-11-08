from datetime import datetime, timedelta, timezone

from services.data_quality import validate_snapshot


NOW = datetime(2026, 11, 9, 4, 0, 0, tzinfo=timezone.utc)


def valid_snapshot():
    stamp = NOW.isoformat()
    return {
        "instrument": "NIFTY",
        "source_timestamp": stamp,
        "received_at": stamp,
        "spot": 24000.0,
        "expiry_dates": ["2026-11-12"],
        "selected_expiry": "2026-11-12",
        "rows": [{
            "strike": 24000,
            "expiry": "2026-11-12",
            "ce": {
                "lastPrice": 100.0,
                "openInterest": 1000,
                "changeinOpenInterest": 20,
                "totalTradedVolume": 500,
            },
            "pe": {
                "lastPrice": 95.0,
                "openInterest": 1200,
                "changeinOpenInterest": 30,
                "totalTradedVolume": 600,
            },
        }],
    }


def test_provider_permission_fails_closed_by_default():
    result = validate_snapshot(valid_snapshot(), now=NOW)
    assert result.status == "UNAVAILABLE"
    assert "approved" in result.error


def test_valid_fresh_snapshot_is_live_only_when_provider_is_permitted():
    result = validate_snapshot(valid_snapshot(), provider_permitted=True, now=NOW)
    assert result.status == "LIVE"
    assert result.is_fresh is True
    assert "ce_pe_quotes" in result.checks


def test_old_source_timestamp_is_stale():
    snapshot = valid_snapshot()
    snapshot["source_timestamp"] = (NOW - timedelta(seconds=20)).isoformat()
    result = validate_snapshot(
        snapshot, provider_permitted=True, now=NOW, max_age_seconds=15
    )
    assert result.status == "STALE"
    assert result.is_fresh is False


def test_timezone_naive_source_timestamp_is_rejected():
    snapshot = valid_snapshot()
    snapshot["source_timestamp"] = "2026-11-09T04:00:00"
    result = validate_snapshot(snapshot, provider_permitted=True, now=NOW)
    assert result.status == "UNAVAILABLE"


def test_duplicate_strike_expiry_is_rejected():
    snapshot = valid_snapshot()
    snapshot["rows"].append(dict(snapshot["rows"][0]))
    result = validate_snapshot(snapshot, provider_permitted=True, now=NOW)
    assert result.status == "UNAVAILABLE"
    assert "Duplicate" in result.error


def test_mixed_expiries_are_rejected():
    snapshot = valid_snapshot()
    snapshot["rows"][0]["expiry"] = "2026-11-19"
    result = validate_snapshot(snapshot, provider_permitted=True, now=NOW)
    assert result.status == "UNAVAILABLE"
    assert "different expiries" in result.error


def test_invalid_leg_price_is_rejected():
    snapshot = valid_snapshot()
    snapshot["rows"][0]["pe"]["lastPrice"] = None
    result = validate_snapshot(snapshot, provider_permitted=True, now=NOW)
    assert result.status == "UNAVAILABLE"
    assert "CE or PE" in result.error
