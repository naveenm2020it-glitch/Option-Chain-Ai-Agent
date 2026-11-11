from nifty_ai.eod_intelligence import _participant_hedge_candidates


def test_opposite_futures_and_options_exposure_is_only_a_hedge_candidate():
    participant = {
        "sources": {
            "participant_oi": {
                "participant_positions": [
                    {
                        "participant": "FII",
                        "future_index_net": 120,
                        "option_directional_proxy_contracts": -90,
                        "source": "participant_oi.csv",
                    }
                ]
            }
        }
    }
    result = _participant_hedge_candidates(participant)
    assert result["available"] is True
    candidate = result["candidates"][0]
    assert candidate["classification"] == "POTENTIAL_HEDGE_OFFSET"
    assert candidate["offset_ratio"] == 0.75
    assert "not proof" in candidate["interpretation"]


def test_same_direction_exposure_is_not_labelled_as_hedging():
    participant = {
        "sources": {
            "participant_oi": {
                "participant_positions": [
                    {
                        "participant": "FII",
                        "future_index_net": -100,
                        "option_directional_proxy_contracts": -80,
                    }
                ]
            }
        }
    }
    result = _participant_hedge_candidates(participant)
    assert result["candidates"][0]["classification"] == "NO_CLEAR_OFFSET"


def test_missing_or_zero_exposure_does_not_create_hedge_signal():
    participant = {
        "sources": {
            "participant_oi": {
                "participant_positions": [
                    {"participant": "FII", "future_index_net": None, "option_directional_proxy_contracts": -50},
                    {"participant": "DII", "future_index_net": 0, "option_directional_proxy_contracts": 0},
                ]
            }
        }
    }
    result = _participant_hedge_candidates(participant)
    assert result["available"] is True
    assert result["candidates"][0]["classification"] == "NO_CLEAR_OFFSET"
    assert result["candidates"][0]["offset_ratio"] == 0


def test_no_participant_oi_reports_hedge_analysis_as_insufficient():
    result = _participant_hedge_candidates({"sources": {}})
    assert result["available"] is False
    assert result["status"] == "INSUFFICIENT DATA"
    assert result["candidates"] == []
