"""Snapshot-to-snapshot NIFTY option signal engine.

No candle data is consumed. Decisions use verified option-chain snapshots,
selected-contract LTP/bid/ask, OI/change-OI, spot movement and model Greeks.
This is a transparent rules engine, not a trained or performance-validated AI model.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from services.greeks import calculate_greeks
from services.oi_analytics import analyze_option_chain

IST = ZoneInfo("Asia/Kolkata")
ENGINE_VERSION = "snapshot-confluence-v1"
TARGET_POINTS = 20.0
STOP_POINTS = 10.0
MAX_HORIZON_SECONDS = 900
MAX_SNAPSHOT_GAP_SECONDS = 15


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _stamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(IST)
    except ValueError:
        return None


def _contract_key(expiry: str, strike: float, side: str) -> str:
    return f"NIFTY|{expiry}|{strike:g}|{side}"


def _find_leg(rows: list[Mapping[str, Any]], contract_key: str) -> Mapping[str, Any] | None:
    try:
        _, expiry, strike_text, side = contract_key.split("|", 3)
        strike = float(strike_text)
    except (ValueError, AttributeError):
        return None
    for row in rows:
        if row.get("expiry") == expiry and _num(row.get("strike")) == strike:
            leg = row.get(side.lower())
            return leg if isinstance(leg, Mapping) else None
    return None


def _tte_years(expiry: str, at: datetime) -> float | None:
    try:
        expiry_day = datetime.fromisoformat(expiry[:10]).date()
        close = datetime.combine(expiry_day, datetime.strptime("15:30", "%H:%M").time(), tzinfo=IST)
        seconds = (close - at).total_seconds()
        return max(seconds / (365.0 * 24 * 3600), 1.0 / (365.0 * 24 * 3600)) if seconds > 0 else None
    except ValueError:
        return None


def analyze_snapshot(snapshot: Mapping[str, Any], previous: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a confluence decision and optional contract candidate."""
    if snapshot.get("status") != "LIVE":
        return {"direction": "WAIT", "decision": "NO_TRADE", "engine_version": ENGINE_VERSION,
                "missing_inputs": ["verified live option-chain snapshot"], "candidates": []}
    rows = snapshot.get("rows")
    spot = _num(snapshot.get("spot"))
    expiry = snapshot.get("selected_expiry")
    if not isinstance(rows, list) or not rows or spot is None or not expiry:
        return {"direction": "WAIT", "decision": "NO_TRADE", "engine_version": ENGINE_VERSION,
                "missing_inputs": ["spot", "selected expiry", "option-chain rows"], "candidates": []}
    metrics_result = analyze_option_chain(rows, top_n=10)
    if metrics_result.get("status") != "OK":
        return {"direction": "WAIT", "decision": "NO_TRADE", "engine_version": ENGINE_VERSION,
                "missing_inputs": ["valid single-expiry OI metrics"], "candidates": []}
    metrics = metrics_result["metrics"]
    current_time = _stamp(snapshot.get("source_timestamp"))
    if previous is None:
        return {"direction": "WAIT", "decision": "WARMING_UP", "engine_version": ENGINE_VERSION,
                "missing_inputs": ["second distinct verified snapshot"], "metrics": metrics,
                "candidates": []}
    prior_time = _stamp(previous.get("source_timestamp"))
    prior_spot = _num(previous.get("spot"))
    if current_time is None or prior_time is None or prior_spot is None:
        return {"direction": "WAIT", "decision": "NO_TRADE", "engine_version": ENGINE_VERSION,
                "missing_inputs": ["timezone-aware timestamps and prior spot"], "metrics": metrics,
                "candidates": []}
    gap = (current_time - prior_time).total_seconds()
    if gap <= 0:
        return {"direction": "WAIT", "decision": "DUPLICATE_OR_OUT_OF_ORDER", "engine_version": ENGINE_VERSION,
                "missing_inputs": ["newer distinct source timestamp"], "metrics": metrics, "candidates": []}
    if gap > MAX_SNAPSHOT_GAP_SECONDS:
        return {"direction": "WAIT", "decision": "SNAPSHOT_GAP", "engine_version": ENGINE_VERSION,
                "missing_inputs": [f"snapshot gap {gap:.0f}s exceeds {MAX_SNAPSHOT_GAP_SECONDS}s"], 
                "metrics": metrics, "candidates": []}
    spot_change_pct = (spot - prior_spot) / prior_spot * 100.0 if prior_spot else 0.0
    prior_rows = previous.get("rows") if isinstance(previous.get("rows"), list) else []
    atm = round(spot / 50.0) * 50.0
    candidates: list[dict[str, Any]] = []
    bullish_score = 0
    bearish_score = 0
    evidence: list[str] = []
    counter: list[str] = []

    prior_metrics_result = analyze_option_chain(prior_rows, top_n=10) if prior_rows else {}
    prior_metrics = prior_metrics_result.get("metrics") if prior_metrics_result.get("status") == "OK" else {}
    if spot_change_pct >= 0.025:
        bullish_score += 1; evidence.append("NIFTY spot advanced between verified snapshots")
    elif spot_change_pct <= -0.025:
        bearish_score += 1; evidence.append("NIFTY spot declined between verified snapshots")
    else:
        counter.append("Spot movement is small between snapshots")

    ce_oi_delta = (metrics.get("total_ce_oi") or 0) - (prior_metrics.get("total_ce_oi") or 0)
    pe_oi_delta = (metrics.get("total_pe_oi") or 0) - (prior_metrics.get("total_pe_oi") or 0)
    ce_oi_base = max(1.0, float(prior_metrics.get("total_ce_oi") or 1))
    pe_oi_base = max(1.0, float(prior_metrics.get("total_pe_oi") or 1))
    if ce_oi_delta > ce_oi_base * 0.0005:
        bearish_score += 1; evidence.append("Call OI increased between snapshots")
    elif ce_oi_delta < -ce_oi_base * 0.0005:
        bullish_score += 1; evidence.append("Call OI unwound between snapshots")
    if pe_oi_delta > pe_oi_base * 0.0005:
        bullish_score += 1; evidence.append("Put OI increased between snapshots")
    elif pe_oi_delta < -pe_oi_base * 0.0005:
        bearish_score += 1; evidence.append("Put OI unwound between snapshots")

    candidates_pool = []
    for row in rows:
        strike = _num(row.get("strike"))
        if strike is None or row.get("expiry") != expiry or abs(strike - atm) > 250:
            continue
        old_leg_ce = _find_leg(prior_rows, _contract_key(expiry, strike, "CE"))
        old_leg_pe = _find_leg(prior_rows, _contract_key(expiry, strike, "PE"))
        for side, leg, old_leg in (("CE", row.get("ce"), old_leg_ce), ("PE", row.get("pe"), old_leg_pe)):
            if not isinstance(leg, Mapping) or not isinstance(old_leg, Mapping):
                continue
            ltp, old_ltp = _num(leg.get("lastPrice")), _num(old_leg.get("lastPrice"))
            bid, ask = _num(leg.get("bidprice")), _num(leg.get("askPrice"))
            oi = _num(leg.get("openInterest"))
            change_oi = _num(leg.get("changeinOpenInterest"))
            volume = _num(leg.get("totalTradedVolume"))
            iv = _num(leg.get("impliedVolatility"))
            if None in (ltp, old_ltp, bid, ask, oi, change_oi, volume) or ltp <= 0 or bid <= 0 or ask < bid or ask <= 0 or volume <= 0:
                continue
            spread_pct = (ask - bid) / ltp * 100.0
            if spread_pct > 1.5 or ltp < 20 or ltp > 1000:
                continue
            premium_change_points = ltp - old_ltp
            premium_move = premium_change_points / old_ltp * 100.0 if old_ltp > 0 else 0.0
            premium_velocity_per_second = premium_change_points / gap
            projected_move_15m_points = max(0.0, premium_velocity_per_second * MAX_HORIZON_SECONDS)
            estimated_minutes_to_target = (TARGET_POINTS / premium_velocity_per_second / 60.0) if premium_velocity_per_second > 0 else None
            # Only alert when the latest observed premium velocity extrapolates to
            # at least +20 points inside the 15-minute horizon. This is a simple
            # feasibility screen, not a calibrated probability forecast.
            if projected_move_15m_points < TARGET_POINTS or estimated_minutes_to_target is None or estimated_minutes_to_target > 15.0:
                continue
            tte = _tte_years(expiry, current_time)
            greeks = None
            if iv and iv > 0 and tte:
                try:
                    greeks = calculate_greeks(
                        spot=spot, strike=strike, time_to_expiry_years=tte,
                        implied_volatility=iv / 100.0, option_type=side,
                        risk_free_rate=0.065, dividend_yield=0.0,
                        iv_source="option_chain_snapshot",
                    ).as_dict()
                except ValueError:
                    greeks = None
            # Missing or invalid IV/Greeks must never be silently guessed.
            if not greeks:
                continue
            delta = float(greeks["delta"])
            theta_day = abs(float(greeks["theta_per_calendar_day"]))
            delta_aligned = (0.30 <= delta <= 0.80) if side == "CE" else (-0.80 <= delta <= -0.30)
            theta_manageable = theta_day <= max(1.0, ltp * 0.12)
            score = 0
            if side == "CE":
                score += 1 if spot_change_pct > 0 else 0
                score += 1 if premium_move >= 0.35 else 0
                score += 1 if change_oi > 0 else 0
                score += 1 if ce_oi_delta > 0 else 0
            else:
                score += 1 if spot_change_pct < 0 else 0
                score += 1 if premium_move >= 0.35 else 0
                score += 1 if change_oi > 0 else 0
                score += 1 if pe_oi_delta > 0 else 0
            if abs(strike - atm) <= 50: score += 1
            score += 1 if spread_pct <= 0.75 else 0
            score += 1 if delta_aligned else 0
            score += 1 if theta_manageable else 0
            candidates_pool.append({
                "contract_key": _contract_key(expiry, strike, side),
                "side": side, "strike": strike, "expiry": expiry,
                "ltp": ltp, "bid": bid, "ask": ask, "entry_price": ask,
                "target_price": round(ask + TARGET_POINTS, 2),
                "stop_price": round(ask - STOP_POINTS, 2),
                "premium_change_pct": round(premium_move, 3),
                "projected_move_15m_points": round(projected_move_15m_points, 2),
                "estimated_minutes_to_target": round(estimated_minutes_to_target, 2),
                "change_oi": change_oi, "oi": oi, "volume": volume,
                "spread_pct": round(spread_pct, 3), "iv": iv,
                "greeks": greeks, "score": score,
                "delta_aligned": delta_aligned, "theta_manageable": theta_manageable,
                "observed_at": snapshot.get("source_timestamp"),
            })
    candidates_pool.sort(key=lambda x: (x["score"], -abs(x["strike"] - atm), -x["volume"]), reverse=True)
    ce_best = next((x for x in candidates_pool if x["side"] == "CE"), None)
    pe_best = next((x for x in candidates_pool if x["side"] == "PE"), None)
    if ce_best and ce_best["score"] >= 5: bullish_score += 1
    if pe_best and pe_best["score"] >= 5: bearish_score += 1
    direction = "WAIT"
    selected = None
    if bullish_score >= 3 and bullish_score > bearish_score and ce_best and ce_best["score"] >= 4:
        direction, selected = "BULLISH", ce_best
    elif bearish_score >= 3 and bearish_score > bullish_score and pe_best and pe_best["score"] >= 4:
        direction, selected = "BEARISH", pe_best
    elif bullish_score and bearish_score and bullish_score == bearish_score:
        direction = "SIDEWAYS"
    if selected:
        evidence.append(f"{selected['side']} contract passed {selected['score']}/8 option-chain + Greeks confluence checks")
    else:
        counter.append("No CE/PE contract met the target-feasibility, Greeks, OI, direction and liquidity thresholds")
    return {
        "direction": direction,
        "decision": "SIGNAL_CANDIDATE" if selected else ("NO_TRADE" if direction == "WAIT" else "SIDEWAYS"),
        "engine_version": ENGINE_VERSION,
        "evidence_score": max(bullish_score, bearish_score),
        "confidence_calibrated": None,
        "evidence": evidence,
        "counter_evidence": counter,
        "missing_inputs": [],
        "spot_change_pct": round(spot_change_pct, 4),
        "bullish_score": bullish_score,
        "bearish_score": bearish_score,
        "metrics": metrics,
        "candidates": candidates_pool[:10],
        "selected_candidate": selected,
        "target_points": TARGET_POINTS,
        "stop_points": STOP_POINTS,
        "horizon_seconds": MAX_HORIZON_SECONDS,
        "premium_candles_required": False,
        "note": "Transparent snapshot-confluence rules with a two-snapshot 15-minute premium-velocity feasibility screen; not a trained or validated prediction model.",
    }


