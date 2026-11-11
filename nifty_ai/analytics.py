from collections import defaultdict
from datetime import datetime, timezone
import math
import re
import pandas as pd
import numpy as np
from . import database
from .reports import REPORTS, load_tables, norm_columns, find_column, numeric


SPOT_ALIASES = {
    "date": ["Date", "Index Date", "IndexDate", "TradDt", "BizDt"],
    "open": ["Open", "Open Index Value", "OpnPric", "OpenPrice"],
    "high": ["High", "High Index Value", "HghPric", "HighPrice"],
    "low": ["Low", "Low Index Value", "LwPric", "LowPrice"],
    "close": ["Close", "Closing Index Value", "ClsPric", "ClosePrice", "LastPric"],
}


def _last_valid_row(df, cols):
    work = df.copy()
    for key, col in cols.items():
        work[key] = numeric(work[col]) if col else np.nan
    work = work.dropna(subset=["high", "low", "close"])
    if work.empty:
        return None
    return work.iloc[-1]


def extract_spot_history(reports):
    """Return every valid NIFTY 50 OHLC row found in eligible reports."""
    candidates = []
    for item in reports:
        if item["report_type"] not in {"nifty50", "cm_bhavcopy"}:
            continue
        try:
            tables = load_tables(item["file_path"])
        except Exception:
            continue
        for _, frame in tables:
            df = norm_columns(frame)
            cols = {k: find_column(df, v) for k, v in SPOT_ALIASES.items()}
            # CM bhavcopy is not an index file; use it only if a NIFTY index row is explicit.
            if item["report_type"] in {"cm_bhavcopy", "nifty50"}:
                sym = find_column(df, ["TckrSymb", "Symbol", "Index Name", "Index", "Underlying"])
                if sym:
                    sym_values = df[sym].astype(str).str.upper().str.replace(r"[^A-Z0-9]", "", regex=True)
                    mask = sym_values.str.contains("NIFTY50", na=False) | sym_values.eq("NIFTY")
                    df = df[mask]
                    cols = {k: find_column(df, v) for k, v in SPOT_ALIASES.items()}
                elif item["report_type"] == "cm_bhavcopy":
                    continue
            if not all(cols.get(k) for k in ("high", "low", "close")):
                continue
            work = df.copy()
            for key in ("open", "high", "low", "close"):
                work["_" + key] = numeric(work[cols[key]]) if cols.get(key) else np.nan
            work = work.dropna(subset=["_high", "_low", "_close"])
            for _, row in work.iterrows():
                date = item.get("trading_date")
                if cols.get("date"):
                    parsed = pd.to_datetime(row[cols["date"]], errors="coerce", dayfirst=True)
                    if pd.notna(parsed): date = parsed.date().isoformat()
                candidates.append({"date": date, "open": _float(row.get("_open")), "high": _float(row.get("_high")),
                                   "low": _float(row.get("_low")), "close": _float(row.get("_close")),
                                   "source": item["original_name"]})
    # Remove repeated dates while preferring the last report encountered for that date.
    by_date = {}
    for row in candidates:
        if row.get("date"):
            by_date[row["date"]] = row
    return [by_date[d] for d in sorted(by_date)]


def extract_spot(reports):
    rows = extract_spot_history(reports)
    return rows[-1] if rows else None


def _float(v):
    try:
        if pd.isna(v): return None
        return float(v)
    except Exception:
        return None


def extract_vix(reports):
    for item in reports:
        if item["report_type"] != "india_vix":
            continue
        try:
            for _, frame in load_tables(item["file_path"]):
                df = norm_columns(frame)
                c = find_column(df, ["Close", "Closing Index Value", "ClosePrice", "Last"])
                if c:
                    vals = numeric(df[c]).dropna()
                    if not vals.empty:
                        return _float(vals.iloc[-1])
        except Exception:
            pass
    return None


