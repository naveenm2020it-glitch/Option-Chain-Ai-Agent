from __future__ import annotations

"""Seller-positioning support/resistance estimates from a normalized NIFTY chain.

These are evidence-weighted proxies, not identification of the buyer/seller on
each trade. Callers must supply validated, same-session option-chain rows.
"""

from math import log1p
from typing import Any


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _score_rows(rows: list[dict], side: str) -> list[dict]:
    oi_key = f"{side.lower()}_oi"
    change_key = f"{side.lower()}_change_oi"
    volume_key = f"{side.lower()}_volume"
    premium_key = f"{side.lower()}_price_change_pct"
    parsed = []
    for row in rows:
        strike = _number(row.get("strike"))
        oi = _number(row.get(oi_key))
        change = _number(row.get(change_key))
        volume = _number(row.get(volume_key))
        premium = _number(row.get(premium_key))
        if strike is None or strike <= 0:
            continue
        parsed.append({"strike": strike, "oi": max(0, oi) if oi is not None else None,
                       "oi_value": max(0, oi or 0), "change": change, "volume": max(0, volume or 0),
                       "premium_change_pct": premium})
    if not parsed:
        return []
    max_oi = max((log1p(r["oi_value"]) for r in parsed), default=0) or 1
    max_change = max((log1p(max(0, r["change"] or 0)) for r in parsed), default=0) or 1
    max_volume = max((log1p(r["volume"]) for r in parsed), default=0) or 1
    output = []
    for row in parsed:
        writing = row["change"] is not None and row["change"] > 0 and row["premium_change_pct"] is not None and row["premium_change_pct"] <= 0
        squeeze = row["change"] is not None and row["change"] > 0 and row["premium_change_pct"] is not None and row["premium_change_pct"] > 0
        unwinding = row["change"] is not None and row["change"] < 0
        oi_strength = log1p(row["oi_value"]) / max_oi
        change_strength = log1p(max(0, row["change"] or 0)) / max_change
        volume_strength = log1p(row["volume"]) / max_volume
        premium_confirmation = 1.0 if writing else 0.0
        raw = 100 * (0.35 * oi_strength + 0.35 * change_strength +
                     0.15 * volume_strength + 0.15 * premium_confirmation)
        if squeeze:
            raw *= 0.45
        if unwinding:
            raw *= 0.35
        available = sum(value is not None for value in
                        (row["oi"], row["change"], row["volume"], row["premium_change_pct"]))
        confidence = round(raw * (0.65 + 0.0875 * available))
        row.update({"writing_proxy": writing, "unwinding": unwinding,
                    "seller_pressure_risk": squeeze,
                    "strength_score": round(min(100, raw), 1),
                    "confidence": min(95, confidence),
                    "classification": ("writing_candidate" if writing else
                                       "unwinding" if unwinding else
                                       "seller_pressure_risk" if squeeze else "open_interest_context")})
        output.append(row)
    return output


def build_seller_levels(data: dict) -> dict:
    """Build CE resistance and PE support zones from a validated chain snapshot."""
    if not isinstance(data, dict):
        raise ValueError("Input must be a JSON object")
    quality = data.get("data_quality") if isinstance(data.get("data_quality"), dict) else {}
    if not quality.get("aligned") or not quality.get("session_date"):
        return {"status": "blocked", "session_date": quality.get("session_date"),
                "support": [], "resistance": [], "seller_exit_zones": [],
                "reason": "Validated same-session option-chain data is required."}
    rows = data.get("option_chain")
    if not isinstance(rows, list) or len([r for r in rows if isinstance(r, dict)]) < 3:
        return {"status": "blocked", "session_date": quality.get("session_date"),
                "support": [], "resistance": [], "seller_exit_zones": [],
                "reason": "At least three valid NIFTY option-chain strikes are required."}
    rows = [r for r in rows if isinstance(r, dict)]
    ce = _score_rows(rows, "ce")
    pe = _score_rows(rows, "pe")
    if not ce and not pe:
        return {"status": "blocked", "session_date": quality.get("session_date"),
                "support": [], "resistance": [], "seller_exit_zones": [],
                "reason": "No valid strike prices were supplied."}
    spot = _number(data.get("spot_price"))
    def select(items: list[dict], side: str) -> list[dict]:
        candidates = [r for r in items if r["strength_score"] >= 25]
        candidates.sort(key=lambda r: (
            abs(r["strike"] - spot) if spot is not None else 0,
            -r["strength_score"]))
        return [{
            "level": int(round(r["strike"])),
            "side": side,
            "strength": ("strong" if r["strength_score"] >= 70 else
                         "moderate" if r["strength_score"] >= 45 else "watch"),
            "strength_score": r["strength_score"],
            "confidence": r["confidence"],
            "evidence": {
                "oi": r["oi"], "change_oi": r["change"],
                "volume": r["volume"], "premium_change_pct": r["premium_change_pct"],
                "classification": r["classification"],
            },
        } for r in candidates[:5]]
    support = select(pe, "PE")
    resistance = select(ce, "CE")
    exits = []
    for side, side_rows in (("CE", ce), ("PE", pe)):
        for row in side_rows:
            if row["unwinding"] or row["seller_pressure_risk"]:
                exits.append({
                    "level": int(round(row["strike"])),
                    "side": side,
                    "risk": "seller_pressure" if row["seller_pressure_risk"] else "unwinding",
                    "confidence": row["confidence"],
                })
    exits.sort(key=lambda r: -r["confidence"])
    return {
        "status": "analysis_complete", "session_date": quality["session_date"],
        "spot_reference": spot, "support": support, "resistance": resistance,
        "seller_exit_zones": exits[:8],
        "method": "OI concentration + change-in-OI + volume + premium confirmation",
        "interpretation": "Levels are seller-activity proxies, not proof of participant identity. "
                          "A strike is not a confirmed defence or exit until price reaction confirms it.",
        "data_coverage": {"ce_strikes": len(ce), "pe_strikes": len(pe),
                          "ce_premium_coverage": sum(r["premium_change_pct"] is not None for r in ce),
                          "pe_premium_coverage": sum(r["premium_change_pct"] is not None for r in pe)},
    }
