from __future__ import annotations

"""Seller-first, evidence-led NIFTY analysis ensemble.

Input is a normalized snapshot. This module deliberately does not parse raw NSE
CSV files yet; callers must supply validated, same-session data.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _rows(data: dict) -> list[dict]:
    rows = data.get("option_chain")
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict) and _num(r.get("strike")) > 0]


def _base(name: str, direction: str = "neutral", seller: str = "neither",
          score: float = 0, risk: str = "unknown", evidence: list[str] | None = None,
          available: bool = True) -> dict:
    return {"model": name, "direction": direction, "seller_advantage": seller,
            "score": round(max(-1.0, min(1.0, score)), 3), "risk": risk,
            "available": available, "evidence": evidence or []}


def ce_seller_model(data: dict) -> dict:
    rows = _rows(data)
    writing, unwinding, traps = [], [], []
    for r in rows:
        oi, prem = _num(r.get("ce_change_oi")), _num(r.get("ce_price_change_pct"))
        if oi > 0 and prem <= 0:
            writing.append((oi, _num(r.get("strike"))))
        elif oi < 0 and prem > 0:
            unwinding.append((abs(oi), _num(r.get("strike"))))
        if oi > 0 and prem > 0:
            traps.append(_num(r.get("strike")))
    if not rows:
        return _base("ce_seller", available=False, evidence=["Option-chain rows unavailable"])
    net = sum(x[0] for x in writing) - sum(x[0] for x in unwinding)
    total = sum(abs(_num(r.get("ce_change_oi"))) for r in rows) or 1
    score = max(-1, min(1, net / total))
    strongest = sorted(writing, reverse=True)[:3]
    ev = [f"Call-writing candidates at strikes {[int(x[1]) for x in strongest]}"] if strongest else []
    if unwinding:
        ev.append(f"Call unwinding observed at {len(unwinding)} strike(s)")
    if traps:
        ev.append(f"Call-side squeeze/trap watch at strikes {[int(x) for x in traps[:3]]}")
    return _base("ce_seller", "bearish" if score > .12 else "bullish" if score < -.12 else "neutral",
                 "ce" if score > .12 else "neither", score,
                 "high" if traps else "medium" if writing or unwinding else "unknown", ev)


def pe_seller_model(data: dict) -> dict:
    rows = _rows(data)
    writing, unwinding, traps = [], [], []
    for r in rows:
        oi, prem = _num(r.get("pe_change_oi")), _num(r.get("pe_price_change_pct"))
        if oi > 0 and prem <= 0:
            writing.append((oi, _num(r.get("strike"))))
        elif oi < 0 and prem > 0:
            unwinding.append((abs(oi), _num(r.get("strike"))))
        if oi > 0 and prem > 0:
            traps.append(_num(r.get("strike")))
    if not rows:
        return _base("pe_seller", available=False, evidence=["Option-chain rows unavailable"])
    net = sum(x[0] for x in writing) - sum(x[0] for x in unwinding)
    total = sum(abs(_num(r.get("pe_change_oi"))) for r in rows) or 1
    score = max(-1, min(1, net / total))
    strongest = sorted(writing, reverse=True)[:3]
    ev = [f"Put-writing candidates at strikes {[int(x[1]) for x in strongest]}"] if strongest else []
    if unwinding:
        ev.append(f"Put unwinding observed at {len(unwinding)} strike(s)")
    if traps:
        ev.append(f"Put-side squeeze/trap watch at strikes {[int(x) for x in traps[:3]]}")
    return _base("pe_seller", "bullish" if score > .12 else "bearish" if score < -.12 else "neutral",
                 "pe" if score > .12 else "neither", score,
                 "high" if traps else "medium" if writing or unwinding else "unknown", ev)


def oi_pcr_model(data: dict) -> dict:
    rows = _rows(data)
    if not rows:
        return _base("oi_pcr", available=False, evidence=["Option-chain rows unavailable"])
    ce_oi = sum(_num(r.get("ce_oi")) for r in rows)
    pe_oi = sum(_num(r.get("pe_oi")) for r in rows)
    ce_chg = sum(_num(r.get("ce_change_oi")) for r in rows)
    pe_chg = sum(_num(r.get("pe_change_oi")) for r in rows)
    pcr = pe_oi / ce_oi if ce_oi else None
    change_pcr = pe_chg / abs(ce_chg) if ce_chg else None
    score = max(-1, min(1, ((pcr or 1) - 1) * .7 + ((change_pcr or 1) - 1) * .3))
    return _base("oi_pcr", "bullish" if score > .12 else "bearish" if score < -.12 else "neutral",
                 "pe" if score > .12 else "ce" if score < -.12 else "neither", score, "medium",
                 [f"Aggregate PCR: {pcr:.3f}" if pcr is not None else "PCR unavailable",
                  f"Aggregate change-OI PCR: {change_pcr:.3f}" if change_pcr is not None else "Change-OI PCR unavailable"])


def seller_strength_model(data: dict) -> dict:
    rows = _rows(data)
    if not rows:
        return _base("seller_strength", available=False, evidence=["Option-chain rows unavailable"])
    ce = sum(max(0, _num(r.get("ce_change_oi"))) for r in rows if _num(r.get("ce_price_change_pct")) <= 0)
    pe = sum(max(0, _num(r.get("pe_change_oi"))) for r in rows if _num(r.get("pe_price_change_pct")) <= 0)
    total = ce + pe
    score = (pe - ce) / total if total else 0
    ce_strikes = sorted(((max(0, _num(r.get("ce_change_oi"))), _num(r.get("strike"))) for r in rows), reverse=True)[:3]
    pe_strikes = sorted(((max(0, _num(r.get("pe_change_oi"))), _num(r.get("strike"))) for r in rows), reverse=True)[:3]
    return _base("seller_strength", "bullish" if score > .12 else "bearish" if score < -.12 else "neutral",
                 "pe" if score > .12 else "ce" if score < -.12 else "both" if total else "neither",
                 score, "medium", [f"CE writing candidates: {[int(x[1]) for x in ce_strikes if x[0] > 0]}",
                                   f"PE writing candidates: {[int(x[1]) for x in pe_strikes if x[0] > 0]}"])


def futures_model(data: dict) -> dict:
    f = data.get("futures") if isinstance(data.get("futures"), dict) else {}
    price, oi = _num(f.get("price_change_pct")), _num(f.get("oi_change_pct"))
    if not f:
        return _base("futures", available=False, evidence=["Futures snapshot unavailable"])
    # Price up + OI up = long build-up; price down + OI up = short build-up.
    score = 0.0
    label = "mixed futures positioning"
    if price > 0 and oi > 0: score, label = .7, "price and futures OI both rose (long build-up candidate)"
    elif price < 0 and oi > 0: score, label = -.7, "price fell while futures OI rose (short build-up candidate)"
    elif price > 0 and oi < 0: score, label = .25, "price rose while futures OI fell (short-covering candidate)"
    elif price < 0 and oi < 0: score, label = -.25, "price and futures OI both fell (long-unwinding candidate)"
    return _base("futures", "bullish" if score > .12 else "bearish" if score < -.12 else "neutral",
                 "pe" if score > .12 else "ce" if score < -.12 else "neither", score, "medium",
                 [label, f"Futures price change {price:.2f}%; OI change {oi:.2f}%"])


def participant_model(data: dict) -> dict:
    p = data.get("participants") if isinstance(data.get("participants"), dict) else {}
    if not p:
        return _base("participants", available=False, evidence=["Participant positioning unavailable"])
    # Positive net_futures_change is treated as net long addition only when
    # upstream adapter supplies a consistently signed normalized value.
    net = _num(p.get("fii_net_futures_change"))
    score = max(-1, min(1, net / max(abs(net), _num(p.get("fii_gross_change")), 1)))
    return _base("participants", "bullish" if score > .12 else "bearish" if score < -.12 else "neutral",
                 "pe" if score > .12 else "ce" if score < -.12 else "neither", score, "medium",
                 [f"Normalized FII net futures change: {net:g} (adapter sign convention required)"])


def volatility_model(data: dict) -> dict:
    v = data.get("volatility") if isinstance(data.get("volatility"), dict) else {}
    if not v:
        return _base("volatility", available=False, evidence=["Volatility snapshot unavailable"])
    iv = _num(v.get("atm_iv"))
    iv_change = _num(v.get("atm_iv_change"))
    expiry_days = _num(v.get("days_to_expiry"), 99)
    high = iv >= _num(v.get("high_iv_threshold"), 20) or iv_change >= 2 or expiry_days <= 1
    return _base("volatility", "neutral", "neither", 0,
                 "high" if high else "low" if iv and iv < 12 else "medium",
                 [f"ATM IV {iv:g}; IV change {iv_change:g}; days to expiry {expiry_days:g}"])


def trap_risk_model(data: dict) -> dict:
    rows = _rows(data)
    if not rows:
        return _base("trap_risk", available=False, evidence=["Option-chain rows unavailable"])
    ce = sum(1 for r in rows if _num(r.get("ce_change_oi")) > 0 and _num(r.get("ce_price_change_pct")) > 0)
    pe = sum(1 for r in rows if _num(r.get("pe_change_oi")) > 0 and _num(r.get("pe_price_change_pct")) > 0)
    count = ce + pe
    risk = "high" if count >= 3 else "medium" if count else "low"
    return _base("trap_risk", risk=risk, evidence=[
        f"Call-side OI addition with premium rise: {ce} strike(s)",
        f"Put-side OI addition with premium rise: {pe} strike(s)",
        "These are squeeze-risk flags, not proof of seller identity",
    ])


MODEL_FUNCTIONS = (
    ce_seller_model, pe_seller_model, oi_pcr_model, seller_strength_model,
    futures_model, participant_model, volatility_model, trap_risk_model,
)
MODEL_WEIGHTS = {
    "ce_seller": 1.2, "pe_seller": 1.2, "oi_pcr": .8,
    "seller_strength": 1.0, "futures": 1.0, "participants": .5,
    "volatility": .3, "trap_risk": .4,
}


def analyze_seller_market(data: dict) -> dict:
    """Run independent models concurrently and return one cautious consensus."""
    if not isinstance(data, dict):
        raise ValueError("Input must be a JSON object")
    quality = data.get("data_quality") if isinstance(data.get("data_quality"), dict) else {}
    if not quality.get("aligned") or not quality.get("session_date"):
        return {"status": "blocked", "final_decision": "NO TRADE",
                "market_bias": "unavailable", "seller_advantage": "unknown",
                "confidence": 0, "reason": "Validated same-session data is required.",
                "models": [], "warnings": ["Data-quality gate failed"]}
    if len(_rows(data)) < 3:
        return {"status": "blocked", "final_decision": "NO TRADE",
                "market_bias": "unavailable", "seller_advantage": "unknown",
                "confidence": 0, "reason": "At least three valid option-chain strikes are required.",
                "models": [], "warnings": ["Insufficient option-chain rows"]}

    results = []
    with ThreadPoolExecutor(max_workers=len(MODEL_FUNCTIONS)) as pool:
        futures = [pool.submit(fn, data) for fn in MODEL_FUNCTIONS]
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda x: x["model"])

    directional = [r for r in results if r["available"] and r["model"] not in {"volatility", "trap_risk"}]
    weighted = [(r["score"], MODEL_WEIGHTS[r["model"]]) for r in directional]
    total_weight = sum(w for _, w in weighted)
    score = sum(s * w for s, w in weighted) / total_weight if total_weight else 0
    bullish = sum(1 for r in directional if r["direction"] == "bullish")
    bearish = sum(1 for r in directional if r["direction"] == "bearish")
    conflict = bullish > 0 and bearish > 0 and min(bullish, bearish) >= 2
    high_risk = any(r["risk"] == "high" for r in results)
    available_count = sum(1 for r in results if r["available"])
    confidence = round(min(90, abs(score) * 65 + available_count * 3))
    if conflict or high_risk or available_count < 5 or abs(score) < .16:
        decision, bias, seller = "NO TRADE", "neutral", "neither"
    elif score > 0:
        decision, bias, seller = "PE WRITING CANDIDATE", "bullish", "pe"
    else:
        decision, bias, seller = "CE WRITING CANDIDATE", "bearish", "ce"
    warnings = []
    if conflict: warnings.append("Sub-models show material directional disagreement")
    if high_risk: warnings.append("At least one model raised high seller-risk")
    if available_count < 5: warnings.append("Too few independent models have usable inputs")
    if not warnings and decision == "NO TRADE": warnings.append("Consensus strength is below the minimum threshold")
    return {
        "status": "analysis_complete", "session_date": quality["session_date"],
        "final_decision": decision, "market_bias": bias, "seller_advantage": seller,
        "consensus_score": round(score, 3), "confidence": confidence,
        "available_models": available_count, "total_models": len(results),
        "reason": "Seller-first weighted consensus with disagreement and risk gates.",
        "warnings": warnings, "models": results,
        "disclaimer": "Research output only; not a trade instruction or guarantee.",
    }