def extract_options(reports):
    total_ce = total_pe = 0.0
    chg_ce = chg_pe = 0.0
    oi_by_strike = defaultdict(lambda: {"ce": 0.0, "pe": 0.0, "ce_change": 0.0, "pe_change": 0.0, "ce_volume": 0.0, "pe_volume": 0.0})
    expiries = set()
    contract_count = 0
    total_ce_volume = total_pe_volume = 0.0
    latest_spot = None
    for item in reports:
        if item["report_type"] != "fo_bhavcopy":
            continue
        try:
            tables = load_tables(item["file_path"])
        except Exception:
            continue
        for _, frame in tables:
            df = norm_columns(frame)
            sym = find_column(df, ["TckrSymb", "Symbol", "Underlying", "FinInstrmNm"])
            opt = find_column(df, ["OptnTp", "Option Type", "OptionType", "OptType"])
            strike = find_column(df, ["StrkPric", "Strike Price", "StrikePrice"])
            oi = find_column(df, ["OpnIntrst", "Open Interest", "OpenInterest", "OI"])
            expiry = find_column(df, ["XpryDt", "Expiry Date", "ExpiryDate"])
            chg = find_column(df, ["ChngInOpnIntrst", "Change in OI", "ChangeInOI", "ChgOI"])
            underlying = find_column(df, ["UndrlygPric", "Underlying Price", "UnderlyingValue"])
            volume = find_column(df, ["TtlTradgVol", "Total Traded Volume", "Volume", "TradedVolume"])
            if not all((opt, strike, oi)):
                continue
            if sym:
                s = df[sym].astype(str).str.upper()
                df = df[s.str.contains("NIFTY", na=False)]
            opt_s = df[opt].astype(str).str.upper().str.strip()
            df = df[opt_s.isin(["CE", "PE", "CALL", "PUT"])]
            if df.empty:
                continue
            df["_strike"] = numeric(df[strike])
            df["_oi"] = numeric(df[oi]).fillna(0)
            df["_change"] = numeric(df[chg]).fillna(0) if chg else 0
            df["_volume"] = numeric(df[volume]).fillna(0) if volume else 0
            df["_type"] = df[opt].astype(str).str.upper().str.strip().replace({"CALL":"CE", "PUT":"PE"})
            if expiry:
                exp_vals = pd.to_datetime(df[expiry], errors="coerce", dayfirst=True).dropna()
                expiries.update(x.date().isoformat() for x in exp_vals.unique())
            if underlying:
                svals = numeric(df[underlying]).dropna()
                if not svals.empty: latest_spot = _float(svals.iloc[-1])
            for _, row in df.dropna(subset=["_strike"]).iterrows():
                k = float(row["_strike"])
                typ = "ce" if row["_type"] == "CE" else "pe"
                oi_value, chg_value = max(0.0, float(row["_oi"])), float(row["_change"])
                vol_value = max(0.0, float(row.get("_volume", 0) or 0))
                oi_by_strike[k][typ] += oi_value
                oi_by_strike[k][typ + "_change"] += chg_value
                oi_by_strike[k][typ + "_volume"] += vol_value
                if typ == "ce": total_ce += oi_value; chg_ce += chg_value; total_ce_volume += vol_value
                else: total_pe += oi_value; chg_pe += chg_value; total_pe_volume += vol_value
                contract_count += 1
    if contract_count == 0:
        return {"available": False, "reason": "No readable NIFTY CE/PE contracts were found in the F&O Bhavcopy."}
    strikes = sorted(oi_by_strike)
    max_ce = max(strikes, key=lambda k: oi_by_strike[k]["ce"]) if strikes else None
    max_pe = max(strikes, key=lambda k: oi_by_strike[k]["pe"]) if strikes else None
    max_pain = None
    if strikes:
        pain = {}
        for settle in strikes:
            call_pain = sum(max(0, settle-k) * v["ce"] for k,v in oi_by_strike.items())
            put_pain = sum(max(0, k-settle) * v["pe"] for k,v in oi_by_strike.items())
            pain[settle] = call_pain + put_pain
        max_pain = min(pain, key=pain.get)
    strike_distribution = [{"strike": k, **oi_by_strike[k]} for k in strikes]
    dominant_volume = max(strike_distribution, key=lambda x: x["ce_volume"] + x["pe_volume"]) if strike_distribution else None
    return {"available": True, "contract_count": contract_count, "ce_oi": total_ce, "pe_oi": total_pe,
            "pcr": total_pe / total_ce if total_ce else None, "ce_change_oi": chg_ce, "pe_change_oi": chg_pe,
            "ce_volume": total_ce_volume, "pe_volume": total_pe_volume,
            "highest_ce_oi_strike": max_ce, "highest_pe_oi_strike": max_pe, "max_pain": max_pain,
            "dominant_volume_strike": dominant_volume.get("strike") if dominant_volume and (total_ce_volume + total_pe_volume) > 0 else None,
            "volume_available": (total_ce_volume + total_pe_volume) > 0,
            "expiries": sorted(expiries), "underlying_price": latest_spot,
            "strike_distribution": strike_distribution}


