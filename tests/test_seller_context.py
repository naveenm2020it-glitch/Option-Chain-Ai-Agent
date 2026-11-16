from scripts.seller_context import snapshot_from_contracts, _window_summary


def _contracts(day_offset=0):
    expiry = "2026-11-19"
    rows = []
    for strike in (25000, 25050, 25100, 25150):
        for side, oi, change, premium in (
            ("CE", 5000 + strike - 25000, 800, -0.025),
            ("PE", 6000 + strike - 25000, 900, -0.03),
        ):
            rows.append({
                "expiry": expiry, "strike": strike, "option_type": side,
                "oi": oi, "change_oi": change, "volume": 1500,
                "close": 100, "previous_close": 103,
                "premium_return": premium, "days_to_expiry": 3,
            })
    return rows


def test_snapshot_uses_front_expiry_and_builds_both_sides():
    rows = _contracts()
    rows.append({
        "expiry": "2026-12-31", "strike": 25000, "option_type": "PE",
        "oi": 999999, "change_oi": 999999, "volume": 999999,
        "premium_return": -0.5,
    })
    result = snapshot_from_contracts("2026-11-16", 25100, rows)
    assert result["status"] == "analysis_complete"
    assert result["support"]
    assert result["resistance"]
    assert result["support"][0]["side"] == "PE"
    assert result["resistance"][0]["side"] == "CE"
    assert result["support"][0]["evidence"]["oi"] < 999999


def test_window_summary_counts_level_persistence_by_session():
    levels = snapshot_from_contracts("2026-11-16", 25100, _contracts())
    history = [(f"2026-11-{day:02d}", levels) for day in range(9, 14)]
    result = _window_summary(history, 5, "2026-11-16")
    assert result["sessions_available"] == 5
    assert result["sessions_analyzed"] == 5
    assert result["support_persistence"]
    assert result["support_persistence"][0]["persistence_pct"] == 100
    assert result["resistance_persistence"][0]["sessions_present"] == 5


def test_window_summary_uses_only_requested_trailing_sessions():
    levels = snapshot_from_contracts("2026-11-16", 25100, _contracts())
    history = [(f"2026-10-{day:02d}", levels) for day in range(1, 24)]
    result = _window_summary(history, 22, "2026-10-24")
    assert result["sessions_available"] == 22
    assert result["sessions_analyzed"] == 22



def test_missing_previous_close_does_not_confirm_option_writing():
    rows = _contracts()
    for row in rows:
        row["previous_close"] = 0
    result = snapshot_from_contracts("2026-11-16", 25100, rows)
    candidates = result["support"] + result["resistance"]
    assert candidates
    assert all(item["evidence"]["classification"] != "writing_candidate" for item in candidates)



def test_window_summary_flags_missing_weekday_context():
    levels = snapshot_from_contracts("2026-11-16", 25100, _contracts())
    history = [("2026-11-09", levels), ("2026-11-11", levels)]
    result = _window_summary(history, 5, "2026-11-16")
    assert result["coverage_status"] == "incomplete_or_exchange_holiday_gap"
    assert result["possible_missing_weekdays"] >= 1
