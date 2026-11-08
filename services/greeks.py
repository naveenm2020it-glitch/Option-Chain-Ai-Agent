"""European Black-Scholes Greeks with explicit units and input provenance."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from math import erf, exp, isfinite, log, pi, sqrt


@dataclass(frozen=True)
class GreeksResult:
    option_type: str
    delta: float
    gamma: float
    theta_per_calendar_day: float
    vega_per_1_vol_point: float
    rho_per_1_rate_point: float
    model: str
    iv_source: str
    risk_free_rate: float
    dividend_yield: float
    time_to_expiry_years: float

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _cdf(x: float) -> float:
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def _pdf(x: float) -> float:
    return exp(-0.5 * x * x) / sqrt(2.0 * pi)


def calculate_greeks(
    *,
    spot: float,
    strike: float,
    time_to_expiry_years: float,
    implied_volatility: float,
    option_type: str,
    risk_free_rate: float,
    dividend_yield: float = 0.0,
    iv_source: str = "provider",
) -> GreeksResult:
    """Calculate European option Greeks.

    Rates and IV are annual decimals (e.g. 0.07 = 7%). Vega and Rho are
    returned per one percentage-point move; Theta is per calendar day.
    This is a model estimate, not an exchange-provided Greek.
    """
    values = (spot, strike, time_to_expiry_years, implied_volatility,
              risk_free_rate, dividend_yield)
    if any(not isinstance(v, (int, float)) or not isfinite(v) for v in values):
        raise ValueError("All Greek inputs must be finite numbers.")
    if spot <= 0 or strike <= 0 or time_to_expiry_years <= 0 or implied_volatility <= 0:
        raise ValueError("Spot, strike, time to expiry and IV must be positive.")
    side = option_type.upper()
    if side not in {"CE", "PE", "CALL", "PUT"}:
        raise ValueError("option_type must be CE/PE or CALL/PUT.")
    is_call = side in {"CE", "CALL"}

    t_sqrt = sqrt(time_to_expiry_years)
    sigma_sqrt_t = implied_volatility * t_sqrt
    d1 = (
        log(spot / strike)
        + (risk_free_rate - dividend_yield
           + 0.5 * implied_volatility * implied_volatility) * time_to_expiry_years
    ) / sigma_sqrt_t
    d2 = d1 - sigma_sqrt_t
    discount_q = exp(-dividend_yield * time_to_expiry_years)
    discount_r = exp(-risk_free_rate * time_to_expiry_years)
    pdf_d1 = _pdf(d1)

    gamma = discount_q * pdf_d1 / (spot * sigma_sqrt_t)
    vega = spot * discount_q * pdf_d1 * t_sqrt / 100.0
    common_theta = -(spot * discount_q * pdf_d1 * implied_volatility) / (2.0 * t_sqrt)

    if is_call:
        delta = discount_q * _cdf(d1)
        theta = (
            common_theta
            - risk_free_rate * strike * discount_r * _cdf(d2)
            + dividend_yield * spot * discount_q * _cdf(d1)
        ) / 365.0
        rho = strike * time_to_expiry_years * discount_r * _cdf(d2) / 100.0
    else:
        delta = discount_q * (_cdf(d1) - 1.0)
        theta = (
            common_theta
            + risk_free_rate * strike * discount_r * _cdf(-d2)
            - dividend_yield * spot * discount_q * _cdf(-d1)
        ) / 365.0
        rho = -strike * time_to_expiry_years * discount_r * _cdf(-d2) / 100.0

    return GreeksResult(
        option_type="CE" if is_call else "PE",
        delta=delta,
        gamma=gamma,
        theta_per_calendar_day=theta,
        vega_per_1_vol_point=vega,
        rho_per_1_rate_point=rho,
        model="European Black-Scholes",
        iv_source=iv_source,
        risk_free_rate=risk_free_rate,
        dividend_yield=dividend_yield,
        time_to_expiry_years=time_to_expiry_years,
    )
