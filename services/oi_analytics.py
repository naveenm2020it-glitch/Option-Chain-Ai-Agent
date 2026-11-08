"""Option-chain OI, PCR, Max Pain and concentration analytics."""
from __future__ import annotations

from math import isfinite
from typing import Any, Mapping


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        return None
    return float(value)


def _leg_value(row: Mapping[str, Any], side: str, key: str) -> float | None:
    leg = row.get(side)
    if not isinstance(leg, Mapping):
        return None
    return _number(leg.get(key))


def analyze_option_chain(rows: list[Mapping[str, Any]], *, top_n: int = 5) -> dict[str, Any]:
    """Return OI metrics for one expiry; missing fields are not fabricated.

    Max Pain uses current OI-weighted intrinsic payout. OI levels are zones of
    positioning, not guaranteed support/resistance or price forecasts.
    """
    if not rows:
        return {
            "status": "UNAVAILABLE",
            "error": "No option-chain rows supplied.",
            "metrics": None,
        }
    if top_n < 1:
        raise ValueError("top_n must be at least 1.")

    parsed: list[dict[str, Any]] = []
    expiry_values: set[str] = set()
    seen_pairs: set[tuple[str, float]] = set()
    warnings: list[str] = []

    for row in rows:
        if not isinstance(row, Mapping):
            warnings.append("Ignored a non-object option-chain row.")
            continue
        strike = _number(row.get("strike"))
        expiry = row.get("expiry")
        if strike is None or strike <= 0 or not isinstance(expiry, str) or not expiry:
            warnings.append("Ignored a row with invalid strike or expiry.")
            continue
        identity = (expiry, strike)
        if identity in seen_pairs:
            warnings.append("Duplicate strike/expiry rows were found.")
            continue
        seen_pairs.add(identity)
        expiry_values.add(expiry)
        parsed.append({
            "strike": strike,
            "expiry": expiry,
            "ce_oi": _leg_value(row, "ce", "openInterest"),
            "pe_oi": _leg_value(row, "pe", "openInterest"),
            "ce_change": _leg_value(row, "ce", "changeinOpenInterest"),
            "pe_change": _leg_value(row, "pe", "changeinOpenInterest"),
            "ce_volume": _leg_value(row, "ce", "totalTradedVolume"),
            "pe_volume": _leg_value(row, "pe", "totalTradedVolume"),
        })

    if not parsed:
        return {"status": "UNAVAILABLE", "error": "No valid strike rows supplied.", "metrics": None}
    if len(expiry_values) > 1:
        return {
            "status": "UNAVAILABLE",
            "error": "Input contains multiple expiries; analyze one expiry at a time.",
            "metrics": None,
        }

    def total(key: str) -> float | None:
        values = [item[key] for item in parsed]
        present = [value for value in values if value is not None]
        return sum(present) if present else None

    ce_oi = total("ce_oi")
    pe_oi = total("pe_oi")
    ce_change = total("ce_change")
    pe_change = total("pe_change")
    ce_volume = total("ce_volume")
    pe_volume = total("pe_volume")

    max_pain = None
    if all(item["ce_oi"] is not None and item["pe_oi"] is not None for item in parsed):
        payouts = []
        for settlement in sorted(item["strike"] for item in parsed):
            payout = sum(
                item["ce_oi"] * max(0.0, settlement - item["strike"])
                + item["pe_oi"] * max(0.0, item["strike"] - settlement)
                for item in parsed
            )
            payouts.append((payout, settlement))
        max_pain = min(payouts)[1] if payouts else None
    else:
        warnings.append("Max Pain omitted because one or more OI values are missing.")

    def ranked(oi_key: str, change_key: str, reverse: bool = True) -> list[dict[str, Any]]:
        candidates = [item for item in parsed if item[oi_key] is not None]
        candidates.sort(key=lambda item: item[oi_key], reverse=reverse)
        return [
            {
                "strike": item["strike"],
                "oi": item[oi_key],
                "change_oi": item[change_key],
                "change_state": (
                    "BUILDUP" if item[change_key] is not None and item[change_key] > 0
                    else "UNWINDING" if item[change_key] is not None and item[change_key] < 0
                    else "FLAT_OR_UNKNOWN"
                ),
            }
            for item in candidates[:top_n]
        ]

    return {
        "status": "OK",
        "expiry": next(iter(expiry_values)),
        "metrics": {
            "total_ce_oi": ce_oi,
            "total_pe_oi": pe_oi,
            "total_ce_change_oi": ce_change,
            "total_pe_change_oi": pe_change,
            "total_ce_volume": ce_volume,
            "total_pe_volume": pe_volume,
            "oi_pcr": pe_oi / ce_oi if ce_oi not in (None, 0) and pe_oi is not None else None,
            "volume_pcr": pe_volume / ce_volume if ce_volume not in (None, 0) and pe_volume is not None else None,
            "max_pain": max_pain,
            "put_oi_support_candidates": ranked("pe_oi", "pe_change"),
            "call_oi_resistance_candidates": ranked("ce_oi", "ce_change"),
            "put_oi_build_up": [item for item in ranked("pe_oi", "pe_change") if item["change_oi"] is not None and item["change_oi"] > 0],
            "call_oi_build_up": [item for item in ranked("ce_oi", "ce_change") if item["change_oi"] is not None and item["change_oi"] > 0],
            "put_oi_unwinding": [
                item for item in ranked("pe_oi", "pe_change", reverse=False)
                if item["change_oi"] is not None and item["change_oi"] < 0
            ],
            "call_oi_unwinding": [
                item for item in ranked("ce_oi", "ce_change", reverse=False)
                if item["change_oi"] is not None and item["change_oi"] < 0
            ],
        },
        "warnings": warnings,
        "interpretation": "OI concentrations are positioning evidence, not guaranteed support/resistance or a directional signal.",
    }
