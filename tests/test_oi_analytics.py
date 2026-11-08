from services.oi_analytics import analyze_option_chain


def sample_rows():
    return [
        {
            "strike": 100, "expiry": "2026-11-12",
            "ce": {"openInterest": 100, "changeinOpenInterest": 10, "totalTradedVolume": 50},
            "pe": {"openInterest": 200, "changeinOpenInterest": 20, "totalTradedVolume": 60},
        },
        {
            "strike": 150, "expiry": "2026-11-12",
            "ce": {"openInterest": 300, "changeinOpenInterest": -5, "totalTradedVolume": 70},
            "pe": {"openInterest": 100, "changeinOpenInterest": -10, "totalTradedVolume": 30},
        },
        {
            "strike": 200, "expiry": "2026-11-12",
            "ce": {"openInterest": 50, "changeinOpenInterest": 0, "totalTradedVolume": 20},
            "pe": {"openInterest": 250, "changeinOpenInterest": 5, "totalTradedVolume": 80},
        },
    ]


def test_oi_pcr_and_volume_pcr():
    result = analyze_option_chain(sample_rows())
    metrics = result["metrics"]
    assert result["status"] == "OK"
    assert metrics["total_ce_oi"] == 450
    assert metrics["total_pe_oi"] == 550
    assert metrics["oi_pcr"] == 550 / 450
    assert metrics["volume_pcr"] == 170 / 140


def test_max_pain_returns_a_candidate_strike():
    result = analyze_option_chain(sample_rows())
    assert result["metrics"]["max_pain"] in {100.0, 150.0, 200.0}


def test_unwinding_is_separated_from_oi_concentration():
    result = analyze_option_chain(sample_rows())
    metrics = result["metrics"]
    assert any(x["change_oi"] < 0 for x in metrics["call_oi_unwinding"])
    assert any(x["change_oi"] < 0 for x in metrics["put_oi_unwinding"])


def test_multiple_expiries_are_rejected():
    rows = sample_rows()
    rows[0]["expiry"] = "2026-11-19"
    result = analyze_option_chain(rows)
    assert result["status"] == "UNAVAILABLE"
