from nifty_ai.eod_intelligence import _next_day_regime_agent, _zone_confluence_agent


def _history():
    rows = []
    for i in range(15):
        close = 25000 + i * 18
        rows.append({"date": f"2026-10-{i+1:02d}", "open": close - 5, "high": close + 35,
                     "low": close - 25, "close": close})
    return rows


def test_next_day_regime_requires_minimum_history_and_does_not_claim_probability():
    short = _next_day_regime_agent(_history()[:8], 14.0)
    assert short["status"] == "insufficient_history"
    ready = _next_day_regime_agent(_history(), 14.0)
    assert ready["status"] == "complete"
    assert ready["confidence"] is None
    assert "range_reference_type" in ready["metrics"]
    assert "probability" not in ready["metrics"]


def test_high_volatility_regime_reduces_directional_score():
    history = _history()
    normal = _next_day_regime_agent(history, 14.0)
    stressed = _next_day_regime_agent(history, 27.0)
    assert stressed["metrics"]["regime"] == "HIGH_VOLATILITY"
    assert abs(stressed["score"]) <= abs(normal["score"])


def test_zone_quality_agent_ranks_only_correct_side_and_keeps_score_type_explicit():
    defence = {
        "buyer_defence": [
            {"center": 24900, "zone_low": 24880, "zone_high": 24920, "evidence_score": 80,
             "strength": "STRONG_EVIDENCE", "evidence_sources": ["put_oi_wall"]},
            {"center": 25100, "zone_low": 25080, "zone_high": 25120, "evidence_score": 90,
             "strength": "STRONG_EVIDENCE", "evidence_sources": ["swing_low"]},
        ],
        "seller_defence": [
            {"center": 25200, "zone_low": 25180, "zone_high": 25220, "evidence_score": 75,
             "strength": "STRONG_EVIDENCE", "evidence_sources": ["call_oi_wall"]},
        ],
    }
    result = _zone_confluence_agent(defence, 25000)
    assert result["status"] == "complete"
    assert result["metrics"]["nearest_support_candidates"][0]["center"] == 24900
    assert result["metrics"]["nearest_resistance_candidates"][0]["center"] == 25200
    assert result["metrics"]["score_type"] == "EVIDENCE_SCORE_NOT_PROBABILITY"
    assert result["score"] == 0.0