def extract_fii_dii(reports):
    result = {"available": False, "rows": [], "net_total": None, "bias": "UNAVAILABLE"}
    for item in reports:
        if item["report_type"] not in {"fii_dii", "fii_derivatives", "participant_oi", "participant_volume"}:
            continue
        try:
            for _, frame in load_tables(item["file_path"]):
                df = norm_columns(frame)
                if item["report_type"] == "fii_dii":
                    name = find_column(df, ["Category", "Client Type", "Participant", "Investor Type"])
                    buy = find_column(df, ["Buy Value", "BuyValue", "Purchase Value"])
                    sell = find_column(df, ["Sell Value", "SellValue", "Sales Value"])
                    net = find_column(df, ["Net Value", "NetValue", "Net Purchase"])
                    if name:
                        for _, row in df.iterrows():
                            category = str(row[name]).strip()
                            b, s = _float(numeric(pd.Series([row[buy]])).iloc[0]) if buy else None, _float(numeric(pd.Series([row[sell]])).iloc[0]) if sell else None
                            n = _float(numeric(pd.Series([row[net]])).iloc[0]) if net else ((b-s) if b is not None and s is not None else None)
                            if n is not None:
                                result["rows"].append({"category": category, "buy": b, "sell": s, "net": n})
        except Exception:
            continue
    if result["rows"]:
        total = sum(x["net"] for x in result["rows"])
        result.update({"available": True, "net_total": total, "bias": "BULLISH" if total > 0 else "BEARISH" if total < 0 else "NEUTRAL"})
    return result


def extract_breadth(reports):
    advances = declines = unchanged = 0
    found = False
    for item in reports:
        if item["report_type"] != "cm_bhavcopy": continue
        try:
            for _, frame in load_tables(item["file_path"]):
                df = norm_columns(frame)
                close = find_column(df, ["ClsPric", "Close", "ClosePrice"])
                prev = find_column(df, ["PrvsClsgPric", "Previous Close", "PrevClose"])
                if not close or not prev: continue
                a = numeric(df[close]); p = numeric(df[prev]); valid = a.notna() & p.notna() & (p != 0)
                if not valid.any(): continue
                found = True
                advances += int((a[valid] > p[valid]).sum()); declines += int((a[valid] < p[valid]).sum()); unchanged += int((a[valid] == p[valid]).sum())
                break
        except Exception: pass
    return {"available": found, "advances": advances, "declines": declines, "unchanged": unchanged,
            "ratio": advances / declines if declines else None,
            "bias": "BULLISH" if advances > declines else "BEARISH" if declines > advances else "NEUTRAL" if found else "UNAVAILABLE"}



