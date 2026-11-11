"""EOD buyer/seller defence intelligence.

All scores in this module are evidence scores, not probabilities. Daily reports cannot
reconstruct intraday order-book absorption or price-by-volume profiles.
"""
from collections import defaultdict
from datetime import date
import math
import re

import pandas as pd

from .reports import load_tables, norm_columns, find_column, numeric


def _as_date(value):
    if value is None:
        return None
    text = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        parsed = pd.to_datetime(text, format="%Y-%m-%d", errors="coerce")
    elif re.fullmatch(r"\d{8}", text):
        parsed = pd.to_datetime(text, format="%Y%m%d", errors="coerce")
    else:
        parsed = pd.to_datetime(value, errors="coerce", dayfirst=True)
    return parsed.date() if pd.notna(parsed) else None


def _load_option_rows(reports, trading_date):
    grouped = defaultdict(lambda: defaultdict(lambda: {
        "ce_oi": 0.0, "pe_oi": 0.0, "ce_change": 0.0, "pe_change": 0.0,
        "ce_volume": 0.0, "pe_volume": 0.0, "ce_premium_change": [],
        "pe_premium_change": [], "ce_contracts": 0, "pe_contracts": 0
    }))
    available_fields = set()
    for item in reports:
        if item.get("report_type") != "fo_bhavcopy":
            continue
        try:
            tables = load_tables(item["file_path"])
        except Exception:
            continue
        for _, frame in tables:
            df = norm_columns(frame)
            symbol = find_column(df, ["TckrSymb", "Symbol", "Underlying", "FinInstrmNm"])
            opt = find_column(df, ["OptnTp", "Option Type", "OptionType", "OptType"])
            strike = find_column(df, ["StrkPric", "Strike Price", "StrikePrice"])
            oi = find_column(df, ["OpnIntrst", "Open Interest", "OpenInterest", "OI"])
            expiry = find_column(df, ["XpryDt", "Expiry Date", "ExpiryDate"])
            change = find_column(df, ["ChngInOpnIntrst", "Change in OI", "ChangeInOI", "ChgOI"])
            volume = find_column(df, ["TtlTradgVol", "Total Traded Volume", "Volume", "TradedVolume"])
            close = find_column(df, ["ClsPric", "Close", "ClosePrice", "LastPric"])
            previous = find_column(df, ["PrvsClsgPric", "Previous Close", "PrevClose", "PrvsClsgPric"])
            if not all((symbol, opt, strike, oi)):
                continue
            tokens = df[symbol].astype(str).str.upper().str.replace(r"[^A-Z0-9]", "", regex=True)
            df = df[tokens.isin(["NIFTY", "NIFTY50"])]
            types = df[opt].astype(str).str.upper().str.strip().replace({"CALL": "CE", "PUT": "PE"})
            df = df[types.isin(["CE", "PE"])].copy()
            if df.empty:
                continue
            df["_type"] = df[opt].astype(str).str.upper().str.strip().replace({"CALL": "CE", "PUT": "PE"})
            df["_strike"] = numeric(df[strike])
            df["_oi"] = numeric(df[oi]).fillna(0)
            df["_change"] = numeric(df[change]).fillna(0) if change else 0.0
            df["_volume"] = numeric(df[volume]).fillna(0) if volume else 0.0
            df["_close"] = numeric(df[close]) if close else float("nan")
            df["_previous"] = numeric(df[previous]) if previous else float("nan")
            if expiry:
                df["_expiry"] = df[expiry].map(_as_date)
            else:
                df["_expiry"] = None
            report_day = _as_date(item.get("trading_date")) or _as_date(trading_date)
            for _, row in df.dropna(subset=["_strike"]).iterrows():
                exp = row.get("_expiry") or report_day
                if exp and report_day and exp < report_day:
                    continue
                key = exp.isoformat() if exp else "UNDATED"
                strike_value = float(row["_strike"])
                side = "ce" if row["_type"] == "CE" else "pe"
                record = grouped[key][strike_value]
                record[side + "_oi"] += max(0.0, float(row["_oi"] or 0))
                record[side + "_change"] += float(row["_change"] or 0)
                record[side + "_volume"] += max(0.0, float(row["_volume"] or 0))
                if pd.notna(row["_close"]) and pd.notna(row["_previous"]) and float(row["_previous"]) > 0:
                    record[side + "_premium_change"].append((float(row["_close"]) / float(row["_previous"]) - 1) * 100)
                record[side + "_contracts"] += 1
            available_fields.update([x for x in ("oi", "change_oi" if change else "", "volume" if volume else "",
                                                   "premium_close" if close else "", "previous_premium" if previous else "") if x])
    if not grouped:
        return {"available": False, "reason": "No readable NIFTY option contracts with strike and OI fields were found.",
                "expiry": None, "strikes": [], "available_fields": sorted(available_fields)}
    trade_day = _as_date(trading_date)
    expiries = sorted(k for k in grouped if k != "UNDATED" and (not trade_day or _as_date(k) >= trade_day))
    selected = expiries[0] if expiries else ("UNDATED" if "UNDATED" in grouped else sorted(grouped)[0])
    rows = []
    for strike, values in sorted(grouped[selected].items()):
        item = {"strike": strike, **{k: v for k, v in values.items() if not k.endswith("_premium_change")}}
        for side in ("ce", "pe"):
            changes = values[side + "_premium_change"]
            item[side + "_premium_change_pct"] = sum(changes) / len(changes) if changes else None
        rows.append(item)
    total_ce = sum(x["ce_oi"] for x in rows)
    total_pe = sum(x["pe_oi"] for x in rows)
    total_oi = total_ce + total_pe
    for row in rows:
        row["ce_oi_share"] = row["ce_oi"] / total_ce if total_ce else 0.0
        row["pe_oi_share"] = row["pe_oi"] / total_pe if total_pe else 0.0
        row["ce_fresh_writing_proxy"] = row["ce_change"] > 0 and row["ce_premium_change_pct"] is not None and row["ce_premium_change_pct"] < 0
        row["pe_fresh_writing_proxy"] = row["pe_change"] > 0 and row["pe_premium_change_pct"] is not None and row["pe_premium_change_pct"] < 0
    top_ce = max(rows, key=lambda x: x["ce_oi"]) if rows else None
    top_pe = max(rows, key=lambda x: x["pe_oi"]) if rows else None
    call_writers = sorted((x for x in rows if x["ce_fresh_writing_proxy"]), key=lambda x: (x["ce_change"], x["ce_oi"]), reverse=True)
    put_writers = sorted((x for x in rows if x["pe_fresh_writing_proxy"]), key=lambda x: (x["pe_change"], x["pe_oi"]), reverse=True)
    return {
        "available": bool(rows), "expiry": selected, "strikes": rows, "available_fields": sorted(available_fields),
        "ce_total_oi": total_ce, "pe_total_oi": total_pe,
        "pcr": total_pe / total_ce if total_ce else None,
        "top_call_oi_strike": top_ce["strike"] if top_ce and top_ce["ce_oi"] else None,
        "top_put_oi_strike": top_pe["strike"] if top_pe and top_pe["pe_oi"] else None,
        "top_call_oi_share_pct": round(top_ce["ce_oi_share"] * 100, 2) if top_ce else None,
        "top_put_oi_share_pct": round(top_pe["pe_oi_share"] * 100, 2) if top_pe else None,
        "call_writing_strikes": [x["strike"] for x in call_writers[:5]],
        "put_writing_strikes": [x["strike"] for x in put_writers[:5]],
        "call_writing_count": len(call_writers), "put_writing_count": len(put_writers),
        "total_option_oi": total_oi,
    }


