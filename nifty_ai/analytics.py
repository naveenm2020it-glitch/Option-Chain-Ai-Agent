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
    oi_by_strike = defaultdict(lambda: {"ce": 0.0, "pe": 0.0, "ce_change": 0.0, "pe_change": 0.0})
    expiries = set()
    contract_count = 0
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
                oi_by_strike[k][typ] += oi_value
                oi_by_strike[k][typ + "_change"] += chg_value
                if typ == "ce": total_ce += oi_value; chg_ce += chg_value
                else: total_pe += oi_value; chg_pe += chg_value
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
    return {"available": True, "contract_count": contract_count, "ce_oi": total_ce, "pe_oi": total_pe,
            "pcr": total_pe / total_ce if total_ce else None, "ce_change_oi": chg_ce, "pe_change_oi": chg_pe,
            "highest_ce_oi_strike": max_ce, "highest_pe_oi_strike": max_pe, "max_pain": max_pain,
            "expiries": sorted(expiries), "underlying_price": latest_spot,
            "strike_distribution": [{"strike": k, **oi_by_strike[k]} for k in strikes]}


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
    agents = []

    if price and pivots:
        hist = database.get_session_prices(limit=60)
        prior = [x for x in hist if x.get("trading_date") < (price.get("date") or trading_date) and x.get("close") is not None]
        bias = "BULLISH" if price["close"] > pivots["pivot"] else "BEARISH" if price["close"] < pivots["pivot"] else "NEUTRAL"
        rng = (price["high"] - price["low"]) if price["high"] is not None and price["low"] is not None else None
        agents.append(_agent("Price Action", "complete", bias, 1 if bias=="BULLISH" else -1 if bias=="BEARISH" else 0,
                             metrics={**pivots, "day_range": rng, "historical_sessions":len(prior)},
                             notes=["Pivot and CPR levels were calculated from the uploaded NIFTY OHLC report.", "Direction is a close-versus-pivot observation, not a forecast guarantee."]))
        for historical_row in price_history:
            database.save_session_price(historical_row["date"], historical_row, historical_row.get("source", ""))
    else:
        agents.append(_agent("Price Action", "missing_data", notes=["A readable NIFTY 50 OHLC row with high, low and close is required."]))

    if options.get("available"):
        pcr = options.get("pcr")
        obias = "BULLISH" if pcr and pcr > 1.05 else "BEARISH" if pcr and pcr < 0.95 else "NEUTRAL"
        score = 0.55 if obias=="BULLISH" else -0.55 if obias=="BEARISH" else 0
        if options.get("pe_change_oi",0) > options.get("ce_change_oi",0): score += 0.15
        elif options.get("ce_change_oi",0) > options.get("pe_change_oi",0): score -= 0.15
        agents.append(_agent("Options Intelligence", "complete", obias, score,
                             metrics={**{k:v for k,v in options.items() if k != "strike_distribution"}, "settlement_report_rows":settlement["rows"], "participant_oi_files":participant["files"]},
                             notes=["OI concentration and Max Pain are derived from the uploaded F&O contracts.", "Daily settlement and participant OI files are checked as corroborating inputs; individual field mappings are shown in the data audit.", "PCR is an aggregate of the contracts parsed; check expiry coverage before relying on it."]))
    else:
        agents.append(_agent("Options Intelligence", "missing_data", notes=[options.get("reason", "F&O data unavailable.")]))

    if flows.get("available"):
        fbias=flows["bias"]; fscore=.7 if fbias=="BULLISH" else -.7 if fbias=="BEARISH" else 0
        agents.append(_agent("Institutional Flow", "partial", fbias, fscore, metrics={"net_total":flows["net_total"],"rows":flows["rows"],"participant_files":participant["files"],"participant_rows":participant["rows"],"recognized_numeric_fields":sum(len(v.get("numeric_fields",[])) for v in participant["sources"].values())}, notes=["FII/DII cash activity was parsed where recognized.","Participant-wise OI, participant volumes and FII derivatives statistics are independently ingested and audited.","Broad participant categories are not interpreted as exact NIFTY-only exposure."]))
    else:
        agents.append(_agent("Institutional Flow", "partial", notes=["No recognized FII/DII net activity rows were found. Other participant reports may still be present but need schema mapping."]))

    if breadth.get("available"):
        bbias=breadth["bias"]; bscore=.6 if bbias=="BULLISH" else -.6 if bbias=="BEARISH" else 0
        agents.append(_agent("Market Breadth", "complete", bbias, bscore, metrics={**breadth, "delivery_files":delivery["files"], "delivery_rows":delivery["rows"], **delivery.get("metrics",{})}, notes=["Advance/decline breadth is calculated from CM Bhavcopy rows with close and previous-close fields.","Security-wise delivery participation is also ingested and summarized when the report columns are recognized."]))
    else:
        agents.append(_agent("Market Breadth", "missing_data", notes=["CM Bhavcopy did not contain recognized close and previous-close columns."]))

    if vix is not None:
        vbias="NEUTRAL" if 12 <= vix <= 22 else "BEARISH" if vix > 22 else "BULLISH"
        agents.append(_agent("Volatility", "partial", vbias, -.25 if vix > 22 else .15 if vix < 12 else 0,
                             metrics={"india_vix_close":vix,"daily_volatility_files":volatility_reports["files"],"daily_volatility_rows":volatility_reports["rows"],**volatility_reports.get("metrics",{})}, notes=["India VIX is an uncertainty/volatility context indicator; it does not independently determine direction.","CM and F&O daily volatility reports are ingested and their recognized volatility fields are summarized separately."]))
    else:
        agents.append(_agent("Volatility", "missing_data", notes=["India VIX close was not found in the uploaded report."]))

    history = database.get_session_prices(limit=250)
    valid_hist = [x for x in history if x.get("close") is not None]
    returns = []
    for prev, cur in zip(valid_hist, valid_hist[1:]):
        if prev["close"]: returns.append((cur["close"] / prev["close"])-1)
    if len(returns) >= 20:
        mu = float(np.mean(returns)); sigma = float(np.std(returns, ddof=1))
        ml_score = max(-1.0, min(1.0, mu/(sigma or 1e-9)))
        regime = "HIGH_VOLATILITY" if sigma > .012 else "LOW_VOLATILITY" if sigma < .006 else "NORMAL_VOLATILITY"
        agents.append(_agent("ML Pattern Engine", "baseline_ready", "BULLISH" if ml_score>.15 else "BEARISH" if ml_score<-.15 else "NEUTRAL",
                             ml_score*.35, metrics={"historical_sessions":len(valid_hist),"mean_daily_return":mu,"daily_return_std":sigma,"regime":regime},
                             notes=["Historical baseline is calculated from stored daily index closes.","A trained classifier and walk-forward metrics are withheld until sufficient clean history is available."]))
    else:
        agents.append(_agent("ML Pattern Engine", "insufficient_history", notes=[f"Only {len(valid_hist)} valid daily index closes are stored; at least 21 sessions are required for the initial historical baseline."]))

    ready = [a for a in agents if a["status"] not in {"missing_data", "insufficient_history"}]
    scores = [a["score"] for a in ready]
    master_score = float(np.mean(scores)) if scores else 0.0
    direction = "BULLISH" if master_score > .18 else "BEARISH" if master_score < -.18 else "NEUTRAL"
    completeness = len(ready) / len(agents)
    if completeness < .5: direction = "INSUFFICIENT_DATA"
    confidence = round(min(85, 35 + completeness*35 + min(15, abs(master_score)*15)), 1) if completeness >= .5 else None
    warnings = []
    if not price: warnings.append("NIFTY spot OHLC is missing; pivot levels cannot be published.")
    if not options.get("available"): warnings.append("F&O option chain was not parsed; OI support/resistance and Max Pain are unavailable.")
    if len(valid_hist) < 21: warnings.append("Historical sample is insufficient for a trained ML prediction.")
    if len(ready) < 6: warnings.append("One or more agents have incomplete inputs; treat the consolidated view as partial.")
    next_trading_date = None
    levels = pivots
    if options.get("available"):
        levels = {**levels, "oi_support_candidate":options.get("highest_pe_oi_strike"),"oi_resistance_candidate":options.get("highest_ce_oi_strike"),"max_pain":options.get("max_pain")}
    result = {"trading_date":trading_date,"generated_at":datetime.now(timezone.utc).isoformat(),
              "direction":direction,"master_score":round(master_score,4),"confidence":confidence,
              "data_completeness":round(completeness*100,1),"agents":agents,"levels":levels,
              "spot":price,"price_history_rows":len(price_history),"options":options,"institutional":flows,"participant_reports":participant,"delivery_reports":delivery,"volatility_reports":volatility_reports,"settlement_report":settlement,"breadth":breadth,"india_vix":vix,
              "warnings":warnings,"source_reports":[{"name":r["original_name"],"type":r["report_type"],"status":r["status"]} for r in reports],
              "methodology":"Deterministic specialist analytics with a transparent evidence aggregator. No synthetic or missing market values are substituted. A probability forecast is not claimed until walk-forward calibration is available."}
    return result