def report_quality_summary(reports, types):
    matched = [r for r in reports if r["report_type"] in set(types)]
    rows = 0
    columns = set()
    warnings = []
    for item in matched:
        try:
            for _, frame in load_tables(item["file_path"]):
                rows += len(frame)
                columns.update(str(c) for c in frame.columns)
        except Exception as exc:
            warnings.append(f"{item['original_name']}: {exc}")
    return {"files": len(matched), "rows": rows, "columns": sorted(columns)[:40], "readable": rows > 0,
            "warnings": warnings}


def extract_delivery_metrics(reports):
    summary = report_quality_summary(reports, ["cm_delivery"])
    values = {}
    aliases = {
        "deliverable_quantity": ["Deliverable Qty", "Deliverable Quantity", "DeliverableQty"],
        "deliverable_percent": ["Deliverable Percent", "Deliverable Percentage", "Deliverable%"],
        "traded_quantity": ["Quantity Traded", "Traded Quantity", "QtyTraded", "Total Traded Quantity"],
    }
    for item in reports:
        if item["report_type"] != "cm_delivery": continue
        try:
            for _, frame in load_tables(item["file_path"]):
                df = norm_columns(frame)
                for metric, names in aliases.items():
                    col = find_column(df, names)
                    if col:
                        series = numeric(df[col]).dropna()
                        if not series.empty:
                            values[metric + "_sum"] = float(series.sum())
                            values[metric + "_mean"] = float(series.mean())
        except Exception: pass
    summary["metrics"] = values
    return summary


def extract_volatility_metrics(reports):
    cm_summary = report_quality_summary(reports, ["cm_volatility"])
    fo_summary = report_quality_summary(reports, ["fo_volatility"])
    summary = {"files": cm_summary["files"] + fo_summary["files"], "rows": cm_summary["rows"] + fo_summary["rows"],
               "columns": sorted(set(cm_summary["columns"] + fo_summary["columns"])), "readable": cm_summary["readable"] or fo_summary["readable"],
               "warnings": cm_summary["warnings"] + fo_summary["warnings"]}
    metrics = {}
    for item in reports:
        if item["report_type"] not in {"cm_volatility", "fo_volatility"}: continue
        try:
            for _, frame in load_tables(item["file_path"]):
                df = norm_columns(frame)
                for col in df.columns:
                    token = re.sub(r"[^a-z]", "", str(col).lower())
                    if "volatility" in token or token in {"vol", "iv", "impliedvolatility"}:
                        series = numeric(df[col]).dropna()
                        if not series.empty:
                            metrics[item["report_type"] + "_mean"] = float(series.mean())
                            metrics[item["report_type"] + "_latest"] = float(series.iloc[-1])
        except Exception: pass
    summary["metrics"] = metrics
    return summary


def extract_participant_summary(reports):
    summary = report_quality_summary(reports, ["participant_oi", "participant_volume", "fii_derivatives"])
    result = {}
    for item in reports:
        if item["report_type"] not in {"participant_oi", "participant_volume", "fii_derivatives"}: continue
        fields = []
        try:
            for _, frame in load_tables(item["file_path"]):
                df = norm_columns(frame)
                label_col = find_column(df, ["Client Type", "Participant", "Client Type Name", "Category"])
                numeric_fields = []
                for col in df.columns:
                    series = numeric(df[col])
                    if series.notna().sum() > 0:
                        numeric_fields.append(str(col))
                fields.extend(numeric_fields)
                result[item["report_type"]] = {"rows": int(len(df)), "participant_labels": df[label_col].astype(str).head(10).tolist() if label_col else [],
                                                "numeric_fields": numeric_fields[:24]}
        except Exception: pass
    summary["sources"] = result
    return summary

def _pivots(price):
    if not price or any(price.get(k) is None for k in ("high", "low", "close")): return {}
    h,l,c = price["high"], price["low"], price["close"]
    p = (h+l+c)/3
    bc = (h+l)/2
    tc = 2*p-bc
    return {"pdc": c, "pdh": h, "pdl": l, "pivot": p, "r1": 2*p-l, "s1": 2*p-h,
            "r2": p+(h-l), "s2": p-(h-l), "r3": h+2*(p-l), "s3": l-2*(h-p), "cpr_bc": min(bc,tc), "cpr_tc": max(bc,tc)}


