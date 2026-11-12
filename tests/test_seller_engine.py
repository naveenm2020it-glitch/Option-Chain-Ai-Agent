from seller_engine import analyze_seller_market, ce_seller_model, pe_seller_model


def sample_data():
    rows = []
    for strike in (25000, 25100, 25200, 25300):
        rows.append({
            "strike": strike,
            "ce_oi": 1000, "ce_change_oi": 100, "ce_price_change_pct": -2,
            "pe_oi": 1200, "pe_change_oi": 180, "pe_price_change_pct": -3,
        })
    return {
        "data_quality": {"aligned": True, "session_date": "2026-11-12"},
        "option_chain": rows,
        "futures": {"price_change_pct": 0.8, "oi_change_pct": 1.4},
        "participants": {"fii_net_futures_change": 100, "fii_gross_change": 200},
        "volatility": {"atm_iv": 14, "atm_iv_change": 0.2, "days_to_expiry": 3},
    }


def test_missing_session_data_fails_closed():
    result = analyze_seller_market({"option_chain": []})
    assert result["status"] == "blocked"
    assert result["final_decision"] == "NO TRADE"


def test_ce_and_pe_seller_models_are_independent():
    data = sample_data()
    assert ce_seller_model(data)["model"] == "ce_seller"
    assert pe_seller_model(data)["model"] == "pe_seller"
    assert ce_seller_model(data)["seller_advantage"] == "ce"
    assert pe_seller_model(data)["seller_advantage"] == "pe"


def test_ensemble_returns_eight_models_and_one_decision():
    result = analyze_seller_market(sample_data())
    assert result["status"] == "analysis_complete"
    assert result["total_models"] == 8
    assert result["final_decision"] in {
        "NO TRADE", "PE WRITING CANDIDATE", "CE WRITING CANDIDATE"
    }
    assert len(result["models"]) == 8