def monitor_signal(signal: Mapping[str, Any], previous_price: float | None,
                   current_price: float | None, previous_time: str | None,
                   current_time: str | None) -> dict[str, Any]:
    """Resolve a signal from observed LTP snapshots or conservatively mark unresolved."""
    if signal.get("state") not in {"TRIGGERED", "ACTIVE"}:
        return {"state": signal.get("state"), "outcome": signal.get("outcome")}
    now_dt, prior_dt = _stamp(current_time), _stamp(previous_time)
    if now_dt is None or prior_dt is None or current_price is None:
        return {"state": "UNRESOLVED", "outcome": "UNRESOLVED", "reason": "Missing valid source timestamp or contract LTP"}
    elapsed = (now_dt - prior_dt).total_seconds()
    if elapsed <= 0:
        return {"state": signal.get("state"), "outcome": None, "reason": "Duplicate or out-of-order snapshot"}
    if elapsed > MAX_SNAPSHOT_GAP_SECONDS:
        return {"state": "UNRESOLVED", "outcome": "UNRESOLVED", "reason": f"Snapshot gap {elapsed:.0f}s obscures threshold sequence"}
    entry_time = _stamp(signal.get("triggered_at") or signal.get("created_at"))
    if entry_time and (now_dt - entry_time).total_seconds() > int(signal.get("horizon_seconds") or MAX_HORIZON_SECONDS):
        return {"state": "CLOSED", "outcome": "TIME_EXIT", "reason": "15-minute signal horizon elapsed"}
    target, stop = float(signal["target_price"]), float(signal["stop_price"])
    if previous_price is not None:
        # If two consecutive observations jump from beyond one barrier to beyond
        # the other, the ordering between snapshots is unknowable.
        if (previous_price >= target and current_price <= stop) or (previous_price <= stop and current_price >= target):
            return {"state": "UNRESOLVED", "outcome": "UNRESOLVED", "reason": "Snapshot gap crossed both outcome zones; first touch cannot be established"}
    if current_price >= target:
        return {"state": "CLOSED", "outcome": "TARGET", "reason": "Observed contract LTP reached target"}
    if current_price <= stop:
        return {"state": "CLOSED", "outcome": "STOP_LOSS", "reason": "Observed contract LTP reached stop"}
    return {"state": "ACTIVE", "outcome": None, "reason": "Monitoring next verified option-chain snapshot"}