def _touch_count(rows, center, tolerance, side):
    count = 0
    for row in rows[-60:]:
        if side == "support" and row.get("low") is not None and row.get("close") is not None:
            if row["low"] <= center + tolerance and row["high"] >= center - tolerance and row["close"] >= center:
                count += 1
        elif side == "resistance" and row.get("high") is not None and row.get("close") is not None:
            if row["high"] >= center - tolerance and row["low"] <= center + tolerance and row["close"] <= center:
                count += 1
    return count


def build_defence_map(price_history, options, ai_levels):
    rows = [x for x in price_history if all(x.get(k) is not None for k in ("open", "high", "low", "close"))]
    spot = rows[-1]["close"] if rows else None
    tolerance = max(10.0, float(spot or 20000) * 0.0005)
    latest = rows[-1] if rows else None
    span = max((latest["high"] - latest["low"]), 0.01) if latest else None
    close_position = ((latest["close"] - latest["low"]) / span) if latest else None
    zones = {"buyer_defence": [], "seller_defence": []}
    for side, key, oi_key, writer_key, share_key in (
        ("support", "buyer_defence", "top_put_oi_strike", "put_writing_strikes", "top_put_oi_share_pct"),
        ("resistance", "seller_defence", "top_call_oi_strike", "call_writing_strikes", "top_call_oi_share_pct"),
    ):
        kind_zones = list((ai_levels or {}).get(side, []))
        oi_strike = options.get(oi_key) if options.get("available") else None
        if oi_strike is not None and spot is not None and abs(float(oi_strike) - spot) <= max(spot * 0.04, 500):
            kind_zones.append({"center": float(oi_strike), "zone_low": float(oi_strike) - tolerance,
                               "zone_high": float(oi_strike) + tolerance, "sources": ["option_oi_wall"],
                               "evidence_count": 1, "evidence_score": 0})
        candidates = []
        for zone in kind_zones:
            center = float(zone.get("center", 0))
            if not center:
                continue
            if spot is not None and side == "support" and center > spot + tolerance:
                continue
            if spot is not None and side == "resistance" and center < spot - tolerance:
                continue
            share = options.get(share_key) if options.get("available") and center == oi_strike else None
            writers = options.get(writer_key, []) if options.get("available") else []
            writer_confirmed = any(abs(float(x) - center) <= tolerance for x in writers)
            touch_count = _touch_count(rows, center, tolerance, side)
            candle_rejection = False
            if latest and side == "support":
                candle_rejection = latest["low"] <= center + tolerance and latest["close"] > latest["open"] and (close_position or 0) >= 0.55
            elif latest and side == "resistance":
                candle_rejection = latest["high"] >= center - tolerance and latest["close"] < latest["open"] and (close_position or 1) <= 0.45
            score = 15
            score += min(25, int((share or 0) * 0.25)) if share is not None else 0
            score += 25 if writer_confirmed else 0
            score += min(20, touch_count * 5)
            score += 15 if candle_rejection else 0
            score = min(100, score)
            sources = list(zone.get("sources", []))
            if share is not None: sources.append("relative_oi_concentration")
            if writer_confirmed: sources.append("fresh_writing_proxy")
            if candle_rejection: sources.append("latest_daily_candle_rejection")
            candidates.append({
                "side": side, "center": round(center, 2),
                "zone_low": round(float(zone.get("zone_low", center - tolerance)), 2),
                "zone_high": round(float(zone.get("zone_high", center + tolerance)), 2),
                "evidence_score": score,
                "strength": "STRONG_EVIDENCE" if score >= 70 else "MODERATE_EVIDENCE" if score >= 45 else "EARLY_CANDIDATE",
                "historical_rejection_sessions": touch_count, "fresh_writing_proxy": writer_confirmed,
                "oi_concentration_share_pct": share, "latest_candle_rejection": candle_rejection,
                "evidence_sources": sorted(set(sources)),
                "interpretation": "Put-side positioning and price response suggest a possible buyer-defence area." if side == "support" else "Call-side positioning and price response suggest a possible seller-defence area.",
            })
        zones[key] = sorted(candidates, key=lambda x: x["evidence_score"], reverse=True)[:5]
    return {
        **zones, "spot_reference": spot, "tolerance_points": round(tolerance, 2),
        "score_type": "EVIDENCE_SCORE_NOT_PROBABILITY",
        "limitations": ["Daily OI and OHLC cannot prove participant identity or intraday absorption.",
                        "A high OI strike is a candidate wall, not proof that sellers will defend it.",
                        "True absorption requires intraday trade/order-book data; true POC/VAH/VAL requires price-by-volume data."]
    }


