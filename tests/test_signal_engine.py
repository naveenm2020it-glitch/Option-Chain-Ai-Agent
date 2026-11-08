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