def _agent(name, status, bias="NEUTRAL", score=0, confidence=None, metrics=None, notes=None):
    return {"name": name, "status": status, "bias": bias, "score": round(float(score), 4), "confidence": confidence,
            "metrics": metrics or {}, "evidence": notes or []}


def discover_ai_levels(price_history, options):
    """Discover swing/OI zones independently from traditional pivot calculations."""
    candidates = {"support": [], "resistance": []}
    rows = [r for r in price_history if all(r.get(k) is not None for k in ("high", "low", "close"))]
    for i in range(1, len(rows) - 1):
        prev, cur, nxt = rows[i - 1], rows[i], rows[i + 1]
        if cur["high"] >= prev["high"] and cur["high"] >= nxt["high"]:
            candidates["resistance"].append({"price": float(cur["high"]), "source": "swing_high", "weight": 1.0})
        if cur["low"] <= prev["low"] and cur["low"] <= nxt["low"]:
            candidates["support"].append({"price": float(cur["low"]), "source": "swing_low", "weight": 1.0})
    if options.get("available"):
        if options.get("highest_pe_oi_strike") is not None:
            candidates["support"].append({"price": float(options["highest_pe_oi_strike"]), "source": "put_oi_wall", "weight": 1.15})
        if options.get("highest_ce_oi_strike") is not None:
            candidates["resistance"].append({"price": float(options["highest_ce_oi_strike"]), "source": "call_oi_wall", "weight": 1.15})
    spot = rows[-1]["close"] if rows else None
    tolerance = max(20.0, float(spot or 20000) * 0.001)
    result = {"support": [], "resistance": [], "tolerance_points": round(tolerance, 2),
              "method": "Historical three-candle swing pivots clustered with corroborating option OI walls; independent of PDC/pivot formulas."}
    for kind in ("support", "resistance"):
        clusters = []
        for item in sorted(candidates[kind], key=lambda x: x["price"]):
            if not clusters or item["price"] - clusters[-1][-1]["price"] > tolerance:
                clusters.append([item])
            else:
                clusters[-1].append(item)
        zones = []
        for cluster in clusters:
            total_weight = sum(x["weight"] for x in cluster)
            center = sum(x["price"] * x["weight"] for x in cluster) / total_weight
            sources = sorted(set(x["source"] for x in cluster))
            touches = sum(x["source"].startswith("swing_") for x in cluster)
            oi_confirmed = any("oi_wall" in x["source"] for x in cluster)
            score = min(95, 35 + touches * 12 + (15 if oi_confirmed else 0))
            zones.append({"center": round(center, 2), "zone_low": round(min(x["price"] for x in cluster) - tolerance / 2, 2),
                "zone_high": round(max(x["price"] for x in cluster) + tolerance / 2, 2),
                "evidence_count": len(cluster), "historical_swing_touches": touches,
                "oi_confirmation": oi_confirmed, "sources": sources, "evidence_score": score,
                "strength": "STRONG" if score >= 70 else "MODERATE" if score >= 50 else "EARLY"})
        result[kind] = sorted(zones, key=lambda x: x["evidence_score"], reverse=True)[:5]
    return result