def _wilson(successes, total, z=1.96):
    if total <= 0:
        return None, None
    p = successes / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    margin = z * math.sqrt((p * (1 - p) / total) + z * z / (4 * total * total)) / denom
    return max(0.0, centre - margin), min(1.0, centre + margin)


def walk_forward_zone_validation(price_history, lookback=60, min_history=25):
    """Retrospective daily zone-touch test; each test day uses only earlier candles."""
    rows = [x for x in price_history if all(x.get(k) is not None for k in ("high", "low", "close"))]
    support_tests = support_holds = resistance_tests = resistance_holds = 0
    tolerance_samples = []
    for index in range(min_history, len(rows)):
        prior = rows[max(0, index - lookback):index]
        target = rows[index]
        if not prior:
            continue
        ref = prior[-1]["close"]
        tolerance = max(10.0, ref * 0.0005)
        tolerance_samples.append(tolerance)
        swing_lows, swing_highs = [], []
        for j in range(1, len(prior) - 1):
            p, c, n = prior[j - 1], prior[j], prior[j + 1]
            if c["low"] <= p["low"] and c["low"] <= n["low"]:
                swing_lows.append(float(c["low"]))
            if c["high"] >= p["high"] and c["high"] >= n["high"]:
                swing_highs.append(float(c["high"]))
        support_levels = sorted(set(x for x in swing_lows if ref * 0.94 <= x <= ref + tolerance))[-3:]
        resistance_levels = sorted(set(x for x in swing_highs if ref - tolerance <= x <= ref * 1.06))[:3]
        for level in support_levels:
            touched = target["low"] <= level + tolerance and target["high"] >= level - tolerance
            if touched:
                support_tests += 1
                if target["close"] >= level:
                    support_holds += 1
        for level in resistance_levels:
            touched = target["high"] >= level - tolerance and target["low"] <= level + tolerance
            if touched:
                resistance_tests += 1
                if target["close"] <= level:
                    resistance_holds += 1
    slo, shi = _wilson(support_holds, support_tests)
    rlo, rhi = _wilson(resistance_holds, resistance_tests)
    total_tests = support_tests + resistance_tests
    total_holds = support_holds + resistance_holds
    lo, hi = _wilson(total_holds, total_tests)
    ready = total_tests >= 20
    return {
        "method": "Walk-forward daily OHLC zone-touch test; each test session uses only earlier sessions.",
        "sessions_available": len(rows), "minimum_history_sessions": min_history,
        "support": {"tests": support_tests, "holds": support_holds,
                    "hold_rate_pct": round(100 * support_holds / support_tests, 2) if support_tests else None,
                    "wilson_95_ci_pct": [round(100 * slo, 2), round(100 * shi, 2)] if slo is not None else None},
        "resistance": {"tests": resistance_tests, "holds": resistance_holds,
                       "hold_rate_pct": round(100 * resistance_holds / resistance_tests, 2) if resistance_tests else None,
                       "wilson_95_ci_pct": [round(100 * rlo, 2), round(100 * rhi, 2)] if rlo is not None else None},
        "combined": {"tests": total_tests, "holds": total_holds,
                     "hold_rate_pct": round(100 * total_holds / total_tests, 2) if total_tests else None,
                     "wilson_95_ci_pct": [round(100 * lo, 2), round(100 * hi, 2)] if lo is not None else None},
        "validation_status": "RETROSPECTIVE_TEST_READY" if ready else "INSUFFICIENT_TOUCH_SAMPLES",
        "forecast_probability_calibration": "NOT_CALIBRATED_NO_PROSPECTIVE_PROBABILITY_LOG",
        "limitations": ["This is a historical zone-touch study, not a live forecast hit rate.",
                        "Daily OHLC cannot determine intraday sequence or exact touch/reversal order.",
                        "Repeated tests of nearby zones may not be statistically independent."]
    }


