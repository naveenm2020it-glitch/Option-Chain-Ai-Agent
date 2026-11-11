from datetime import date, timedelta

from nifty_ai.eod_intelligence import _phase3_price_action


def _rows(start="2026-01-01", count=30):
    first = date.fromisoformat(start)
    rows = []
    for i in range(count):
        close = 25000 + i * 2
        rows.append({
            "date": (first + timedelta(days=i)).isoformat(),
            "open": close - 5,
            "high": close + 15,
            "low": close - 15,
            "close": close,
        })
    return rows


def test_phase3_returns_eod_daily_weekly_monthly_and_range_context():
    rows = _rows("2026-01-01", 45)
    result = _phase3_price_action(rows, india_vix=14.5)
    assert result["status"] == "READY"
    assert result["daily_levels"]["previous_close"] == rows[-2]["close"]
    assert result["weekly_levels"]["status"] == "READY"
    assert result["monthly_levels"]["status"] == "READY"
    assert result["expected_range"]["status"] == "READY"
    assert result["expected_range"]["is_forecast_probability"] is False
    assert result["intraday_status"] == "LIMITED — EOD DATA ONLY"


def test_phase3_close_breakout_uses_only_prior_sessions():
    rows = _rows("2026-01-01", 25)
    rows[-1]["open"] = 25100
    rows[-1]["high"] = 25200
    rows[-1]["low"] = 25090
    rows[-1]["close"] = 25180
    result = _phase3_price_action(rows)
    assert result["breakout"]["status"] == "READY"
    assert result["breakout"]["breakout_up"] is True
    assert result["breakout"]["classification"] == "CLOSE_CONFIRMED_UPSIDE_BREAKOUT"


def test_phase3_daily_failed_break_is_not_called_intraday_fake_breakout():
    rows = _rows("2026-01-01", 25)
    prior_high = max(x["high"] for x in rows[-21:-1])
    rows[-1]["high"] = prior_high + 20
    rows[-1]["close"] = prior_high - 2
    rows[-1]["open"] = prior_high - 5
    rows[-1]["low"] = prior_high - 15
    result = _phase3_price_action(rows)
    assert result["breakout"]["daily_failed_upside_attempt"] is True
    assert result["breakout"]["classification"] == "DAILY_FAILED_UPSIDE_ATTEMPT"
    assert "intraday" in " ".join(result["intraday_limitations"]).lower()


def test_phase3_gap_fill_counts_are_historical_and_sample_gated():
    rows = _rows("2026-01-01", 30)
    for i in range(1, len(rows)):
        if i % 2:
            rows[i]["open"] = rows[i-1]["close"] * 1.01
            rows[i]["high"] = rows[i]["open"] + 10
            rows[i]["low"] = rows[i-1]["close"] - 2
            rows[i]["close"] = rows[i]["open"]
    result = _phase3_price_action(rows)
    assert result["gap_scenarios"]["status"] == "READY"
    assert result["gap_scenarios"]["historical_gap_up_sessions"] >= 10
    assert result["gap_scenarios"]["gap_up_fill_rate_pct"] is not None
    assert "not calibrated" in " ".join(result["gap_scenarios"]["limitations"])


def test_phase3_suppresses_outputs_when_history_is_insufficient():
    result = _phase3_price_action(_rows(count=5))
    assert result["expected_range"]["status"] == "INSUFFICIENT HISTORY"
    assert result["breakout"]["status"] == "INSUFFICIENT HISTORY"
    assert result["gap_scenarios"]["status"] == "INSUFFICIENT HISTORY"