def _trend_metrics(price_history, flows, breadth):
    closes = [float(x["close"]) for x in price_history if x.get("close") is not None]
    metrics = {"sessions": len(closes), "fii_dii_available": bool(flows.get("available")),
               "market_breadth_available": bool(breadth.get("available"))}
    if len(closes) < 3:
        return "NEUTRAL", 0.0, metrics, ["At least three NIFTY 50 closes are required for trend analysis."]
    series = pd.Series(closes, dtype="float64")
    ema5 = float(series.ewm(span=5, adjust=False).mean().iloc[-1])
    ema20 = float(series.ewm(span=20, adjust=False).mean().iloc[-1]) if len(series) >= 20 else None
    change5 = float(series.iloc[-1] / series.iloc[-min(6, len(series))] - 1)
    metrics.update({"last_close": closes[-1], "ema5": ema5, "ema20": ema20, "five_session_change_pct": change5 * 100})
    score = max(-1.0, min(1.0, change5 * 20))
    if ema20 is not None:
        score += 0.15 if ema5 > ema20 else -0.15 if ema5 < ema20 else 0
    if flows.get("available"):
        score += 0.10 if flows.get("bias") == "BULLISH" else -0.10 if flows.get("bias") == "BEARISH" else 0
    if breadth.get("available"):
        score += 0.10 if breadth.get("bias") == "BULLISH" else -0.10 if breadth.get("bias") == "BEARISH" else 0
    score = max(-1.0, min(1.0, score))
    bias = "BULLISH" if score > 0.12 else "BEARISH" if score < -0.12 else "NEUTRAL"
    return bias, score, metrics, ["Trend uses uploaded NIFTY closes, EMA structure, FII/DII and breadth where available.",
                                  "Broad institutional/breadth reports are corroboration, not NIFTY-only positioning."]


def _pattern_metrics(price_history):
    rows = [r for r in price_history if all(r.get(k) is not None for k in ("open", "high", "low", "close"))]
    if len(rows) < 3:
        return "insufficient_history", "NEUTRAL", 0.0, {}, ["Three complete OHLC sessions are required for candle-pattern recognition."]
    prev, cur = rows[-2], rows[-1]
    span = max(cur["high"] - cur["low"], 0.01)
    lower = min(cur["open"], cur["close"]) - cur["low"]
    upper = cur["high"] - max(cur["open"], cur["close"])
    pattern, score = "NEUTRAL_CANDLE", 0.0
    if lower / span >= 0.5 and cur["close"] > cur["open"]:
        pattern, score = "BULLISH_REJECTION", 0.25
    elif upper / span >= 0.5 and cur["close"] < cur["open"]:
        pattern, score = "BEARISH_REJECTION", -0.25
    elif cur["high"] <= prev["high"] and cur["low"] >= prev["low"]:
        pattern = "INSIDE_DAY"
    elif cur["close"] > prev["high"] and cur["close"] > cur["open"]:
        pattern, score = "BULLISH_RANGE_BREAK", 0.3
    elif cur["close"] < prev["low"] and cur["close"] < cur["open"]:
        pattern, score = "BEARISH_RANGE_BREAK", -0.3
    bias = "BULLISH" if score > 0 else "BEARISH" if score < 0 else "NEUTRAL"
    return "baseline_ready", bias, score, {"pattern": pattern, "range_points": round(span, 2),
        "lower_wick_ratio": round(lower / span, 3), "upper_wick_ratio": round(upper / span, 3)}, [
        "Transparent candle rules are used; no untrained model probability is claimed."]