def _daily_absorption_proxy(price_history):
    rows = [x for x in price_history if all(x.get(k) is not None for k in ("open", "high", "low", "close"))]
    recent = rows[-5:]
    lower = upper = 0.0
    bullish = bearish = 0
    details = []
    for row in recent:
        span = max(row["high"] - row["low"], 0.01)
        lower_ratio = max(0.0, min(row["open"], row["close"]) - row["low"]) / span
        upper_ratio = max(0.0, row["high"] - max(row["open"], row["close"])) / span
        close_pos = (row["close"] - row["low"]) / span
        lower += lower_ratio
        upper += upper_ratio
        bullish += int(lower_ratio >= 0.35 and close_pos >= 0.60)
        bearish += int(upper_ratio >= 0.35 and close_pos <= 0.40)
        details.append({"date": row.get("date"), "lower_wick_ratio": round(lower_ratio, 3),
                        "upper_wick_ratio": round(upper_ratio, 3), "close_position": round(close_pos, 3)})
    count = len(recent)
    return {"sessions": count, "bullish_rejection_days": bullish, "bearish_rejection_days": bearish,
            "mean_lower_wick_ratio": round(lower / count, 3) if count else None,
            "mean_upper_wick_ratio": round(upper / count, 3) if count else None,
            "recent_candles": details,
            "evidence_mode": "DAILY_CANDLE_PROXY",
            "limitations": ["Not true order-flow absorption: no intraday bid/ask trades or order-book depth were supplied."]}


