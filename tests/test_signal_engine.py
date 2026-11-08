from services.signal_engine import monitor_signal


def signal():
    return {
        "state": "ACTIVE", "target_price": 120, "stop_price": 90,
        "horizon_seconds": 900, "triggered_at": "2026-11-09T09:30:00+05:30",
    }


def test_target_hit_from_option_ltp_snapshot():
    result = monitor_signal(signal(), 110, 120, "2026-11-09T09:30:05+05:30", "2026-11-09T09:30:10+05:30")
    assert result["outcome"] == "TARGET"


def test_stop_hit_from_option_ltp_snapshot():
    result = monitor_signal(signal(), 100, 89, "2026-11-09T09:30:05+05:30", "2026-11-09T09:30:10+05:30")
    assert result["outcome"] == "STOP_LOSS"


def test_large_snapshot_gap_is_unresolved():
    result = monitor_signal(signal(), 100, 100, "2026-11-09T09:30:05+05:30", "2026-11-09T09:30:30+05:30")
    assert result["outcome"] == "UNRESOLVED"


def test_crossed_outcome_zones_is_unresolved():
    result = monitor_signal(signal(), 121, 89, "2026-11-09T09:30:05+05:30", "2026-11-09T09:30:10+05:30")
    assert result["outcome"] == "UNRESOLVED"


def test_snapshot_confluence_selects_contract_without_candles():
    from services.signal_engine import analyze_snapshot

    expiry = "2026-11-12"
    previous = {
        "status": "LIVE", "spot": 24000,
        "source_timestamp": "2026-11-09T09:30:00+05:30",
        "selected_expiry": expiry,
        "rows": [{
            "strike": 24000, "expiry": expiry,
            "ce": {"lastPrice": 99, "bidprice": 98.5, "askPrice": 99.5, "openInterest": 900,
                   "changeinOpenInterest": 20, "totalTradedVolume": 1000, "impliedVolatility": 18},
            "pe": {"lastPrice": 100, "bidprice": 99.5, "askPrice": 100.5, "openInterest": 1100,
                   "changeinOpenInterest": 10, "totalTradedVolume": 1000, "impliedVolatility": 19},
        }],
    }
    current = {
        "status": "LIVE", "spot": 24020,
        "source_timestamp": "2026-11-09T09:30:05+05:30",
        "selected_expiry": expiry,
        "rows": [{
            "strike": 24000, "expiry": expiry,
            "ce": {"lastPrice": 101, "bidprice": 100.5, "askPrice": 101.5, "openInterest": 900,
                   "changeinOpenInterest": 30, "totalTradedVolume": 1200, "impliedVolatility": 18},
            "pe": {"lastPrice": 98, "bidprice": 97.5, "askPrice": 98.5, "openInterest": 1200,
                   "changeinOpenInterest": 15, "totalTradedVolume": 1100, "impliedVolatility": 19},
        }],
    }
    result = analyze_snapshot(current, previous)
    assert result["direction"] == "BULLISH"
    assert result["selected_candidate"]["side"] == "CE"
    assert result["selected_candidate"]["target_price"] == result["selected_candidate"]["entry_price"] + 20
    assert result["selected_candidate"]["stop_price"] == result["selected_candidate"]["entry_price"] - 10
    assert result["premium_candles_required"] is False



def test_signal_requires_projected_20_point_move_within_15_minutes():
    from services.signal_engine import analyze_snapshot

    expiry = "2026-11-12"
    previous = {
        "status": "LIVE", "spot": 24000,
        "source_timestamp": "2026-11-09T09:30:00+05:30",
        "selected_expiry": expiry,
        "rows": [{
            "strike": 24000, "expiry": expiry,
            "ce": {"lastPrice": 100, "bidprice": 99.5, "askPrice": 100.5, "openInterest": 900,
                   "changeinOpenInterest": 20, "totalTradedVolume": 1000, "impliedVolatility": 18},
            "pe": {"lastPrice": 100, "bidprice": 99.5, "askPrice": 100.5, "openInterest": 1100,
                   "changeinOpenInterest": 10, "totalTradedVolume": 1000, "impliedVolatility": 19},
        }],
    }
    current = {
        "status": "LIVE", "spot": 24020,
        "source_timestamp": "2026-11-09T09:30:05+05:30",
        "selected_expiry": expiry,
        "rows": [{
            "strike": 24000, "expiry": expiry,
            "ce": {"lastPrice": 100.1, "bidprice": 99.6, "askPrice": 100.6, "openInterest": 930,
                   "changeinOpenInterest": 30, "totalTradedVolume": 1200, "impliedVolatility": 18},
            "pe": {"lastPrice": 100.1, "bidprice": 99.6, "askPrice": 100.6, "openInterest": 1200,
                   "changeinOpenInterest": 15, "totalTradedVolume": 1100, "impliedVolatility": 19},
        }],
    }
    result = analyze_snapshot(current, previous)
    assert result["selected_candidate"] is None
    assert result["decision"] != "SIGNAL_CANDIDATE"