def run_analysis(trading_date: str, reports):
    price_history = extract_spot_history(reports)
    price = price_history[-1] if price_history else None
    vix = extract_vix(reports)
    options = extract_options(reports)
    flows = extract_fii_dii(reports)
    participant = extract_participant_summary(reports)
    delivery = extract_delivery_metrics(reports)
    volatility_reports = extract_volatility_metrics(reports)
    settlement = report_quality_summary(reports, ["fo_settlement"])
    breadth = extract_breadth(reports)
    pivots = _pivots(price)
    ai_levels = discover_ai_levels(price_history, options)
    agents = []

    if price and pivots:
        agents.append(_agent("Price Action", "complete", "BULLISH" if price["close"] > pivots["pivot"] else "BEARISH" if price["close"] < pivots["pivot"] else "NEUTRAL",
            1 if price["close"] > pivots["pivot"] else -1 if price["close"] < pivots["pivot"] else 0,
            metrics={**pivots, "ai_support_zones": len(ai_levels["support"]), "ai_resistance_zones": len(ai_levels["resistance"])},
            notes=["Traditional pivot references are separate from AI-discovered swing/OI zones.",
                   "AI zones are clustered from historical swing highs/lows and corroborating option OI walls."]))
        for row in price_history:
            database.save_session_price(row["date"], row, row.get("source", ""))
    else:
        agents.append(_agent("Price Action", "missing_data", notes=["A readable NIFTY 50 OHLC report is required."]))

    if options.get("available"):
        pcr = options.get("pcr")
        bias = "BULLISH" if pcr and pcr > 1.05 else "BEARISH" if pcr and pcr < 0.95 else "NEUTRAL"
        score = 0.55 if bias == "BULLISH" else -0.55 if bias == "BEARISH" else 0
        if options.get("pe_change_oi", 0) > options.get("ce_change_oi", 0): score += 0.15
        elif options.get("ce_change_oi", 0) > options.get("pe_change_oi", 0): score -= 0.15
        agents.append(_agent("Option Chain", "complete", bias, score,
            metrics={k: v for k, v in options.items() if k != "strike_distribution"},
            notes=["OI, change in OI, PCR and Max Pain are derived from uploaded NSE F&O contracts.",
                   "OI walls are evidence candidates, not guaranteed support or resistance."]))
    else:
        agents.append(_agent("Option Chain", "missing_data", notes=[options.get("reason", "F&O bhavcopy data unavailable.")]))

    if options.get("available") and options.get("volume_available"):
        top = sorted(options["strike_distribution"], key=lambda x: x["ce_volume"] + x["pe_volume"], reverse=True)[:5]
        agents.append(_agent("Volume Profile", "partial", "NEUTRAL", 0,
            metrics={"ce_volume": options["ce_volume"], "pe_volume": options["pe_volume"],
                     "dominant_volume_strike": options.get("dominant_volume_strike"),
                     "top_volume_strikes": [{"strike": x["strike"], "volume": x["ce_volume"] + x["pe_volume"]} for x in top]},
            notes=["This is an options strike-activity profile from NSE traded-volume fields, not a full intraday price-by-volume profile.",
                   "POC/VAH/VAL are withheld because daily bhavcopy has no intraday price-level volume distribution."]))
    else:
        agents.append(_agent("Volume Profile", "missing_data", notes=["Recognized NIFTY option traded-volume fields are required; no volume profile is fabricated."]))

    trend_bias, trend_score, trend_metrics, trend_notes = _trend_metrics(price_history, flows, breadth)
    agents.append(_agent("Trend", "complete" if len(price_history) >= 3 else "missing_data", trend_bias, trend_score,
        metrics={**trend_metrics, "fii_dii_net_total": flows.get("net_total"),
                 "breadth_advances": breadth.get("advances"), "breadth_declines": breadth.get("declines"),
                 "participant_files": participant.get("files"), "delivery_files": delivery.get("files")}, notes=trend_notes))

    if vix is not None:
        vbias = "NEUTRAL" if 12 <= vix <= 22 else "BEARISH" if vix > 22 else "BULLISH"
        agents.append(_agent("Volatility", "partial", vbias, -0.25 if vix > 22 else 0.15 if vix < 12 else 0,
            metrics={"india_vix_close": vix, "daily_volatility_files": volatility_reports["files"],
                     "daily_volatility_rows": volatility_reports["rows"], **volatility_reports.get("metrics", {})},
            notes=["India VIX and daily volatility reports describe risk regime, not direction by themselves."]))
    else:
        agents.append(_agent("Volatility", "missing_data", notes=["India VIX close was not found in the uploaded reports."]))

    ps, pb, pscore, pm, pn = _pattern_metrics(price_history)
    agents.append(_agent("Pattern Recognition", ps, pb, pscore, metrics=pm, notes=pn))

    hist = [x for x in database.get_session_prices(limit=1000) if x.get("close") is not None]
    if len(hist) >= 30:
        agents.append(_agent("Historical Validation", "partial", "NEUTRAL", 0,
            metrics={"stored_sessions": len(hist), "forecast_outcomes_recorded": 0, "validation_status": "AWAITING_OUTCOME_MATCHING"},
            notes=["Historical samples exist, but forecast-versus-actual outcomes are not yet matched.",
                   "Walk-forward accuracy and S/R hit rates are withheld until outcome evaluation is implemented."]))
    else:
        agents.append(_agent("Historical Validation", "insufficient_history", "NEUTRAL", 0,
            metrics={"stored_sessions": len(hist), "minimum_sessions_for_review": 30},
            notes=["At least 30 dated NIFTY sessions are required before historical validation begins."]))

    usable = [x for x in agents if x["status"] not in {"missing_data", "insufficient_history"}]
    weights = {"Price Action": 1.2, "Option Chain": 1.3, "Volume Profile": 0.7, "Trend": 1.1,
               "Volatility": 0.6, "Pattern Recognition": 0.8, "Historical Validation": 0.5}
    total_weight = sum(weights[x["name"]] for x in usable)
    master = sum(x["score"] * weights[x["name"]] for x in usable) / total_weight if total_weight else 0.0
    completeness = len(usable) / 7
    direction = "BULLISH" if master > 0.18 else "BEARISH" if master < -0.18 else "NEUTRAL"
    if completeness < 0.5: direction = "INSUFFICIENT_DATA"
    agents.append(_agent("Final Consensus", "complete" if completeness >= 0.7 else "partial", direction, master,
        metrics={"specialist_agents": 7, "available_specialists": len(usable),
                 "data_completeness_pct": round(completeness * 100, 1), "weighted_score": round(master, 4),
                 "probability": None},
        notes=["Evidence-weighted synthesis across seven specialist agents.",
               "No calibrated probability is shown; missing or conflicting evidence reduces readiness."]))

    warnings = []
    if not price: warnings.append("NIFTY spot OHLC is missing; AI swing zones and traditional pivots are unavailable.")
    if not options.get("available"): warnings.append("F&O option contracts were not parsed; option-chain evidence and OI corroboration are unavailable.")
    if not options.get("volume_available"): warnings.append("Recognized option traded-volume fields are missing; volume profile is withheld.")
    if len(hist) < 30: warnings.append("Historical validation is not ready; at least 30 dated sessions are required.")
    result = {"trading_date": trading_date, "generated_at": datetime.now(timezone.utc).isoformat(),
        "direction": direction, "master_score": round(master, 4), "confidence": None,
        "data_completeness": round(completeness * 100, 1), "agents": agents,
        "levels": pivots, "traditional_levels": pivots, "ai_support_resistance": ai_levels,
        "spot": price, "price_history_rows": len(price_history), "options": options,
        "institutional": flows, "participant_reports": participant, "delivery_reports": delivery,
        "volatility_reports": volatility_reports, "settlement_report": settlement, "breadth": breadth,
        "india_vix": vix, "warnings": warnings,
        "source_reports": [{"name": x["original_name"], "type": x["report_type"], "status": x["status"]} for x in reports],
        "methodology": "Seven specialist agents discover price-action/OI zones and an eighth final-consensus agent combines evidence. Traditional pivots remain reference-only. No missing values, probabilities or accuracy are fabricated.",
        "_price_history_for_eod": price_history}
    return result


# EOD buyer/seller defence intelligence is layered on top of the stable base analytics.
# This keeps traditional reference levels available while preventing them from driving AI S/R.
_base_run_analysis = run_analysis


def run_analysis(trading_date: str, reports, history_reports=None):
    from .eod_intelligence import augment_analysis
    result = _base_run_analysis(trading_date, reports)
    return augment_analysis(result, reports, history_reports=history_reports)