def _cell_number(row, column):
    if not column:
        return None
    try:
        value = pd.to_numeric(row[column], errors="coerce")
        return float(value) if pd.notna(value) else None
    except Exception:
        return None


def _participant_index_net(reports):
    totals = []
    for item in reports:
        if item.get("report_type") not in {"participant_oi", "fii_derivatives"}:
            continue
        try:
            tables = load_tables(item["file_path"])
        except Exception:
            continue
        for _, frame in tables:
            df = norm_columns(frame)
            participant_col = find_column(df, ["Client Type", "Participant", "Client Type Name", "Category"])
            if not participant_col:
                continue
            fields = {
                "future_long": find_column(df, ["Future Index Long", "FutIdxLong"]),
                "future_short": find_column(df, ["Future Index Short", "FutIdxShort"]),
                "call_long": find_column(df, ["Option Index Call Long", "OptIdxCallLong"]),
                "call_short": find_column(df, ["Option Index Call Short", "OptIdxCallShort"]),
                "put_long": find_column(df, ["Option Index Put Long", "OptIdxPutLong"]),
                "put_short": find_column(df, ["Option Index Put Short", "OptIdxPutShort"]),
            }
            has_future = fields["future_long"] and fields["future_short"]
            has_options = all(fields[k] for k in ("call_long", "call_short", "put_long", "put_short"))
            if not (has_future or has_options):
                continue
            for _, row in df.iterrows():
                label = str(row[participant_col]).upper()
                if not ("FII" in label or "FPI" in label or "FOREIGN" in label):
                    continue
                future_net = 0.0
                option_net = 0.0
                if has_future:
                    long_value, short_value = _cell_number(row, fields["future_long"]), _cell_number(row, fields["future_short"])
                    if long_value is not None and short_value is not None:
                        future_net = long_value - short_value
                if has_options:
                    cl, cs = _cell_number(row, fields["call_long"]), _cell_number(row, fields["call_short"])
                    pl, ps = _cell_number(row, fields["put_long"]), _cell_number(row, fields["put_short"])
                    if None not in (cl, cs, pl, ps):
                        option_net = (cl - cs) + (ps - pl)
                totals.append({"participant": label, "future_index_net": future_net,
                               "option_index_net": option_net, "combined_index_net": future_net + option_net,
                               "source": item.get("original_name")})
    if not totals:
        return {"available": False, "net": None, "rows": []}
    net = sum(x["combined_index_net"] for x in totals)
    return {"available": True, "net": net, "rows": totals}


