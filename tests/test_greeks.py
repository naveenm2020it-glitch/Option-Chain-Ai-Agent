import pytest

from services.greeks import calculate_greeks


def test_atm_call_and_put_delta_are_reasonable():
    common = dict(
        spot=100, strike=100, time_to_expiry_years=0.25,
        implied_volatility=0.20, risk_free_rate=0.05,
    )
    call = calculate_greeks(**common, option_type="CE")
    put = calculate_greeks(**common, option_type="PE")
    assert 0 < call.delta < 1
    assert -1 < put.delta < 0
    assert call.gamma == pytest.approx(put.gamma)
    assert call.vega_per_1_vol_point == pytest.approx(put.vega_per_1_vol_point)


def test_greek_units_and_provenance_are_explicit():
    result = calculate_greeks(
        spot=100, strike=105, time_to_expiry_years=0.1,
        implied_volatility=0.25, option_type="PE",
        risk_free_rate=0.06, iv_source="provider",
    )
    assert result.model == "European Black-Scholes"
    assert result.iv_source == "provider"
    assert result.theta_per_calendar_day < 0


def test_invalid_inputs_are_rejected():
    with pytest.raises(ValueError):
        calculate_greeks(
            spot=100, strike=100, time_to_expiry_years=0,
            implied_volatility=0.2, option_type="CE", risk_free_rate=0.05,
        )
