from seller_levels import build_seller_levels


def sample():
    return {
        "data_quality": {"aligned": True, "session_date": "2026-11-13"},
        "spot_price": 25340,
        "option_chain": [
            {"strike": 25200, "ce_oi": 1000, "ce_change_oi": 120, "ce_volume": 500,
             "ce_price_change_pct": -2, "pe_oi": 5000, "pe_change_oi": 900,
             "pe_volume": 1200, "pe_price_change_pct": -3},
            {"strike": 25300, "ce_oi": 2000, "ce_change_oi": 250, "ce_volume": 700,
             "ce_price_change_pct": -1, "pe_oi": 3500, "pe_change_oi": 400,
             "pe_volume": 900, "pe_price_change_pct": -1},
            {"strike": 25400, "ce_oi": 7000, "ce_change_oi": 1100, "ce_volume": 1500,
             "ce_price_change_pct": -2, "pe_oi": 1200, "pe_change_oi": -300,
             "pe_volume": 400, "pe_price_change_pct": 2},
            {"strike": 25500, "ce_oi": 9000, "ce_change_oi": 1500, "ce_volume": 1800,
             "ce_price_change_pct": 2, "pe_oi": 900, "pe_change_oi": 50,
             "pe_volume": 300, "pe_price_change_pct": 1},
        ],
    }


def test_levels_fail_closed_without_same_session_quality():
    result = build_seller_levels(sample() | {"data_quality": {"aligned": False}})
    assert result["status"] == "blocked"
    assert result["support"] == []


def test_put_writing_candidate_becomes_support_and_call_writing_resistance():
    result = build_seller_levels(sample())
    assert result["status"] == "analysis_complete"
    assert result["support"]
    assert result["support"][0]["side"] == "PE"
    assert result["resistance"]
    assert result["resistance"][0]["side"] == "CE"
    assert result["support"][0]["evidence"]["classification"] == "writing_candidate"


def test_premium_rise_with_oi_addition_is_risk_not_strong_defence():
    result = build_seller_levels(sample())
    risky = [item for item in result["seller_exit_zones"] if item["risk"] == "seller_pressure"]
    assert any(item["level"] == 25500 and item["side"] == "CE" for item in risky)


def test_missing_premium_does_not_claim_confirmed_writing():
    data = sample()
    for row in data["option_chain"]:
        row["ce_price_change_pct"] = None
        row["pe_price_change_pct"] = None
    result = build_seller_levels(data)
    assert all(item["evidence"]["classification"] != "writing_candidate"
               for item in result["resistance"] + result["support"])


def test_empty_or_bad_strikes_are_ignored():
    data = sample()
    data["option_chain"].append({"strike": "bad"})
    result = build_seller_levels(data)
    assert result["status"] == "analysis_complete"