def _compare_oi_migration(history_reports, trading_date):
    target_day = _as_date(trading_date)
    dated = {}
    for item in history_reports:
        if item.get("report_type") != "fo_bhavcopy":
            continue
        day = _as_date(item.get("trading_date"))
        if day and (target_day is None or day <= target_day):
            dated.setdefault(day, []).append(item)
    days = sorted(dated)
    if len(days) < 2:
        return {"available": False, "reason": "Upload at least two dated F&O Bhavcopies to measure strike migration."}
    current_day = days[-1]
    previous_day = days[-2]
    current = _load_option_rows(dated[current_day], current_day.isoformat())
    previous = _load_option_rows(dated[previous_day], previous_day.isoformat())
    if not current.get("available") or not previous.get("available"):
        return {"available": False, "reason": "Both sessions need readable NIFTY option OI data."}
    if current.get("expiry") != previous.get("expiry"):
        return {"available": False, "reason": "Nearest expiry changed between sessions; strike migration is withheld across expiry rollover.",
                "current_expiry": current.get("expiry"), "previous_expiry": previous.get("expiry")}
    prior_by_strike = {x["strike"]: x for x in previous["strikes"]}
    deltas = []
    for row in current["strikes"]:
        old = prior_by_strike.get(row["strike"], {})
        deltas.append({"strike": row["strike"], "ce_oi_change": row["ce_oi"] - old.get("ce_oi", 0.0),
                       "pe_oi_change": row["pe_oi"] - old.get("pe_oi", 0.0)})
    top_call_add = max(deltas, key=lambda x: x["ce_oi_change"]) if deltas else None
    top_put_add = max(deltas, key=lambda x: x["pe_oi_change"]) if deltas else None
    call_shift = None if current.get("top_call_oi_strike") is None or previous.get("top_call_oi_strike") is None else current["top_call_oi_strike"] - previous["top_call_oi_strike"]
    put_shift = None if current.get("top_put_oi_strike") is None or previous.get("top_put_oi_strike") is None else current["top_put_oi_strike"] - previous["top_put_oi_strike"]
    return {
        "available": True, "current_date": current_day.isoformat(), "previous_date": previous_day.isoformat(),
        "expiry": current.get("expiry"),
        "previous_top_call_oi_strike": previous.get("top_call_oi_strike"),
        "current_top_call_oi_strike": current.get("top_call_oi_strike"),
        "call_wall_shift_points": call_shift,
        "previous_top_put_oi_strike": previous.get("top_put_oi_strike"),
        "current_top_put_oi_strike": current.get("top_put_oi_strike"),
        "put_wall_shift_points": put_shift,
        "largest_call_oi_addition_strike": top_call_add["strike"] if top_call_add else None,
        "largest_call_oi_addition": round(top_call_add["ce_oi_change"], 2) if top_call_add else None,
        "largest_put_oi_addition_strike": top_put_add["strike"] if top_put_add else None,
        "largest_put_oi_addition": round(top_put_add["pe_oi_change"], 2) if top_put_add else None,
        "interpretation": "Strike-wise OI migration is compared across two uploaded sessions for the same expiry; OI movement alone does not prove writing."
    }


def _institutional_positioning(reports, institutional, participant, migration):
    sources = [x for x in reports if x.get("report_type") in {"participant_oi", "participant_volume", "fii_derivatives", "fii_dii"}]
    available = [x for x in sources if int(x.get("row_count") or 0) > 0]
    fields = {}
    for item in available:
        fields[item["report_type"]] = {"file": item.get("original_name"), "rows": item.get("row_count"),
                                       "columns": item.get("columns", [])[:30]}
    participant_net = _participant_index_net(reports)
    score = 0.0
    if participant_net.get("available"):
        score = max(-0.25, min(0.25, participant_net["net"] / max(abs(participant_net["net"]), 1) * 0.25))
    elif institutional.get("available"):
        cash_bias = institutional.get("bias", "UNAVAILABLE")
        score = 0.12 if cash_bias == "BULLISH" else -0.12 if cash_bias == "BEARISH" else 0.0
    migration_score = 0.0
    if migration.get("available"):
        call_shift = migration.get("call_wall_shift_points")
        put_shift = migration.get("put_wall_shift_points")
        if call_shift is not None:
            migration_score += 0.05 if call_shift > 0 else -0.05 if call_shift < 0 else 0.0
        if put_shift is not None:
            migration_score += 0.05 if put_shift > 0 else -0.05 if put_shift < 0 else 0.0
    score = max(-0.35, min(0.35, score + migration_score))
    bias = "BULLISH" if score > 0.04 else "BEARISH" if score < -0.04 else "NEUTRAL"
    if not (participant_net.get("available") or institutional.get("available") or migration.get("available")):
        bias = "UNAVAILABLE"
    status = "partial" if participant_net.get("available") or institutional.get("available") or migration.get("available") else "missing_data"
    evidence = ["Participant positioning is a broad index context and cannot identify exact defending traders at an individual strike."]
    if participant_net.get("available"):
        evidence.append("FII index futures/options net positioning was calculated only where the report supplied all required long/short fields.")
    elif available:
        evidence.append("Participant report files were found, but their columns did not support a safe directional net-position calculation.")
    if migration.get("available"):
        evidence.append("Strike-wise OI migration was measured against the previous uploaded session for the same expiry.")
    if institutional.get("available"):
        evidence.append("Cash-market FII/DII net activity is included as secondary context, not treated as NIFTY option writing.")
    return {"status": status, "bias": bias, "score": score,
            "metrics": {"files_available": len(available), "sources": fields,
                        "cash_fii_dii_net": institutional.get("net_total"),
                        "participant_index_position": participant_net,
                        "oi_migration": migration,
                        "migration_available": migration.get("available", False),
                        "previous_top_call_oi_strike": migration.get("previous_top_call_oi_strike"),
                        "current_top_call_oi_strike": migration.get("current_top_call_oi_strike"),
                        "call_wall_shift_points": migration.get("call_wall_shift_points"),
                        "previous_top_put_oi_strike": migration.get("previous_top_put_oi_strike"),
                        "current_top_put_oi_strike": migration.get("current_top_put_oi_strike"),
                        "put_wall_shift_points": migration.get("put_wall_shift_points"),
                        "participant_summary": participant.get("sources", {})},
            "evidence": evidence}

def augment_analysis(result, reports, history_reports=None):
    price_history = []
    for row in result.get("spot_history", []):
        price_history.append(row)
    # run_analysis passes the full dated history through a private key during augmentation.
    price_history = result.pop("_price_history_for_eod", price_history)
    option_evidence = _load_option_rows(reports, result.get("trading_date"))
    if option_evidence.get("available"):
        result["option_positioning"] = {k: v for k, v in option_evidence.items() if k != "strikes"}
        # Replace the coarse all-expiry OI walls with nearest-expiry evidence for the defence map.
        options_for_map = {
            "available": True,
            "top_call_oi_strike": option_evidence.get("top_call_oi_strike"),
            "top_put_oi_strike": option_evidence.get("top_put_oi_strike"),
            "top_call_oi_share_pct": option_evidence.get("top_call_oi_share_pct"),
            "top_put_oi_share_pct": option_evidence.get("top_put_oi_share_pct"),
            "call_writing_strikes": option_evidence.get("call_writing_strikes", []),
            "put_writing_strikes": option_evidence.get("put_writing_strikes", []),
        }
    else:
        options_for_map = {"available": False}
    ai_levels = result.get("ai_support_resistance", {})
    defence = build_defence_map(price_history, options_for_map, ai_levels)
    result["defence_map"] = defence
    absorption = _daily_absorption_proxy(price_history)
    absorption_score = max(-0.25, min(0.25, (absorption["bullish_rejection_days"] - absorption["bearish_rejection_days"]) * 0.05))
    result["agents"] = [x for x in result.get("agents", []) if x.get("name") != "Final Consensus"]
    if len(price_history) >= 3:
        last = price_history[-1]
        span = max(last["high"] - last["low"], 0.01)
        close_position = (last["close"] - last["low"]) / span
        pa_score = max(-0.35, min(0.35, (close_position - 0.5) * 0.7))
        pa_bias = "BULLISH" if pa_score > 0.06 else "BEARISH" if pa_score < -0.06 else "NEUTRAL"
        for agent in result["agents"]:
            if agent.get("name") == "Price Action":
                agent.update({"bias": pa_bias, "score": round(pa_score, 4),
                              "metrics": {**agent.get("metrics", {}), "latest_close_position_in_range": round(close_position, 3),
                                          "direction_source": "daily_candle_structure_not_pivot"},
                              "evidence": ["Directional score uses daily candle close location and rejection structure, not pivot formulas."] + agent.get("evidence", [])})
    if option_evidence.get("available"):
        call_pressure = sum(1 for x in option_evidence["strikes"] if x["ce_fresh_writing_proxy"])
        put_pressure = sum(1 for x in option_evidence["strikes"] if x["pe_fresh_writing_proxy"])
        chain_score = max(-0.5, min(0.5, (put_pressure - call_pressure) / max(1, put_pressure + call_pressure) * 0.5))
        chain_bias = "BULLISH" if chain_score > 0.06 else "BEARISH" if chain_score < -0.06 else "NEUTRAL"
        for agent in result["agents"]:
            if agent.get("name") == "Option Chain":
                base_metrics = {k: v for k, v in agent.get("metrics", {}).items()
                                if k not in {"ce_oi", "pe_oi", "pcr", "ce_change_oi", "pe_change_oi",
                                             "highest_ce_oi_strike", "highest_pe_oi_strike", "max_pain"}}
                agent.update({"bias": chain_bias, "score": round(chain_score, 4),
                              "metrics": {**base_metrics, "selected_expiry": option_evidence.get("expiry"),
                                          "selected_expiry_ce_oi": option_evidence.get("ce_total_oi"),
                                          "selected_expiry_pe_oi": option_evidence.get("pe_total_oi"),
                                          "selected_expiry_pcr": option_evidence.get("pcr"),
                                          "fresh_call_writing_proxy_strikes": option_evidence.get("call_writing_strikes", []),
                                          "fresh_put_writing_proxy_strikes": option_evidence.get("put_writing_strikes", []),
                                          "nearest_expiry_only": True},
                              "evidence": ["Fresh writing proxy requires OI addition plus option premium decline; nearest available expiry is isolated.",
                                           "This is a positioning proxy, not verified participant identity or guaranteed defence."]})
    result["agents"].append({
        "name": "Buyer & Seller Absorption", "status": "partial" if absorption["sessions"] else "missing_data",
        "bias": "BULLISH" if absorption_score > 0.05 else "BEARISH" if absorption_score < -0.05 else "NEUTRAL",
        "score": round(absorption_score, 4), "confidence": None,
        "metrics": absorption, "evidence": ["Daily wick and close-location proxy only; true absorption requires intraday trade/order-book data."]
    })
    migration = _compare_oi_migration(history_reports or reports, result.get("trading_date"))
    inst = _institutional_positioning(reports, result.get("institutional", {}), result.get("participant_reports", {}), migration)
    result["agents"].append({"name": "Institutional Positioning & Migration", **inst, "confidence": None})
    validation = walk_forward_zone_validation(price_history)
    result["accuracy_validation"] = validation
    calibration_status = "partial" if validation["validation_status"] == "RETROSPECTIVE_TEST_READY" else "insufficient_history"
    result["agents"].append({
        "name": "Prediction Accuracy & Calibration", "status": calibration_status,
        "bias": "NEUTRAL", "score": 0.0, "confidence": None,
        "metrics": validation,
        "evidence": ["Retrospective zone-touch rates are not prospective forecast accuracy.",
                     "Probability calibration remains disabled until timestamped predictions are logged before outcomes."]
    })
    specialists = result["agents"]
    weights = {"Price Action": 1.0, "Option Chain": 1.4, "Volume Profile": 0.45, "Trend": 0.9,
               "Volatility": 0.45, "Pattern Recognition": 0.65, "Historical Validation": 0.55,
               "Buyer & Seller Absorption": 0.55, "Institutional Positioning & Migration": 0.55,
               "Prediction Accuracy & Calibration": 0.15}
    usable = [x for x in specialists if x.get("status") not in {"missing_data", "insufficient_history"}]
    total_weight = sum(weights.get(x["name"], 0.25) for x in usable)
    master = sum(float(x.get("score") or 0) * weights.get(x["name"], 0.25) for x in usable) / total_weight if total_weight else 0.0
    completeness = len(usable) / 10
    direction = "BULLISH" if master > 0.15 else "BEARISH" if master < -0.15 else "NEUTRAL"
    if completeness < 0.5:
        direction = "INSUFFICIENT_DATA"
    result["agents"].append({
        "name": "Final Consensus", "status": "complete" if completeness >= 0.7 else "partial",
        "bias": direction, "score": round(master, 4), "confidence": None,
        "metrics": {"specialist_agents": 10, "available_specialists": len(usable),
                    "data_completeness_pct": round(completeness * 100, 1),
                    "weighted_score": round(master, 4), "probability": None},
        "evidence": ["Agent 11 synthesises the ten specialist outputs, including buyer/seller defence and independent accuracy review.",
                     "This is not a calibrated probability; incomplete evidence and conflicting signals remain visible."]
    })
    result["direction"] = direction
    result["master_score"] = round(master, 4)
    result["confidence"] = None
    result["data_completeness"] = round(completeness * 100, 1)
    result["agent_count"] = 11
    result["methodology"] = ("Ten specialist agents analyse daily price structure, nearest-expiry option positioning, "
        "institutional context, volatility, historical patterns and retrospective zone tests. Agent 8 is final consensus. "
        "Evidence scores are not probabilities; no pivot-derived support/resistance is used as AI discovery.")
    warnings = list(result.get("warnings", []))
    if not option_evidence.get("available"):
        warnings.append("Nearest-expiry option positioning could not be isolated from the uploaded F&O reports.")
    if absorption["sessions"]:
        warnings.append("Buyer/seller absorption agent uses daily candle proxies only; intraday order-flow data was not supplied.")
    if validation["validation_status"] != "RETROSPECTIVE_TEST_READY":
        warnings.append("Accuracy agent needs more historical touch samples; no accuracy percentage is asserted.")
    result["warnings"] = list(dict.fromkeys(warnings))
    return result
